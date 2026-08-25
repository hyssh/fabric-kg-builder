"""Deterministic Graph, Search, and Data Agent runtime acceptance."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


class RuntimeAcceptanceError(ValueError):
    """Raised when a runtime evidence receipt is malformed."""


_TECHNICAL_ERROR_SIGNALS = (
    "technical error",
    "technical issue",
    "something went wrong",
    "encountered an issue",
    "there was an error",
    "request failed",
    "try again later",
    "could not complete",
    "couldn't complete",
    "unable to process your request",
    "an unexpected error occurred",
)

_THRESHOLDS = {
    "direct_graph_score": 0.80,
    "search_score": 0.80,
    "knowledge_base_score": 0.80,
    "canonical_id_linkage": 0.95,
    "composed_score": 0.80,
    "accepted_fact_evidence_coverage": 1.00,
    "accepted_relationship_evidence_coverage": 1.00,
    "citation_resolution": 1.00,
    "data_agent_citation_resolution": 1.00,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return empty if denominator == 0 else numerator / denominator


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _string_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for value in values:
        if isinstance(value, str) and value:
            result.add(value)
        elif isinstance(value, dict):
            semantic_id = (
                value.get("semantic_id")
                or value.get("id")
                or value.get("name")
            )
            if semantic_id:
                result.add(str(semantic_id))
    return result


def _required_relationships(expected: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(expected, list):
        return {}
    relationships: dict[str, dict[str, Any]] = {}
    for value in expected:
        if isinstance(value, str):
            relationships[value] = {
                "semantic_id": value,
                "requirement": "required",
            }
            continue
        if not isinstance(value, dict):
            continue
        semantic_id = value.get("semantic_id") or value.get("id") or value.get(
            "name"
        )
        if not semantic_id:
            continue
        if value.get("requirement", "required") == "not_expected":
            continue
        relationships[str(semantic_id)] = value
    return relationships


def _observed_relationships(observed: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(observed, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for value in observed:
        if isinstance(value, str):
            result[value] = {"semantic_id": value}
        elif isinstance(value, dict):
            semantic_id = (
                value.get("semantic_id")
                or value.get("id")
                or value.get("name")
            )
            if semantic_id:
                result[str(semantic_id)] = value
    return result


def _route_required(case: dict[str, Any], route: str) -> bool:
    return _route_requirement(case, route) == "required"


def _route_requirement(case: dict[str, Any], route: str) -> str:
    routes = case.get("routes")
    if isinstance(routes, dict):
        value = routes.get(route)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("requirement", "required"))
    return (
        "required"
        if route in {
            "direct_graph",
            "search",
            "data_agent_mcp",
            "composed",
        }
        else "optional"
    )


def _route_is_evaluated(
    case: dict[str, Any],
    route_name: str,
    route: dict[str, Any],
) -> bool:
    requirement = _route_requirement(case, route_name)
    if requirement == "not_expected":
        return False
    return not (
        requirement == "optional"
        and str(route.get("status") or "").lower()
        in {
            "",
            "capability_unavailable",
            "capability_gated",
            "not_expected",
        }
    )


def _successful(route: dict[str, Any]) -> bool:
    transport_success = str(route.get("status") or "").lower() in {
        "success",
        "succeeded",
        "passed",
        "complete",
        "completed",
    }
    result_category = str(route.get("result_category") or "").lower()
    semantic_success = result_category in {
        "",
        "success",
        "optional_data_absent",
    }
    return transport_success and semantic_success


def _request_ids(route: dict[str, Any]) -> list[str]:
    values = route.get("request_ids")
    if isinstance(values, list):
        return [str(value) for value in values if value]
    value = route.get("request_id") or route.get("correlation_id")
    return [str(value)] if value else []


def _citation_blob_url(citation: dict[str, Any]) -> str | None:
    candidates = [
        citation.get("blob_url"),
        citation.get("source_locator"),
    ]
    for candidate in candidates:
        value = candidate
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                value = stripped
            else:
                value = parsed
        if isinstance(value, dict):
            value = next(
                (
                    value.get(key)
                    for key in (
                        "blob_uri",
                        "blob_url",
                        "blobUrl",
                        "landing_uri",
                        "landingUri",
                    )
                    if value.get(key)
                ),
                None,
            )
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _immutable_citation_identity(
    citation: dict[str, Any],
) -> tuple[str, str, str] | None:
    evidence_id = str(citation.get("evidence_id") or "").strip()
    asset_version_id = str(
        citation.get("asset_version_id") or ""
    ).strip()
    blob_url = _citation_blob_url(citation)
    if not evidence_id or not asset_version_id or not blob_url:
        return None
    parsed = urlparse(blob_url)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or not parsed.hostname.casefold().endswith(
            ".blob.core.windows.net"
        )
    ):
        return None
    normalized_path = unquote(parsed.path).replace("\\", "/").casefold()
    version_segment = f"/versions/{asset_version_id}/".casefold()
    if version_segment not in normalized_path:
        return None
    normalized_url = (
        f"https://{parsed.hostname.casefold()}"
        f"{normalized_path.rstrip('/')}"
    )
    return evidence_id, asset_version_id, normalized_url


def load_runtime_evidence(path: Path | str) -> dict[str, Any]:
    """Load and minimally validate a redacted runtime evidence receipt."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeAcceptanceError(
            f"Could not load runtime evidence {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeAcceptanceError("Runtime evidence must be a JSON object.")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeAcceptanceError(
            "Runtime evidence requires at least one competency case."
        )
    for ordinal, case in enumerate(cases):
        if not isinstance(case, dict) or not case.get("id"):
            raise RuntimeAcceptanceError(
                f"Runtime evidence case {ordinal} requires an id."
            )
        if not isinstance(case.get("observed"), dict):
            raise RuntimeAcceptanceError(
                f"Runtime evidence case {case.get('id')} requires observed routes."
            )
    return payload


def is_technical_error_answer(answer: object) -> bool:
    """Return True for transport-success responses that are semantic failures."""
    normalized = " ".join(str(answer or "").casefold().split())
    return bool(normalized) and any(
        signal in normalized for signal in _TECHNICAL_ERROR_SIGNALS
    )


def _score_case(case: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    observed = case["observed"]
    graph = observed.get("direct_graph")
    search = observed.get("search")
    knowledge_base = observed.get("knowledge_base")
    mcp = observed.get("data_agent_mcp")
    composed = observed.get("composed")
    graph = graph if isinstance(graph, dict) else {}
    search = search if isinstance(search, dict) else {}
    knowledge_base = (
        knowledge_base if isinstance(knowledge_base, dict) else {}
    )
    mcp = mcp if isinstance(mcp, dict) else {}
    composed = composed if isinstance(composed, dict) else {}
    graph_evaluated = _route_is_evaluated(case, "direct_graph", graph)
    search_evaluated = _route_is_evaluated(case, "search", search)
    knowledge_base_evaluated = _route_is_evaluated(
        case,
        "knowledge_base",
        knowledge_base,
    )
    mcp_evaluated = _route_is_evaluated(case, "data_agent_mcp", mcp)
    composed_evaluated = _route_is_evaluated(
        case,
        "composed",
        composed,
    )
    mcp_answer = str(mcp.get("answer") or "")
    answer_concepts = _string_set(expected.get("answer_concepts"))
    normalized_answer = mcp_answer.casefold()
    answer_relevance = _ratio(
        sum(
            1
            for concept in answer_concepts
            if concept.casefold() in normalized_answer
        ),
        len(answer_concepts),
        empty=0.0
        if _route_required(case, "data_agent_mcp")
        else 1.0,
    )
    if not mcp_evaluated:
        answer_relevance = 1.0

    expected_entities = _string_set(expected.get("entity_types"))
    observed_entities = _string_set(graph.get("entity_types"))
    entity_coverage = _ratio(
        len(expected_entities & observed_entities),
        len(expected_entities),
    )

    expected_relationships = _required_relationships(
        expected.get("relationship_types")
    )
    observed_relationships = _observed_relationships(
        graph.get("relationships")
    )
    required_relationships = {
        semantic_id: value
        for semantic_id, value in expected_relationships.items()
        if value.get("requirement", "required") == "required"
    }
    relationship_coverage = _ratio(
        len(set(required_relationships) & set(observed_relationships)),
        len(required_relationships),
    )
    direction_checks: list[bool] = []
    for semantic_id, relationship in required_relationships.items():
        observed_relationship = observed_relationships.get(semantic_id)
        if observed_relationship is None:
            direction_checks.append(False)
            continue
        expected_direction = relationship.get("direction")
        if expected_direction:
            direction_checks.append(
                observed_relationship.get("direction") == expected_direction
            )
    direction_correctness = _ratio(
        sum(direction_checks),
        len(direction_checks),
    )
    if not graph_evaluated:
        entity_coverage = 1.0
        relationship_coverage = 1.0
        direction_correctness = 1.0
    graph_reliability = (
        1.0 if not graph_evaluated or _successful(graph) else 0.0
    )
    graph_score = (
        _mean(
            [
                graph_reliability,
                entity_coverage,
                relationship_coverage,
                direction_correctness,
            ]
        )
        if graph_evaluated
        else 1.0
    )

    partial_source = bool(search.get("partial_source")) or int(
        search.get("http_status") or 0
    ) == 206
    search_reliability = (
        1.0
        if not search_evaluated
        or (_successful(search) and not partial_source)
        else 0.0
    )
    citations = search.get("citations")
    citations = citations if isinstance(citations, list) else []
    trusted_search_citations: set[tuple[str, str, str]] = set()
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        identity = _immutable_citation_identity(citation)
        if identity is not None:
            trusted_search_citations.add(identity)
    resolved_evidence_ids = {
        identity[0] for identity in trusted_search_citations
    }
    citation_resolution = _ratio(
        sum(
            1
            for citation in citations
            if isinstance(citation, dict)
            and _immutable_citation_identity(citation) is not None
        ),
        len(citations),
        empty=0.0 if expected.get("evidence_required") else 1.0,
    )
    evidence_support = (
        1.0
        if not expected.get("evidence_required")
        else (1.0 if citations else 0.0)
    )
    search_score = (
        _mean(
            [search_reliability, evidence_support, citation_resolution]
        )
        if search_evaluated
        else 1.0
    )
    knowledge_base_score = (
        search_score
        if knowledge_base_evaluated
        and knowledge_base == search
        else (
            1.0
            if not knowledge_base_evaluated
            else (1.0 if _successful(knowledge_base) else 0.0)
        )
    )

    graph_ids = _string_set(graph.get("canonical_ids"))
    search_ids = (
        _string_set(search.get("canonical_ids"))
        if trusted_search_citations
        else set()
    )
    canonical_linkage = _ratio(
        len(graph_ids & search_ids),
        len(graph_ids),
        empty=0.0
        if _route_required(case, "direct_graph")
        and _route_required(case, "search")
        else 1.0,
    )

    source_failure = not _successful(graph) or not _successful(search)
    accepted_facts = observed.get("accepted_facts")
    accepted_facts = accepted_facts if isinstance(accepted_facts, list) else []
    accepted_relationships = observed.get("accepted_relationships")
    accepted_relationships = (
        accepted_relationships
        if isinstance(accepted_relationships, list)
        else []
    )
    fact_evidence_coverage = _ratio(
        sum(
            1
            for fact in accepted_facts
            if isinstance(fact, dict)
            and any(
                str(evidence_id) in resolved_evidence_ids
                for evidence_id in fact.get("evidence_ids", [])
            )
        ),
        len(accepted_facts),
        empty=1.0 if not expected.get("evidence_required") else 0.0,
    )
    relationship_evidence_coverage = _ratio(
        sum(
            1
            for relationship in accepted_relationships
            if isinstance(relationship, dict)
            and any(
                str(evidence_id) in resolved_evidence_ids
                for evidence_id in relationship.get("evidence_ids", [])
            )
        ),
        len(accepted_relationships),
        empty=1.0 if not required_relationships else 0.0,
    )
    if not search_evaluated:
        citation_resolution = 1.0
        fact_evidence_coverage = 1.0
        relationship_evidence_coverage = 1.0

    mcp_technical_error = bool(mcp.get("technical_error")) or (
        bool(mcp.get("is_error"))
        or (_successful(mcp) and is_technical_error_answer(mcp_answer))
    )
    mcp_capability_gated = str(mcp.get("status") or "").lower() in {
        "capability_unavailable",
        "capability_gated",
    }
    mcp_citations = (
        mcp.get("citations")
        if isinstance(mcp.get("citations"), list)
        else []
    )
    mcp_citation_required = bool(
        expected.get("evidence_required")
        and _route_required(case, "data_agent_mcp")
        and not mcp_capability_gated
    )
    mcp_citation_resolution = (
        _ratio(
            sum(
                1
                for citation in mcp_citations
                if isinstance(citation, dict)
                and (
                    (identity := _immutable_citation_identity(citation))
                    is not None
                )
                and identity in trusted_search_citations
            ),
            len(mcp_citations),
            empty=0.0,
        )
        if mcp_citation_required
        else 1.0
    )
    mcp_semantic_success = (
        _successful(mcp)
        and not mcp_technical_error
        and bool(str(mcp_answer or "").strip())
        and (
            not _route_required(case, "data_agent_mcp")
            or answer_relevance == 1.0
        )
        and (
            not mcp_citation_required
            or mcp_citation_resolution == 1.0
        )
    )
    if (
        _route_required(case, "data_agent_mcp")
        and not mcp_capability_gated
    ):
        mcp_score = 1.0 if mcp_semantic_success else 0.0
    elif mcp_evaluated and not mcp_capability_gated:
        mcp_score = 1.0 if mcp_semantic_success else 0.0
    else:
        mcp_score = 1.0
    composed_score = (
        _mean(
            [
                1.0 if composed.get("graph_used") is True else 0.0,
                1.0 if composed.get("search_used") is True else 0.0,
                canonical_linkage,
                0.0 if composed.get("contradiction") is True else 1.0,
                (
                    1.0
                    if not source_failure
                    or composed.get("source_failures_disclosed") is True
                    else 0.0
                ),
                answer_relevance,
                mcp_score,
            ]
        )
        if composed_evaluated
        else 1.0
    )
    temporal_values = []
    for route in (graph, search, mcp, composed):
        values = route.get("temporal_values")
        if isinstance(values, list):
            temporal_values.extend(value for value in values if value)
    temporal_correctness = (
        1.0
        if not expected.get("temporal_required")
        else (1.0 if temporal_values else 0.0)
    )
    evidence_support_score = _mean(
        [fact_evidence_coverage, relationship_evidence_coverage]
    )
    unsupported_claim_avoidance = (
        1.0
        if (
            not mcp_evaluated
            or mcp_semantic_success
        )
        and fact_evidence_coverage == 1.0
        and relationship_evidence_coverage == 1.0
        else 0.0
    )
    reliability_values = []
    if graph_evaluated:
        reliability_values.append(graph_reliability)
    if search_evaluated:
        reliability_values.append(search_reliability)
    if mcp_evaluated:
        reliability_values.append(mcp_score)
    runtime_reliability = (
        _mean(reliability_values) if reliability_values else 1.0
    )
    required_route_failures = [
        route_name
        for route_name in (
            "direct_graph",
            "search",
            "knowledge_base",
            "data_agent_ui",
            "data_agent_mcp",
            "foundry_agent",
            "composed",
        )
        if _route_required(case, route_name)
        and not _successful(
            observed.get(route_name)
            if isinstance(observed.get(route_name), dict)
            else {}
        )
    ]
    semantic_answer_score = _mean(
        [
            answer_relevance,
            entity_coverage,
            relationship_coverage,
            direction_correctness,
            evidence_support_score,
            citation_resolution,
            mcp_citation_resolution,
            temporal_correctness,
            unsupported_claim_avoidance,
            runtime_reliability,
        ]
    )

    return {
        "id": str(case["id"]),
        "scores": {
            "direct_graph": round(graph_score, 4),
            "search": round(search_score, 4),
            "knowledge_base": round(knowledge_base_score, 4),
            "canonical_id_linkage": round(canonical_linkage, 4),
            "composed": round(composed_score, 4),
            "accepted_fact_evidence_coverage": round(
                fact_evidence_coverage, 4
            ),
            "accepted_relationship_evidence_coverage": round(
                relationship_evidence_coverage, 4
            ),
            "citation_resolution": round(citation_resolution, 4),
            "data_agent_citation_resolution": round(
                mcp_citation_resolution,
                4,
            ),
            "data_agent_mcp": mcp_score,
            "answer_relevance": round(answer_relevance, 4),
            "temporal_correctness": round(temporal_correctness, 4),
            "unsupported_claim_avoidance": round(
                unsupported_claim_avoidance, 4
            ),
            "source_runtime_reliability": round(runtime_reliability, 4),
            "semantic_answer": round(semantic_answer_score, 4),
        },
        "signals": {
            "partial_source": partial_source,
            "mcp_technical_error": mcp_technical_error,
            "mcp_capability_gated": mcp_capability_gated,
            "graph_success": _successful(graph),
            "search_success": _successful(search) and not partial_source,
            "mcp_request_ids": _request_ids(mcp),
            "required_route_failures": required_route_failures,
        },
    }


def evaluate_runtime_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Score all competency routes and classify platform runtime failures."""
    case_results = [_score_case(case) for case in evidence["cases"]]
    metrics = {
        "direct_graph_score": round(
            _mean([case["scores"]["direct_graph"] for case in case_results]), 4
        ),
        "search_score": round(
            _mean([case["scores"]["search"] for case in case_results]), 4
        ),
        "knowledge_base_score": round(
            _mean(
                [
                    case["scores"]["knowledge_base"]
                    for case in case_results
                ]
            ),
            4,
        ),
        "canonical_id_linkage": round(
            _mean(
                [
                    case["scores"]["canonical_id_linkage"]
                    for case in case_results
                ]
            ),
            4,
        ),
        "composed_score": round(
            _mean([case["scores"]["composed"] for case in case_results]), 4
        ),
        "accepted_fact_evidence_coverage": round(
            _mean(
                [
                    case["scores"]["accepted_fact_evidence_coverage"]
                    for case in case_results
                ]
            ),
            4,
        ),
        "accepted_relationship_evidence_coverage": round(
            _mean(
                [
                    case["scores"][
                        "accepted_relationship_evidence_coverage"
                    ]
                    for case in case_results
                ]
            ),
            4,
        ),
        "citation_resolution": round(
            _mean(
                [
                    case["scores"]["citation_resolution"]
                    for case in case_results
                ]
            ),
            4,
        ),
        "data_agent_citation_resolution": round(
            _mean(
                [
                    case["scores"]["data_agent_citation_resolution"]
                    for case in case_results
                ]
            ),
            4,
        ),
        "semantic_answer_score": round(
            _mean(
                [
                    case["scores"]["semantic_answer"]
                    for case in case_results
                ]
            ),
            4,
        ),
        "data_agent_technical_error_answers": sum(
            1
            for case in case_results
            if case["signals"]["mcp_technical_error"]
        ),
        "data_agent_semantic_failures": sum(
            1
            for case in case_results
            if case["scores"]["data_agent_mcp"] < 1.0
            and not case["signals"]["mcp_capability_gated"]
        ),
        "knowledge_partial_source_responses": sum(
            1 for case in case_results if case["signals"]["partial_source"]
        ),
        "required_route_failures": sum(
            len(case["signals"]["required_route_failures"])
            for case in case_results
        ),
    }
    violations = [
        {
            "metric": metric,
            "observed": metrics[metric],
            "threshold": threshold,
        }
        for metric, threshold in _THRESHOLDS.items()
        if float(metrics[metric]) < threshold
    ]
    if metrics["data_agent_technical_error_answers"]:
        violations.append(
            {
                "metric": "data_agent_technical_error_answers",
                "observed": metrics["data_agent_technical_error_answers"],
                "threshold": 0,
            }
        )
    if metrics["data_agent_semantic_failures"]:
        violations.append(
            {
                "metric": "data_agent_semantic_failures",
                "observed": metrics["data_agent_semantic_failures"],
                "threshold": 0,
            }
        )
    if metrics["knowledge_partial_source_responses"]:
        violations.append(
            {
                "metric": "knowledge_partial_source_responses",
                "observed": metrics["knowledge_partial_source_responses"],
                "threshold": 0,
            }
        )
    if metrics["required_route_failures"]:
        violations.append(
            {
                "metric": "required_route_failures",
                "observed": metrics["required_route_failures"],
                "threshold": 0,
            }
        )

    direct_sources_required = any(
        _route_required(case, "direct_graph")
        and _route_required(case, "search")
        for case in evidence["cases"]
        if isinstance(case, dict)
    )
    direct_sources_pass = (
        direct_sources_required
        and
        metrics["direct_graph_score"] >= _THRESHOLDS["direct_graph_score"]
        and metrics["search_score"] >= _THRESHOLDS["search_score"]
    )
    if direct_sources_pass and metrics["data_agent_semantic_failures"]:
        status = "runtime_blocked"
    elif violations:
        status = "failed"
    else:
        status = "passed"
    return {
        "schema": "fabric-kg.runtime-evaluation.v1",
        "evaluated_at_utc": _utc_now(),
        "contract_hash": evidence.get("contract_hash"),
        "environment": evidence.get("environment"),
        "status": status,
        "metrics": metrics,
        "violations": violations,
        "cases": case_results,
    }


def validate_deployment_evidence(
    evidence: dict[str, Any],
    *,
    require_spec008a_diagnostic: bool = True,
) -> dict[str, Any]:
    """Validate deployment, authorization, publication, and failure telemetry."""
    deployment = (
        evidence.get("deployment")
        if isinstance(evidence.get("deployment"), dict)
        else {}
    )
    findings: list[dict[str, Any]] = []
    targets = (
        evidence.get("runtime_targets")
        if isinstance(evidence.get("runtime_targets"), dict)
        else {}
    )
    schema_mode = str(
        deployment.get("schema_mode") or "schema1_compatibility"
    )
    schema2_bounded = schema_mode == "schema2_bounded"
    receipt_present = bool(deployment.get("receipt_sha256"))
    required_receipt_hashes = [
        "semantic_contract_hash",
        "semantic_artifact_set_hash",
        "graph_artifact_set_hash",
        "search_artifact_set_hash",
        "semantic_model_manifest_hash",
        "ontology_persisted_projection_hash",
        "graph_persisted_projection_hash",
        "persisted_query_schema_hash",
        "competency_contract_hash",
        "package_hash",
    ]
    if not schema2_bounded:
        required_receipt_hashes.extend([
            "receipt_instruction_hash",
            "receipt_deployed_instruction_hash",
        ])
    receipt_hashes_present = all(
        deployment.get(key)
        for key in required_receipt_hashes
    )
    receipt_linked = (
        receipt_present
        and (
            receipt_hashes_present
            and deployment.get("contract_hash_consistent") is True
            and deployment.get("competency_contract_hash")
            == evidence.get("contract_hash")
            and (
                schema2_bounded
                or (
                    deployment.get("receipt_instruction_hash")
                    == deployment.get("compiled_instruction_hash")
                    and deployment.get("receipt_deployed_instruction_hash")
                    == deployment.get("deployed_instruction_hash")
                    and deployment.get("receipt_instruction_hash")
                    == deployment.get("receipt_deployed_instruction_hash")
                )
            )
            and (
                not deployment.get("graph_model_id")
                or deployment.get("graph_model_id")
                == targets.get("graph_model_id")
            )
            and (
                targets.get("search_mode") == "knowledge_base"
                or not deployment.get("search_index_name")
                or deployment.get("search_index_name")
                == targets.get("search_index_name")
            )
            and (
                targets.get("search_mode") != "knowledge_base"
                or not deployment.get("knowledge_base_id")
                or deployment.get("knowledge_base_id")
                == targets.get("knowledge_base_id")
            )
            and (
                not deployment.get("data_agent_id")
                or deployment.get("data_agent_id")
                == targets.get("data_agent_id")
            )
        )
    )
    checks = {
        "artifact_validation_passed": (
            deployment.get("artifact_validation_status") == "passed"
        ),
        "knowledge_sources_complete": not bool(
            deployment.get("partial_source")
        )
        and int(deployment.get("knowledge_http_status") or 200) != 206,
        "data_agent_published": (
            schema2_bounded
            or deployment.get("data_agent_published") is True
        ),
        "instruction_hash_matches": (
            schema2_bounded
            or (
                bool(deployment.get("compiled_instruction_hash"))
                and deployment.get("compiled_instruction_hash")
                == deployment.get("deployed_instruction_hash")
            )
        ),
        "no_duplicate_deployments": int(
            deployment.get("unintended_duplicate_deployments") or 0
        )
        == 0,
        "breaking_change_approved": (
            deployment.get("breaking_change") is not True
            or deployment.get("migration_approved") is True
        ),
        "deployment_receipt_linked": receipt_linked,
    }
    for name, passed in checks.items():
        if not passed:
            findings.append({"code": name, "severity": "fail"})

    for case in evidence["cases"]:
        observed = case["observed"]
        for route_name, route in observed.items():
            if not isinstance(route, dict):
                continue
            result_category = str(
                route.get("result_category") or ""
            ).lower()
            route_failed = (
                str(route.get("status") or "").lower()
                in {"failed", "error", "partial"}
                or result_category in {
                    "invalid_semantic_plan",
                    "invalid_physical_query",
                    "authorization_failure",
                    "platform_failure",
                    "timeout",
                    "concurrency_conflict",
                    "partial_result",
                }
                or route.get("partial_source") is True
                or is_technical_error_answer(route.get("answer"))
                or (
                    route_name == "data_agent_mcp"
                    and _successful(route)
                    and not str(route.get("answer") or "").strip()
                )
            )
            if not route_failed:
                continue
            missing = []
            if not _request_ids(route):
                missing.append("request_ids")
            if not route.get("timestamp_utc"):
                missing.append("timestamp_utc")
            if not route.get("remediation"):
                missing.append("remediation")
            if missing:
                findings.append(
                    {
                        "code": "failure_telemetry_incomplete",
                        "severity": "fail",
                        "case_id": case["id"],
                        "route": route_name,
                        "missing": missing,
                    }
                )

    # ------------------------------------------------------------------
    # SPEC-008A §10.4 diagnostic record validation (Blocker 1 wiring)
    # Wire validate_diagnostic_record into the deployment acceptance path so
    # that diagnostic records are validated before being accepted as evidence.
    # PartialDiagnosticExport cannot be passed as acceptance evidence.
    # ------------------------------------------------------------------
    _diagnostic_payloads = evidence.get("diagnostic_records")
    if _diagnostic_payloads is None:
        _single_diagnostic = evidence.get("diagnostic_record")
        _diagnostic_payloads = (
            [_single_diagnostic]
            if _single_diagnostic is not None
            else []
        )
    if not isinstance(_diagnostic_payloads, list):
        findings.append({
            "code": "diagnostic_records_invalid",
            "severity": "fail",
            "message": "diagnostic_records must be a JSON array.",
        })
        _diagnostic_payloads = []
    if not _diagnostic_payloads and require_spec008a_diagnostic:
        findings.append({
            "code": "diagnostic_record_missing",
            "severity": "fail",
            "message": (
                "SPEC-008A acceptance requires a sealed "
                "SemanticDiagnosticRecord."
            ),
        })
    case_count = len(evidence.get("cases") or [])
    if _diagnostic_payloads and len(_diagnostic_payloads) != case_count:
        findings.append({
            "code": "diagnostic_record_count_mismatch",
            "severity": "fail",
            "message": (
                f"Expected one diagnostic per competency case ({case_count}); "
                f"found {len(_diagnostic_payloads)}."
            ),
        })
    if _diagnostic_payloads:
        from pydantic import ValidationError as _ValidationError

        from fabric_kg_builder.semantic.query_validation import (
            validate_diagnostic_record as _validate_diagnostic_record,
        )
        from fabric_kg_builder.semantic.schemas import (
            PartialDiagnosticExport as _PartialDiagnosticExport,
        )
        from fabric_kg_builder.semantic.schemas import (
            SemanticDiagnosticRecord as _SemanticDiagnosticRecord,
        )

        expected_diagnostic_hashes = {
            "semantic_contract_hash": deployment.get(
                "semantic_contract_hash"
            ),
            "manifest_hash": deployment.get(
                "semantic_model_manifest_hash"
            ),
            "ontology_projection_hash": deployment.get(
                "ontology_persisted_projection_hash"
            ),
            "graph_projection_hash": deployment.get(
                "graph_persisted_projection_hash"
            ),
            "search_projection_hash": deployment.get(
                "search_artifact_set_hash"
            ),
            "query_schema_hash": deployment.get(
                "persisted_query_schema_hash"
            ),
        }
        if not schema2_bounded:
            expected_diagnostic_hashes["instruction_hash"] = deployment.get(
                "receipt_deployed_instruction_hash"
            )
        for _diagnostic_index, _diag_raw in enumerate(
            _diagnostic_payloads
        ):
            _diag_record = None
            try:
                _diag_record = _SemanticDiagnosticRecord.model_validate(
                    _diag_raw
                )
            except _ValidationError:
                try:
                    _PartialDiagnosticExport.model_validate(_diag_raw)
                    findings.append({
                        "code": "partial_diagnostic_as_acceptance_evidence",
                        "severity": "fail",
                        "message": (
                            f"diagnostic_records[{_diagnostic_index}] is "
                            "partial and cannot serve as acceptance evidence."
                        ),
                    })
                except _ValidationError:
                    findings.append({
                        "code": "diagnostic_record_invalid_schema",
                        "severity": "fail",
                        "message": (
                            f"diagnostic_records[{_diagnostic_index}] failed "
                            "the sealed and partial diagnostic schemas."
                        ),
                    })
            if _diag_record is None:
                continue
            for _qf in _validate_diagnostic_record(
                _diag_record,
                reference_watermark=evidence.get(
                    "diagnostic_reference_watermark"
                ),
            ):
                findings.append({
                    "code": _qf.code,
                    "severity": "fail",
                    "message": _qf.message,
                })
            for _field, _expected in expected_diagnostic_hashes.items():
                if getattr(_diag_record, _field) != _expected:
                    findings.append({
                        "code": "diagnostic_deployment_hash_mismatch",
                        "severity": "fail",
                        "message": (
                            f"diagnostic_records[{_diagnostic_index}]."
                            f"{_field} does not match the deployment receipt."
                        ),
                    })

    return {
        "schema": "fabric-kg.deployment-validation.v1",
        "validated_at_utc": _utc_now(),
        "contract_hash": evidence.get("contract_hash"),
        "environment": evidence.get("environment"),
        "status": "failed" if findings else "passed",
        "checks": checks,
        "findings": findings,
    }


def build_runtime_report(
    evidence: dict[str, Any],
    *,
    deployment_validation: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted support- and release-ready runtime report."""
    validation = deployment_validation or validate_deployment_evidence(evidence)
    eval_result = evaluation or evaluate_runtime_evidence(evidence)
    deployment = (
        evidence.get("deployment")
        if isinstance(evidence.get("deployment"), dict)
        else {}
    )
    case_receipts = []
    for case in evidence["cases"]:
        routes = {}
        for name, route in case["observed"].items():
            if not isinstance(route, dict):
                continue
            answer = str(route.get("answer") or "")
            routes[name] = {
                "status": route.get("status"),
                "result_category": route.get("result_category"),
                "final_semantic_status": route.get(
                    "final_semantic_status"
                ),
                "http_status": route.get("http_status"),
                "request_ids": _request_ids(route),
                "retry_request_ids": route.get("retry_request_ids", []),
                "retry_count": route.get("retry_count", 0),
                "first_failure": route.get("first_failure"),
                "timestamp_utc": route.get("timestamp_utc"),
                "remediation": route.get("remediation"),
                "evidence_trace": route.get("evidence_trace"),
                "unsupported_portion": route.get("unsupported_portion"),
                "answer_sha256": (
                    "sha256:"
                    + hashlib.sha256(answer.encode("utf-8")).hexdigest()
                    if answer
                    else None
                ),
                "answer_length": len(answer),
                "technical_error": is_technical_error_answer(answer),
                "partial_source": bool(route.get("partial_source")),
            }
        case_receipts.append({"id": str(case["id"]), "routes": routes})

    if validation["status"] != "passed":
        status = "failed"
    else:
        status = eval_result["status"]
    return {
        "schema": "fabric-kg.runtime-report.v1",
        "generated_at_utc": _utc_now(),
        "contract_hash": evidence.get("contract_hash"),
        "environment": evidence.get("environment"),
        "status": status,
        "deployment_validation": validation,
        "evaluation": eval_result,
        "deployment_receipt": {
            key: deployment.get(key)
            for key in (
                "receipt_sha256",
                "semantic_contract_hash",
                "semantic_artifact_set_hash",
                "graph_artifact_set_hash",
                "search_artifact_set_hash",
                "receipt_instruction_hash",
                "receipt_deployed_instruction_hash",
                "persisted_query_schema_hash",
                "competency_contract_hash",
                "package_hash",
                "graph_model_id",
                "search_index_name",
                "data_agent_id",
                "knowledge_base_id",
            )
            if deployment.get(key) is not None
        },
        "runtime_targets": evidence.get("runtime_targets", {}),
        "request_receipts": case_receipts,
        "support_case_ready": status == "runtime_blocked",
    }
