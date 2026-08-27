"""L2-only input gates and complete-corpus SourceUnit materialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.evidence import EvidenceSpan, SourceUnit
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.contracts.receipts import (
    ArtifactEntry,
    ArtifactManifest,
    StageReceipt,
)
from fabric_kg_builder.domain.contexts import (
    DomainApprovalContext,
    DomainDesignContext,
    DomainSourceProfile,
)
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.proposal import DomainProposal
from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract
from fabric_kg_builder.domain.stage import validate_approval_bindings
from fabric_kg_builder.model.schemas import (
    AssetRow,
    AssetVersionRow,
    DocumentElementRow,
)
from fabric_kg_builder.sources.adapter import AdapterError
from fabric_kg_builder.sources.corpus import (
    DesignSampleManifest,
    SourceCorpusEntry,
    SourceCorpusManifest,
    SourceSnapshotIntegrityError,
    extract_verified_source_snapshot,
    open_verified_source_snapshot,
)

L2_STAGE_NAME = "Schema-Constrained Extraction"
L2_STATE_DIR = Path(".fkg") / "l2"
L2_ACCEPTED_VERSIONS = {
    "c0.artifact_manifest": "1.0.0",
    "c0.candidate_accounting_disposition": "1.0.0",
    "c0.candidate_lifecycle_record": "1.0.0",
    "c0.extraction_candidate_batch": "1.0.0",
    "c0.required_member_set_proposal": "1.1.0",
    "c0.source_unit": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
    "domain.contract": "2.0.0",
    "l1.design_sample_manifest": "1.0.0",
    "l1.domain_approval_context": "1.0.0",
    "l1.source_corpus_manifest": "1.0.0",
}
_L1_HANDOFF_VERSIONS = {
    "c0.artifact_manifest": "1.0.0",
    "c0.evidence_span": "1.0.0",
    "c0.source_unit": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
    "domain.contract": "2.0.0",
    "l1.design_sample_manifest": "1.0.0",
    "l1.domain_approval_context": "1.0.0",
    "l1.source_corpus_manifest": "1.0.0",
}


class L2StageError(ValueError):
    """Fail-closed L2 error carrying one stable audit code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class L2Inputs:
    l1_receipt: StageReceipt
    l1_output_manifest: ArtifactManifest
    l1_input_manifest: ArtifactManifest
    corpus_manifest: SourceCorpusManifest
    design_sample_manifest: DesignSampleManifest
    approval_context: DomainApprovalContext
    domain_contract: DomainContractV2

    @property
    def authority_hashes(self) -> dict[str, str]:
        return {
            "domain_contract_hash": compute_contract_hash(self.domain_contract),
            "hierarchy_hash": self.domain_contract.hierarchy_closure.hierarchy_hash,
            "identity_policy_hash": self.domain_contract.identity_policy_hash,
            "completeness_requirement_hash": (
                self.domain_contract.completeness_requirement_hash
            ),
            "external_reference_decision_hash": (
                self.domain_contract.external_reference_decision_hash
            ),
        }


@dataclass(frozen=True)
class SourceElement:
    """One eligible normalized element returned by an existing media adapter."""

    element_id: str
    unit_kind: str
    text: str
    ordinal: int
    locator: ImmutableSourceLocator
    parent_element_id: str | None = None


@dataclass(frozen=True)
class CorpusAsset:
    """Asset/version bytes and adapter output resolved from the frozen inventory."""

    asset: AssetRow
    version: AssetVersionRow
    consumed_byte_hash: str
    consumed_byte_count: int
    adapter_name: str
    adapter_version: str
    elements: tuple[SourceElement, ...]


class SourceCorpusReader(Protocol):
    """Resolve one manifest entry from immutable Asset/AssetVersion authority."""

    def read(self, entry: SourceCorpusEntry) -> CorpusAsset:
        """Read exact landed bytes and dispatch the approved existing adapter."""


class IndexedSourceCorpusReader:
    """Filesystem adapter backed by explicit AssetRow/AssetVersionRow indexes.

    The source root is only a byte-access mount. Persisted SourceUnit locators are
    derived from immutable asset-version URIs, never from local paths.
    """

    def __init__(
        self,
        *,
        source_root: Path,
        assets: tuple[AssetRow, ...],
        versions: tuple[AssetVersionRow, ...],
        adapter_versions: dict[str, str] | None = None,
    ) -> None:
        self._source_root = source_root.resolve()
        self._assets = {item.asset_id: item for item in assets}
        self._versions = {item.asset_version_id: item for item in versions}
        self._adapter_versions = adapter_versions or {}
        if len(self._assets) != len(assets) or len(self._versions) != len(versions):
            raise L2StageError(
                "L2_CORPUS_INVENTORY_INVALID",
                "asset and asset-version identities must be unique",
            )

    def read(self, entry: SourceCorpusEntry) -> CorpusAsset:
        try:
            asset = self._assets[entry.asset_id]
            version = self._versions[entry.asset_version_id]
        except KeyError as exc:
            raise L2StageError(
                "L2_ASSET_VERSION_MISSING",
                f"missing asset authority for {entry.source_file_id}",
            ) from exc
        if version.asset_id != asset.asset_id:
            raise L2StageError(
                "L2_CORPUS_INVENTORY_INVALID",
                "asset version references a different logical asset",
            )
        source_path = (self._source_root / entry.relative_source_ref).resolve()
        try:
            source_path.relative_to(self._source_root)
        except ValueError as exc:
            raise L2StageError(
                "L2_ASSET_VERSION_MISSING",
                f"landed bytes are unavailable for {entry.source_file_id}",
            ) from exc
        try:
            with open_verified_source_snapshot(
                source_path,
                entry=entry,
            ) as snapshot:
                extraction = extract_verified_source_snapshot(snapshot)
                adapted = extraction.adapter_result
        except AdapterError as exc:
            if isinstance(exc, SourceSnapshotIntegrityError):
                raise L2StageError(
                    "L2_ASSET_CONTENT_MISMATCH",
                    f"landed bytes differ for {entry.source_file_id}",
                ) from exc
            raise L2StageError(
                "L2_SOURCE_ADAPTER_FAILED",
                f"adapter failed for {entry.source_file_id}: {type(exc).__name__}",
            ) from exc
        except (OSError, UnicodeError, ValueError, ImportError) as exc:
            raise L2StageError(
                "L2_SOURCE_ADAPTER_FAILED",
                f"adapter failed for {entry.source_file_id}: {type(exc).__name__}",
            ) from exc
        adapter_name = entry.adapter_name or type(adapted).__name__
        adapter_version = self._adapter_versions.get(adapter_name, "1.0.0")
        eligible_rows = [
            row for row in adapted.document_elements if _eligible_element_text(row)
        ]
        stable_element_ids = {
            row.document_element_id: deterministic_contract_id(
                "source-element",
                {
                    "asset_version_id": version.asset_version_id,
                    "ordinal": index,
                    "element_type": row.element_type,
                    "content_hash": row.content_hash,
                },
            )
            for index, row in enumerate(eligible_rows)
        }
        elements = tuple(
            _source_element_from_row(
                row,
                version=version,
                ordinal=index,
                element_id=stable_element_ids[row.document_element_id],
                parent_element_id=stable_element_ids.get(row.parent_element_id),
            )
            for index, row in enumerate(eligible_rows)
        )
        return CorpusAsset(
            asset=asset,
            version=version,
            consumed_byte_hash=extraction.consumed_byte_hash,
            consumed_byte_count=extraction.consumed_byte_count,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            elements=elements,
        )


@dataclass(frozen=True)
class CorpusEntryDisposition:
    source_file_id: str
    asset_version_id: str
    disposition: str
    source_unit_ids: tuple[str, ...]
    adapter_name: str
    adapter_version: str


@dataclass(frozen=True)
class CorpusMaterializationReport:
    source_corpus_manifest_id: str
    source_corpus_manifest_hash: str
    source_corpus_entry_count: int
    materialized_corpus_entry_count: int
    explicitly_empty_corpus_entry_count: int
    ineligible_corpus_entry_count: int
    source_unit_count: int
    source_unit_count_by_asset_version: tuple[tuple[str, int], ...]
    dispositions: tuple[CorpusEntryDisposition, ...]
    source_unit_id_set_hash: str
    report_hash: str


@dataclass(frozen=True)
class MaterializedCorpus:
    source_units: tuple[SourceUnit, ...]
    source_unit_manifest: ArtifactManifest
    report: CorpusMaterializationReport


def _read_json(path: Path, model_type: type, code: str):
    try:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise L2StageError(code, f"invalid artifact {path.name}: {exc}") from exc


def _manifest_entry(
    manifest: ArtifactManifest,
    *,
    artifact_id: str,
    contract_kind: str,
    version: str,
    content_hash: str,
    code: str,
) -> ArtifactEntry:
    matches = [
        entry
        for entry in manifest.entries
        if entry.artifact_id == artifact_id and entry.contract_kind == contract_kind
    ]
    if len(matches) != 1:
        raise L2StageError(
            code,
            f"manifest must contain exactly one {contract_kind} entry {artifact_id}",
        )
    entry = matches[0]
    if entry.contract_version != version:
        raise L2StageError(
            "L2_CONTRACT_VERSION_UNSUPPORTED",
            f"{contract_kind}@{entry.contract_version} is unsupported",
        )
    if entry.content_hash != content_hash:
        raise L2StageError(code, f"{contract_kind} content hash differs from manifest")
    return entry


def _safe_id(value: str) -> str:
    return value.replace(":", "-", 1)


def _validate_design_sample_artifacts(
    state_root: Path,
    sample: DesignSampleManifest,
    output_manifest: ArtifactManifest,
) -> None:
    for sample_entry in sample.entries:
        for source_unit_id in sample_entry.source_unit_ids:
            path = (
                state_root
                / "design-samples"
                / "source-units"
                / f"{_safe_id(source_unit_id)}.json"
            )
            unit = _read_json(path, SourceUnit, "L2_SOURCE_MANIFEST_INVALID")
            _manifest_entry(
                output_manifest,
                artifact_id=source_unit_id,
                contract_kind="c0.source_unit",
                version="1.0.0",
                content_hash=canonical_sha256(unit),
                code="L2_SOURCE_MANIFEST_INVALID",
            )
        for evidence_id in sample_entry.evidence_span_ids:
            path = (
                state_root
                / "design-samples"
                / "evidence-spans"
                / f"{_safe_id(evidence_id)}.json"
            )
            evidence = _read_json(
                path,
                EvidenceSpan,
                "L2_SOURCE_MANIFEST_INVALID",
            )
            _manifest_entry(
                output_manifest,
                artifact_id=evidence_id,
                contract_kind="c0.evidence_span",
                version="1.0.0",
                content_hash=canonical_sha256(evidence),
                code="L2_SOURCE_MANIFEST_INVALID",
            )


def load_l2_inputs(
    *,
    l1_state_root: Path = Path(".fkg") / "l1",
    domain_path: Path = Path("domain.yaml"),
) -> L2Inputs:
    """Load an intact approved L1 handoff; never infer authority from samples."""

    receipt = _read_json(
        l1_state_root / "stage-receipt.json",
        StageReceipt,
        "L2_INPUT_RECEIPT_INVALID",
    )
    if (
        receipt.stage_id != "L1"
        or receipt.stage_name != "Domain Design/Approval"
        or receipt.status != "succeeded"
        or receipt.stage_contract_version != "1.0.0"
    ):
        raise L2StageError(
            "L2_INPUT_RECEIPT_INVALID",
            "L2 requires one succeeded L1 Domain Design/Approval receipt",
        )
    for kind, version in _L1_HANDOFF_VERSIONS.items():
        if receipt.accepted_contract_versions.get(kind) != version:
            raise L2StageError(
                "L2_CONTRACT_VERSION_UNSUPPORTED",
                f"L1 did not bind required {kind}@{version}",
            )

    output_manifest = _read_json(
        l1_state_root / "output-manifest.json",
        ArtifactManifest,
        "L2_INPUT_RECEIPT_INVALID",
    )
    if (
        receipt.output_manifest_id != output_manifest.artifact_manifest_id
        or receipt.output_manifest_hash != output_manifest.manifest_hash
    ):
        raise L2StageError(
            "L2_INPUT_RECEIPT_INVALID",
            "L1 output manifest ID/hash differs from its receipt",
        )
    input_manifest = _read_json(
        l1_state_root / "input-manifest.json",
        ArtifactManifest,
        "L2_INPUT_RECEIPT_INVALID",
    )
    if (
        receipt.input_manifest_id != input_manifest.artifact_manifest_id
        or receipt.input_manifest_hash != input_manifest.manifest_hash
    ):
        raise L2StageError(
            "L2_INPUT_RECEIPT_INVALID",
            "L1 input manifest ID/hash differs from its receipt",
        )

    corpus = _read_json(
        l1_state_root / "source-corpus-manifest.json",
        SourceCorpusManifest,
        "L2_SOURCE_MANIFEST_INVALID",
    )
    sample = _read_json(
        l1_state_root / "design-sample-manifest.json",
        DesignSampleManifest,
        "L2_SOURCE_MANIFEST_INVALID",
    )
    approval = _read_json(
        l1_state_root / "domain-approval-context.json",
        DomainApprovalContext,
        "L2_DOMAIN_CONTRACT_INVALID",
    )
    design = _read_json(
        l1_state_root / "domain-design-context.json",
        DomainDesignContext,
        "L2_DOMAIN_CONTRACT_INVALID",
    )
    proposal = _read_json(
        l1_state_root / "domain-proposal.json",
        DomainProposal,
        "L2_DOMAIN_CONTRACT_INVALID",
    )
    profile = _read_json(
        l1_state_root / "source-profile.json",
        DomainSourceProfile,
        "L2_DOMAIN_CONTRACT_INVALID",
    )
    try:
        contract = load_domain_contract(domain_path)
    except Exception as exc:
        raise L2StageError(
            "L2_DOMAIN_CONTRACT_INVALID",
            f"invalid approved domain contract: {exc}",
        ) from exc
    if not isinstance(contract, DomainContractV2):
        raise L2StageError(
            "L2_DOMAIN_CONTRACT_INVALID",
            "L2 requires DomainContractV2 schema_version 2.0",
        )
    if contract.approval.status != "approved" or approval.decision != "approve":
        raise L2StageError(
            "L2_DOMAIN_CONTRACT_INVALID",
            "draft, corrected, or aborted domain decisions are not consumable",
        )
    domain_hash = compute_contract_hash(contract)
    if contract.approval.contract_hash != domain_hash:
        raise L2StageError(
            "L2_DOMAIN_HASH_MISMATCH",
            "approved domain hash does not recompute",
        )

    try:
        validate_approval_bindings(
            contract=contract,
            approval_context=approval,
            proposal=proposal,
            design_context=design,
            profile=profile,
            corpus=corpus,
            sample=sample,
            input_manifest=input_manifest,
        )
    except Exception as exc:
        text = str(exc).casefold()
        code = "L2_DOMAIN_CONTRACT_INVALID"
        for label, candidate in (
            ("hierarchy", "L2_HIERARCHY_HASH_MISMATCH"),
            ("identity", "L2_IDENTITY_POLICY_HASH_MISMATCH"),
            ("completeness", "L2_COMPLETENESS_HASH_MISMATCH"),
            ("external reference", "L2_EXTERNAL_REFERENCE_DECISION_HASH_MISMATCH"),
            ("domain", "L2_DOMAIN_HASH_MISMATCH"),
        ):
            if label in text:
                code = candidate
                break
        raise L2StageError(code, f"L1 approval chain is stale: {exc}") from exc

    _manifest_entry(
        output_manifest,
        artifact_id=corpus.source_corpus_manifest_id,
        contract_kind="l1.source_corpus_manifest",
        version="1.0.0",
        content_hash=corpus.corpus_hash,
        code="L2_SOURCE_MANIFEST_INVALID",
    )
    _manifest_entry(
        output_manifest,
        artifact_id=sample.design_sample_manifest_id,
        contract_kind="l1.design_sample_manifest",
        version="1.0.0",
        content_hash=sample.sample_hash,
        code="L2_SOURCE_MANIFEST_INVALID",
    )
    _manifest_entry(
        output_manifest,
        artifact_id=approval.domain_approval_context_id,
        contract_kind="l1.domain_approval_context",
        version="1.0.0",
        content_hash=approval.approval_context_hash,
        code="L2_DOMAIN_CONTRACT_INVALID",
    )
    _manifest_entry(
        output_manifest,
        artifact_id="domain.contract",
        contract_kind="domain.contract",
        version="2.0.0",
        content_hash=domain_hash,
        code="L2_DOMAIN_HASH_MISMATCH",
    )
    _validate_design_sample_artifacts(l1_state_root, sample, output_manifest)

    if receipt.identity.domain_contract_hash != domain_hash:
        raise L2StageError(
            "L2_DOMAIN_HASH_MISMATCH",
            "L1 receipt domain authority differs from approved domain",
        )
    if corpus.inventory_scope != "complete":
        raise L2StageError(
            "L2_SOURCE_MANIFEST_INVALID",
            "bounded design samples cannot substitute for a complete corpus",
        )

    return L2Inputs(
        l1_receipt=receipt,
        l1_output_manifest=output_manifest,
        l1_input_manifest=input_manifest,
        corpus_manifest=corpus,
        design_sample_manifest=sample,
        approval_context=approval,
        domain_contract=contract,
    )


def _eligible_element_text(row: DocumentElementRow) -> bool:
    return bool((row.content or row.title or "").strip())


def _unit_kind(row: DocumentElementRow) -> str:
    normalized = row.element_type.casefold()
    if normalized in {"heading", "title"}:
        return "heading"
    if normalized in {"table", "table_row"}:
        return "table"
    if normalized in {"table_cell", "cell"}:
        return "cell"
    if normalized in {"image_ref", "vision_description", "ocr_text"}:
        return "visual_description"
    return "paragraph"


def _source_element_from_row(
    row: DocumentElementRow,
    *,
    version: AssetVersionRow,
    ordinal: int,
    element_id: str,
    parent_element_id: str | None,
) -> SourceElement:
    locator = ImmutableSourceLocator.from_authority(
        blob_uri=version.blob_uri,
        blob_version_id=version.blob_version_id or version.version_identity,
        page=row.page_number,
        section_path=row.section_path,
        native_object_id=element_id,
    )
    return SourceElement(
        element_id=element_id,
        unit_kind=_unit_kind(row),
        text=(row.content or row.title or "").strip(),
        ordinal=row.sort_order if row.sort_order is not None else ordinal,
        locator=locator,
        parent_element_id=parent_element_id,
    )


def _source_unit_manifest(
    source_units: tuple[SourceUnit, ...],
    *,
    identity: CanonicalIdentityEnvelope,
) -> ArtifactManifest:
    entries = tuple(
        ArtifactEntry(
            artifact_id=unit.source_unit_id,
            contract_kind="c0.source_unit",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(SourceUnit.model_json_schema()),
            content_hash=canonical_sha256(unit),
            canonical_id_set_hash=None,
            row_count=1,
            byte_count=len((canonical_json(unit) + "\n").encode("utf-8")),
            partition_count=1,
            media_type="application/json",
            immutable_locator=unit.locator,
            blob_asset_ref_id=unit.identity.asset_version_id,
        )
        for unit in sorted(source_units, key=lambda item: item.source_unit_id)
    )
    entries_hash = canonical_sha256(
        [entry.model_dump(mode="json") for entry in entries]
    )
    manifest_identity = identity.model_copy(
        update={
            "contract_kind": "c0.artifact_manifest",
            "source_file_id": None,
            "source_unit_id": None,
            "asset_id": None,
            "asset_version_id": None,
            "content_hash": None,
            "immutable_locator": None,
        }
    )
    manifest_id = deterministic_contract_id(
        "artifact-manifest",
        {"stage_id": "L2", "partition": "source-units", "entries_hash": entries_hash},
    )
    values = {
        "identity": manifest_identity,
        "artifact_manifest_id": manifest_id,
        "entries": entries,
        "total_row_count": len(entries),
        "total_byte_count": sum(entry.byte_count for entry in entries),
    }
    return ArtifactManifest(**values, manifest_hash=canonical_sha256(values))


def materialize_source_corpus(
    inputs: L2Inputs,
    reader: SourceCorpusReader,
) -> MaterializedCorpus:
    """Materialize and reconcile every complete-corpus entry before extraction."""

    units: list[SourceUnit] = []
    dispositions: list[CorpusEntryDisposition] = []
    counts_by_version: dict[str, int] = {}
    seen_unit_ids: set[str] = set()
    seen_asset_ids: set[str] = set()
    seen_version_ids: set[str] = set()
    for entry in inputs.corpus_manifest.entries:
        if entry.asset_id in seen_asset_ids or entry.asset_version_id in seen_version_ids:
            raise L2StageError(
                "L2_CORPUS_INVENTORY_INVALID",
                "asset and asset-version IDs must each occur once in the corpus",
            )
        seen_asset_ids.add(entry.asset_id)
        seen_version_ids.add(entry.asset_version_id)
        if entry.disposition != "eligible":
            counts_by_version[entry.asset_version_id] = 0
            dispositions.append(
                CorpusEntryDisposition(
                    source_file_id=entry.source_file_id,
                    asset_version_id=entry.asset_version_id,
                    disposition=entry.disposition,
                    source_unit_ids=(),
                    adapter_name=entry.adapter_name or "not-applicable",
                    adapter_version="not-applicable",
                )
            )
            continue

        asset = reader.read(entry)
        if (
            asset.consumed_byte_hash != entry.original_byte_hash
            or asset.consumed_byte_count != entry.byte_count
            or asset.version.content_hash != entry.original_byte_hash
            or asset.version.size_bytes != entry.byte_count
        ):
            raise L2StageError(
                "L2_ASSET_CONTENT_MISMATCH",
                f"landed bytes differ for {entry.source_file_id}",
            )
        if (
            asset.asset.asset_id != entry.asset_id
            or asset.version.asset_version_id != entry.asset_version_id
            or asset.version.asset_id != entry.asset_id
        ):
            raise L2StageError(
                "L2_CORPUS_DISPOSITION_MISMATCH",
                f"asset authority differs for {entry.source_file_id}",
            )
        identity = inputs.l1_receipt.identity.model_copy(
            update={
                "contract_kind": "c0.source_unit",
                "asset_id": entry.asset_id,
                "asset_version_id": entry.asset_version_id,
                "source_file_id": entry.source_file_id,
                "source_unit_id": None,
                "content_hash": entry.original_byte_hash,
                "immutable_locator": None,
                "parent_artifact_ids": (
                    inputs.corpus_manifest.source_corpus_manifest_id,
                ),
            }
        )
        entry_units: list[SourceUnit] = []
        parent_units: dict[str, str] = {}
        for element in sorted(asset.elements, key=lambda item: (item.ordinal, item.element_id)):
            parent_source_unit_id = (
                parent_units.get(element.parent_element_id)
                if element.parent_element_id is not None
                else None
            )
            try:
                unit = SourceUnit.mint(
                    identity=identity,
                    unit_kind=element.unit_kind,
                    text=element.text,
                    ordinal=element.ordinal,
                    locator=element.locator,
                    parent_source_unit_id=parent_source_unit_id,
                )
            except (ValidationError, ValueError) as exc:
                raise L2StageError(
                    "L2_SOURCE_UNIT_INVALID",
                    f"invalid SourceUnit for {entry.source_file_id}: {exc}",
                ) from exc
            if unit.source_unit_id in seen_unit_ids:
                raise L2StageError(
                    "L2_SOURCE_UNIT_ID_COLLISION",
                    f"duplicate SourceUnit ID {unit.source_unit_id}",
                )
            seen_unit_ids.add(unit.source_unit_id)
            parent_units[element.element_id] = unit.source_unit_id
            entry_units.append(unit)
        units.extend(entry_units)
        counts_by_version[entry.asset_version_id] = len(entry_units)
        dispositions.append(
            CorpusEntryDisposition(
                source_file_id=entry.source_file_id,
                asset_version_id=entry.asset_version_id,
                disposition="materialized" if entry_units else "explicitly_empty",
                source_unit_ids=tuple(
                    sorted(unit.source_unit_id for unit in entry_units)
                ),
                adapter_name=asset.adapter_name,
                adapter_version=asset.adapter_version,
            )
        )

    source_units = tuple(sorted(units, key=lambda item: item.source_unit_id))
    if len(dispositions) != inputs.corpus_manifest.total_entry_count:
        raise L2StageError(
            "L2_CORPUS_ACCOUNTING_INCOMPLETE",
            "every corpus entry requires one L2 disposition",
        )
    if set(counts_by_version) != {
        entry.asset_version_id for entry in inputs.corpus_manifest.entries
    }:
        raise L2StageError(
            "L2_CORPUS_ACCOUNTING_INCOMPLETE",
            "asset-version accounting does not cover the frozen inventory",
        )
    manifest = _source_unit_manifest(
        source_units,
        identity=inputs.l1_receipt.identity,
    )
    report_values = {
        "source_corpus_manifest_id": inputs.corpus_manifest.source_corpus_manifest_id,
        "source_corpus_manifest_hash": inputs.corpus_manifest.corpus_hash,
        "source_corpus_entry_count": len(dispositions),
        "materialized_corpus_entry_count": sum(
            item.disposition == "materialized" for item in dispositions
        ),
        "explicitly_empty_corpus_entry_count": sum(
            item.disposition == "explicitly_empty" for item in dispositions
        ),
        "ineligible_corpus_entry_count": sum(
            item.disposition in {"excluded", "blocked"} for item in dispositions
        ),
        "source_unit_count": len(source_units),
        "source_unit_count_by_asset_version": tuple(sorted(counts_by_version.items())),
        "dispositions": tuple(
            item.__dict__
            for item in sorted(dispositions, key=lambda item: item.source_file_id)
        ),
        "source_unit_id_set_hash": canonical_sha256(
            sorted(item.source_unit_id for item in source_units)
        ),
    }
    report = CorpusMaterializationReport(
        **{
            **report_values,
            "dispositions": tuple(
                sorted(dispositions, key=lambda item: item.source_file_id)
            ),
        },
        report_hash=canonical_sha256(report_values),
    )
    if (
        report.source_corpus_entry_count
        != report.materialized_corpus_entry_count
        + report.explicitly_empty_corpus_entry_count
        + report.ineligible_corpus_entry_count
        or report.source_unit_count != sum(counts_by_version.values())
        or report.source_unit_count != manifest.total_row_count
    ):
        raise L2StageError(
            "L2_CORPUS_ACCOUNTING_INCOMPLETE",
            "corpus/SourceUnit counts do not reconcile",
        )
    return MaterializedCorpus(
        source_units=source_units,
        source_unit_manifest=manifest,
        report=report,
    )


def l2_input_fingerprint(
    inputs: L2Inputs,
    source_unit_manifest: ArtifactManifest,
    *,
    prompt_version: str,
    prompt_hash: str,
    model_version: str,
    model_hash: str,
    extractor_name: str,
    extractor_version: str,
    response_schema_hash: str,
    split_policy_version: str,
) -> str:
    """Bind every semantic input; design-sample hash is context, never coverage."""

    contract = inputs.domain_contract
    return canonical_sha256(
        {
            "l1_receipt_hash": inputs.l1_receipt.receipt_hash,
            "l1_output_manifest_hash": inputs.l1_output_manifest.manifest_hash,
            "source_corpus_manifest_id": (
                inputs.corpus_manifest.source_corpus_manifest_id
            ),
            "source_corpus_manifest_hash": inputs.corpus_manifest.corpus_hash,
            "source_unit_manifest_id": source_unit_manifest.artifact_manifest_id,
            "source_unit_manifest_hash": source_unit_manifest.manifest_hash,
            "domain_contract_hash": compute_contract_hash(contract),
            "hierarchy_hash": contract.hierarchy_closure.hierarchy_hash,
            "identity_policy_hash": contract.identity_policy_hash,
            "completeness_requirement_hash": (
                contract.completeness_requirement_hash
            ),
            "external_reference_decision_hash": (
                contract.external_reference_decision_hash
            ),
            "relationship_type_count_n": (
                contract.reasoning_policy.relationship_type_count
            ),
            "max_hops_k": contract.reasoning_policy.max_hops,
            "max_relations_per_work_unit": (
                contract.reasoning_policy.max_relations_per_work_unit
            ),
            "prompt": [prompt_version, prompt_hash],
            "model": [model_version, model_hash],
            "extractor": [extractor_name, extractor_version],
            "response_schema_hash": response_schema_hash,
            "split_policy_version": split_policy_version,
            "accepted_contract_versions": L2_ACCEPTED_VERSIONS,
        }
    )
