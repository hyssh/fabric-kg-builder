"""serving.lineage_verifier — cross-store lineage verifier.

M6 SRV-010: Samples Search AND Ontology records and uses lineage.trace_record
to resolve each sampled Search document back to its immutable original Blob
landing record (asset_versions row with blob_uri populated).

Verification strategy
---------------------
1. Sample Search documents from the AI Search index.
2. For each document, extract lineage fields (asset_id, run_id, etc.)
3. Look up the corresponding records in the canonical Parquet tables.
4. Call lineage.trace_record backward; check that the path reaches an
   ``asset_versions`` row with ``blob_uri`` (immutable Blob landing).
5. (Optional) Sample Ontology instance records and verify their lineage fields.
6. Report resolved, broken, and missing-from-tables counts per domain.

All lookups go through injectable transports/clients — no real cloud calls in
tests.  The public ``TraceResult.broken_edges`` property is used rather than
the private ``_broken_edge_detail`` field.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from fabric_kg_builder.lineage.trace import TraceResult, trace_record

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sampler protocols — injectable for tests
# ---------------------------------------------------------------------------


class SearchSampler(Protocol):
    """Returns a sample of documents from an AI Search index."""

    def sample(
        self,
        index_name: str,
        size: int,
        select_fields: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        ...


class OntologySampler(Protocol):
    """Returns a sample of instances from an Ontology/Graph."""

    def sample_instances(
        self,
        workspace_id: str,
        ontology_item_id: str,
        type_name: str,
        size: int,
    ) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Production Search sampler
# ---------------------------------------------------------------------------


def _default_search_token_provider() -> str:
    """Return an Azure AI Search data-plane token."""
    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    from fabric_kg_builder.azure_identity import default_azure_credential

    return default_azure_credential().get_token(
        "https://search.azure.com/.default"
    ).token


class AzureSearchSampler:
    """Sample documents from a deployed Azure AI Search index."""

    def __init__(
        self,
        endpoint: str,
        *,
        token_provider: Optional[Callable[[], str]] = None,
        api_version: str = "2024-07-01",
        request_post: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not endpoint or not endpoint.startswith(("https://", "http://")):
            raise ValueError("A valid Azure AI Search endpoint is required")
        self._endpoint = endpoint.rstrip("/")
        self._token_provider = token_provider or _default_search_token_provider
        self._api_version = api_version
        self._request_post = request_post

    def sample(
        self,
        index_name: str,
        size: int,
        select_fields: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        if not index_name:
            raise ValueError("index_name is required")
        if size <= 0:
            return []
        post = self._request_post
        if post is None:
            import requests  # type: ignore[import]

            post = requests.post
        body: dict[str, Any] = {
            "search": "*",
            "top": size,
            "count": False,
        }
        if select_fields:
            body["select"] = ",".join(select_fields)
        response = post(
            (
                f"{self._endpoint}/indexes/{index_name}/docs/search"
                f"?api-version={self._api_version}"
            ),
            headers={
                "Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("value", [])
        if not isinstance(rows, list):
            raise RuntimeError("Azure AI Search returned a non-list 'value' payload")
        return [dict(row) for row in rows if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# Fake samplers for tests
# ---------------------------------------------------------------------------


class FakeSearchSampler:
    """In-memory fake Search sampler — no network calls."""

    def __init__(self, docs: Optional[list[dict[str, Any]]] = None) -> None:
        self._docs = docs or []
        self.call_log: list[tuple[str, int]] = []

    def sample(
        self,
        index_name: str,
        size: int,
        select_fields: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        self.call_log.append((index_name, size))
        return self._docs[:size]


class FakeOntologySampler:
    """In-memory fake Ontology sampler — no network calls."""

    def __init__(self, instances: Optional[list[dict[str, Any]]] = None) -> None:
        self._instances = instances or []
        self.call_log: list[tuple[str, str, int]] = []

    def sample_instances(
        self,
        workspace_id: str,
        ontology_item_id: str,
        type_name: str,
        size: int,
    ) -> list[dict[str, Any]]:
        self.call_log.append((ontology_item_id, type_name, size))
        return self._instances[:size]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SampleVerification:
    """Verification result for one sampled document."""

    doc_id: str
    index_name: str
    asset_id: Optional[str]
    run_id: Optional[str]
    trace_result: Optional[TraceResult]
    resolved: bool
    missing_from_tables: bool
    has_blob_landing: bool = False  # True when path reaches asset_versions with blob_uri
    error: Optional[str] = None


@dataclass
class VerificationReport:
    """Aggregated cross-store lineage verification report.

    Attributes
    ----------
    ok:
        True iff all sampled documents resolved to an asset_versions Blob landing.
    sample_count:
        Number of documents sampled.
    resolved_count:
        Documents that traced to asset_versions with blob_uri (immutable Blob).
    missing_count:
        Documents absent from the Parquet tables.
    broken_count:
        Documents with broken lineage edges.
    ontology_sample_count:
        Number of Ontology instances sampled (0 when no ontology_sampler).
    verifications:
        Per-document verification results.
    errors:
        Accumulated error messages.
    """

    ok: bool
    sample_count: int
    resolved_count: int = 0
    missing_count: int = 0
    broken_count: int = 0
    ontology_sample_count: int = 0
    verifications: list[SampleVerification] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class LineageVerifier:
    """Cross-store lineage verifier (Search + optional Ontology).

    Parameters
    ----------
    sampler:
        Injectable Search sampler.  Use ``FakeSearchSampler`` in tests.
    doc_id_field:
        The field in Search documents that holds the primary record ID.
    table_name:
        Canonical Parquet table to start the backward trace from.
    ontology_sampler:
        Optional injectable Ontology sampler.  When provided, a sample of
        Ontology instances is also verified (SRV-010 Search + Ontology).
    workspace_id / ontology_item_id / ontology_type_name:
        Coordinates for the Ontology sample (used when ontology_sampler is set).
    """

    def __init__(
        self,
        sampler: SearchSampler,
        doc_id_field: str = "chunk_id",
        table_name: str = "chunks",
        ontology_sampler: Optional[Any] = None,
        workspace_id: str = "",
        ontology_item_id: str = "",
        ontology_type_name: str = "",
    ) -> None:
        self._sampler = sampler
        self._doc_id_field = doc_id_field
        self._table_name = table_name
        self._ontology_sampler = ontology_sampler
        self._workspace_id = workspace_id
        self._ontology_item_id = ontology_item_id
        self._ontology_type_name = ontology_type_name

    def _check_blob_landing(
        self,
        result: TraceResult,
        tables: dict[str, list[dict[str, Any]]],
        expected_asset_version_id: Optional[str] = None,
    ) -> bool:
        """Return True when the trace reaches an immutable landing locator."""
        for table_name, record_id in result.path:
            if table_name == "asset_versions":
                avs = tables.get("asset_versions", [])
                for row in avs:
                    if row.get("asset_version_id") == record_id:
                        if row.get("blob_uri"):
                            return True
            if table_name == "source_files":
                for row in tables.get("source_files", []):
                    if row.get("source_file_id") != record_id:
                        continue
                    if (
                        expected_asset_version_id
                        and row.get("asset_version_id")
                        != expected_asset_version_id
                    ):
                        continue
                    locator = row.get("source_locator_json")
                    if isinstance(locator, str):
                        try:
                            locator = json.loads(locator)
                        except (TypeError, ValueError):
                            locator = {}
                    if isinstance(locator, dict) and locator.get("blob_uri"):
                        return True
        return False

    @staticmethod
    def _is_serving_projection_root(result: TraceResult) -> bool:
        """Accept a source-file landing when registry parent tables are omitted."""
        broken_edges = result.broken_edges
        if len(broken_edges) != 1:
            return False
        broken = broken_edges[0]
        return (
            broken.from_table == "source_files"
            and broken.fk_field == "parent_record_id"
            and broken.expected_table == "parent_record"
        )

    def verify(
        self,
        tables: dict[str, list[dict[str, Any]]],
        index_name: str = "kg-chunks",
        sample_size: int = 10,
    ) -> VerificationReport:
        """Sample documents from *index_name* and trace each back to Blob landing.

        Parameters
        ----------
        tables:
            Canonical table name → list of row dicts (from Parquet).
            Required to include the primary table and ``asset_versions`` for
            Blob landing verification.
        index_name:
            AI Search index name to sample from.
        sample_size:
            Number of documents to sample.
        """
        docs = self._sampler.sample(index_name, sample_size, select_fields=[
            self._doc_id_field,
            "asset_id",
            "asset_version_id",
            "run_id",
            "source_file_id",
            "project_id",
            "schema_version",
        ])

        verifications: list[SampleVerification] = []
        errors: list[str] = []

        for doc in docs:
            doc_id = (
                doc.get(self._doc_id_field)
                or doc.get("chunk_id")
                or doc.get("document_element_id")
                or ""
            )
            asset_id = doc.get("asset_id")
            run_id = doc.get("run_id")

            if not doc_id:
                err = f"Sampled document missing primary key field '{self._doc_id_field}'"
                errors.append(err)
                verifications.append(SampleVerification(
                    doc_id="(missing)", index_name=index_name,
                    asset_id=asset_id, run_id=run_id,
                    trace_result=None, resolved=False,
                    missing_from_tables=True, error=err,
                ))
                continue

            try:
                result = trace_record(
                    record_id=doc_id,
                    tables=tables,
                    table_name=self._table_name,
                    direction="backward",
                )
            except Exception as exc:
                err = f"trace_record({doc_id!r}) failed: {exc}"
                errors.append(err)
                verifications.append(SampleVerification(
                    doc_id=doc_id, index_name=index_name,
                    asset_id=asset_id, run_id=run_id,
                    trace_result=None, resolved=False,
                    missing_from_tables=True, error=err,
                ))
                continue

            # Use the public broken_edges property — NOT the private _broken_edge_detail.
            broken_edge_str: Optional[str] = None
            if result.broken_edges:
                broken_edge_str = str(result.broken_edges[0])

            missing = not result.path and not result.is_complete
            has_blob = self._check_blob_landing(
                result,
                tables,
                expected_asset_version_id=doc.get("asset_version_id"),
            )
            resolved = has_blob and (
                result.is_complete
                or self._is_serving_projection_root(result)
            )

            verifications.append(SampleVerification(
                doc_id=doc_id, index_name=index_name,
                asset_id=asset_id, run_id=run_id,
                trace_result=result, resolved=resolved,
                missing_from_tables=missing,
                has_blob_landing=has_blob,
                error=None if resolved else broken_edge_str,
            ))

        # Optional Ontology sample
        ontology_sample_count = 0
        if self._ontology_sampler and self._ontology_item_id and self._ontology_type_name:
            try:
                onto_docs = self._ontology_sampler.sample_instances(
                    self._workspace_id,
                    self._ontology_item_id,
                    self._ontology_type_name,
                    sample_size,
                )
                ontology_sample_count = len(onto_docs)
                logger.info(
                    "[lineage_verifier] Ontology sample: %d %s instances",
                    ontology_sample_count, self._ontology_type_name,
                )
            except Exception as exc:
                err = f"Ontology sample failed: {exc}"
                errors.append(err)

        resolved_count = sum(1 for v in verifications if v.resolved)
        missing_count = sum(1 for v in verifications if v.missing_from_tables)
        broken_count = sum(
            1 for v in verifications if not v.resolved and not v.missing_from_tables
        )
        ok = len(verifications) > 0 and resolved_count == len(verifications)

        logger.info(
            "[lineage_verifier] %s: sampled=%d resolved=%d missing=%d broken=%d onto=%d",
            index_name, len(verifications), resolved_count,
            missing_count, broken_count, ontology_sample_count,
        )

        return VerificationReport(
            ok=ok,
            sample_count=len(verifications),
            resolved_count=resolved_count,
            missing_count=missing_count,
            broken_count=broken_count,
            ontology_sample_count=ontology_sample_count,
            verifications=verifications,
            errors=errors,
        )
