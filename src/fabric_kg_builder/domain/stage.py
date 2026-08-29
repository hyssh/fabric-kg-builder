"""L1 Domain Design/Approval orchestration over C0 manifests and receipts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import ValidationError

from fabric_kg_builder.contracts.base import (
    ContractModel,
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.evidence import EvidenceSpan, SourceUnit
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.receipts import (
    ArtifactEntry,
    ArtifactManifest,
    StageReceipt,
    validate_skip_preconditions,
)
from fabric_kg_builder.contracts.resources import (
    StageResourceMetrics,
    validate_receipt_resources,
)
from fabric_kg_builder.platform import process_resource_usage
from fabric_kg_builder.sources.corpus import (
    DesignSampleManifest,
    SourceCorpusManifest,
    build_source_corpus_manifest,
    validate_corpus_manifest_against_source,
)
from fabric_kg_builder.sources.inspector import (
    DesignSamplingBudget,
    build_l1_design_artifacts,
)

from .contexts import (
    DomainApprovalContext,
    DomainDesignContext,
    DomainIntake,
    DomainSourceProfile,
)
from .models import ApprovalMetadataV2, DomainContractV2
from .proposal import (
    DOMAIN_PROPOSAL_PROMPT_HASH,
    DOMAIN_PROPOSAL_SYSTEM_PROMPT,
    DOMAIN_PROPOSAL_PROMPT_VERSION,
    DomainProposal,
    DomainProposalCandidatesV2,
    ProposalQuestionRouteV2,
    QuestionRouteRepairV2,
    build_domain_proposal,
    build_draft_contract_from_candidates,
    build_proposal_user_message,
    compute_model_hash,
    domain_proposal_candidates_schema,
    normalize_candidate_scores,
)
from .scoring import SCORER_HASH, SCORER_VERSION
from .selection import ProposalSelectionError, SELECTOR_VERSION
from .service import compute_contract_hash, load_domain_contract, render_domain_contract_yaml

L1_STAGE_NAME = "Domain Design/Approval"
L1_STATE_DIR = Path(".fkg") / "l1"
L1_ACCEPTED_VERSIONS = {
    "c0.artifact_manifest": "1.0.0",
    "c0.evidence_span": "1.0.0",
    "c0.source_unit": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
    "domain.contract": "2.0.0",
    "l1.design_sample_manifest": "1.0.0",
    "l1.domain_approval_context": "1.0.0",
    "l1.domain_design_context": "1.0.0",
    "l1.domain_intake": "1.0.0",
    "l1.domain_proposal": "1.0.0",
    "l1.domain_source_profile": "1.0.0",
    "l1.source_corpus_manifest": "1.0.0",
}


class L1StageError(ValueError):
    """Raised when L1 cannot produce a coherent immutable stage result."""


class L1ProposalSchemaRepairError(L1StageError):
    """Raised after bounded same-authority schema repair is exhausted."""

    error_code = "L1_PROPOSAL_SCHEMA_REPAIR_EXHAUSTED"

    def __init__(
        self,
        *,
        attempt_count: int,
        validation_error_codes: tuple[str, ...] = (),
        validation_failures: tuple[tuple[str, str], ...] = (),
        candidate_attempts: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.attempt_count = attempt_count
        self.candidate_attempts = candidate_attempts
        self.validation_failures = validation_failures or tuple(
            ("proposal", code) for code in validation_error_codes
        )
        self.validation_error_codes = tuple(
            code for _path, code in self.validation_failures
        )
        super().__init__(
            f"{self.error_code}: proposal schema remained invalid after "
            f"{attempt_count} bounded attempt(s); validation_paths="
            + ",".join(
                f"{path}:{code}"
                for path, code in self.validation_failures
            )
        )


class L1ZeroSupportedRoutesError(L1StageError):
    """Raised after one strict route-only repair still proves zero coverage."""

    error_code = "L1_ZERO_SUPPORTED_ROUTES"

    def __init__(self, audit_payload: "L1ZeroRouteAudit") -> None:
        self.audit_payload = audit_payload
        super().__init__(
            f"{self.error_code}: no validated relationship path supports any "
            "competency question after one route-only repair"
        )


class L1ZeroRouteAudit(ContractModel):
    error_code: Literal["L1_ZERO_SUPPORTED_ROUTES"]
    reason_code: str
    model_call_count: Literal[1, 2]
    model_version: str
    model_hash: str
    intake_hash: str
    candidate_hash: str
    candidate_attempt_hashes: tuple[str, ...]
    candidate_attempt_type_counts: tuple[int, ...]
    candidate_attempt_relationship_counts: tuple[int, ...]
    candidate_regeneration_attempted: bool
    candidate_regeneration_result_code: str
    question_ids: tuple[str, ...]
    question_id_hashes: tuple[str, ...]
    route_states: tuple[Literal["supported", "unsupported"], ...]
    unsupported_reason_codes: tuple[str, ...]
    initial_route_codes: tuple[str, ...]
    supported_route_count: int
    unsupported_route_count: int
    initial_supported_route_count: int
    initial_unsupported_route_count: int
    eligible_type_count: int
    eligible_type_id_hash: str
    eligible_relationship_count: int
    eligible_relationship_id_hash: str
    coverable_question_count: int
    critical_question_count: int
    critical_supported_route_count: int
    critical_coverable_question_count: int
    route_repair_attempted: bool
    route_repair_result_code: str
    terminal_error_code: str
    proposed_type_count: int
    proposed_type_id_hash: str
    proposed_relationship_count: int
    proposed_relationship_id_hash: str


def _sanitized_validation_failures(
    error: ValidationError | ArithmeticError,
) -> list[dict[str, Any]]:
    if not isinstance(error, ValidationError):
        return [
            {
                "location": "score_inputs",
                "type": "arithmetic_error",
                "message": "score normalization failed",
            }
        ]
    return [
        {
            "location": ".".join(
                "[index]" if isinstance(part, int) else str(part)
                for part in item["loc"]
            ),
            "type": str(item["type"]),
            "message": str(item["msg"])[:200],
        }
        for item in error.errors(include_url=False, include_input=False)
    ][:20]


def _stable_semantic_validation_code(
    message: str,
    error_type: str,
) -> str:
    known = (
        ("exactly one path plan", "question_plan_cardinality_invalid"),
        ("at least one question must be covered", "question_coverage_zero"),
        ("question path references unknown relationship", "question_path_relationship_unknown"),
        ("question path references unknown type", "question_path_type_unknown"),
        ("path endpoint or direction mismatch", "question_path_endpoint_mismatch"),
        ("question path exceeds approved K", "question_path_exceeds_k"),
        ("question plan is not shortest", "question_path_not_shortest"),
        ("N must equal approved relationship type count", "relationship_count_policy_mismatch"),
        ("K must equal maximum shortest covered path", "max_hops_policy_mismatch"),
        ("Unknown relationship endpoints", "relationship_endpoint_unknown"),
        ("Unknown relationship questions", "relationship_question_unknown"),
        ("Duplicate semantic type ID", "semantic_type_id_duplicate"),
        ("Duplicate semantic key", "semantic_key_duplicate"),
        ("Duplicate relationship type ID", "relationship_type_id_duplicate"),
        ("Duplicate predicate ID", "predicate_id_duplicate"),
        ("hierarchy closure/hash", "hierarchy_closure_mismatch"),
        ("root type must identify itself as identity root", "identity_root_self_mismatch"),
        ("every hierarchy root requires one identity key policy", "identity_root_policy_missing"),
        ("descendants inherit and cannot override root identity", "identity_descendant_policy_forbidden"),
        ("identity_root_type_id must resolve to transitive root", "identity_root_reference_invalid"),
        ("identity key policy", "identity_key_policy_invalid"),
        ("identity_policy_hash", "identity_policy_hash_mismatch"),
        ("completeness_requirement_hash", "completeness_hash_mismatch"),
        ("external_reference_decision_hash", "external_reference_hash_mismatch"),
        ("identity.contract_kind must be l1.domain_proposal", "proposal_identity_kind_mismatch"),
        ("domain_contract_hash does not equal domain.service.compute_contract_hash", "proposal_domain_hash_authority_mismatch"),
        ("proposal identity domain hash mismatch", "proposal_identity_domain_hash_mismatch"),
        ("proposal must contain a draft contract", "proposal_draft_status_invalid"),
        ("selected relationship IDs do not match draft contract", "proposal_relationship_selection_mismatch"),
        ("completeness IDs do not match draft contract", "proposal_completeness_selection_mismatch"),
        ("competency question coverage hash mismatch", "proposal_question_coverage_hash_mismatch"),
        ("selected_candidate_count does not match audit", "proposal_selected_count_mismatch"),
        ("candidate_count does not match audit", "proposal_candidate_count_mismatch"),
        ("proposal_hash does not match proposal content", "proposal_content_hash_mismatch"),
        ("domain_proposal_id does not match deterministic seed", "proposal_id_mismatch"),
        ("proposal identity content_hash mismatch", "proposal_identity_content_hash_mismatch"),
        ("identity.contract_kind must be", "l1_identity_kind_mismatch"),
        ("L1 identity contract version must be", "l1_identity_version_mismatch"),
        ("identity.content_hash must equal the L1 semantic hash", "l1_identity_semantic_hash_mismatch"),
        ("design_context_hash does not match context content", "design_context_hash_mismatch"),
        ("domain_design_context_id does not match deterministic seed", "design_context_id_mismatch"),
        ("source-derived identity requires asset", "identity_source_binding_incomplete"),
        ("prompt_version and prompt_hash must be paired", "identity_prompt_binding_incomplete"),
        ("model_version and model_hash must be paired", "identity_model_binding_incomplete"),
        ("extractor_name and extractor_version must be paired", "identity_extractor_binding_incomplete"),
        ("source_unit_id requires source-derived identity", "identity_source_unit_binding_incomplete"),
    )
    for fragment, code in known:
        if fragment in message:
            return code
    if error_type != "value_error":
        return error_type
    return "domain_contract_invariant_unclassified"


def _is_authority_validation_failure(code: str) -> bool:
    return (
        "authority_drift" in code
        or code == "route_question_id_unknown"
        or code.endswith("_unknown")
    )


def _normalize_question_route_shapes(
    candidate: dict[str, Any],
    *,
    trusted_question_ids: tuple[str, ...],
    attempt_count: int = 1,
) -> dict[str, Any]:
    """Classify raw routes and rebuild them in trusted question order."""
    normalized = json.loads(json.dumps(candidate))
    routes = normalized.get("question_routes")
    if not isinstance(routes, list):
        normalized["question_routes"] = [
            {
                "question_id": question_id,
                "start_type_id": None,
                "end_type_id": None,
                "unsupported_reason": "route_collection_invalid",
            }
            for question_id in trusted_question_ids
        ]
        return normalized

    trusted = set(trusted_question_ids)
    grouped: dict[str, list[dict[str, Any]]] = {
        question_id: [] for question_id in trusted_question_ids
    }
    unknown_question_id = False
    for route in routes:
        if not isinstance(route, dict):
            unknown_question_id = True
            continue
        question_id = route.get("question_id")
        if isinstance(question_id, str) and question_id in trusted:
            grouped[question_id].append(route)
        else:
            unknown_question_id = True
    if unknown_question_id:
        raise L1ProposalSchemaRepairError(
            attempt_count=attempt_count,
            validation_failures=(
                ("question_routes", "route_question_id_unknown"),
            ),
        )

    rebuilt: list[dict[str, Any]] = []
    for question_id in trusted_question_ids:
        matches = grouped[question_id]
        if not matches:
            code = "route_question_id_missing"
            route = {}
        elif len(matches) > 1:
            code = "route_question_id_duplicate"
            route = {}
        else:
            route = matches[0]
            code = ""
        if code:
            rebuilt.append(
                {
                    "question_id": question_id,
                    "start_type_id": None,
                    "end_type_id": None,
                    "unsupported_reason": code,
                }
            )
            continue

        has_start = "start_type_id" in route
        has_end = "end_type_id" in route
        start = route.get("start_type_id")
        end = route.get("end_type_id")
        reason = route.get("unsupported_reason")
        if not has_start or not has_end:
            code = "route_endpoint_key_missing"
        elif not (
            start is None
            or (isinstance(start, str) and bool(start.strip()))
        ) or not (
            end is None
            or (isinstance(end, str) and bool(end.strip()))
        ):
            code = "route_endpoint_type_invalid"
        elif (start is None) != (end is None):
            code = "route_endpoint_pair_half_defined"
        elif start is None:
            if reason is None or (
                isinstance(reason, str) and not reason.strip()
            ):
                code = "unsupported_reason_missing"
            elif not isinstance(reason, str):
                code = "unsupported_reason_type_invalid"
            else:
                code = ""
        else:
            code = ""
            if reason is not None and not isinstance(reason, str):
                code = "supported_reason_type_invalid"
            if not code:
                rebuilt.append(
                    {
                        "question_id": question_id,
                        "start_type_id": start,
                        "end_type_id": end,
                        "unsupported_reason": None,
                    }
                )
                continue
        rebuilt.append(
            {
                "question_id": question_id,
                "start_type_id": None,
                "end_type_id": None,
                "unsupported_reason": code or str(reason),
            }
        )
    normalized["question_routes"] = rebuilt
    return normalized


def _validate_proposal_candidate(
    raw: dict[str, Any],
    *,
    trusted_question_ids: tuple[str, ...],
    attempt_count: int,
) -> DomainProposalCandidatesV2:
    try:
        values = normalize_candidate_scores(
            _normalize_question_route_shapes(
                json.loads(json.dumps(raw)),
                trusted_question_ids=trusted_question_ids,
                attempt_count=attempt_count,
            )
        )
        return DomainProposalCandidatesV2.model_validate(values)
    except (ValidationError, ArithmeticError) as error:
        failures = _sanitized_validation_failures(error)
        raise L1ProposalSchemaRepairError(
            attempt_count=attempt_count,
            validation_failures=tuple(
                (
                    item["location"] or "proposal.root",
                    item["type"],
                )
                for item in failures
            ),
        ) from error


def _raw_candidate_diagnostics(
    raw: object,
    *,
    attempt: int,
    failures: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    mapping = raw if isinstance(raw, dict) else {}
    return {
        "attempt": attempt,
        "candidate_hash": canonical_sha256(
            raw if isinstance(raw, (dict, list, str, int, float, bool)) or raw is None
            else {"payload_type": type(raw).__name__}
        ),
        "proposed_type_count": (
            len(mapping.get("semantic_type_candidates", ()))
            if isinstance(mapping.get("semantic_type_candidates"), list)
            else 0
        ),
        "proposed_relationship_count": (
            len(mapping.get("relationship_candidates", ()))
            if isinstance(mapping.get("relationship_candidates"), list)
            else 0
        ),
        "failures": [
            {"path": path or "proposal.root", "code": code}
            for path, code in failures
        ],
    }


def _uncoverable_critical_question_ids(
    preflight: L1Preflight,
    candidates: DomainProposalCandidatesV2,
) -> tuple[str, ...]:
    from .selection import eligible_relationship_vocabulary

    eligible_type_ids = {
        item.proposed_type.type_id
        for item in candidates.semantic_type_candidates
        if item.score.ip_governance_eligible
        and item.score.ambiguity_conflict_penalty == 0
    }
    relationships, _aliases, _groups = eligible_relationship_vocabulary(
        candidates.relationship_candidates,
        eligible_type_ids=eligible_type_ids,
    )
    coverable = {
        question_id
        for relationship in relationships
        for question_id in relationship.competency_question_ids
    }
    return tuple(
        item.id
        for item in preflight.intake.competency_questions
        if item.business_critical and item.id not in coverable
    )


def _zero_route_audit(
    *,
    preflight: L1Preflight,
    candidates: DomainProposalCandidatesV2,
    model_call_count: Literal[1, 2],
    reason_code: str,
    route_repair_attempted: bool | None = None,
    route_repair_result_code: str | None = None,
    terminal_error_code: str | None = None,
    candidate_attempt_hashes: tuple[str, ...] | None = None,
    candidate_attempt_type_counts: tuple[int, ...] | None = None,
    candidate_attempt_relationship_counts: tuple[int, ...] | None = None,
    candidate_regeneration_attempted: bool = False,
    candidate_regeneration_result_code: str = "not_attempted",
    initial_route_codes: tuple[str, ...] | None = None,
    initial_supported_route_count: int | None = None,
    initial_unsupported_route_count: int | None = None,
) -> L1ZeroRouteAudit:
    from .selection import _enumerate_paths, eligible_relationship_vocabulary

    eligible_type_ids = {
        item.proposed_type.type_id
        for item in candidates.semantic_type_candidates
        if item.score.ip_governance_eligible
        and item.score.ambiguity_conflict_penalty == 0
    }
    eligible_relationships, _aliases, _groups = (
        eligible_relationship_vocabulary(
            candidates.relationship_candidates,
            eligible_type_ids=eligible_type_ids,
        )
    )
    trusted_question_ids = {
        question.id for question in preflight.intake.competency_questions
    }
    critical_question_ids = {
        question.id
        for question in preflight.intake.competency_questions
        if question.business_critical
    }
    route_codes: list[str] = []
    route_states: list[Literal["supported", "unsupported"]] = []
    supported_count = 0
    critical_supported_count = 0
    for route in candidates.question_routes:
        if route.start_type_id is not None:
            if _enumerate_paths(
                route, eligible_relationships, max_hops=4
            ):
                route_codes.append("supported_path_valid")
                route_states.append("supported")
                supported_count += 1
                if route.question_id in critical_question_ids:
                    critical_supported_count += 1
            else:
                route_codes.append("supported_path_unavailable")
                route_states.append("unsupported")
        else:
            route_states.append("unsupported")
            route_codes.append(
                route.unsupported_reason
                if route.unsupported_reason
                in {
                    "no_supported_route_proposed",
                    "route_collection_invalid",
                    "route_endpoint_key_missing",
                    "route_endpoint_pair_half_defined",
                    "route_endpoint_type_invalid",
                    "route_question_id_duplicate",
                    "route_question_id_missing",
                    "supported_reason_type_invalid",
                    "unsupported_reason_missing",
                    "unsupported_reason_type_invalid",
                }
                else "model_unsupported_reason_present"
            )
    return L1ZeroRouteAudit(
        error_code=L1ZeroSupportedRoutesError.error_code,
        reason_code=reason_code,
        model_call_count=model_call_count,
        model_version=preflight.model_version,
        model_hash=preflight.model_hash,
        intake_hash=preflight.intake.intake_hash,
        candidate_hash=canonical_sha256(candidates),
        candidate_attempt_hashes=(
            (canonical_sha256(candidates),)
            if candidate_attempt_hashes is None
            else candidate_attempt_hashes
        ),
        candidate_attempt_type_counts=(
            (len(candidates.semantic_type_candidates),)
            if candidate_attempt_type_counts is None
            else candidate_attempt_type_counts
        ),
        candidate_attempt_relationship_counts=(
            (len(candidates.relationship_candidates),)
            if candidate_attempt_relationship_counts is None
            else candidate_attempt_relationship_counts
        ),
        candidate_regeneration_attempted=candidate_regeneration_attempted,
        candidate_regeneration_result_code=(
            candidate_regeneration_result_code
        ),
        question_ids=tuple(
            question.id for question in preflight.intake.competency_questions
        ),
        question_id_hashes=tuple(
            canonical_sha256({"question_id": question.id})
            for question in preflight.intake.competency_questions
        ),
        route_states=tuple(route_states),
        unsupported_reason_codes=tuple(route_codes),
        initial_route_codes=(
            tuple(route_codes)
            if initial_route_codes is None
            else initial_route_codes
        ),
        supported_route_count=supported_count,
        unsupported_route_count=(
            len(preflight.intake.competency_questions) - supported_count
        ),
        initial_supported_route_count=(
            supported_count
            if initial_supported_route_count is None
            else initial_supported_route_count
        ),
        initial_unsupported_route_count=(
            len(preflight.intake.competency_questions) - supported_count
            if initial_unsupported_route_count is None
            else initial_unsupported_route_count
        ),
        eligible_type_count=len(eligible_type_ids),
        eligible_type_id_hash=canonical_sha256(
            sorted(eligible_type_ids)
        ),
        eligible_relationship_count=len(eligible_relationships),
        eligible_relationship_id_hash=canonical_sha256(
            sorted(
                item.relationship_type_id
                for item in eligible_relationships
            )
        ),
        coverable_question_count=len(
            {
                question_id
                for relationship in eligible_relationships
                for question_id in relationship.competency_question_ids
                if question_id in trusted_question_ids
            }
        ),
        critical_question_count=len(critical_question_ids),
        critical_supported_route_count=critical_supported_count,
        critical_coverable_question_count=len(
            {
                question_id
                for relationship in eligible_relationships
                for question_id in relationship.competency_question_ids
                if question_id in critical_question_ids
            }
        ),
        route_repair_attempted=(
            model_call_count == 2
            if route_repair_attempted is None
            else route_repair_attempted
        ),
        route_repair_result_code=(
            route_repair_result_code or reason_code
        ),
        terminal_error_code=(
            terminal_error_code or "L1_ZERO_SUPPORTED_ROUTES"
        ),
        proposed_type_count=len(candidates.semantic_type_candidates),
        proposed_type_id_hash=canonical_sha256(
            sorted(
                item.proposed_type.type_id
                for item in candidates.semantic_type_candidates
            )
        ),
        proposed_relationship_count=len(candidates.relationship_candidates),
        proposed_relationship_id_hash=canonical_sha256(
            sorted(
                item.relationship_type_id
                for item in candidates.relationship_candidates
            )
        ),
    )


def _assert_candidate_authority(
    candidates: DomainProposalCandidatesV2,
    *,
    trusted_question_ids: tuple[str, ...],
    trusted_evidence_ids: set[str],
    attempt_count: int,
) -> None:
    questions = set(trusted_question_ids)
    type_ids = {
        item.proposed_type.type_id
        for item in candidates.semantic_type_candidates
    }
    failures: list[tuple[str, str]] = []
    for relationship in candidates.relationship_candidates:
        if set(relationship.competency_question_ids) - questions:
            failures.append(
                (
                    "relationship_candidates.competency_question_ids",
                    "relationship_question_unknown",
                )
            )
        if (
            set(relationship.source_type_ids)
            | set(relationship.target_type_ids)
        ) - type_ids:
            failures.append(
                (
                    "relationship_candidates.source_type_ids",
                    "relationship_endpoint_unknown",
                )
            )
        if set(relationship.evidence_span_ids) - trusted_evidence_ids:
            failures.append(
                (
                    "relationship_candidates.evidence_span_ids",
                    "relationship_evidence_unknown",
                )
            )
    if failures:
        raise L1ProposalSchemaRepairError(
            attempt_count=attempt_count,
            validation_failures=tuple(dict.fromkeys(failures)),
        )


def _assert_raw_candidate_authority(
    raw: dict[str, Any],
    *,
    trusted_question_ids: tuple[str, ...],
    trusted_evidence_ids: set[str],
    attempt_count: int,
) -> None:
    questions = set(trusted_question_ids)
    semantic_candidates = raw.get("semantic_type_candidates")
    type_ids: set[str] = set()
    if isinstance(semantic_candidates, list):
        for item in semantic_candidates:
            if not isinstance(item, dict):
                continue
            proposed = item.get("proposed_type")
            if isinstance(proposed, dict) and isinstance(
                proposed.get("type_id"), str
            ):
                type_ids.add(proposed["type_id"])
    failures: list[tuple[str, str]] = []
    relationship_values = raw.get("relationship_candidates")
    relationship_ids: set[str] = set()
    if isinstance(relationship_values, list):
        for item in relationship_values:
            if isinstance(item, dict) and isinstance(
                item.get("relationship_type_id"), str
            ):
                relationship_ids.add(item["relationship_type_id"])

    type_reference_fields = {
        "child_type_id",
        "parent_type_id",
        "identity_root_type_id",
        "scope_type_id",
        "scoped_subtype_id",
        "aggregate_type_id",
        "from_type_id",
        "to_type_id",
        "source_type_id",
        "target_type_id",
        "start_type_id",
        "end_type_id",
        "source_type_ids",
        "target_type_ids",
        "semantic_target_ids",
        "allowed_target_type_ids",
        "allowed_member_type_ids",
    }
    relationship_reference_fields = {
        "relationship_type_id",
        "membership_relationship_type_id",
        "hop_relationship_type_ids",
    }

    def references(value: object) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return {item for item in value if isinstance(item, str)}
        return set()

    def walk(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, str(index)))
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            item_path = ".".join((*path, key))
            item_references = references(item)
            if (
                not (path and path[0] == "question_routes")
                and (
                    key == "question_id"
                    or key.endswith("_question_ids")
                    or key in {"question_ids", "competency_question_ids"}
                )
            ) and item_references - questions:
                failures.append(
                    (item_path, "candidate_question_unknown")
                )
            if (
                key == "evidence_span_id"
                or key.endswith("_evidence_span_ids")
                or key == "evidence_span_ids"
            ) and item_references - trusted_evidence_ids:
                failures.append(
                    (item_path, "candidate_evidence_unknown")
                )
            if (
                key in type_reference_fields
                and item_references - type_ids
            ):
                failures.append(
                    (item_path, "candidate_type_reference_unknown")
                )
            relationship_declaration = (
                key == "relationship_type_id"
                and len(path) == 2
                and path[0] == "relationship_candidates"
            )
            if (
                key in relationship_reference_fields
                and not relationship_declaration
                and item_references - relationship_ids
            ):
                failures.append(
                    (
                        item_path,
                        "candidate_relationship_reference_unknown",
                    )
                )
            walk(item, (*path, key))

    walk(raw, ())

    if failures:
        raise L1ProposalSchemaRepairError(
            attempt_count=attempt_count,
            validation_failures=tuple(dict.fromkeys(failures)),
        )


def _repair_zero_supported_routes(
        *,
        preflight: L1Preflight,
        candidates: DomainProposalCandidatesV2,
        client: Any,
        initial_audit: L1ZeroRouteAudit,
) -> DomainProposalCandidatesV2:
        from .selection import _enumerate_paths, eligible_relationship_vocabulary

        type_ids = {
            item.proposed_type.type_id
            for item in candidates.semantic_type_candidates
            if item.score.ip_governance_eligible
            and item.score.ambiguity_conflict_penalty == 0
        }
        relationships, _aliases, _groups = eligible_relationship_vocabulary(
            candidates.relationship_candidates,
            eligible_type_ids=type_ids,
        )
        if not type_ids or not relationships:
            raise L1ZeroSupportedRoutesError(
                _zero_route_audit(
                    preflight=preflight,
                    candidates=candidates,
                    model_call_count=1,
                    reason_code="proposal_vocabulary_empty",
                )
            )
        existing_routes = {
            route.question_id: route for route in candidates.question_routes
        }
        valid_route_ids = {
            route.question_id
            for route in candidates.question_routes
            if route.start_type_id is not None
            and _enumerate_paths(route, relationships, max_hops=4)
        }
        ordered_questions = [
            {"question_id": item.id, "question": item.question}
            for item in preflight.intake.competency_questions
            if item.business_critical and item.id not in valid_route_ids
        ]
        diagnostic_by_id = dict(
            zip(
                initial_audit.question_ids,
                initial_audit.unsupported_reason_codes,
                strict=True,
            )
        )
        try:
            route_response = client.complete_json(
                system=(
                    "Return only question_routes using the exact ordered question IDs "
                    "and exact proposed type IDs supplied. Do not add or alter types, "
                    "relationships, evidence, scores, or question order. Use endpoints "
                    "only when the supplied relationships form a path; otherwise return "
                    "both endpoints null with a non-empty unsupported_reason."
                ),
                user=canonical_json(
                    {
                        "ordered_competency_questions": ordered_questions,
                        "initial_route_diagnostics": [
                            {
                                "question_id": question["question_id"],
                                "reason_code": diagnostic_by_id[
                                    question["question_id"]
                                ],
                            }
                            for question in ordered_questions
                        ],
                        "proposed_type_ids": sorted(type_ids),
                        "proposed_relationships": [
                            {
                                "relationship_type_id": item.relationship_type_id,
                                "source_type_ids": list(item.source_type_ids),
                                "target_type_ids": list(item.target_type_ids),
                                "endpoint_policy": item.endpoint_policy,
                                "competency_question_ids": list(
                                    item.competency_question_ids
                                ),
                            }
                            for item in relationships
                        ],
                    }
                ),
                json_schema=QuestionRouteRepairV2.model_json_schema(),
            )
        except Exception as exc:
            raise L1ZeroSupportedRoutesError(
                _zero_route_audit(
                    preflight=preflight,
                    candidates=candidates,
                    model_call_count=2,
                    reason_code="route_patch_provider_failure",
                )
            ) from exc
        try:
            repaired = QuestionRouteRepairV2.model_validate(route_response)
        except ValidationError as exc:
            raise L1ZeroSupportedRoutesError(
                _zero_route_audit(
                    preflight=preflight,
                    candidates=candidates,
                    model_call_count=2,
                    reason_code="route_patch_schema_invalid",
                )
            ) from exc
        expected_ids = [item["question_id"] for item in ordered_questions]
        actual_ids = [item.question_id for item in repaired.question_routes]
        if (
            actual_ids != expected_ids
            or len(actual_ids) != len(set(actual_ids))
        ):
            raise L1ZeroSupportedRoutesError(
                _zero_route_audit(
                    preflight=preflight,
                    candidates=candidates,
                    model_call_count=2,
                    reason_code="route_patch_question_ids_invalid",
                )
            )
        repaired_routes = dict(existing_routes)
        for patch in repaired.question_routes:
            if patch.source_type_id is not None:
                if (
                    patch.source_type_id not in type_ids
                    or patch.target_type_id not in type_ids
                ):
                    raise L1ZeroSupportedRoutesError(
                        _zero_route_audit(
                            preflight=preflight,
                            candidates=candidates,
                            model_call_count=2,
                            reason_code="route_patch_type_id_unknown",
                        )
                    )
                route = ProposalQuestionRouteV2(
                    question_id=patch.question_id,
                    start_type_id=patch.source_type_id,
                    end_type_id=patch.target_type_id,
                    unsupported_reason=None,
                )
            else:
                route = ProposalQuestionRouteV2(
                    question_id=patch.question_id,
                    start_type_id=None,
                    end_type_id=None,
                    unsupported_reason=patch.unsupported_reason,
                )
            repaired_routes[route.question_id] = route
        routes = [
            repaired_routes[item.id]
            for item in preflight.intake.competency_questions
        ]
        repaired_candidates = candidates.model_copy(
            update={"question_routes": tuple(routes)}
        )
        critical_ids = {
            item.id
            for item in preflight.intake.competency_questions
            if item.business_critical
        }
        supported_critical_ids: set[str] = set()
        for route in repaired_candidates.question_routes:
            if route.question_id not in critical_ids:
                continue
            if route.start_type_id is None:
                continue
            if not _enumerate_paths(route, relationships, max_hops=4):
                raise L1ZeroSupportedRoutesError(
                    _zero_route_audit(
                        preflight=preflight,
                        candidates=repaired_candidates,
                        model_call_count=2,
                        reason_code="route_patch_path_unavailable",
                    )
                )
            supported_critical_ids.add(route.question_id)
        if supported_critical_ids != critical_ids:
            raise L1ZeroSupportedRoutesError(
                _zero_route_audit(
                    preflight=preflight,
                    candidates=repaired_candidates,
                    model_call_count=2,
                    reason_code="route_patch_critical_coverage_incomplete",
                )
            )
        return repaired_candidates


def _require_reason_only_route_repair(
    original: dict[str, Any],
    repaired: dict[str, Any],
) -> None:
    original_copy = json.loads(json.dumps(original))
    repaired_copy = json.loads(json.dumps(repaired))
    original_routes = original_copy.get("question_routes")
    repaired_routes = repaired_copy.get("question_routes")
    if not isinstance(original_routes, list) or not isinstance(repaired_routes, list):
        raise L1ProposalSchemaRepairError(
            attempt_count=2,
            validation_error_codes=("question_routes.invalid",),
        )
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("question_id"), str)
        or not item.get("question_id")
        for item in [*original_routes, *repaired_routes]
    ):
        raise L1ProposalSchemaRepairError(
            attempt_count=2,
            validation_error_codes=("question_routes.invalid_id",),
        )
    original_by_id = {
        item.get("question_id"): item
        for item in original_routes
        if isinstance(item, dict)
    }
    original_ids = [
        item.get("question_id")
        for item in original_routes
        if isinstance(item, dict)
    ]
    repaired_ids = [
        item.get("question_id")
        for item in repaired_routes
        if isinstance(item, dict)
    ]
    if (
        repaired_ids != original_ids
        or len(original_ids) != len(set(original_ids))
        or len(repaired_ids) != len(set(repaired_ids))
    ):
        raise L1ProposalSchemaRepairError(
            attempt_count=2,
            validation_error_codes=("question_routes.authority_drift",),
        )
    for route in repaired_routes:
        if not isinstance(route, dict):
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_error_codes=("question_routes.invalid",),
            )
        prior = original_by_id.get(route.get("question_id"))
        if not isinstance(prior, dict):
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_error_codes=("question_routes.authority_drift",),
            )
        candidate = dict(route)
        prior_candidate = dict(prior)
        if (
            prior.get("start_type_id") is None
            and prior.get("end_type_id") is None
            and prior.get("unsupported_reason") in (None, "")
        ):
            reason = candidate.pop("unsupported_reason", None)
            prior_candidate.pop("unsupported_reason", None)
            if not isinstance(reason, str) or not reason.strip():
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_error_codes=("unsupported_reason.missing",),
                )
        elif (
            prior.get("start_type_id") is not None
            and prior.get("end_type_id") is not None
            and "unsupported_reason" in prior
        ):
            repaired_reason = candidate.pop("unsupported_reason", None)
            prior_candidate.pop("unsupported_reason", None)
            if repaired_reason not in (None, ""):
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_error_codes=("unsupported_reason.unexpected",),
                )
        if candidate != prior_candidate:
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_error_codes=("question_routes.authority_drift",),
            )
    original_copy["question_routes"] = []
    repaired_copy["question_routes"] = []
    if original_copy != repaired_copy or len(repaired_routes) != len(original_routes):
        raise L1ProposalSchemaRepairError(
            attempt_count=2,
            validation_error_codes=("proposal.authority_drift",),
        )


@dataclass(frozen=True)
class L1Preflight:
    source_path: Path
    run_id: str
    base_identity: CanonicalIdentityEnvelope
    intake: DomainIntake
    corpus: SourceCorpusManifest
    input_manifest: ArtifactManifest
    budget: DesignSamplingBudget
    model_version: str
    model_hash: str


@dataclass(frozen=True)
class L1PreparedStage:
    preflight: L1Preflight
    sample_manifest: DesignSampleManifest
    source_profile: DomainSourceProfile
    source_units: tuple[SourceUnit, ...]
    evidence_spans: tuple[EvidenceSpan, ...]
    candidates: DomainProposalCandidatesV2
    design_context: DomainDesignContext
    proposal: DomainProposal
    summary: str
    summary_hash: str
    started_at_utc: datetime
    model_call_count: int


@dataclass(frozen=True)
class L1StageResult:
    status: Literal["succeeded", "blocked", "skipped"]
    receipt: StageReceipt | None
    output_manifest: ArtifactManifest | None
    approval_context: DomainApprovalContext | None
    contract: DomainContractV2 | None
    summary: str
    planned_paths: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _artifact_seed(project_id: str, run_id: str) -> dict[str, str]:
    return {"project_id": project_id, "run_id": run_id, "stage_id": "L1"}


def make_l1_identity(
    *,
    project_id: str,
    run_id: str,
    domain_contract_hash: str = "0" * 64,
) -> CanonicalIdentityEnvelope:
    """Create the shared L1 identity envelope without claiming source authority."""
    seed = _artifact_seed(project_id, run_id)
    content_hash = canonical_sha256(seed)
    asset_id = deterministic_contract_id("l1-run-asset", seed)
    asset_version_id = deterministic_contract_id(
        "l1-run-asset-version",
        {"asset_id": asset_id, "content_hash": content_hash},
    )
    source_file_id = deterministic_contract_id(
        "l1-run-source", {"asset_version_id": asset_version_id}
    )
    return CanonicalIdentityEnvelope(
        contract_kind="l1.stage",
        project_id=project_id,
        asset_id=asset_id,
        asset_version_id=asset_version_id,
        run_id=run_id,
        source_file_id=source_file_id,
        source_unit_id=None,
        content_hash=content_hash,
        domain_schema_version="2.0",
        domain_contract_hash=domain_contract_hash,
        semantic_contract_hash=None,
        canonical_schema_version="c0-core/1.0.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name=None,
        extractor_version=None,
        parent_artifact_ids=(),
        parent_record_ids=(),
        immutable_locator=None,
    )


def _question_records(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise L1StageError("competency_questions must be a list")
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            questions.append(
                {
                    "id": f"cq:q{index}",
                    "question": item,
                    "business_critical": True,
                }
            )
        elif isinstance(item, dict):
            record = dict(item)
            record.setdefault("id", f"cq:q{index}")
            record.setdefault("business_critical", True)
            questions.append(record)
        else:
            raise L1StageError("competency questions must be strings or objects")
    return questions


def seal_domain_intake(
    raw: dict[str, Any],
    *,
    identity: CanonicalIdentityEnvelope,
) -> DomainIntake:
    """Normalize a user YAML/JSON draft and seal its deterministic identity."""
    forbidden = {"identity", "domain_intake_id", "intake_hash", "contract_version"}
    supplied = forbidden & set(raw)
    if supplied:
        raise L1StageError(
            f"intake automation must not supply authority fields: {sorted(supplied)}"
        )
    values = {
        "contract_version": "1.0.0",
        "business_goal": raw.get("business_goal"),
        "organization_context": raw.get("organization_context"),
        "users": tuple(raw.get("users", ())),
        "decisions": tuple(raw.get("decisions", ())),
        "desired_outcomes": tuple(raw.get("desired_outcomes", ())),
        "in_scope": tuple(raw.get("in_scope", ())),
        "out_of_scope": tuple(raw.get("out_of_scope", ())),
        "competency_questions": tuple(
            _question_records(raw.get("competency_questions", []))
        ),
        "terminology": tuple(raw.get("terminology", ())),
        "examples": tuple(raw.get("examples", ())),
        "temporal_constraints": tuple(raw.get("temporal_constraints", ())),
        "regulatory_constraints": tuple(raw.get("regulatory_constraints", ())),
        "privacy_constraints": tuple(raw.get("privacy_constraints", ())),
        "safety_constraints": tuple(raw.get("safety_constraints", ())),
        "structural_completeness_expectations": tuple(
            raw.get("structural_completeness_expectations", ())
        ),
    }
    try:
        normalized_values = DomainIntake.normalize_content(
            values,
            identity=identity,
        )
    except ValidationError as exc:
        raise L1StageError(f"invalid L1 domain intake: {exc}") from exc
    intake_hash = canonical_sha256(normalized_values)
    intake_id = deterministic_contract_id(
        "domain-intake", {"intake_hash": intake_hash}
    )
    intake_identity = identity.model_copy(
        update={
            "contract_kind": "l1.domain_intake",
            "content_hash": intake_hash,
        }
    )
    try:
        return DomainIntake(
            identity=intake_identity,
            domain_intake_id=intake_id,
            **normalized_values,
            intake_hash=intake_hash,
        )
    except ValidationError as exc:
        raise L1StageError(f"invalid L1 domain intake: {exc}") from exc


def _schema_hash(model_type: type[ContractModel]) -> str:
    return canonical_sha256(model_type.model_json_schema())


def _artifact_entry(
    *,
    artifact_id: str,
    contract_kind: str,
    contract_version: str,
    schema_hash: str,
    content_hash: str,
    byte_count: int,
    row_count: int | None = 1,
    media_type: str = "application/json",
) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_id=artifact_id,
        contract_kind=contract_kind,
        contract_version=contract_version,
        schema_hash=schema_hash,
        content_hash=content_hash,
        canonical_id_set_hash=None,
        row_count=row_count,
        byte_count=byte_count,
        partition_count=1,
        media_type=media_type,
        immutable_locator=None,
        blob_asset_ref_id=None,
    )


def _model_size(model: ContractModel) -> int:
    return len((canonical_json(model) + "\n").encode("utf-8"))


def _build_manifest(
    *,
    entries: list[ArtifactEntry],
    identity: CanonicalIdentityEnvelope,
    label: str,
) -> ArtifactManifest:
    ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
    identity_hash = canonical_sha256(
        [item.model_dump(mode="json") for item in ordered]
    )
    manifest_identity = identity.model_copy(
        update={
            "contract_kind": "c0.artifact_manifest",
            "content_hash": identity_hash,
        }
    )
    seed = {
        "identity": manifest_identity,
        "entries": ordered,
        "total_row_count": sum(
            item.row_count for item in ordered if item.row_count is not None
        ),
        "total_byte_count": sum(item.byte_count for item in ordered),
    }
    provisional_id = deterministic_contract_id(
        "artifact-manifest",
        {"label": label, "entries_hash": identity_hash},
    )
    values = {
        **seed,
        "artifact_manifest_id": provisional_id,
    }
    manifest_hash = canonical_sha256(values)
    return ArtifactManifest(**values, manifest_hash=manifest_hash)


def _input_manifest(
    *,
    intake: DomainIntake,
    corpus: SourceCorpusManifest,
    identity: CanonicalIdentityEnvelope,
) -> ArtifactManifest:
    entries = [
        _artifact_entry(
            artifact_id=intake.domain_intake_id,
            contract_kind="l1.domain_intake",
            contract_version="1.0.0",
            schema_hash=_schema_hash(DomainIntake),
            content_hash=intake.intake_hash,
            byte_count=_model_size(intake),
        ),
        _artifact_entry(
            artifact_id=corpus.source_corpus_manifest_id,
            contract_kind="l1.source_corpus_manifest",
            contract_version="1.0.0",
            schema_hash=_schema_hash(SourceCorpusManifest),
            content_hash=corpus.corpus_hash,
            byte_count=_model_size(corpus),
            row_count=corpus.total_entry_count,
        ),
    ]
    source_schema_hash = canonical_sha256(
        {"contract_kind": "source.original_bytes", "version": "1.0.0"}
    )
    for item in corpus.entries:
        entries.append(
            _artifact_entry(
                artifact_id=item.source_file_id,
                contract_kind="source.original_bytes",
                contract_version="1.0.0",
                schema_hash=source_schema_hash,
                content_hash=item.original_byte_hash,
                byte_count=item.byte_count,
                row_count=None,
                media_type=item.media_type,
            )
        )
    return _build_manifest(entries=entries, identity=identity, label="l1-input")


def load_source_corpus_manifest(path: Path | str) -> SourceCorpusManifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return SourceCorpusManifest.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise L1StageError(f"invalid source corpus manifest: {exc}") from exc


def preflight_l1_inputs(
    *,
    source_path: Path,
    intake_raw: dict[str, Any],
    project_id: str,
    run_id: str,
    model_version: str,
    model_hash: str,
    budget: DesignSamplingBudget | None = None,
    source_corpus_manifest_path: Path | None = None,
) -> L1Preflight:
    """Inventory all source bytes and validate automation input without writes."""
    source_path = source_path.resolve()
    base_identity = make_l1_identity(project_id=project_id, run_id=run_id)
    intake = seal_domain_intake(intake_raw, identity=base_identity)
    if source_corpus_manifest_path is None:
        corpus_root_id = deterministic_contract_id(
            "corpus-root",
            {"project_id": project_id, "declared_root": source_path.name},
        )
        corpus = build_source_corpus_manifest(
            source_path,
            corpus_root_id=corpus_root_id,
            identity=base_identity,
        )
    else:
        corpus = load_source_corpus_manifest(source_corpus_manifest_path)
        validate_corpus_manifest_against_source(
            corpus,
            source_path,
            identity=base_identity,
        )
    selected_budget = budget or DesignSamplingBudget.default()
    return L1Preflight(
        source_path=source_path,
        run_id=run_id,
        base_identity=base_identity,
        intake=intake,
        corpus=corpus,
        input_manifest=_input_manifest(
            intake=intake,
            corpus=corpus,
            identity=base_identity,
        ),
        budget=selected_budget,
        model_version=model_version,
        model_hash=model_hash,
    )


def _selector_hash() -> str:
    return canonical_sha256(
        {
            "selector_version": SELECTOR_VERSION,
            "policy": (
                "minimum-cq-path-union-plus-mandatory-relationships;"
                "n-advisory-8-20-hard-24;k-shortest-max-4"
            ),
        }
    )


def _build_design_context(
    *,
    preflight: L1Preflight,
    sample_manifest: DesignSampleManifest,
    source_profile: DomainSourceProfile,
    source_units: tuple[SourceUnit, ...],
    evidence_spans: tuple[EvidenceSpan, ...],
    draft_contract: DomainContractV2,
    parent_correction_context_id: str | None,
) -> DomainDesignContext:
    domain_hash = compute_contract_hash(draft_contract)
    values = {
        "contract_version": "1.0.0",
        "domain_intake_id": preflight.intake.domain_intake_id,
        "domain_intake_hash": preflight.intake.intake_hash,
        "source_profile_id": source_profile.domain_source_profile_id,
        "source_profile_hash": source_profile.profile_hash,
        "input_manifest_id": preflight.input_manifest.artifact_manifest_id,
        "input_manifest_hash": preflight.input_manifest.manifest_hash,
        "source_corpus_manifest_id": preflight.corpus.source_corpus_manifest_id,
        "source_corpus_manifest_hash": preflight.corpus.corpus_hash,
        "design_sample_manifest_id": sample_manifest.design_sample_manifest_id,
        "design_sample_manifest_hash": sample_manifest.sample_hash,
        "competency_question_ids": tuple(
            sorted(item.id for item in preflight.intake.competency_questions)
        ),
        "completeness_requirement_ids": tuple(
            sorted(
                item.requirement_id
                for item in draft_contract.completeness_requirements
            )
        ),
        "completeness_requirement_hash": (
            draft_contract.completeness_requirement_hash
        ),
        "hierarchy_hash": draft_contract.hierarchy_closure.hierarchy_hash,
        "identity_policy_hash": draft_contract.identity_policy_hash,
        "source_unit_ids": tuple(
            sorted(item.source_unit_id for item in source_units)
        ),
        "source_unit_content_hash": canonical_sha256(
            [
                item.model_dump(mode="json")
                for item in sorted(
                    source_units, key=lambda value: value.source_unit_id
                )
            ]
        ),
        "evidence_span_ids": tuple(
            sorted(item.evidence_span_id for item in evidence_spans)
        ),
        "evidence_span_content_hash": canonical_sha256(
            [
                item.model_dump(mode="json")
                for item in sorted(
                    evidence_spans,
                    key=lambda value: value.evidence_span_id,
                )
            ]
        ),
        "prompt_version": DOMAIN_PROPOSAL_PROMPT_VERSION,
        "prompt_hash": DOMAIN_PROPOSAL_PROMPT_HASH,
        "model_version": preflight.model_version,
        "model_hash": preflight.model_hash,
        "selector_version": SELECTOR_VERSION,
        "selector_hash": _selector_hash(),
        "scorer_version": SCORER_VERSION,
        "scorer_hash": SCORER_HASH,
        "budget_snapshot_hash": preflight.budget.budget_snapshot_hash,
        "parent_correction_context_id": parent_correction_context_id,
    }
    context_hash = canonical_sha256(values)
    context_id = deterministic_contract_id(
        "domain-design-context", {"design_context_hash": context_hash}
    )
    identity = preflight.base_identity.model_copy(
        update={
            "contract_kind": "l1.domain_design_context",
            "content_hash": context_hash,
            "domain_contract_hash": domain_hash,
            "prompt_version": DOMAIN_PROPOSAL_PROMPT_VERSION,
            "prompt_hash": DOMAIN_PROPOSAL_PROMPT_HASH,
            "model_version": preflight.model_version,
            "model_hash": preflight.model_hash,
            "parent_artifact_ids": tuple(
                item
                for item in (
                    preflight.intake.domain_intake_id,
                    source_profile.domain_source_profile_id,
                    preflight.input_manifest.artifact_manifest_id,
                    preflight.corpus.source_corpus_manifest_id,
                    sample_manifest.design_sample_manifest_id,
                    parent_correction_context_id,
                )
                if item is not None
            ),
        }
    )
    return DomainDesignContext(
        identity=identity,
        domain_design_context_id=context_id,
        **values,
        design_context_hash=context_hash,
    )


def _evidence_payload(spans: tuple[EvidenceSpan, ...]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_span_id": item.evidence_span_id,
            "source_unit_id": item.source_unit_id,
            "source_file_id": item.source_file_id,
            "quote": item.quote,
            "locator": item.locator.model_dump(mode="json"),
            "quote_hash": item.quote_hash,
        }
        for item in spans
    ]


def prepare_l1_stage(
    preflight: L1Preflight,
    *,
    candidates: DomainProposalCandidatesV2 | dict[str, Any] | None = None,
    client: Any = None,
    correction_instruction: str | None = None,
    parent_correction_context_id: str | None = None,
    started_at_utc: datetime | None = None,
) -> L1PreparedStage:
    """Build a complete proposal in memory; this function never persists artifacts."""
    started = started_at_utc or _utc_now()
    sample_manifest, profile, source_units, evidence_spans = (
        build_l1_design_artifacts(
            preflight.source_path,
            corpus=preflight.corpus,
            base_identity=preflight.base_identity,
            verified_at_utc=started,
            budget=preflight.budget,
        )
    )
    model_call_count = 0
    candidate_regeneration_attempted = False
    rejected_candidate_diagnostics: dict[str, Any] | None = None
    route_repair_attempted = False
    trusted_question_ids = tuple(
        item.id for item in preflight.intake.competency_questions
    )
    proposal_user_message = build_proposal_user_message(
        preflight.intake,
        source_profile_summary=profile.model_dump(
            mode="json", exclude={"identity"}
        ),
        verified_design_evidence=_evidence_payload(evidence_spans),
        correction_instruction=correction_instruction,
    )
    if candidates is None:
        if client is None:
            raise L1StageError("proposal candidates or a Foundry client are required")
        raw = client.complete_json(
            system=DOMAIN_PROPOSAL_SYSTEM_PROMPT,
            user=proposal_user_message,
            json_schema=domain_proposal_candidates_schema(),
            max_completion_tokens=16_000,
            max_attempts=1,
        )
        model_call_count = 1
        candidates = raw
    if model_call_count == 1 and not isinstance(candidates, dict):
        root_failure = (("proposal.root", "proposal_root_not_object"),)
        first_diagnostics = _raw_candidate_diagnostics(
            candidates,
            attempt=1,
            failures=root_failure,
        )
        retry_feedback = {
            "reason_code": "provider_candidate_validation_failed",
            "validation_codes": ["proposal_root_not_object"],
            "proposed_type_count": 0,
            "proposed_relationship_count": 0,
        }
        try:
            second_raw = client.complete_json(
                system=(
                    DOMAIN_PROPOSAL_SYSTEM_PROMPT
                    + "\nTrusted local candidate-regeneration feedback:\n"
                    + canonical_json(retry_feedback)
                ),
                user=proposal_user_message,
                json_schema=domain_proposal_candidates_schema(),
                max_completion_tokens=16_000,
                max_attempts=1,
            )
        except Exception as exc:
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_failures=(
                    ("proposal.provider", "provider_retry_failed"),
                ),
                candidate_attempts=(
                    first_diagnostics,
                    _raw_candidate_diagnostics(
                        None,
                        attempt=2,
                        failures=(
                            ("proposal.provider", "provider_retry_failed"),
                        ),
                    ),
                ),
            ) from exc
        if not isinstance(second_raw, dict):
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_failures=root_failure,
                candidate_attempts=(
                    first_diagnostics,
                    _raw_candidate_diagnostics(
                        second_raw,
                        attempt=2,
                        failures=root_failure,
                    ),
                ),
            )
        _assert_raw_candidate_authority(
            second_raw,
            trusted_question_ids=trusted_question_ids,
            trusted_evidence_ids={
                item.evidence_span_id for item in evidence_spans
            },
            attempt_count=2,
        )
        try:
            candidates = _validate_proposal_candidate(
                second_raw,
                trusted_question_ids=trusted_question_ids,
                attempt_count=2,
            )
        except L1ProposalSchemaRepairError as exc:
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_failures=exc.validation_failures,
                candidate_attempts=(
                    first_diagnostics,
                    _raw_candidate_diagnostics(
                        second_raw,
                        attempt=2,
                        failures=exc.validation_failures,
                    ),
                ),
            ) from exc
        rejected_candidate_diagnostics = first_diagnostics
        candidate_regeneration_attempted = True
        model_call_count = 2
    if isinstance(candidates, dict):
        first_raw = candidates
        if model_call_count == 1:
            _assert_raw_candidate_authority(
                first_raw,
                trusted_question_ids=trusted_question_ids,
                trusted_evidence_ids={
                    item.evidence_span_id for item in evidence_spans
                },
                attempt_count=1,
            )
        try:
            candidates = _validate_proposal_candidate(
                first_raw,
                trusted_question_ids=trusted_question_ids,
                attempt_count=model_call_count or 1,
            )
        except L1ProposalSchemaRepairError as first_error:
            if client is None or model_call_count != 1:
                raise
            if any(
                _is_authority_validation_failure(code)
                for _path, code in first_error.validation_failures
            ):
                raise
            first_diagnostics = _raw_candidate_diagnostics(
                first_raw,
                attempt=1,
                failures=first_error.validation_failures,
            )
            rejected_candidate_diagnostics = first_diagnostics
            retry_feedback = {
                "reason_code": "provider_candidate_validation_failed",
                "validation_codes": sorted(
                    {
                        code
                        for _path, code in first_error.validation_failures
                    }
                ),
                "proposed_type_count": first_diagnostics[
                    "proposed_type_count"
                ],
                "proposed_relationship_count": first_diagnostics[
                    "proposed_relationship_count"
                ],
            }
            candidate_regeneration_attempted = True
            model_call_count = 2
            try:
                second_raw = client.complete_json(
                    system=(
                        DOMAIN_PROPOSAL_SYSTEM_PROMPT
                        + "\nTrusted local candidate-regeneration feedback:\n"
                        + canonical_json(retry_feedback)
                    ),
                    user=proposal_user_message,
                    json_schema=domain_proposal_candidates_schema(),
                    max_completion_tokens=16_000,
                    max_attempts=1,
                )
            except Exception as exc:
                second_failure = (
                    ("proposal.provider", "provider_retry_failed"),
                )
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=second_failure,
                    candidate_attempts=(
                        first_diagnostics,
                        _raw_candidate_diagnostics(
                            None,
                            attempt=2,
                            failures=second_failure,
                        ),
                    ),
                ) from exc
            if not isinstance(second_raw, dict):
                second_failure = (
                    ("proposal.root", "proposal_root_not_object"),
                )
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=second_failure,
                    candidate_attempts=(
                        first_diagnostics,
                        _raw_candidate_diagnostics(
                            second_raw,
                            attempt=2,
                            failures=second_failure,
                        ),
                    ),
                )
            try:
                _assert_raw_candidate_authority(
                    second_raw,
                    trusted_question_ids=trusted_question_ids,
                    trusted_evidence_ids={
                        item.evidence_span_id for item in evidence_spans
                    },
                    attempt_count=2,
                )
                candidates = _validate_proposal_candidate(
                    second_raw,
                    trusted_question_ids=trusted_question_ids,
                    attempt_count=2,
                )
            except L1ProposalSchemaRepairError as second_error:
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=second_error.validation_failures,
                    candidate_attempts=(
                        first_diagnostics,
                        _raw_candidate_diagnostics(
                            second_raw,
                            attempt=2,
                            failures=second_error.validation_failures,
                        ),
                    ),
                ) from second_error
    _assert_candidate_authority(
        candidates,
        trusted_question_ids=trusted_question_ids,
        trusted_evidence_ids={
            item.evidence_span_id for item in evidence_spans
        },
        attempt_count=model_call_count or 1,
    )
    initial_route_audit = _zero_route_audit(
        preflight=preflight,
        candidates=candidates,
        model_call_count=1,
        reason_code="initial_route_diagnostics",
        route_repair_attempted=False,
    )
    first_route_audit = initial_route_audit
    candidate_attempt_hashes = (
        (
            str(rejected_candidate_diagnostics["candidate_hash"]),
            initial_route_audit.candidate_hash,
        )
        if rejected_candidate_diagnostics is not None
        else (initial_route_audit.candidate_hash,)
    )
    candidate_attempt_type_counts = (
        (
            int(rejected_candidate_diagnostics["proposed_type_count"]),
            initial_route_audit.proposed_type_count,
        )
        if rejected_candidate_diagnostics is not None
        else (initial_route_audit.proposed_type_count,)
    )
    candidate_attempt_relationship_counts = (
        (
            int(
                rejected_candidate_diagnostics[
                    "proposed_relationship_count"
                ]
            ),
            initial_route_audit.proposed_relationship_count,
        )
        if rejected_candidate_diagnostics is not None
        else (initial_route_audit.proposed_relationship_count,)
    )
    if (
        client is not None
        and model_call_count == 1
        and initial_route_audit.critical_coverable_question_count
        < initial_route_audit.critical_question_count
    ):
        retry_feedback = {
            "reason_code": "minimum_viable_vocabulary_insufficient",
            "proposed_type_count": initial_route_audit.proposed_type_count,
            "proposed_relationship_count": (
                initial_route_audit.proposed_relationship_count
            ),
            "eligible_type_count": initial_route_audit.eligible_type_count,
            "eligible_relationship_count": (
                initial_route_audit.eligible_relationship_count
            ),
            "supported_route_count": (
                initial_route_audit.supported_route_count
            ),
            "coverable_question_count": (
                initial_route_audit.coverable_question_count
            ),
            "critical_question_count": (
                initial_route_audit.critical_question_count
            ),
            "critical_coverable_question_count": (
                initial_route_audit.critical_coverable_question_count
            ),
            "uncovered_critical_question_ids": (
                _uncoverable_critical_question_ids(
                    preflight,
                    candidates,
                )
            ),
        }
        candidate_regeneration_attempted = True
        model_call_count = 2
        try:
            second_raw = client.complete_json(
                system=(
                    DOMAIN_PROPOSAL_SYSTEM_PROMPT
                    + "\nTrusted local candidate-regeneration feedback:\n"
                    + canonical_json(retry_feedback)
                ),
                user=proposal_user_message,
                json_schema=domain_proposal_candidates_schema(),
                max_completion_tokens=16_000,
                max_attempts=1,
            )
        except Exception as exc:
            raise L1ZeroSupportedRoutesError(
                initial_route_audit.model_copy(
                    update={
                        "model_call_count": 2,
                        "candidate_regeneration_attempted": True,
                        "candidate_regeneration_result_code": (
                            "candidate_regeneration_provider_failure"
                        ),
                        "terminal_error_code": (
                            "L1_ZERO_SUPPORTED_ROUTES"
                        ),
                    }
                )
            ) from exc
        if not isinstance(second_raw, dict):
            raise L1ZeroSupportedRoutesError(
                initial_route_audit.model_copy(
                    update={
                        "model_call_count": 2,
                        "candidate_regeneration_attempted": True,
                        "candidate_regeneration_result_code": (
                            "candidate_regeneration_response_invalid"
                        ),
                    }
                )
            )
        try:
            _assert_raw_candidate_authority(
                second_raw,
                trusted_question_ids=trusted_question_ids,
                trusted_evidence_ids={
                    item.evidence_span_id for item in evidence_spans
                },
                attempt_count=2,
            )
            second = _validate_proposal_candidate(
                second_raw,
                trusted_question_ids=trusted_question_ids,
                attempt_count=2,
            )
        except L1ProposalSchemaRepairError as exc:
            first_failures = tuple(
                (
                    f"question_routes.{question_id}",
                    "critical_route_or_vocabulary_incomplete",
                )
                for question_id in _uncoverable_critical_question_ids(
                    preflight,
                    candidates,
                )
            )
            raise L1ProposalSchemaRepairError(
                attempt_count=2,
                validation_failures=exc.validation_failures,
                candidate_attempts=(
                    _raw_candidate_diagnostics(
                        candidates.model_dump(mode="json"),
                        attempt=1,
                        failures=first_failures,
                    ),
                    _raw_candidate_diagnostics(
                        second_raw,
                        attempt=2,
                        failures=exc.validation_failures,
                    ),
                ),
            ) from exc
        second_audit = _zero_route_audit(
            preflight=preflight,
            candidates=second,
            model_call_count=2,
            reason_code="candidate_regeneration_diagnostics",
            route_repair_attempted=False,
            initial_route_codes=(
                first_route_audit.unsupported_reason_codes
            ),
            initial_supported_route_count=(
                first_route_audit.supported_route_count
            ),
            initial_unsupported_route_count=(
                first_route_audit.unsupported_route_count
            ),
            candidate_attempt_hashes=(
                initial_route_audit.candidate_hash,
                canonical_sha256(second),
            ),
            candidate_attempt_type_counts=(
                initial_route_audit.proposed_type_count,
                len(second.semantic_type_candidates),
            ),
            candidate_attempt_relationship_counts=(
                initial_route_audit.proposed_relationship_count,
                len(second.relationship_candidates),
            ),
            candidate_regeneration_attempted=True,
            candidate_regeneration_result_code=(
                "candidate_regeneration_validated"
            ),
        )
        candidate_attempt_hashes = second_audit.candidate_attempt_hashes
        candidate_attempt_type_counts = (
            second_audit.candidate_attempt_type_counts
        )
        candidate_attempt_relationship_counts = (
            second_audit.candidate_attempt_relationship_counts
        )
        candidates = second
        if (
            second_audit.critical_coverable_question_count
            < second_audit.critical_question_count
        ):
            raise L1ZeroSupportedRoutesError(
                second_audit.model_copy(
                    update={
                        "reason_code": "candidate_regeneration_insufficient",
                        "terminal_error_code": "L1_ZERO_SUPPORTED_ROUTES",
                        "route_repair_result_code": "not_attempted",
                        "candidate_regeneration_result_code": (
                            "candidate_regeneration_insufficient"
                        ),
                    }
                )
            )
        initial_route_audit = second_audit
    if (
        client is not None
        and model_call_count == 1
        and initial_route_audit.critical_supported_route_count
        < initial_route_audit.critical_question_count
    ):
        try:
            route_repair_attempted = True
            candidates = _repair_zero_supported_routes(
                preflight=preflight,
                candidates=candidates,
                client=client,
                initial_audit=initial_route_audit,
            )
        except L1ZeroSupportedRoutesError as exc:
            raise L1ZeroSupportedRoutesError(
                exc.audit_payload.model_copy(
                    update={
                        "initial_route_codes": (
                            initial_route_audit.unsupported_reason_codes
                        ),
                        "initial_supported_route_count": (
                            initial_route_audit.supported_route_count
                        ),
                        "initial_unsupported_route_count": (
                            initial_route_audit.unsupported_route_count
                        ),
                    }
                )
            ) from exc
        model_call_count = 2
    known_evidence_ids = {item.evidence_span_id for item in evidence_spans}
    try:
        draft_contract, merge_groups, selected_candidate_ids = (
            build_draft_contract_from_candidates(
            preflight.intake,
            candidates,
            known_evidence_span_ids=known_evidence_ids,
        )
        )
    except (ProposalSelectionError, ValidationError, ArithmeticError) as exc:
        if isinstance(exc, ProposalSelectionError):
            semantic_failures = (
                (
                    "proposal.selection",
                    next(
                        (
                            token.strip("[]")
                            for token in str(exc).split()
                            if token.startswith("[DOM-")
                        ),
                        "proposal_selection_invalid",
                    ),
                ),
            )
        else:
            semantic_failures = tuple(
                (
                    "proposal.draft_contract."
                    + (item["location"] or "root"),
                    _stable_semantic_validation_code(
                        item["message"],
                        item["type"],
                    ),
                )
                for item in _sanitized_validation_failures(exc)
            )
        if (
            client is not None
            and model_call_count == 1
            and not any(
                _is_authority_validation_failure(code)
                for _path, code in semantic_failures
            )
        ):
            first_diagnostics = _raw_candidate_diagnostics(
                candidates.model_dump(mode="json"),
                attempt=1,
                failures=semantic_failures,
            )
            feedback = {
                "reason_code": "provider_candidate_semantic_validation_failed",
                "validation_codes": sorted(
                    {code for _path, code in semantic_failures}
                ),
                "proposed_type_count": first_diagnostics[
                    "proposed_type_count"
                ],
                "proposed_relationship_count": first_diagnostics[
                    "proposed_relationship_count"
                ],
            }
            try:
                second_raw = client.complete_json(
                    system=(
                        DOMAIN_PROPOSAL_SYSTEM_PROMPT
                        + "\nTrusted local candidate-regeneration feedback:\n"
                        + canonical_json(feedback)
                    ),
                    user=proposal_user_message,
                    json_schema=domain_proposal_candidates_schema(),
                    max_completion_tokens=16_000,
                    max_attempts=1,
                )
                if not isinstance(second_raw, dict):
                    raise L1ProposalSchemaRepairError(
                        attempt_count=2,
                        validation_failures=(
                            ("proposal.root", "proposal_root_not_object"),
                        ),
                    )
                _assert_raw_candidate_authority(
                    second_raw,
                    trusted_question_ids=trusted_question_ids,
                    trusted_evidence_ids={
                        item.evidence_span_id for item in evidence_spans
                    },
                    attempt_count=2,
                )
                second = _validate_proposal_candidate(
                    second_raw,
                    trusted_question_ids=trusted_question_ids,
                    attempt_count=2,
                )
                retried = prepare_l1_stage(
                    preflight,
                    candidates=second,
                    client=None,
                    correction_instruction=correction_instruction,
                    parent_correction_context_id=parent_correction_context_id,
                    started_at_utc=started,
                )
                return replace(retried, model_call_count=2)
            except L1ZeroSupportedRoutesError as second_error:
                raise L1ZeroSupportedRoutesError(
                    second_error.audit_payload.model_copy(
                        update={
                            "model_call_count": 2,
                            "candidate_regeneration_attempted": True,
                            "candidate_regeneration_result_code": (
                                "candidate_regeneration_insufficient"
                            ),
                            "candidate_attempt_hashes": (
                                first_diagnostics["candidate_hash"],
                                canonical_sha256(second_raw),
                            ),
                        }
                    )
                ) from second_error
            except L1ProposalSchemaRepairError as second_error:
                second_failures = second_error.validation_failures
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=second_failures,
                    candidate_attempts=(
                        first_diagnostics,
                        _raw_candidate_diagnostics(
                            locals().get("second_raw"),
                            attempt=2,
                            failures=second_failures,
                        ),
                    ),
                ) from second_error
            except Exception as provider_error:
                provider_failure = (
                    ("proposal.provider", "provider_retry_failed"),
                )
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=provider_failure,
                    candidate_attempts=(
                        first_diagnostics,
                        _raw_candidate_diagnostics(
                            locals().get("second_raw"),
                            attempt=2,
                            failures=provider_failure,
                        ),
                    ),
                ) from provider_error
        if not (
            isinstance(exc, ProposalSelectionError)
            and str(exc)
            == "[DOM-104] at least one competency question must be covered"
        ):
            raise L1ProposalSchemaRepairError(
                attempt_count=2 if model_call_count >= 2 else 1,
                validation_failures=semantic_failures,
                candidate_attempts=tuple(
                    item
                    for item in (
                        rejected_candidate_diagnostics,
                        _raw_candidate_diagnostics(
                            candidates.model_dump(mode="json"),
                            attempt=2 if model_call_count >= 2 else 1,
                            failures=semantic_failures,
                        ),
                    )
                    if item is not None
                ),
            ) from exc
        raise L1ZeroSupportedRoutesError(
            _zero_route_audit(
                preflight=preflight,
                candidates=candidates,
                model_call_count=(
                    2 if model_call_count >= 2 else 1
                ),
                reason_code="selection_dom_104",
                route_repair_attempted=route_repair_attempted,
                route_repair_result_code=(
                    "route_patch_validated"
                    if route_repair_attempted
                    else "not_attempted"
                ),
                terminal_error_code="DOM-104",
                candidate_regeneration_attempted=(
                    candidate_regeneration_attempted
                ),
                candidate_regeneration_result_code=(
                    "candidate_regeneration_validated"
                    if candidate_regeneration_attempted
                    else "not_attempted"
                ),
                initial_route_codes=(
                    first_route_audit.unsupported_reason_codes
                ),
                initial_supported_route_count=(
                    first_route_audit.supported_route_count
                ),
                initial_unsupported_route_count=(
                    first_route_audit.unsupported_route_count
                ),
                candidate_attempt_hashes=candidate_attempt_hashes,
                candidate_attempt_type_counts=(
                    candidate_attempt_type_counts
                ),
                candidate_attempt_relationship_counts=(
                    candidate_attempt_relationship_counts
                ),
            )
        ) from exc
    try:
        design_context = _build_design_context(
            preflight=preflight,
            sample_manifest=sample_manifest,
            source_profile=profile,
            source_units=source_units,
            evidence_spans=evidence_spans,
            draft_contract=draft_contract,
            parent_correction_context_id=parent_correction_context_id,
        )
        proposal = build_domain_proposal(
            design_context=design_context,
            candidates=candidates,
            draft_contract=draft_contract,
            merge_groups=merge_groups,
            selected_candidate_ids=selected_candidate_ids,
            identity=design_context.identity,
        )
    except ValidationError as exc:
        proposal_failures = tuple(
            (
                "proposal."
                + (
                    ".".join(
                        "[index]" if isinstance(part, int) else str(part)
                        for part in item["loc"]
                    )
                    or "root"
                ),
                _stable_semantic_validation_code(
                    str(item["msg"]),
                    str(item["type"]),
                ),
            )
            for item in exc.errors(
                include_url=False,
                include_input=False,
            )
        )
        current_diagnostics = _raw_candidate_diagnostics(
            candidates.model_dump(mode="json"),
            attempt=2 if model_call_count >= 2 else 1,
            failures=proposal_failures,
        )
        if (
            client is not None
            and model_call_count == 1
            and not any(
                _is_authority_validation_failure(code)
                for _path, code in proposal_failures
            )
        ):
            feedback = {
                "reason_code": "provider_candidate_semantic_validation_failed",
                "validation_codes": sorted(
                    {code for _path, code in proposal_failures}
                ),
                "proposed_type_count": current_diagnostics[
                    "proposed_type_count"
                ],
                "proposed_relationship_count": current_diagnostics[
                    "proposed_relationship_count"
                ],
            }
            try:
                second_raw = client.complete_json(
                    system=(
                        DOMAIN_PROPOSAL_SYSTEM_PROMPT
                        + "\nTrusted local candidate-regeneration feedback:\n"
                        + canonical_json(feedback)
                    ),
                    user=proposal_user_message,
                    json_schema=domain_proposal_candidates_schema(),
                    max_completion_tokens=16_000,
                    max_attempts=1,
                )
                if not isinstance(second_raw, dict):
                    raise L1ProposalSchemaRepairError(
                        attempt_count=2,
                        validation_failures=(
                            ("proposal.root", "proposal_root_not_object"),
                        ),
                    )
                _assert_raw_candidate_authority(
                    second_raw,
                    trusted_question_ids=trusted_question_ids,
                    trusted_evidence_ids=known_evidence_ids,
                    attempt_count=2,
                )
                second = _validate_proposal_candidate(
                    second_raw,
                    trusted_question_ids=trusted_question_ids,
                    attempt_count=2,
                )
                return replace(
                    prepare_l1_stage(
                        preflight,
                        candidates=second,
                        client=None,
                        correction_instruction=correction_instruction,
                        parent_correction_context_id=(
                            parent_correction_context_id
                        ),
                        started_at_utc=started,
                    ),
                    model_call_count=2,
                )
            except L1ProposalSchemaRepairError as second_error:
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=second_error.validation_failures,
                    candidate_attempts=(
                        current_diagnostics,
                        _raw_candidate_diagnostics(
                            locals().get("second_raw"),
                            attempt=2,
                            failures=second_error.validation_failures,
                        ),
                    ),
                ) from second_error
            except Exception as provider_error:
                provider_failure = (
                    ("proposal.provider", "provider_retry_failed"),
                )
                raise L1ProposalSchemaRepairError(
                    attempt_count=2,
                    validation_failures=provider_failure,
                    candidate_attempts=(
                        current_diagnostics,
                        _raw_candidate_diagnostics(
                            locals().get("second_raw"),
                            attempt=2,
                            failures=provider_failure,
                        ),
                    ),
                ) from provider_error
        raise L1ProposalSchemaRepairError(
            attempt_count=2 if model_call_count >= 2 else 1,
            validation_failures=proposal_failures,
            candidate_attempts=(current_diagnostics,),
        ) from exc
    summary = render_l1_summary(
        intake=preflight.intake,
        profile=profile,
        design_context=design_context,
        proposal=proposal,
    )
    return L1PreparedStage(
        preflight=preflight,
        sample_manifest=sample_manifest,
        source_profile=profile,
        source_units=source_units,
        evidence_spans=evidence_spans,
        candidates=candidates,
        design_context=design_context,
        proposal=proposal,
        summary=summary,
        summary_hash=canonical_sha256(summary),
        started_at_utc=started,
        model_call_count=model_call_count,
    )


def render_l1_summary(
    *,
    intake: DomainIntake,
    profile: DomainSourceProfile,
    design_context: DomainDesignContext,
    proposal: DomainProposal,
) -> str:
    """Render the one complete review summary used by the terminal decision."""
    contract = proposal.draft_contract
    lines = [
        "L1 DOMAIN DESIGN SUMMARY",
        f"Domain: {contract.domain.name} — {contract.domain.description}",
        f"Scope: in={'; '.join(contract.problem.in_scope)} | out={'; '.join(contract.problem.out_of_scope)}",
        f"Users: {', '.join(contract.business.users)}",
        f"Decisions: {', '.join(contract.business.decisions)}",
        f"Outcomes: {', '.join(contract.problem.desired_outcomes)}",
        (
            "Corpus: "
            f"{profile.complete_source_count} complete entries; "
            f"{profile.eligible_source_count} eligible, "
            f"{profile.excluded_source_count} excluded, "
            f"{profile.blocked_source_count} blocked"
        ),
        "Competency questions:",
    ]
    plans = {item.question_id: item for item in contract.question_plans}
    completeness = {
        item.question_id: item
        for item in contract.completeness_question_coverage
    }
    for question in intake.competency_questions:
        plan = plans[question.id]
        coverage = completeness[question.id]
        path = " -> ".join(
            item.relationship_type_id for item in plan.required_path
        ) or "UNSUPPORTED"
        lines.append(
            f"  - {question.id}: {question.question} | path={path} | "
            f"completeness={coverage.coverage_status}"
        )
    lines.append("Semantic types:")
    for item in contract.candidate_model.entity_types:
        key_policy = (
            item.identity_key_policy.model_dump(mode="json")
            if item.identity_key_policy is not None
            else "inherited"
        )
        lines.append(
            f"  - {item.type_id} ({item.classification}) name={item.display_name}; "
            f"parent={item.parent_type_id}; abstract={item.abstract}; "
            f"identity_root={item.identity_root_type_id}; key={key_policy}"
        )
    lines.append("Relationship types:")
    for item in contract.candidate_model.relationship_types:
        lines.append(
            f"  - {item.relationship_type_id}: {item.source_type_ids} -> "
            f"{item.target_type_ids}; predicate={item.predicate_id}; "
            f"questions={item.competency_question_ids}; evidence={item.evidence_span_ids}"
        )
    lines.append(
        f"N={contract.reasoning_policy.relationship_type_count}; "
        f"K={contract.reasoning_policy.max_hops}; "
        f"N rationales={contract.reasoning_policy.retained_type_rationales}; "
        f"K4 rationales={contract.reasoning_policy.k4_rationales}"
    )
    lines.append("Completeness requirements:")
    for item in contract.completeness_requirements:
        lines.append(
            f"  - {item.requirement_id}: kind={item.requirement_kind}; "
            f"scope={item.scope_type_id}; coverage={item.coverage_status}; "
            f"roles={item.required_roles}; fact_set={item.structured_fact_set}; "
            f"evidence={item.evidence_span_ids}"
        )
    lines.append("Candidate audit:")
    for item in proposal.candidate_audit:
        lines.append(
            f"  - {item.candidate_id}: {item.disposition}; "
            f"reasons={item.reason_codes}; evidence={item.evidence_span_ids}"
        )
    lines.extend(
        [
            f"External decisions: {contract.approved_external_references}",
            f"Drift policy: {contract.drift_policy.model_dump(mode='json')}",
            f"Publication policy: {contract.publication_policy.model_dump(mode='json')}",
            f"Warnings: {proposal.warnings}; source warnings={profile.warnings}",
            f"Corpus hash: {profile.source_corpus_manifest_hash}",
            f"Sample hash: {profile.design_sample_manifest_hash}",
            f"Hierarchy hash: {contract.hierarchy_closure.hierarchy_hash}",
            f"Identity policy hash: {contract.identity_policy_hash}",
            f"Completeness hash: {contract.completeness_requirement_hash}",
            f"External reference decision hash: {contract.external_reference_decision_hash}",
            f"Design context: {design_context.domain_design_context_id} / {design_context.design_context_hash}",
            f"Proposal: {proposal.domain_proposal_id} / {proposal.proposal_hash}",
            f"Domain contract hash: {proposal.domain_contract_hash}",
        ]
    )
    def terminal_safe(value: str) -> str:
        return "".join(
            char
            if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
            else f"\\u{ord(char):04x}"
            for char in value
        )

    return "\n".join(terminal_safe(line) for line in lines)


def _approval_context(
    prepared: L1PreparedStage,
    *,
    decision: Literal["approve", "correct", "abort"],
    actor: str,
    correction_text: str | None,
    decided_at_utc: datetime,
) -> DomainApprovalContext:
    contract = prepared.proposal.draft_contract
    correction_hash = (
        canonical_sha256(correction_text)
        if correction_text is not None
        else None
    )
    values = {
        "contract_version": "1.0.0",
        "domain_design_context_id": prepared.design_context.domain_design_context_id,
        "domain_design_context_hash": prepared.design_context.design_context_hash,
        "domain_proposal_id": prepared.proposal.domain_proposal_id,
        "domain_proposal_hash": prepared.proposal.proposal_hash,
        "source_profile_id": prepared.source_profile.domain_source_profile_id,
        "source_profile_hash": prepared.source_profile.profile_hash,
        "input_manifest_id": prepared.preflight.input_manifest.artifact_manifest_id,
        "input_manifest_hash": prepared.preflight.input_manifest.manifest_hash,
        "source_corpus_manifest_id": (
            prepared.preflight.corpus.source_corpus_manifest_id
        ),
        "source_corpus_manifest_hash": prepared.preflight.corpus.corpus_hash,
        "design_sample_manifest_id": (
            prepared.sample_manifest.design_sample_manifest_id
        ),
        "design_sample_manifest_hash": prepared.sample_manifest.sample_hash,
        "domain_contract_hash": prepared.proposal.domain_contract_hash,
        "hierarchy_hash": contract.hierarchy_closure.hierarchy_hash,
        "identity_policy_hash": contract.identity_policy_hash,
        "completeness_requirement_hash": contract.completeness_requirement_hash,
        "external_reference_decision_hash": (
            contract.external_reference_decision_hash
        ),
        "summary_hash": prepared.summary_hash,
        "decision": decision,
        "actor": actor,
        "correction_text": correction_text,
        "correction_hash": correction_hash,
    }
    context_hash = canonical_sha256(
        {
            **values,
            "decided_at_utc": decided_at_utc.isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )
    context_id = deterministic_contract_id(
        "domain-approval-context", {"approval_context_hash": context_hash}
    )
    identity = prepared.design_context.identity.model_copy(
        update={
            "contract_kind": "l1.domain_approval_context",
            "content_hash": context_hash,
            "parent_artifact_ids": (
                prepared.design_context.domain_design_context_id,
                prepared.proposal.domain_proposal_id,
            ),
        }
    )
    return DomainApprovalContext(
        identity=identity,
        domain_approval_context_id=context_id,
        **values,
        decided_at_utc=decided_at_utc,
        approval_context_hash=context_hash,
    )


def _approved_contract(
    prepared: L1PreparedStage,
    approval_context: DomainApprovalContext,
    *,
    approved_at_utc: datetime,
) -> DomainContractV2:
    draft = prepared.proposal.draft_contract
    approved = draft.model_copy(
        update={
            "approval": ApprovalMetadataV2(
                status="approved",
                approved_by=approval_context.actor,
                approved_at_utc=approved_at_utc.isoformat().replace("+00:00", "Z"),
                contract_hash=prepared.proposal.domain_contract_hash,
                domain_approval_context_id=(
                    approval_context.domain_approval_context_id
                ),
                domain_approval_context_hash=approval_context.approval_context_hash,
                notes=draft.approval.notes,
            )
        }
    )
    approved = DomainContractV2.model_validate_json(
        canonical_json(approved)
    )
    if compute_contract_hash(approved) != approval_context.domain_contract_hash:
        raise L1StageError("approved contract hash differs from approval context")
    return approved


def validate_approval_bindings(
    *,
    contract: DomainContractV2,
    approval_context: DomainApprovalContext,
    proposal: DomainProposal,
    design_context: DomainDesignContext,
    profile: DomainSourceProfile,
    corpus: SourceCorpusManifest,
    sample: DesignSampleManifest,
    input_manifest: ArtifactManifest,
) -> None:
    """Prove every repeated approval/corpus/sample/domain binding by equality."""
    approval = contract.approval
    if approval_context.decision != "approve":
        raise L1StageError("only an approve decision can seal a domain contract")
    if approval.domain_approval_context_id != approval_context.domain_approval_context_id:
        raise L1StageError("contract approval context ID mismatch")
    if approval.domain_approval_context_hash != approval_context.approval_context_hash:
        raise L1StageError("contract approval context hash mismatch")
    if approval.approved_by != approval_context.actor:
        raise L1StageError("contract approval actor mismatch")
    if approval.approved_at_utc != (
        approval_context.decided_at_utc.isoformat().replace("+00:00", "Z")
    ):
        raise L1StageError("contract approval timestamp mismatch")
    if approval.contract_hash != compute_contract_hash(contract):
        raise L1StageError("approved contract authority hash mismatch")
    equalities = (
        (
            approval_context.domain_design_context_id,
            design_context.domain_design_context_id,
            "design context ID",
        ),
        (
            approval_context.domain_design_context_hash,
            design_context.design_context_hash,
            "design context hash",
        ),
        (
            approval_context.domain_proposal_id,
            proposal.domain_proposal_id,
            "proposal ID",
        ),
        (
            approval_context.domain_proposal_hash,
            proposal.proposal_hash,
            "proposal hash",
        ),
        (
            approval_context.source_profile_id,
            profile.domain_source_profile_id,
            "profile ID",
        ),
        (
            approval_context.source_profile_hash,
            profile.profile_hash,
            "profile hash",
        ),
        (
            approval_context.source_corpus_manifest_id,
            corpus.source_corpus_manifest_id,
            "corpus ID",
        ),
        (
            approval_context.source_corpus_manifest_hash,
            corpus.corpus_hash,
            "corpus hash",
        ),
        (
            approval_context.design_sample_manifest_id,
            sample.design_sample_manifest_id,
            "sample ID",
        ),
        (
            approval_context.design_sample_manifest_hash,
            sample.sample_hash,
            "sample hash",
        ),
        (
            approval_context.input_manifest_id,
            input_manifest.artifact_manifest_id,
            "input manifest ID",
        ),
        (
            approval_context.input_manifest_hash,
            input_manifest.manifest_hash,
            "input manifest hash",
        ),
        (
            approval_context.hierarchy_hash,
            contract.hierarchy_closure.hierarchy_hash,
            "hierarchy hash",
        ),
        (
            approval_context.identity_policy_hash,
            contract.identity_policy_hash,
            "identity policy hash",
        ),
        (
            approval_context.completeness_requirement_hash,
            contract.completeness_requirement_hash,
            "completeness hash",
        ),
        (
            approval_context.external_reference_decision_hash,
            contract.external_reference_decision_hash,
            "external reference decision hash",
        ),
    )
    for actual, expected, label in equalities:
        if actual != expected:
            raise L1StageError(f"{label} mismatch")
    sample.validate_subset_of(corpus)


def _output_manifest(
    prepared: L1PreparedStage,
    *,
    contract: DomainContractV2,
    approval_context: DomainApprovalContext | None,
) -> ArtifactManifest:
    domain_yaml = render_domain_contract_yaml(contract).encode("utf-8")
    entries = [
        _artifact_entry(
            artifact_id=prepared.preflight.intake.domain_intake_id,
            contract_kind="l1.domain_intake",
            contract_version="1.0.0",
            schema_hash=_schema_hash(DomainIntake),
            content_hash=prepared.preflight.intake.intake_hash,
            byte_count=_model_size(prepared.preflight.intake),
        ),
        _artifact_entry(
            artifact_id=prepared.preflight.corpus.source_corpus_manifest_id,
            contract_kind="l1.source_corpus_manifest",
            contract_version="1.0.0",
            schema_hash=_schema_hash(SourceCorpusManifest),
            content_hash=prepared.preflight.corpus.corpus_hash,
            byte_count=_model_size(prepared.preflight.corpus),
            row_count=prepared.preflight.corpus.total_entry_count,
        ),
        _artifact_entry(
            artifact_id=prepared.sample_manifest.design_sample_manifest_id,
            contract_kind="l1.design_sample_manifest",
            contract_version="1.0.0",
            schema_hash=_schema_hash(DesignSampleManifest),
            content_hash=prepared.sample_manifest.sample_hash,
            byte_count=_model_size(prepared.sample_manifest),
            row_count=len(prepared.sample_manifest.entries),
        ),
        _artifact_entry(
            artifact_id=prepared.source_profile.domain_source_profile_id,
            contract_kind="l1.domain_source_profile",
            contract_version="1.0.0",
            schema_hash=_schema_hash(DomainSourceProfile),
            content_hash=prepared.source_profile.profile_hash,
            byte_count=_model_size(prepared.source_profile),
        ),
        _artifact_entry(
            artifact_id=prepared.design_context.domain_design_context_id,
            contract_kind="l1.domain_design_context",
            contract_version="1.0.0",
            schema_hash=_schema_hash(DomainDesignContext),
            content_hash=prepared.design_context.design_context_hash,
            byte_count=_model_size(prepared.design_context),
        ),
        _artifact_entry(
            artifact_id=prepared.proposal.domain_proposal_id,
            contract_kind="l1.domain_proposal",
            contract_version="1.0.0",
            schema_hash=_schema_hash(DomainProposal),
            content_hash=prepared.proposal.proposal_hash,
            byte_count=_model_size(prepared.proposal),
        ),
        _artifact_entry(
            artifact_id="domain.contract",
            contract_kind="domain.contract",
            contract_version="2.0.0",
            schema_hash=canonical_sha256(DomainContractV2.model_json_schema()),
            content_hash=compute_contract_hash(contract),
            byte_count=len(domain_yaml),
            media_type="application/yaml",
        ),
    ]
    if approval_context is not None:
        entries.append(
            _artifact_entry(
                artifact_id=approval_context.domain_approval_context_id,
                contract_kind="l1.domain_approval_context",
                contract_version="1.0.0",
                schema_hash=_schema_hash(DomainApprovalContext),
                content_hash=approval_context.approval_context_hash,
                byte_count=_model_size(approval_context),
            )
        )
    for item in prepared.source_units:
        entries.append(
            _artifact_entry(
                artifact_id=item.source_unit_id,
                contract_kind="c0.source_unit",
                contract_version="1.0.0",
                schema_hash=_schema_hash(SourceUnit),
                content_hash=canonical_sha256(item),
                byte_count=_model_size(item),
            )
        )
    for item in prepared.evidence_spans:
        entries.append(
            _artifact_entry(
                artifact_id=item.evidence_span_id,
                contract_kind="c0.evidence_span",
                contract_version="1.0.0",
                schema_hash=_schema_hash(EvidenceSpan),
                content_hash=canonical_sha256(item),
                byte_count=_model_size(item),
            )
        )
    identity = prepared.design_context.identity.model_copy(
        update={"domain_contract_hash": compute_contract_hash(contract)}
    )
    return _build_manifest(entries=entries, identity=identity, label="l1-output")


def _skip_key(prepared: L1PreparedStage) -> str:
    contract = prepared.proposal.draft_contract
    return canonical_sha256(
        {
            "source_corpus_manifest_id": (
                prepared.preflight.corpus.source_corpus_manifest_id
            ),
            "source_corpus_manifest_hash": prepared.preflight.corpus.corpus_hash,
            "intake_hash": prepared.preflight.intake.intake_hash,
            "prompt_hash": DOMAIN_PROPOSAL_PROMPT_HASH,
            "model_hash": prepared.preflight.model_hash,
            "selector_hash": _selector_hash(),
            "scorer_hash": SCORER_HASH,
            "domain_schema_hash": canonical_sha256(
                DomainContractV2.model_json_schema()
            ),
            "l1_schema_hashes": {
                "intake": _schema_hash(DomainIntake),
                "corpus": _schema_hash(SourceCorpusManifest),
                "sample": _schema_hash(DesignSampleManifest),
                "profile": _schema_hash(DomainSourceProfile),
                "design": _schema_hash(DomainDesignContext),
                "proposal": _schema_hash(DomainProposal),
                "approval": _schema_hash(DomainApprovalContext),
            },
            "hierarchy_hash": contract.hierarchy_closure.hierarchy_hash,
            "identity_policy_hash": contract.identity_policy_hash,
            "completeness_requirement_hash": (
                contract.completeness_requirement_hash
            ),
            "external_reference_decision_hash": (
                contract.external_reference_decision_hash
            ),
            "budget_snapshot_hash": (
                prepared.preflight.budget.budget_snapshot_hash
            ),
        }
    )


def _resource_metrics(
    prepared: L1PreparedStage,
    *,
    identity: CanonicalIdentityEnvelope,
    completed_at_utc: datetime,
    storage_write_bytes: int,
    status: str,
) -> StageResourceMetrics:
    elapsed = max(
        0,
        int(
            (completed_at_utc - prepared.started_at_utc).total_seconds()
            * 1000
        ),
    )
    usage = process_resource_usage()
    metrics_id = deterministic_contract_id(
        "stage-resource-metrics",
        {
            "run_id": prepared.preflight.run_id,
            "stage_id": "L1",
            "status": status,
            "proposal_hash": prepared.proposal.proposal_hash,
        },
    )
    values = {
        "identity": identity.model_copy(
            update={"contract_kind": "c0.stage_resource_metrics"}
        ),
        "resource_metrics_id": metrics_id,
        "stage_id": "L1",
        "stage_name": L1_STAGE_NAME,
        "wall_ms": elapsed,
        "cpu_ms": max(0, int(usage.cpu_seconds * 1000)),
        "peak_rss_bytes": usage.peak_rss_bytes,
        "storage_read_bytes": prepared.preflight.corpus.total_byte_count,
        "storage_write_bytes": storage_write_bytes,
        "network_request_bytes": 0,
        "network_response_bytes": 0,
        "source_units_read": len(prepared.source_units),
        "source_units_written": len(prepared.source_units),
        "source_units_skipped": 0,
        "document_intelligence_calls": 0,
        "document_intelligence_pages": 0,
        "foundry_calls": prepared.model_call_count,
        "foundry_input_tokens": 0,
        "foundry_output_tokens": 0,
        "embedding_calls": 0,
        "embedding_items": 0,
        "fabric_calls": 0,
        "fabric_rows_read": 0,
        "fabric_rows_written": 0,
        "search_calls": 0,
        "search_documents_read": 0,
        "search_documents_written": 0,
        "retry_count": 0,
        "retry_wait_ms": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "max_observed_concurrency": 1,
        "budget_snapshot_hash": prepared.preflight.budget.budget_snapshot_hash,
        "exceeded_dimensions": (),
    }
    metrics_hash = canonical_sha256(values)
    return StageResourceMetrics(**values, metrics_hash=metrics_hash)


def _receipt(
    prepared: L1PreparedStage,
    *,
    status: Literal["succeeded", "blocked", "skipped"],
    output_manifest: ArtifactManifest | None,
    metrics: StageResourceMetrics,
    completed_at_utc: datetime,
    error_codes: tuple[str, ...] = (),
    prior_skip_key: str | None = None,
) -> StageReceipt:
    skip_key = prior_skip_key or _skip_key(prepared)
    receipt_id = deterministic_contract_id(
        "stage-receipt",
        {
            "run_id": prepared.preflight.run_id,
            "stage_id": "L1",
            "status": status,
            "skip_key": skip_key,
        },
    )
    identity = prepared.design_context.identity.model_copy(
        update={
            "contract_kind": "c0.stage_receipt",
            "content_hash": prepared.proposal.proposal_hash,
        }
    )
    values = {
        "identity": identity,
        "stage_receipt_id": receipt_id,
        "stage_id": "L1",
        "stage_name": L1_STAGE_NAME,
        "stage_contract_version": "1.0.0",
        "status": status,
        "input_manifest_id": prepared.preflight.input_manifest.artifact_manifest_id,
        "input_manifest_hash": prepared.preflight.input_manifest.manifest_hash,
        "output_manifest_id": (
            output_manifest.artifact_manifest_id
            if output_manifest is not None
            else None
        ),
        "output_manifest_hash": (
            output_manifest.manifest_hash if output_manifest is not None else None
        ),
        "skip_key": skip_key,
        "accepted_contract_versions": L1_ACCEPTED_VERSIONS,
        "resource_metrics_id": metrics.resource_metrics_id,
        "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1,
        "remote_operation_refs": (),
        "error_codes": error_codes,
        "started_at_utc": prepared.started_at_utc,
        "completed_at_utc": completed_at_utc,
    }
    receipt_hash = canonical_sha256(
        {
            key: value
            for key, value in values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }
    )
    receipt = StageReceipt(**values, receipt_hash=receipt_hash)
    validate_receipt_resources(receipt, metrics)
    return receipt


def _safe_id(value: str) -> str:
    return value.replace(":", "-", 1)


def _artifact_payloads(
    prepared: L1PreparedStage,
    *,
    contract: DomainContractV2,
    approval_context: DomainApprovalContext | None,
    output_manifest: ArtifactManifest,
    metrics: StageResourceMetrics,
    receipt: StageReceipt,
) -> dict[Path, bytes]:
    payloads: dict[Path, bytes] = {
        Path("domain-intake.json"): (
            canonical_json(prepared.preflight.intake) + "\n"
        ).encode("utf-8"),
        Path("source-corpus-manifest.json"): (
            canonical_json(prepared.preflight.corpus) + "\n"
        ).encode("utf-8"),
        Path("design-sample-manifest.json"): (
            canonical_json(prepared.sample_manifest) + "\n"
        ).encode("utf-8"),
        Path("source-profile.json"): (
            canonical_json(prepared.source_profile) + "\n"
        ).encode("utf-8"),
        Path("domain-design-context.json"): (
            canonical_json(prepared.design_context) + "\n"
        ).encode("utf-8"),
        Path("domain-proposal.json"): (
            canonical_json(prepared.proposal) + "\n"
        ).encode("utf-8"),
        Path("input-manifest.json"): (
            canonical_json(prepared.preflight.input_manifest) + "\n"
        ).encode("utf-8"),
        Path("output-manifest.json"): (
            canonical_json(output_manifest) + "\n"
        ).encode("utf-8"),
        Path("resource-metrics.json"): (
            canonical_json(metrics) + "\n"
        ).encode("utf-8"),
        Path("stage-receipt.json"): (
            canonical_json(receipt) + "\n"
        ).encode("utf-8"),
        Path("domain.yaml"): render_domain_contract_yaml(contract).encode("utf-8"),
    }
    if approval_context is not None:
        encoded = (canonical_json(approval_context) + "\n").encode("utf-8")
        payloads[Path("domain-approval-context.json")] = encoded
        if approval_context.decision == "correct":
            payloads[
                Path(
                    "approval-contexts",
                    f"{_safe_id(approval_context.domain_approval_context_id)}.json",
                )
            ] = encoded
    for item in prepared.source_units:
        payloads[
            Path(
                "design-samples",
                "source-units",
                f"{_safe_id(item.source_unit_id)}.json",
            )
        ] = (canonical_json(item) + "\n").encode("utf-8")
    for item in prepared.evidence_spans:
        payloads[
            Path(
                "design-samples",
                "evidence-spans",
                f"{_safe_id(item.evidence_span_id)}.json",
            )
        ] = (canonical_json(item) + "\n").encode("utf-8")
    return payloads


def _persist_payloads(
    payloads: dict[Path, bytes],
    *,
    state_root: Path,
    domain_path: Path,
    remove_relative: tuple[Path, ...] = (),
    transition_lock_held: bool = False,
) -> None:
    lock_path = state_root.parent / f".{state_root.name}.transition.lock"
    lock_descriptor = -1
    if not transition_lock_held:
        state_root.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise L1StageError(
                "an L1 state transition is already in progress"
            ) from exc
    state_root.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(prefix=".l1-stage-", dir=str(state_root.parent))
    )
    domain_temp = domain_path.with_name(
        f".{domain_path.name}.{os.getpid()}.tmp"
    )
    try:
        for relative, content in payloads.items():
            if relative == Path("domain.yaml"):
                domain_temp.parent.mkdir(parents=True, exist_ok=True)
                domain_temp.write_bytes(content)
                continue
            target = temp_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if state_root.exists():
            for existing in state_root.rglob("*"):
                if existing.is_file():
                    relative = existing.relative_to(state_root)
                    if relative in remove_relative:
                        continue
                    if relative.parts[:2] in {
                        ("design-samples", "source-units"),
                        ("design-samples", "evidence-spans"),
                    }:
                        continue
                    replacement = temp_root / relative
                    if not replacement.exists():
                        replacement.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(existing, replacement)
        backup_root = state_root.with_name(f".{state_root.name}.previous")
        if backup_root.exists():
            shutil.rmtree(backup_root)
        if state_root.exists():
            os.replace(state_root, backup_root)
        os.replace(temp_root, state_root)
        os.replace(domain_temp, domain_path)
        if backup_root.exists():
            shutil.rmtree(backup_root)
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        if domain_temp.exists():
            domain_temp.unlink()
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
            lock_path.unlink(missing_ok=True)


def finalize_l1_stage(
    prepared: L1PreparedStage,
    *,
    decision: Literal["approve", "correct", "abort"] | None,
    actor: str | None,
    correction_text: str | None = None,
    state_root: Path = L1_STATE_DIR,
    domain_path: Path = Path("domain.yaml"),
    persist: bool = True,
    transition_lock_held: bool = False,
) -> L1StageResult:
    """Seal one terminal decision and emit a succeeded or blocked C0 receipt."""
    completed = _utc_now()
    approval_context: DomainApprovalContext | None = None
    if decision is None:
        if actor is not None or correction_text is not None:
            raise L1StageError("draft automation cannot supply approval fields")
        contract = prepared.proposal.draft_contract
        error_codes = ("L1_APPROVAL_REQUIRED",)
        status: Literal["succeeded", "blocked"] = "blocked"
    else:
        if not actor or not actor.strip():
            raise L1StageError("an explicit actor is required for every decision")
        approval_context = _approval_context(
            prepared,
            decision=decision,
            actor=actor.strip(),
            correction_text=correction_text,
            decided_at_utc=completed,
        )
        if decision == "approve":
            contract = _approved_contract(
                prepared,
                approval_context,
                approved_at_utc=completed,
            )
            validate_approval_bindings(
                contract=contract,
                approval_context=approval_context,
                proposal=prepared.proposal,
                design_context=prepared.design_context,
                profile=prepared.source_profile,
                corpus=prepared.preflight.corpus,
                sample=prepared.sample_manifest,
                input_manifest=prepared.preflight.input_manifest,
            )
            status = "succeeded"
            error_codes = ()
        elif decision == "correct":
            contract = prepared.proposal.draft_contract
            status = "blocked"
            error_codes = ("L1_CORRECTION_REQUESTED",)
        else:
            contract = prepared.proposal.draft_contract
            status = "blocked"
            error_codes = ("L1_ABORTED_BY_USER",)
    output_manifest = _output_manifest(
        prepared,
        contract=contract,
        approval_context=approval_context,
    )
    storage_bytes = output_manifest.total_byte_count
    metrics_identity = prepared.design_context.identity.model_copy(
        update={"domain_contract_hash": compute_contract_hash(contract)}
    )
    metrics = _resource_metrics(
        prepared,
        identity=metrics_identity,
        completed_at_utc=completed,
        storage_write_bytes=storage_bytes,
        status=status,
    )
    receipt = _receipt(
        prepared,
        status=status,
        output_manifest=output_manifest,
        metrics=metrics,
        completed_at_utc=completed,
        error_codes=error_codes,
    )
    if persist:
        payloads = _artifact_payloads(
            prepared,
            contract=contract,
            approval_context=approval_context,
            output_manifest=output_manifest,
            metrics=metrics,
            receipt=receipt,
        )
        _persist_payloads(
            payloads,
            state_root=state_root,
            domain_path=domain_path,
            remove_relative=(
                (Path("domain-approval-context.json"),)
                if decision is None
                else ()
            ),
            transition_lock_held=transition_lock_held,
        )
    return L1StageResult(
        status=status,
        receipt=receipt,
        output_manifest=output_manifest,
        approval_context=approval_context,
        contract=contract,
        summary=prepared.summary,
    )


def dry_run_l1(
    preflight: L1Preflight,
    *,
    state_root: Path = L1_STATE_DIR,
    domain_path: Path = Path("domain.yaml"),
) -> L1StageResult:
    """Report a read-only plan after complete inventory and intake validation."""
    planned = (
        str(state_root / "domain-intake.json"),
        str(state_root / "source-corpus-manifest.json"),
        str(state_root / "design-sample-manifest.json"),
        str(state_root / "source-profile.json"),
        str(state_root / "domain-design-context.json"),
        str(state_root / "domain-proposal.json"),
        str(state_root / "domain-approval-context.json"),
        str(state_root / "input-manifest.json"),
        str(state_root / "output-manifest.json"),
        str(state_root / "resource-metrics.json"),
        str(state_root / "stage-receipt.json"),
        str(domain_path),
    )
    summary = (
        f"Dry run: complete corpus entries={preflight.corpus.total_entry_count}; "
        f"eligible={preflight.corpus.eligible_entry_count}; "
        f"excluded={preflight.corpus.excluded_entry_count}; "
        f"blocked={preflight.corpus.blocked_entry_count}; "
        "planned remote calls=1; writes=0; approval=not recorded."
    )
    return L1StageResult(
        status="blocked",
        receipt=None,
        output_manifest=None,
        approval_context=None,
        contract=None,
        summary=summary,
        planned_paths=planned,
    )


def _load_json_model(path: Path, model_type: type[ContractModel]) -> ContractModel:
    try:
        return model_type.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, ValidationError) as exc:
        raise L1StageError(f"invalid persisted artifact {path}: {exc}") from exc


def load_prepared_l1_stage(
    *,
    state_root: Path = L1_STATE_DIR,
) -> L1PreparedStage:
    """Load a blocked draft for explicit approval without another model call."""
    intake = _load_json_model(state_root / "domain-intake.json", DomainIntake)
    corpus = _load_json_model(
        state_root / "source-corpus-manifest.json", SourceCorpusManifest
    )
    sample = _load_json_model(
        state_root / "design-sample-manifest.json", DesignSampleManifest
    )
    profile = _load_json_model(
        state_root / "source-profile.json", DomainSourceProfile
    )
    design = _load_json_model(
        state_root / "domain-design-context.json", DomainDesignContext
    )
    proposal = _load_json_model(
        state_root / "domain-proposal.json", DomainProposal
    )
    input_manifest = _load_json_model(
        state_root / "input-manifest.json", ArtifactManifest
    )
    source_units = tuple(
        _load_json_model(path, SourceUnit)
        for path in sorted(
            (state_root / "design-samples" / "source-units").glob("*.json")
        )
    )
    evidence_spans = tuple(
        _load_json_model(path, EvidenceSpan)
        for path in sorted(
            (state_root / "design-samples" / "evidence-spans").glob("*.json")
        )
    )
    expected_bindings = (
        (
            design.domain_intake_id,
            intake.domain_intake_id,
            "design/intake ID",
        ),
        (
            design.domain_intake_hash,
            intake.intake_hash,
            "design/intake hash",
        ),
        (
            design.source_profile_id,
            profile.domain_source_profile_id,
            "design/profile ID",
        ),
        (
            design.source_profile_hash,
            profile.profile_hash,
            "design/profile hash",
        ),
        (
            design.source_corpus_manifest_id,
            corpus.source_corpus_manifest_id,
            "design/corpus ID",
        ),
        (
            design.source_corpus_manifest_hash,
            corpus.corpus_hash,
            "design/corpus hash",
        ),
        (
            design.design_sample_manifest_id,
            sample.design_sample_manifest_id,
            "design/sample ID",
        ),
        (
            design.design_sample_manifest_hash,
            sample.sample_hash,
            "design/sample hash",
        ),
        (
            design.input_manifest_id,
            input_manifest.artifact_manifest_id,
            "design/input-manifest ID",
        ),
        (
            design.input_manifest_hash,
            input_manifest.manifest_hash,
            "design/input-manifest hash",
        ),
        (
            proposal.domain_design_context_id,
            design.domain_design_context_id,
            "proposal/design ID",
        ),
        (
            proposal.domain_design_context_hash,
            design.design_context_hash,
            "proposal/design hash",
        ),
        (
            proposal.domain_contract_hash,
            compute_contract_hash(proposal.draft_contract),
            "proposal/domain hash",
        ),
    )
    for actual, expected, label in expected_bindings:
        if actual != expected:
            raise L1StageError(
                f"persisted L1 cross-artifact binding mismatch: {label}"
            )
    if tuple(sorted(design.source_unit_ids)) != tuple(
        sorted(item.source_unit_id for item in source_units)
    ):
        raise L1StageError(
            "persisted L1 cross-artifact binding mismatch: source units"
        )
    if design.source_unit_content_hash != canonical_sha256(
        [
            item.model_dump(mode="json")
            for item in sorted(
                source_units, key=lambda value: value.source_unit_id
            )
        ]
    ):
        raise L1StageError(
            "persisted L1 cross-artifact binding mismatch: source-unit content"
        )
    if tuple(sorted(design.evidence_span_ids)) != tuple(
        sorted(item.evidence_span_id for item in evidence_spans)
    ):
        raise L1StageError(
            "persisted L1 cross-artifact binding mismatch: evidence spans"
        )
    if design.evidence_span_content_hash != canonical_sha256(
        [
            item.model_dump(mode="json")
            for item in sorted(
                evidence_spans,
                key=lambda value: value.evidence_span_id,
            )
        ]
    ):
        raise L1StageError(
            "persisted L1 cross-artifact binding mismatch: evidence content"
        )
    source_unit_ids = {item.source_unit_id for item in source_units}
    source_units_by_id = {
        item.source_unit_id: item for item in source_units
    }
    for span in evidence_spans:
        source_unit = source_units_by_id.get(span.source_unit_id)
        if source_unit is None:
            raise L1StageError(
                "persisted L1 cross-artifact binding mismatch: evidence source unit"
            )
        try:
            span.verify_against(source_unit)
        except ValueError as exc:
            raise L1StageError(
                "persisted L1 evidence verification failed"
            ) from exc
    identity_pairs = (
        intake.identity,
        corpus.identity,
        sample.identity,
        profile.identity,
        design.identity,
        proposal.identity,
        input_manifest.identity,
        *(item.identity for item in source_units),
        *(item.identity for item in evidence_spans),
    )
    if any(
        identity.project_id != design.identity.project_id
        or identity.run_id != design.identity.run_id
        for identity in identity_pairs
    ):
        raise L1StageError(
            "persisted L1 cross-artifact binding mismatch: identity lineage"
        )
    source_path = Path(".")
    budget = DesignSamplingBudget(
        max_source_files=12,
        max_samples_per_kind=4,
        max_excerpt_codepoints=1_200,
        sample_kinds=("heading", "text", "table", "visual_description"),
        budget_snapshot_hash=design.budget_snapshot_hash,
    )
    preflight = L1Preflight(
        source_path=source_path,
        run_id=design.identity.run_id,
        base_identity=design.identity.model_copy(
            update={
                "contract_kind": "l1.stage",
                "prompt_version": None,
                "prompt_hash": None,
                "model_version": None,
                "model_hash": None,
            }
        ),
        intake=intake,
        corpus=corpus,
        input_manifest=input_manifest,
        budget=budget,
        model_version=design.model_version,
        model_hash=design.model_hash,
    )
    summary = render_l1_summary(
        intake=intake,
        profile=profile,
        design_context=design,
        proposal=proposal,
    )
    return L1PreparedStage(
        preflight=preflight,
        sample_manifest=sample,
        source_profile=profile,
        source_units=source_units,
        evidence_spans=evidence_spans,
        candidates=DomainProposalCandidatesV2(
            domain_boundary_candidates=(),
            semantic_type_candidates=(),
            relationship_candidates=(),
            question_routes=(),
        ),
        design_context=design,
        proposal=proposal,
        summary=summary,
        summary_hash=canonical_sha256(summary),
        started_at_utc=_utc_now(),
        model_call_count=0,
    )


def approve_persisted_l1_draft(
    *,
    actor: str,
    state_root: Path = L1_STATE_DIR,
    domain_path: Path = Path("domain.yaml"),
    reviewed_contract: DomainContractV2 | None = None,
    expected_project_id: str | None = None,
    expected_run_id: str | None = None,
    expected_proposal_hash: str | None = None,
) -> L1StageResult:
    """Explicitly approve a current blocked draft after complete binding checks."""
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root.parent / f".{state_root.name}.transition.lock"
    state_root.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise L1StageError(
            "schema-2 approval is already in progress or requires reconciliation"
        ) from exc
    try:
        os.write(
            descriptor,
            (
                json.dumps(
                    {
                        "project_id": expected_project_id,
                        "run_id": expected_run_id,
                        "proposal_hash": expected_proposal_hash,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()
        )
        os.fsync(descriptor)
        prior = _load_json_model(
            state_root / "stage-receipt.json", StageReceipt
        )
        if (
            prior.status != "blocked"
            or prior.error_codes != ("L1_APPROVAL_REQUIRED",)
            or prior.identity.content_hash != expected_proposal_hash
            or (state_root / "domain-approval-context.json").exists()
        ):
            raise L1StageError(
                "schema-2 approval requires the matching unapproved blocked state"
            )
        return _approve_persisted_l1_draft_locked(
            actor=actor,
            state_root=state_root,
            domain_path=domain_path,
            reviewed_contract=reviewed_contract,
            expected_project_id=expected_project_id,
            expected_run_id=expected_run_id,
            expected_proposal_hash=expected_proposal_hash,
        )
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)
        directory = os.open(state_root.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _approve_persisted_l1_draft_locked(
    *,
    actor: str,
    state_root: Path,
    domain_path: Path,
    reviewed_contract: DomainContractV2 | None,
    expected_project_id: str | None,
    expected_run_id: str | None,
    expected_proposal_hash: str | None,
) -> L1StageResult:
    prepared = load_prepared_l1_stage(state_root=state_root)
    if (
        not expected_project_id
        or not expected_run_id
        or not expected_proposal_hash
    ):
        raise L1StageError(
            "schema-2 approval requires expected project ID, run ID, "
            "and proposal hash"
        )
    if (
        prepared.design_context.identity.project_id
        != expected_project_id
        or prepared.design_context.identity.run_id != expected_run_id
    ):
        raise L1StageError(
            "persisted L1 identity does not match expected project/run"
        )
    if prepared.proposal.proposal_hash != expected_proposal_hash:
        raise L1StageError(
            "persisted L1 proposal does not match expected proposal hash"
        )
    if reviewed_contract is not None and (
        canonical_json(reviewed_contract)
        != canonical_json(prepared.proposal.draft_contract)
    ):
        raise L1StageError(
            "reviewed domain contract does not match the persisted L1 draft"
        )
    if reviewed_contract is not None and (
        reviewed_contract.approval.status != "draft"
    ):
        raise L1StageError(
            "schema-2 approval requires a current blocked draft"
        )
    return finalize_l1_stage(
        prepared,
        decision="approve",
        actor=actor,
        state_root=state_root,
        domain_path=domain_path,
        transition_lock_held=True,
    )


def try_resume_l1(
    prepared: L1PreparedStage,
    *,
    state_root: Path = L1_STATE_DIR,
    domain_path: Path = Path("domain.yaml"),
) -> L1StageResult | None:
    """Emit a skip only for an intact prior success with the exact skip key."""
    receipt_path = state_root / "stage-receipt.json"
    output_path = state_root / "output-manifest.json"
    metrics_path = state_root / "resource-metrics.json"
    if not receipt_path.exists() or not output_path.exists() or not domain_path.exists():
        return None
    prior = _load_json_model(receipt_path, StageReceipt)
    output_manifest = _load_json_model(output_path, ArtifactManifest)
    if prior.status != "succeeded" or prior.skip_key != _skip_key(prepared):
        return None
    if prior.input_manifest_hash != prepared.preflight.input_manifest.manifest_hash:
        return None
    contract = load_domain_contract(domain_path)
    if not isinstance(contract, DomainContractV2):
        return None
    if compute_contract_hash(contract) != prepared.proposal.domain_contract_hash:
        return None
    completed = _utc_now()
    metrics = _resource_metrics(
        prepared,
        identity=prepared.design_context.identity,
        completed_at_utc=completed,
        storage_write_bytes=0,
        status="skipped",
    )
    skipped = _receipt(
        prepared,
        status="skipped",
        output_manifest=output_manifest,
        metrics=metrics,
        completed_at_utc=completed,
        prior_skip_key=prior.skip_key,
    )
    validate_skip_preconditions(
        skipped,
        prior_succeeded=prior,
        intact_output_manifest=output_manifest,
    )
    validate_receipt_resources(skipped, metrics)
    metrics_path.write_text(canonical_json(metrics) + "\n", encoding="utf-8")
    receipt_path.write_text(canonical_json(skipped) + "\n", encoding="utf-8")
    return L1StageResult(
        status="skipped",
        receipt=skipped,
        output_manifest=output_manifest,
        approval_context=None,
        contract=contract,
        summary="L1 skipped: prior succeeded output remains intact.",
    )
