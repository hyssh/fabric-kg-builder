"""Collect redacted runtime evidence from executable competency contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from azure.identity import DefaultAzureCredential
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fabric_kg_builder.knowledge.transport import HttpTransport
from fabric_kg_builder.semantic.query_validation import (
    compute_physical_query_hash,
)
from fabric_kg_builder.semantic.schemas import (
    SemanticDiagnosticRecord,
    compute_query_plan_hash,
)
from fabric_kg_builder.serving.graph_model import GraphModelGQLClient

from .contract import CompetencyCase, CompetencyContract
from .executors import (
    DataAgentMcpExecutor,
    FabricGraphExecutor,
    SearchKnowledgeExecutor,
)
from .semantic_reliability import (
    EvidenceLocator,
    EvidenceTrace,
    GroundedAnswerViolationError,
    QueryExecutionStatus,
    SEMANTICALLY_SUCCESSFUL_STATUSES,
    SourceExecutionOutcome,
    SourceRequirement,
    resolve_required_source_status,
    semantic_determinism_signature,
    validate_grounded_answer,
)


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _route_result_category(
    route: dict[str, Any],
) -> QueryExecutionStatus:
    raw = str(route.get("result_category") or "").strip()
    if raw:
        return QueryExecutionStatus(raw)
    status = str(route.get("status") or "").casefold()
    if status in {"success", "succeeded", "passed", "complete"}:
        return QueryExecutionStatus.SUCCESS
    if status == "partial":
        return QueryExecutionStatus.PARTIAL_RESULT
    return QueryExecutionStatus.PLATFORM_FAILURE


_ANSWER_ID_PATTERN = re.compile(
    r"\b(?:entity|src|evid|evidence|chunk|asset-version):"
    r"[A-Za-z0-9_.-]+\b",
    re.IGNORECASE,
)
_ANSWER_FIELD_ID_PATTERN = re.compile(
    r"\b(?P<field>(?:[A-Za-z][A-Za-z0-9_]*_)?entity_id|"
    r"source_file_id|evidence_id|asset_version_id)"
    r"\s*[:=]\s*`?(?P<value>[A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)
_GROUNDING_NO_MATCH_SIGNALS = (
    "no verified",
    "no data",
    "no matching",
    "not found",
    "returned no",
)


def _answer_identifiers(answer: str) -> set[str]:
    identifiers = set(_ANSWER_ID_PATTERN.findall(answer))
    known_prefixes = {
        "entity",
        "src",
        "evid",
        "evidence",
        "chunk",
        "asset-version",
    }
    for match in _ANSWER_FIELD_ID_PATTERN.finditer(answer):
        value = match.group("value").rstrip(".,;:)")
        field = match.group("field").casefold()
        prefix_for_field = (
            "entity"
            if field.endswith("entity_id")
            else {
                "source_file_id": "src",
                "evidence_id": "evid",
                "asset_version_id": "asset-version",
            }[field]
        )
        prefix, separator, _suffix = value.partition(":")
        if not separator or prefix.casefold() not in known_prefixes:
            value = f"{prefix_for_field}:{value}"
        identifiers.add(value)
    return identifiers


def _graph_grounding_identifiers(graph: dict[str, Any]) -> set[str]:
    identifiers = {
        str(value) for value in graph.get("canonical_ids", []) if value
    }
    for relationship in graph.get("accepted_relationships", []):
        if not isinstance(relationship, dict):
            continue
        identifiers.update(
            str(value)
            for value in relationship.get("evidence_ids", [])
            if value
        )
    return identifiers


def _mcp_grounding_quality(observation: dict[str, Any]) -> tuple[int, int, int]:
    answer = str(observation.get("answer") or "")
    normalized = " ".join(answer.casefold().split())
    identifiers = _answer_identifiers(answer)
    citations = observation.get("citations")
    citation_count = len(citations) if isinstance(citations, list) else 0
    no_match = any(
        signal in normalized for signal in _GROUNDING_NO_MATCH_SIGNALS
    )
    return (
        1 if identifiers or citation_count else 0,
        0 if no_match else 1,
        len(identifiers) + citation_count,
    )


def _needs_mcp_grounding_retry(
    *,
    case: CompetencyCase,
    graph: dict[str, Any],
    mcp: dict[str, Any],
) -> bool:
    if case.routes.data_agent_mcp != "required":
        return False
    if graph.get("status") != "success":
        return False
    relationships = graph.get("accepted_relationships")
    if not isinstance(relationships, list) or not relationships:
        return False
    if mcp.get("status") != "success":
        return False
    answer = str(mcp.get("answer") or "")
    if not answer.strip():
        return True
    normalized = " ".join(answer.casefold().split())
    if any(signal in normalized for signal in _GROUNDING_NO_MATCH_SIGNALS):
        return True
    citations = mcp.get("citations")
    return bool(case.expected.evidence_required) and not (
        _answer_identifiers(answer)
        or (isinstance(citations, list) and citations)
    )


def _merge_mcp_grounding_retry(
    initial: dict[str, Any],
    retried: dict[str, Any],
) -> dict[str, Any]:
    selected = (
        retried
        if _mcp_grounding_quality(retried)
        > _mcp_grounding_quality(initial)
        else initial
    )
    merged = dict(selected)
    for key in (
        "request_ids",
        "retry_request_ids",
        "retry_correlation_ids",
        "client_request_ids",
    ):
        merged[key] = list(
            dict.fromkeys([
                *initial.get(key, []),
                *retried.get(key, []),
            ])
        )
    merged["retry_count"] = (
        int(initial.get("retry_count") or 0)
        + int(retried.get("retry_count") or 0)
        + 1
    )
    merged["grounding_retry_count"] = 1
    merged["grounding_retry_trigger"] = (
        "graph_relationships_contradicted_or_uncited"
    )
    return merged


def _merge_search_resolution(
    search: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    requested = {
        str(value)
        for value in resolution.get("requested_identifiers", [])
        if value
    }
    resolved = {
        str(value)
        for value in resolution.get("resolved_identifiers", [])
        if value
    }
    linkage = {
        "status": resolution.get("status"),
        "requested_count": len(requested),
        "resolved_count": len(resolved),
        "unresolved_identifiers": sorted(requested - resolved),
        "request_ids": list(resolution.get("request_ids", [])),
    }
    if resolution.get("status") != "success":
        return {
            **search,
            "partial_source": True,
            "identifier_resolution": linkage,
        }

    citations = [
        citation
        for citation in search.get("citations", [])
        if isinstance(citation, dict)
    ]
    seen_citations = {
        json.dumps(citation, sort_keys=True, separators=(",", ":"))
        for citation in citations
    }
    for citation in resolution.get("citations", []):
        if not isinstance(citation, dict):
            continue
        signature = json.dumps(
            citation,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature not in seen_citations:
            citations.append(citation)
            seen_citations.add(signature)

    facts = [
        fact
        for fact in search.get("accepted_facts", [])
        if isinstance(fact, dict)
    ]
    seen_facts = {
        json.dumps(fact, sort_keys=True, separators=(",", ":"))
        for fact in facts
    }
    for fact in resolution.get("accepted_facts", []):
        if not isinstance(fact, dict):
            continue
        signature = json.dumps(
            fact,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature not in seen_facts:
            facts.append(fact)
            seen_facts.add(signature)

    return {
        **search,
        "request_ids": list(
            dict.fromkeys([
                *search.get("request_ids", []),
                *resolution.get("request_ids", []),
            ])
        ),
        "latency_ms": round(
            float(search.get("latency_ms") or 0)
            + float(resolution.get("latency_ms") or 0),
            3,
        ),
        "result_count": len(citations),
        "canonical_ids": sorted({
            *(
                str(value)
                for value in search.get("canonical_ids", [])
                if value
            ),
            *(
                str(value)
                for value in resolution.get("canonical_ids", [])
                if value
            ),
        }),
        "citations": citations,
        "accepted_facts": facts,
        "identifier_resolution": linkage,
    }


def _link_mcp_citations_to_search(
    mcp: dict[str, Any],
    search: dict[str, Any],
) -> dict[str, Any]:
    """Resolve IDs stated by the Data Agent through immutable Search receipts."""
    answer_ids = {
        token.casefold()
        for token in _answer_identifiers(str(mcp.get("answer") or ""))
    }
    if not answer_ids:
        return mcp
    search_citations = search.get("citations")
    if not isinstance(search_citations, list):
        return mcp

    linked: list[dict[str, Any]] = [
        citation
        for citation in mcp.get("citations", [])
        if isinstance(citation, dict)
    ]
    seen = {
        json.dumps(citation, sort_keys=True, separators=(",", ":"))
        for citation in linked
    }
    for citation in search_citations:
        if not isinstance(citation, dict):
            continue
        citation_ids = {
            str(value).casefold()
            for key in (
                "citation_id",
                "evidence_id",
                "source_file_id",
                "asset_version_id",
            )
            if (value := citation.get(key))
        }
        citation_ids.update(
            str(value).casefold()
            for value in citation.get("evidence_ids", [])
            if value
        )
        citation_ids.update(
            str(value).casefold()
            for value in citation.get("canonical_ids", [])
            if value
        )
        if not answer_ids.intersection(citation_ids):
            continue
        signature = json.dumps(
            citation,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature not in seen:
            linked.append(citation)
            seen.add(signature)

    if not linked:
        return mcp
    return {
        **mcp,
        "citations": linked,
        "citation_linkage": "explicit_answer_id_to_search",
    }


def _evidence_trace(
    *,
    citation_sources: list[tuple[str, list[dict[str, Any]]]],
    model_hash: str,
    data_hash: str,
) -> EvidenceTrace | None:
    locators: list[EvidenceLocator] = []
    evidence_ids: set[str] = set()
    source_ids: set[str] = set()
    for source_id, citations in citation_sources:
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            values = citation.get("evidence_ids")
            if isinstance(values, list):
                evidence_ids.update(str(value) for value in values if value)
            for key in ("evidence_id", "citation_id"):
                if citation.get(key):
                    evidence_ids.add(str(citation[key]))
            evidence_id = str(
                citation.get("evidence_id")
                or citation.get("citation_id")
                or ""
            )
            asset_version_id = str(
                citation.get("asset_version_id") or ""
            )
            blob_url = str(
                citation.get("blob_url")
                or citation.get("source_locator")
                or ""
            )
            if not evidence_id or not asset_version_id or not blob_url:
                continue
            try:
                locators.append(EvidenceLocator(
                    evidence_id=evidence_id,
                    asset_version_id=asset_version_id,
                    blob_url=blob_url,
                ))
            except ValueError:
                continue
            source_ids.add(source_id)
    if not evidence_ids or not locators:
        return None
    trace_payload = json.dumps(
        {
            "model_hash": model_hash,
            "data_hash": data_hash,
            "source_ids": sorted(source_ids),
            "evidence_ids": sorted(evidence_ids),
            "locators": [
                locator.model_dump(mode="json")
                for locator in sorted(
                    locators,
                    key=lambda item: (
                        item.evidence_id,
                        item.asset_version_id,
                        item.blob_url,
                    ),
                )
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return EvidenceTrace(
        trace_id=(
            "trace:"
            + hashlib.sha256(
                trace_payload.encode("utf-8")
            ).hexdigest()[:32]
        ),
        origin_status=QueryExecutionStatus.SUCCESS,
        source_ids=sorted(source_ids),
        model_hash=model_hash,
        data_hash=data_hash,
        evidence_ids=sorted(evidence_ids),
        locators=locators,
    )


def _semantic_signature(
    case: Any,
    *,
    source_selection: list[str],
    result_semantics: QueryExecutionStatus,
) -> dict[str, Any]:
    graph_probe = case.probes.direct_graph
    plan = case.semantic_plan or (
        graph_probe.semantic_plan if graph_probe is not None else None
    )
    return semantic_determinism_signature(
        source_selection=source_selection,
        intent=plan.intent if plan is not None else case.id,
        required_relationships=(
            plan.required_relationships
            if plan is not None
            else [
                relationship.semantic_id
                for relationship in case.expected.relationship_types
                if relationship.requirement == "required"
            ]
        ),
        optional_relationships=(
            plan.optional_relationships
            if plan is not None
            else [
                relationship.semantic_id
                for relationship in case.expected.relationship_types
                if relationship.requirement == "optional"
            ]
        ),
        requested_properties=(
            plan.requested_properties if plan is not None else []
        ),
        complexity_budget=(
            plan.budget.model_dump(mode="json")
            if plan is not None
            else {
                "max_hops": 4,
                "max_nodes": 6,
                "max_relationships": 5,
                "max_rows_per_subquery": 100,
                "max_subqueries": 4,
            }
        ),
        evidence_policy=(
            "required" if case.expected.evidence_required else "optional"
        ),
        result_semantics=result_semantics.value,
    ).model_dump(mode="json")


def _embedding_endpoint_from_environment() -> str | None:
    return (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT")
        or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    )


class RuntimeCollectionError(ValueError):
    """Raised when runtime collection configuration is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GraphRuntimeConfig(_StrictModel):
    workspace_id: str = Field(min_length=1)
    graph_model_id: str = Field(min_length=1)


class SearchRuntimeConfig(_StrictModel):
    endpoint: str = Field(min_length=1)
    mode: str = "direct_search"
    index_name: str | None = None
    knowledge_base_name: str | None = None
    knowledge_base_id: str | None = None
    embedding_endpoint: str | None = None
    embedding_deployment: str = "text-embedding-3-large"
    embedding_dimensions: int = Field(default=1536, ge=1)
    api_version: str = "2024-07-01"
    token_scope: str = "https://search.azure.com/.default"
    obo_token_scope: str | None = None

    @model_validator(mode="after")
    def _validate_mode(self) -> "SearchRuntimeConfig":
        if self.mode == "direct_search" and not self.index_name:
            raise ValueError("direct_search requires index_name.")
        if self.mode == "knowledge_base" and not self.knowledge_base_name:
            raise ValueError("knowledge_base requires knowledge_base_name.")
        if self.mode not in {"direct_search", "knowledge_base"}:
            raise ValueError(
                "mode must be direct_search or knowledge_base."
            )
        return self


class McpRuntimeConfig(_StrictModel):
    endpoint: str = Field(min_length=1)
    token_scope: str = "https://api.fabric.microsoft.com/.default"
    protocol_version: str = "2025-03-26"
    workspace_id: str = Field(min_length=1)
    data_agent_id: str = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: float = Field(default=0.25, ge=0)
    retry_jitter_seconds: float = Field(default=0.25, ge=0)
    request_timeout_seconds: int = Field(default=120, ge=1, le=600)


class DeploymentRuntimeConfig(_StrictModel):
    artifact_validation_status: str
    knowledge_http_status: int = 200
    partial_source: bool = False
    data_agent_published: bool
    compiled_instruction_hash: str = Field(min_length=1)
    deployed_instruction_hash: str = Field(min_length=1)
    unintended_duplicate_deployments: int = Field(default=0, ge=0)
    breaking_change: bool = False
    migration_approved: bool = False
    receipt_path: Path | None = None
    receipt_sha256: str | None = None
    semantic_contract_hash: str | None = None
    domain_contract_hash: str | None = None
    reasoning_policy_hash: str | None = None
    question_plans_hash: str | None = None
    query_authority_hash: str | None = None
    approved_max_hops: int | None = Field(default=None, ge=1, le=4)
    semantic_artifact_set_hash: str | None = None
    graph_artifact_set_hash: str | None = None
    search_artifact_set_hash: str | None = None
    semantic_model_manifest_hash: str | None = None
    ontology_persisted_projection_hash: str | None = None
    graph_persisted_projection_hash: str | None = None
    receipt_instruction_hash: str | None = None
    receipt_deployed_instruction_hash: str | None = None
    persisted_query_schema_hash: str | None = None
    competency_contract_hash: str | None = None
    package_hash: str | None = None
    graph_model_id: str | None = None
    search_index_name: str | None = None
    data_agent_id: str | None = None
    knowledge_base_id: str | None = None
    contract_hash_consistent: bool | None = None


class RuntimeConfig(_StrictModel):
    schema_version: str = "1.0"
    environment: str = Field(min_length=1)
    contract_hash: str = Field(min_length=1)
    deployment: DeploymentRuntimeConfig
    graph: GraphRuntimeConfig | None = None
    search: SearchRuntimeConfig | None = None
    data_agent_mcp: McpRuntimeConfig | None = None


def load_runtime_config(path: Path | str) -> RuntimeConfig:
    """Load a secret-free runtime endpoint and deployment receipt."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeCollectionError(
            f"Could not load runtime config {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeCollectionError(
            f"Runtime config must contain a JSON object: {source}"
        )
    try:
        _merge_deployment_receipt(payload, config_path=source)
        config = RuntimeConfig.model_validate(payload)
        _validate_deployment_linkage(config)
        return config
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeCollectionError(
            f"Could not load runtime config {source}: {exc}"
        ) from exc


def _merge_deployment_receipt(
    payload: dict[str, Any],
    *,
    config_path: Path,
) -> None:
    deployment = payload.get("deployment")
    if not isinstance(deployment, dict) or not deployment.get("receipt_path"):
        return
    receipt_path = Path(str(deployment["receipt_path"]))
    if not receipt_path.is_absolute():
        receipt_path = config_path.parent / receipt_path
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("Deployment receipt must contain a JSON object.")
    if receipt.get("schema") != "fabric-kg.semantic-deployment-receipt.v1":
        raise ValueError(
            "Unsupported deployment receipt schema: "
            f"{receipt.get('schema')!r}"
        )
    deployment.update(
        {
            "receipt_path": receipt_path,
            "receipt_sha256": "sha256:"
            + hashlib.sha256(receipt_bytes).hexdigest(),
            "semantic_contract_hash": receipt.get(
                "semantic_contract_hash"
            ),
            "domain_contract_hash": receipt.get("domain_contract_hash"),
            "reasoning_policy_hash": receipt.get("reasoning_policy_hash"),
            "question_plans_hash": receipt.get("question_plans_hash"),
            "query_authority_hash": receipt.get("query_authority_hash"),
            "approved_max_hops": receipt.get("approved_max_hops"),
            "semantic_artifact_set_hash": receipt.get(
                "semantic_artifact_set_hash"
            ),
            "graph_artifact_set_hash": receipt.get(
                "graph_artifact_set_hash"
            ),
            "search_artifact_set_hash": receipt.get(
                "search_artifact_set_hash"
            ),
            "semantic_model_manifest_hash": receipt.get(
                "semantic_model_manifest_hash"
            ),
            "ontology_persisted_projection_hash": receipt.get(
                "ontology_persisted_projection_hash"
            ),
            "graph_persisted_projection_hash": receipt.get(
                "graph_persisted_projection_hash"
            ),
            "receipt_instruction_hash": receipt.get("instruction_hash"),
            "receipt_deployed_instruction_hash": receipt.get(
                "deployed_instruction_hash"
            ),
            "persisted_query_schema_hash": receipt.get(
                "persisted_query_schema_hash"
            ),
            "competency_contract_hash": receipt.get(
                "competency_contract_hash"
            ),
            "package_hash": receipt.get("package_hash"),
            "graph_model_id": receipt.get("graph_model_id"),
            "search_index_name": receipt.get("search_index_name"),
            "data_agent_id": receipt.get("data_agent_id"),
            "data_agent_published": (
                receipt.get("data_agent_published") is True
            ),
            "knowledge_base_id": receipt.get("knowledge_base_id"),
            "contract_hash_consistent": receipt.get(
                "contract_hash_consistent"
            ),
        }
    )


def _validate_deployment_linkage(config: RuntimeConfig) -> None:
    deployment = config.deployment
    if deployment.receipt_path is None:
        return
    if deployment.contract_hash_consistent is not True:
        raise ValueError(
            "Deployment receipt does not prove consistent semantic hashes."
        )
    required_hashes = {
        "semantic_contract_hash": deployment.semantic_contract_hash,
        "semantic_artifact_set_hash": deployment.semantic_artifact_set_hash,
        "graph_artifact_set_hash": deployment.graph_artifact_set_hash,
        "search_artifact_set_hash": deployment.search_artifact_set_hash,
        "semantic_model_manifest_hash": (
            deployment.semantic_model_manifest_hash
        ),
        "ontology_persisted_projection_hash": (
            deployment.ontology_persisted_projection_hash
        ),
        "graph_persisted_projection_hash": (
            deployment.graph_persisted_projection_hash
        ),
        "receipt_instruction_hash": deployment.receipt_instruction_hash,
        "receipt_deployed_instruction_hash": (
            deployment.receipt_deployed_instruction_hash
        ),
        "persisted_query_schema_hash": (
            deployment.persisted_query_schema_hash
        ),
        "competency_contract_hash": deployment.competency_contract_hash,
        "package_hash": deployment.package_hash,
    }
    missing_hashes = [
        name for name, value in required_hashes.items() if not value
    ]
    if missing_hashes:
        raise ValueError(
            "Deployment receipt is missing required hashes: "
            + ", ".join(missing_hashes)
        )
    if deployment.competency_contract_hash != config.contract_hash:
        raise ValueError(
            "Competency contract hash does not match the deployment receipt."
        )
    if (
        config.graph
        and deployment.graph_model_id
        and config.graph.graph_model_id != deployment.graph_model_id
    ):
        raise ValueError(
            "Graph Model ID does not match the deployment receipt."
        )
    if (
        config.search
        and config.search.mode == "direct_search"
        and deployment.search_index_name
        and config.search.index_name != deployment.search_index_name
    ):
        raise ValueError(
            "Search index does not match the deployment receipt."
        )
    if (
        config.search
        and config.search.mode == "knowledge_base"
        and deployment.knowledge_base_id
        and config.search.knowledge_base_id
        != deployment.knowledge_base_id
    ):
        raise ValueError(
            "Knowledge Base ID does not match the deployment receipt."
        )
    if (
        config.data_agent_mcp
        and deployment.data_agent_id
        and config.data_agent_mcp.data_agent_id
        != deployment.data_agent_id
    ):
        raise ValueError(
            "Data Agent ID does not match the deployment receipt."
        )
    if config.data_agent_mcp:
        expected_endpoint = (
            "https://api.fabric.microsoft.com/v1/mcp/workspaces/"
            f"{config.data_agent_mcp.workspace_id}/dataagents/"
            f"{config.data_agent_mcp.data_agent_id}/agent"
        )
        if (
            config.data_agent_mcp.endpoint.rstrip("/").casefold()
            != expected_endpoint.casefold()
        ):
            raise ValueError(
                "Data Agent MCP endpoint does not match its configured "
                "workspace and Data Agent IDs."
            )
    if (
        deployment.receipt_instruction_hash
        and deployment.receipt_instruction_hash
        != deployment.compiled_instruction_hash
    ):
        raise ValueError(
            "Compiled instruction hash does not match the deployment receipt."
        )
    if (
        deployment.receipt_deployed_instruction_hash
        and deployment.receipt_deployed_instruction_hash
        != deployment.deployed_instruction_hash
    ):
        raise ValueError(
            "Deployed instruction hash does not match the deployment receipt."
        )
    if (
        deployment.receipt_instruction_hash
        != deployment.receipt_deployed_instruction_hash
    ):
        raise ValueError(
            "Compiled and deployed Data Agent instruction hashes differ."
        )


class RuntimeEvidenceCollector:
    """Execute required routes while preserving deterministic case order."""

    def __init__(
        self,
        *,
        contract: CompetencyContract,
        config: RuntimeConfig,
        graph_executor: FabricGraphExecutor | None = None,
        search_executor: SearchKnowledgeExecutor | None = None,
        mcp_executor: DataAgentMcpExecutor | None = None,
    ) -> None:
        expected_semantic_hash = (
            config.deployment.semantic_contract_hash
            or config.contract_hash
        )
        if contract.contract_hash != expected_semantic_hash:
            raise RuntimeCollectionError(
                "Runtime config and competency semantic contract hashes differ."
            )
        self._contract = contract
        self._config = config
        if (
            contract.query_schema is not None
            and config.deployment.persisted_query_schema_hash is not None
            and config.deployment.persisted_query_schema_hash
            != contract.query_schema.schema_hash
        ):
            raise RuntimeCollectionError(
                "Competency persisted query schema hash does not match the "
                "deployment receipt."
            )
        if (
            contract.query_schema is not None
            and config.deployment.semantic_model_manifest_hash is not None
            and config.deployment.semantic_model_manifest_hash
            != contract.query_schema.manifest_hash
        ):
            raise RuntimeCollectionError(
                "Competency query schema manifest does not match the "
                "deployment semantic model manifest."
            )
        if (
            contract.query_schema is not None
            and contract.query_schema.schema_mode == "schema2_bounded"
        ):
            authority = contract.query_schema.authority
            if authority is None:
                raise RuntimeCollectionError(
                    "Schema-2 runtime query authority is missing."
                )
            expected = {
                "domain_contract_hash": authority.domain_contract_hash,
                "reasoning_policy_hash": authority.reasoning_policy_hash,
                "question_plans_hash": authority.question_plans_hash,
                "query_authority_hash": authority.authority_hash,
                "approved_max_hops": authority.approved_max_hops,
            }
            mismatched = sorted(
                field_name
                for field_name, value in expected.items()
                if getattr(config.deployment, field_name) != value
            )
            if mismatched:
                raise RuntimeCollectionError(
                    "Deployment receipt bounded query authority differs from "
                    f"the competency contract: {mismatched}."
                )
        self._graph = graph_executor
        self._search = search_executor
        self._mcp = mcp_executor

    def collect(self) -> dict[str, Any]:
        """Collect route receipts without retaining source passages."""
        cases = []
        diagnostic_records: list[dict[str, Any]] = []
        collection_run_id = f"runtime-collection:{uuid4()}"
        for case in self._contract.cases:
            observed: dict[str, Any] = {}
            observed["direct_graph"] = self._execute_or_unavailable(
                executor=self._graph,
                case=case,
                requirement=case.routes.direct_graph,
                route="direct_graph",
            )
            observed["search"] = self._execute_or_unavailable(
                executor=self._search,
                case=case,
                requirement=case.routes.search,
                route="search",
            )
            observed["data_agent_mcp"] = self._execute_or_unavailable(
                executor=self._mcp,
                case=case,
                requirement=case.routes.data_agent_mcp,
                route="data_agent_mcp",
            )
            if _needs_mcp_grounding_retry(
                case=case,
                graph=observed["direct_graph"],
                mcp=observed["data_agent_mcp"],
            ):
                relationship_ids = ", ".join(
                    relationship.semantic_id
                    for relationship in case.expected.relationship_types
                    if relationship.requirement == "required"
                )
                retry_case = case.model_copy(
                    update={
                        "question": (
                            f"{case.question}\n\n"
                            "The independently validated semantic projection "
                            "contains rows for the required relationship(s): "
                            f"{relationship_ids}. Query the selected Lakehouse "
                            "fallback tables before answering. Return up to 100 "
                            "explicit relationship rows, each with source "
                            "entity_id, target entity_id, and evidence_id. Do "
                            "not report no data when those persisted rows exist."
                        )
                    }
                )
                retried_mcp = self._execute_or_unavailable(
                    executor=self._mcp,
                    case=retry_case,
                    requirement=case.routes.data_agent_mcp,
                    route="data_agent_mcp",
                )
                observed["data_agent_mcp"] = _merge_mcp_grounding_retry(
                    observed["data_agent_mcp"],
                    retried_mcp,
                )
            identifiers = _graph_grounding_identifiers(
                observed["direct_graph"]
            )
            identifiers.update(
                _answer_identifiers(
                    str(
                        observed["data_agent_mcp"].get("answer")
                        or ""
                    )
                )
            )
            resolver = getattr(
                self._search,
                "resolve_identifiers",
                None,
            )
            if identifiers and callable(resolver):
                observed["search"] = _merge_search_resolution(
                    observed["search"],
                    resolver(case, identifiers),
                )
            observed["data_agent_mcp"] = _link_mcp_citations_to_search(
                observed["data_agent_mcp"],
                observed["search"],
            )
            observed["knowledge_base"] = self._knowledge_base_observation(
                observed["search"],
                requirement=case.routes.knowledge_base,
            )
            observed["data_agent_ui"] = self._unsupported_observation(
                requirement=case.routes.data_agent_ui,
                route="data_agent_ui",
            )
            observed["foundry_agent"] = self._unsupported_observation(
                requirement=case.routes.foundry_agent,
                route="foundry_agent",
            )
            graph = observed["direct_graph"]
            search = observed["search"]
            mcp = observed["data_agent_mcp"]
            graph_ids = set(graph.get("canonical_ids", []))
            search_ids = set(search.get("canonical_ids", []))
            if case.routes.composed == "not_expected":
                observed["composed"] = {"status": "not_expected"}
            else:
                route_requirements = {
                    "direct_graph": case.routes.direct_graph,
                    "search": case.routes.search,
                    "data_agent_mcp": case.routes.data_agent_mcp,
                }
                requirements = [
                    SourceRequirement(
                        source_id=route,
                        requirement=requirement,
                    )
                    for route, requirement in route_requirements.items()
                    if requirement != "not_expected"
                ]
                route_observations = {
                    "direct_graph": graph,
                    "search": search,
                    "data_agent_mcp": mcp,
                }
                outcomes = [
                    SourceExecutionOutcome(
                        source_id=route,
                        status=_route_result_category(
                            route_observations[route]
                        ),
                        unsupported_portion=(
                            str(
                                route_observations[route].get(
                                    "error_message"
                                )
                                or route_observations[route].get(
                                    "remediation"
                                )
                                or ""
                            )
                            or None
                        ),
                    )
                    for route in route_requirements
                    if route_requirements[route] != "not_expected"
                ]
                answer = str(mcp.get("answer") or "").strip()
                resolution = resolve_required_source_status(
                    requirements,
                    outcomes,
                    answer_is_fact_bearing=bool(answer),
                )
                model_hash = (
                    self._config.deployment.semantic_artifact_set_hash
                    or self._contract.contract_hash
                )
                data_hash = (
                    self._config.deployment.search_artifact_set_hash
                    or self._config.deployment.package_hash
                    or self._contract.contract_hash
                )
                successful_citation_sources: list[
                    tuple[str, list[dict[str, Any]]]
                ] = []
                for source_id, observation in (
                    ("data_agent_mcp", mcp),
                    ("search", search),
                ):
                    if (
                        _route_result_category(observation)
                        in SEMANTICALLY_SUCCESSFUL_STATUSES
                    ):
                        successful_citation_sources.append(
                            (
                                source_id,
                                list(observation.get("citations", [])),
                            )
                        )
                trace = _evidence_trace(
                    citation_sources=successful_citation_sources,
                    model_hash=model_hash,
                    data_hash=data_hash,
                )
                grounding_error: str | None = None
                try:
                    trace = validate_grounded_answer(
                        fact_bearing=bool(answer),
                        current_model_hash=model_hash,
                        current_data_hash=data_hash,
                        trace=trace,
                    )
                except GroundedAnswerViolationError as exc:
                    grounding_error = str(exc)
                mcp_category = _route_result_category(mcp)
                answer_blocked = (
                    resolution.blocked
                    or bool(grounding_error)
                    or (
                        bool(answer)
                        and mcp_category
                        not in SEMANTICALLY_SUCCESSFUL_STATUSES
                    )
                )
                final_category = resolution.status
                if grounding_error and final_category in {
                    QueryExecutionStatus.SUCCESS,
                    QueryExecutionStatus.OPTIONAL_DATA_ABSENT,
                }:
                    final_category = QueryExecutionStatus.PARTIAL_RESULT
                composed_status = (
                    "success"
                    if final_category in {
                        QueryExecutionStatus.SUCCESS,
                        QueryExecutionStatus.OPTIONAL_DATA_ABSENT,
                        QueryExecutionStatus.NO_MATCH,
                    }
                    else (
                        "partial"
                        if final_category
                        == QueryExecutionStatus.PARTIAL_RESULT
                        else "failed"
                    )
                )
                unsupported_portions = [
                    portion.model_dump(mode="json")
                    for portion in resolution.unsupported_portions
                ]
                if grounding_error:
                    unsupported_portions.append({
                        "source_id": "evidence",
                        "reason": grounding_error,
                    })
                observed["composed"] = {
                    "status": composed_status,
                    "result_category": final_category.value,
                    "final_semantic_status": final_category.value,
                    "graph_used": (
                        graph.get("status") == "success"
                        and int(graph.get("row_count") or 0) > 0
                    ),
                    "search_used": (
                        search.get("status") == "success"
                        and bool(search.get("citations"))
                    ),
                    "contradiction": bool(
                        graph_ids
                        and search_ids
                        and not graph_ids & search_ids
                    ),
                    "source_failures_disclosed": True,
                    "answer": None if answer_blocked else answer,
                    "unsupported_answer_blocked": answer_blocked,
                    "unsupported_portion": unsupported_portions,
                    "evidence_trace": (
                        trace.model_dump(mode="json")
                        if trace is not None
                        else None
                    ),
                    "semantic_determinism_signature": _semantic_signature(
                        case,
                        source_selection=[
                            route
                            for route, requirement in (
                                route_requirements.items()
                            )
                            if requirement != "not_expected"
                        ],
                        result_semantics=final_category,
                    ),
                    "request_ids": list(
                        dict.fromkeys(
                            [
                                *graph.get("request_ids", []),
                                *search.get("request_ids", []),
                                *mcp.get("request_ids", []),
                            ]
                        )
                    ),
                    "retry_request_ids": mcp.get(
                        "retry_request_ids", []
                    ),
                    "retry_correlation_ids": mcp.get(
                        "retry_correlation_ids", []
                    ),
                    "retry_count": mcp.get("retry_count", 0),
                    "first_failure": mcp.get("first_failure"),
                    "idempotency_key": mcp.get("idempotency_key"),
                    "timestamp_utc": _timestamp_utc(),
                }
                if composed_status != "success":
                    observed["composed"]["remediation"] = (
                        "Resolve failed required sources or evidence gaps, "
                        "then rerun the composed competency case."
                    )
            observed["accepted_facts"] = search.get("accepted_facts", [])
            observed["accepted_relationships"] = graph.get(
                "accepted_relationships", []
            )
            cases.append(
                {
                    "id": case.id,
                    "question_id": case.id,
                    "expected": case.expected.model_dump(mode="json"),
                    "routes": case.routes.model_dump(mode="json"),
                    "observed": observed,
                }
            )
            if (
                self._contract.query_schema is not None
                and self._config.deployment.receipt_path is not None
            ):
                diagnostic_records.append(
                    self._build_diagnostic_record(
                        case=case,
                        observed=observed,
                        collection_run_id=collection_run_id,
                    )
                )
        deployment = self._config.deployment.model_dump(mode="json")
        deployment.pop("receipt_path", None)
        evidence = {
            "schema": "fabric-kg.runtime-evidence.v1",
            "contract_hash": self._config.contract_hash,
            "semantic_contract_hash": self._contract.contract_hash,
            "environment": self._config.environment,
            "deployment": deployment,
            "runtime_targets": {
                "graph_model_id": (
                    self._config.graph.graph_model_id
                    if self._config.graph
                    else None
                ),
                "search_index_name": (
                    self._config.search.index_name
                    if self._config.search
                    else None
                ),
                "search_mode": (
                    self._config.search.mode
                    if self._config.search
                    else None
                ),
                "knowledge_base_id": (
                    self._config.search.knowledge_base_id
                    if self._config.search
                    else None
                ),
                "data_agent_id": (
                    self._config.data_agent_mcp.data_agent_id
                    if self._config.data_agent_mcp
                    else None
                ),
                "data_agent_workspace_id": (
                    self._config.data_agent_mcp.workspace_id
                    if self._config.data_agent_mcp
                    else None
                ),
                "data_agent_mcp_endpoint_sha256": (
                    "sha256:"
                    + hashlib.sha256(
                        self._config.data_agent_mcp.endpoint.rstrip("/")
                        .casefold()
                        .encode("utf-8")
                    ).hexdigest()
                    if self._config.data_agent_mcp
                    else None
                ),
            },
            "cases": cases,
        }
        if diagnostic_records:
            evidence["diagnostic_records"] = diagnostic_records
            evidence["diagnostic_record"] = diagnostic_records[0]
            evidence["diagnostic_reference_watermark"] = _timestamp_utc()
        return evidence

    def _build_diagnostic_record(
        self,
        *,
        case: Any,
        observed: dict[str, Any],
        collection_run_id: str,
    ) -> dict[str, Any]:
        """Seal one complete, source-content-free runtime diagnostic."""
        query_schema = self._contract.query_schema
        if query_schema is None:
            raise RuntimeCollectionError(
                "A persisted query schema is required for diagnostics."
            )
        graph_probe = case.probes.direct_graph
        semantic_plan = case.semantic_plan or (
            graph_probe.semantic_plan if graph_probe is not None else None
        )
        if semantic_plan is None:
            raise RuntimeCollectionError(
                f"Competency case {case.id!r} has no semantic plan."
            )

        deployment = self._config.deployment
        required_hashes = {
            "semantic_contract_hash": (
                deployment.semantic_contract_hash
                or self._contract.contract_hash
            ),
            "semantic_model_manifest_hash": (
                deployment.semantic_model_manifest_hash
            ),
            "ontology_persisted_projection_hash": (
                deployment.ontology_persisted_projection_hash
            ),
            "graph_persisted_projection_hash": (
                deployment.graph_persisted_projection_hash
            ),
            "search_artifact_set_hash": (
                deployment.search_artifact_set_hash
            ),
            "instruction_hash": (
                deployment.receipt_deployed_instruction_hash
                or deployment.deployed_instruction_hash
            ),
            "persisted_query_schema_hash": (
                deployment.persisted_query_schema_hash
            ),
        }
        missing_hashes = sorted(
            name for name, value in required_hashes.items() if not value
        )
        if missing_hashes:
            raise RuntimeCollectionError(
                "Deployment receipt cannot produce a complete diagnostic; "
                f"missing {missing_hashes}."
            )
        if (
            required_hashes["semantic_model_manifest_hash"]
            != query_schema.manifest_hash
        ):
            raise RuntimeCollectionError(
                "Deployment semantic model manifest hash does not match the "
                "competency query schema manifest."
            )

        route_requirements = {
            "direct_graph": case.routes.direct_graph,
            "search": case.routes.search,
            "data_agent_mcp": case.routes.data_agent_mcp,
        }
        selected_sources = [
            route
            for route, requirement in route_requirements.items()
            if requirement != "not_expected"
        ]
        source_selection_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    selected_sources,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
        graph = observed["direct_graph"]
        search = observed["search"]
        mcp = observed["data_agent_mcp"]
        composed = observed.get("composed", {})
        result_category = _route_result_category(
            composed if composed.get("result_category") else graph
        )
        physical_query_hash = (
            graph.get("physical_query_hash")
            or compute_physical_query_hash(
                json.dumps(
                    {
                        "case_id": case.id,
                        "selected_sources": selected_sources,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        )
        request_ids = list(dict.fromkeys([
            *graph.get("request_ids", []),
            *search.get("request_ids", []),
            *mcp.get("request_ids", []),
        ]))
        retry_correlation_ids = list(
            mcp.get("retry_correlation_ids", [])
        )
        operation_id = (
            next(
                iter(reversed(mcp.get("client_request_ids", []))),
                None,
            )
            or next(iter(reversed(request_ids)), None)
            or f"operation:{uuid4()}"
        )
        request_id = request_ids[0] if request_ids else operation_id
        correlation_id = (
            retry_correlation_ids[-1]
            if retry_correlation_ids
            else (request_ids[1] if len(request_ids) > 1 else request_id)
        )
        evidence_ids: set[str] = set()
        trace = composed.get("evidence_trace")
        if isinstance(trace, dict):
            evidence_ids.update(
                str(value)
                for value in trace.get("evidence_ids", [])
                if value
            )
        for item in [
            *observed.get("accepted_facts", []),
            *observed.get("accepted_relationships", []),
        ]:
            if isinstance(item, dict):
                evidence_ids.update(
                    str(value)
                    for value in item.get("evidence_ids", [])
                    if value
                )
        row_count = (
            int(graph.get("row_count") or 0)
            + int(search.get("result_count") or 0)
            + (
                1
                if str(mcp.get("answer") or "").strip()
                and _route_result_category(mcp)
                in SEMANTICALLY_SUCCESSFUL_STATUSES
                else 0
            )
        )
        failure_categories = {
            QueryExecutionStatus.INVALID_SEMANTIC_PLAN,
            QueryExecutionStatus.INVALID_PHYSICAL_QUERY,
            QueryExecutionStatus.AUTHORIZATION_FAILURE,
            QueryExecutionStatus.PLATFORM_FAILURE,
            QueryExecutionStatus.TIMEOUT,
            QueryExecutionStatus.CONCURRENCY_CONFLICT,
        }
        workspace_id = (
            self._config.data_agent_mcp.workspace_id
            if self._config.data_agent_mcp
            else (
                self._config.graph.workspace_id
                if self._config.graph
                else ""
            )
        )
        target_item_id = (
            self._config.data_agent_mcp.data_agent_id
            if self._config.data_agent_mcp
            else (
                self._config.graph.graph_model_id
                if self._config.graph
                else ""
            )
        )
        try:
            record = SemanticDiagnosticRecord(
                schema_mode=query_schema.schema_mode,
                export_freshness_watermark=_timestamp_utc(),
                partial_snapshot=False,
                overlapping_snapshot=False,
                workspace_id=workspace_id,
                target_item_id=target_item_id,
                semantic_contract_hash=str(
                    required_hashes["semantic_contract_hash"]
                ),
                domain_contract_hash=(
                    query_schema.authority.domain_contract_hash
                    if query_schema.authority is not None
                    else ""
                ),
                query_authority_hash=(
                    query_schema.authority.authority_hash
                    if query_schema.authority is not None
                    else ""
                ),
                manifest_hash=str(
                    required_hashes["semantic_model_manifest_hash"]
                ),
                ontology_projection_hash=str(
                    required_hashes["ontology_persisted_projection_hash"]
                ),
                graph_projection_hash=str(
                    required_hashes["graph_persisted_projection_hash"]
                ),
                search_projection_hash=str(
                    required_hashes["search_artifact_set_hash"]
                ),
                instruction_hash=str(
                    required_hashes["instruction_hash"]
                ),
                source_selection_hash=source_selection_hash,
                query_schema_hash=str(
                    required_hashes["persisted_query_schema_hash"]
                ),
                route=(
                    "direct_graph"
                    if graph.get("status") != "not_expected"
                    else "composed"
                ),
                selected_source=",".join(selected_sources),
                semantic_plan=(
                    None
                    if query_schema.schema_mode == "schema2_bounded"
                    else semantic_plan
                ),
                semantic_plan_hash=compute_query_plan_hash(semantic_plan),
                actual_hop_count=int(
                    graph.get("actual_hop_count") or 0
                ),
                physical_query_hash=str(physical_query_hash),
                static_validation_passed=bool(
                    graph.get("static_validation_passed", True)
                ),
                query_row_count=row_count,
                result_category=result_category,
                error_category=(
                    result_category.value
                    if result_category in failure_categories
                    else None
                ),
                request_id=str(request_id),
                correlation_id=str(correlation_id),
                thread_id=str(
                    mcp.get("idempotency_key")
                    or f"{collection_run_id}:{case.id}"
                ),
                run_id=collection_run_id,
                operation_id=str(operation_id),
                latency_ms=sum(
                    float(route.get("latency_ms") or 0.0)
                    for route in (graph, search, mcp)
                ),
                retry_count=int(mcp.get("retry_count") or 0),
                evidence_ids=sorted(evidence_ids),
                final_semantic_status=result_category,
                notes=[
                    "Complete runtime envelope generated locally from "
                    "redacted route receipts."
                ],
            )
        except ValueError as exc:
            raise RuntimeCollectionError(
                f"Could not seal diagnostic for case {case.id!r}: {exc}"
            ) from exc
        return record.model_dump(mode="json")

    @staticmethod
    def _execute_or_unavailable(
        *,
        executor: Any,
        case: Any,
        requirement: str,
        route: str,
    ) -> dict[str, Any]:
        if requirement == "not_expected":
            return {"status": "not_expected"}
        if executor is None:
            return {
                "status": (
                    "capability_unavailable"
                    if requirement == "optional"
                    else "failed"
                ),
                "result_category": (
                    QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                    if requirement == "optional"
                    else QueryExecutionStatus.PLATFORM_FAILURE.value
                ),
                "final_semantic_status": (
                    QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                    if requirement == "optional"
                    else QueryExecutionStatus.PLATFORM_FAILURE.value
                ),
                "request_ids": [],
                "remediation": f"Configure the {route} runtime executor.",
            }
        return executor.execute(case)

    def _knowledge_base_observation(
        self,
        search_observation: dict[str, Any],
        *,
        requirement: str,
    ) -> dict[str, Any]:
        if requirement == "not_expected":
            return {"status": "not_expected"}
        if self._config.search and self._config.search.mode == "knowledge_base":
            return dict(search_observation)
        return {
            "status": (
                "capability_unavailable"
                if requirement == "optional"
                else "failed"
            ),
            "result_category": (
                QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                if requirement == "optional"
                else QueryExecutionStatus.PLATFORM_FAILURE.value
            ),
            "final_semantic_status": (
                QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                if requirement == "optional"
                else QueryExecutionStatus.PLATFORM_FAILURE.value
            ),
            "request_ids": [],
            "remediation": (
                "Configure runtime search.mode as knowledge_base for the "
                "knowledge_base route."
            ),
        }

    @staticmethod
    def _unsupported_observation(
        *,
        requirement: str,
        route: str,
    ) -> dict[str, Any]:
        if requirement == "not_expected":
            return {"status": "not_expected"}
        return {
            "status": (
                "capability_unavailable"
                if requirement == "optional"
                else "failed"
            ),
            "result_category": (
                QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                if requirement == "optional"
                else QueryExecutionStatus.PLATFORM_FAILURE.value
            ),
            "final_semantic_status": (
                QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                if requirement == "optional"
                else QueryExecutionStatus.PLATFORM_FAILURE.value
            ),
            "request_ids": [],
            "remediation": (
                f"The {route} route has no runtime executor in this release."
            ),
        }


def build_live_collector(
    *,
    contract: CompetencyContract,
    config: RuntimeConfig,
    credential: Any | None = None,
    graph_transport: Any | None = None,
    search_transport: HttpTransport | None = None,
    mcp_transport: HttpTransport | None = None,
) -> RuntimeEvidenceCollector:
    """Create live executors using DefaultAzureCredential token audiences."""
    if config.deployment.receipt_path is None:
        raise RuntimeCollectionError(
            "Live runtime collection requires deployment.receipt_path so "
            "target IDs and deployed instruction hashes are independently "
            "verified."
        )
    cred = credential or DefaultAzureCredential()

    def token_provider(scope: str) -> Callable[[], str]:
        return lambda: cred.get_token(scope).token

    graph_executor = None
    if config.graph:
        graph_executor = FabricGraphExecutor(
            workspace_id=config.graph.workspace_id,
            graph_model_id=config.graph.graph_model_id,
            query_schema=contract.query_schema,
            client=GraphModelGQLClient(
                token_provider=token_provider(
                    "https://api.fabric.microsoft.com/.default"
                ),
                transport=graph_transport,
            ),
        )
    search_executor = None
    if config.search:
        query_vectorizer = None
        embedding_endpoint = (
            config.search.embedding_endpoint
            or _embedding_endpoint_from_environment()
        )
        if embedding_endpoint:
            from fabric_kg_builder.search.embeddings import attach_vectors

            embedding_deployment = (
                config.search.embedding_deployment
                or os.environ.get("AZURE_AI_EMBEDDING_DEPLOYMENT")
                or os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
                or "text-embedding-3-large"
            )

            def query_vectorizer(text: str) -> list[float]:
                document: dict[str, Any] = {"content": text}
                attach_vectors(
                    [document],
                    text_field="content",
                    vector_field="query_vector",
                    endpoint=embedding_endpoint,
                    deployment=embedding_deployment,
                    dimensions=config.search.embedding_dimensions,
                    batch_size=1,
                )
                return list(document["query_vector"])

        search_executor = SearchKnowledgeExecutor(
            endpoint=config.search.endpoint,
            mode=config.search.mode,
            index_name=config.search.index_name,
            knowledge_base_name=config.search.knowledge_base_name,
            api_version=config.search.api_version,
            token_provider=token_provider(config.search.token_scope),
            obo_token_provider=(
                token_provider(config.search.obo_token_scope)
                if config.search.obo_token_scope
                else None
            ),
            transport=search_transport,
            query_vectorizer=query_vectorizer,
        )
    mcp_executor = None
    if config.data_agent_mcp:
        mcp_executor = DataAgentMcpExecutor(
            endpoint=config.data_agent_mcp.endpoint,
            token_provider=token_provider(
                config.data_agent_mcp.token_scope
            ),
            protocol_version=config.data_agent_mcp.protocol_version,
            transport=mcp_transport,
            max_attempts=config.data_agent_mcp.max_attempts,
            retry_base_delay_seconds=(
                config.data_agent_mcp.retry_base_delay_seconds
            ),
            retry_jitter_seconds=(
                config.data_agent_mcp.retry_jitter_seconds
            ),
            request_timeout_seconds=(
                config.data_agent_mcp.request_timeout_seconds
            ),
        )
    return RuntimeEvidenceCollector(
        contract=contract,
        config=config,
        graph_executor=graph_executor,
        search_executor=search_executor,
        mcp_executor=mcp_executor,
    )
