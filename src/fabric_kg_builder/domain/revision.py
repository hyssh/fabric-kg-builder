"""Regenerate a separately approved L1 draft from an immutable reviewed challenge."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator

from fabric_kg_builder.contracts.base import ContractModel, Sha256, canonical_json, canonical_sha256
from fabric_kg_builder.domain.assessment import (
    AssessmentReport, AssessmentReview, CachedResponse, RevisionRequest, Window, assessment_windows,
    review_assessment, save_new_artifact,
)
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.proposal import compute_model_hash
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.domain.stage import (
    SupplementalDesignLocation,
    finalize_l1_stage, load_prepared_l1_stage,
    preflight_l1_inputs, prepare_l1_stage,
)


class BoundedRevisionClient:
    def __init__(
        self, client: Any, *, max_calls: int, response_cache: Path,
        max_prompt_chars: int = 64_000,
    ):
        if not 1 <= max_calls <= 6:
            raise ValueError("revision max_calls must be between 1 and 6")
        self.client = client
        self.max_calls = max_calls
        self.max_prompt_chars = max_prompt_chars
        if not 256 <= max_prompt_chars <= 64_000:
            raise ValueError("revision max_prompt_chars must be between 256 and 64000")
        self.response_cache = response_cache
        self.calls = 0
        self.replayed_calls = 0

    def execution_identity(self) -> dict[str, Any]:
        return {
            "client": self.client.execution_identity(),
            "max_calls": self.max_calls,
            "max_prompt_chars": self.max_prompt_chars,
        }

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        prompt_chars = sum(len(str(kwargs.get(k, ""))) for k in ("system", "user"))
        prompt_chars += len(canonical_json(kwargs.get("json_schema", {})))
        if prompt_chars > self.max_prompt_chars:
            raise ValueError("revision prompt budget exhausted")
        request = {**kwargs, "max_attempts": 1}
        request_hash = canonical_sha256({
            "revision_client_version": "1.1.0",
            "model_identity": self.client.execution_identity(),
            "request": request,
        })
        path = self.response_cache / f"{request_hash}.json"
        if path.exists():
            cached = CachedResponse.model_validate_json(path.read_text("utf-8"))
            if cached.request_hash != request_hash:
                raise ValueError("revision cached response request fingerprint mismatch")
            if set(cached.response) != {"raw_response"}:
                raise ValueError("revision cached response envelope mismatch")
            self.replayed_calls += 1
            return cached.response["raw_response"]
        if self.calls >= self.max_calls:
            raise ValueError("revision model-call budget exhausted")
        self.calls += 1
        response = self.client.complete_json(**request)
        envelope = {"raw_response": response}
        cached = CachedResponse(
            request_hash=request_hash, response=envelope,
            response_hash=canonical_sha256(envelope),
        )
        try:
            save_new_artifact(path, cached)
        except FileExistsError:
            if CachedResponse.model_validate_json(path.read_text("utf-8")) != cached:
                raise ValueError("revision response cache collision")
        return response


class RevisionChanges(ContractModel):
    contract_version: Literal["1.0.0"] = "1.0.0"
    parent_domain_contract_hash: Sha256
    child_domain_contract_hash: Sha256
    request_hash: Sha256
    breaking_change_authorized: Literal[False] = False
    changes: tuple[dict[str, Any], ...]
    changes_hash: Sha256

    @model_validator(mode="after")
    def _integrity(self) -> "RevisionChanges":
        if self.changes_hash != canonical_sha256(
            self.model_dump(mode="json", exclude={"changes_hash"})
        ):
            raise ValueError("revision changes hash mismatch")
        return self


def _trusted_report_windows(
    source: Path, report: AssessmentReport, parent: DomainContractV2,
    *, ocr_cache: Path | None = None,
) -> tuple[Window, ...]:
    if report.ocr_identity is not None and ocr_cache is None:
        raise ValueError("OCR-assessed revision requires its exact OCR response cache")
    corpus_hash, files, windows = assessment_windows(
        source, window_chars=report.window_chars,
        ocr_cache=ocr_cache, ocr_identity=report.ocr_identity,
    )
    if corpus_hash != report.corpus_manifest_hash:
        raise ValueError("source corpus changed after assessment")
    if files != report.files:
        raise ValueError("assessment file dispositions differ from trusted source")
    trusted = {window.window_id: window for window in windows}
    reported = {window.window_id: window for window in report.windows}
    if set(trusted) != set(reported):
        raise ValueError("assessment window inventory differs from trusted source")
    for window_id, window in trusted.items():
        if window.model_dump(exclude={"status", "reason", "response"}) != reported[
            window_id
        ].model_dump(exclude={"status", "reason", "response"}):
            raise ValueError("assessment window location or text differs from trusted source")
    question_ids = {question.id for question in parent.competency_questions}
    semantic_ids = {
        entity.type_id for entity in parent.candidate_model.entity_types
    } | {
        relation.relationship_type_id for relation in parent.candidate_model.relationship_types
    } | {
        prop.property_id for entity in parent.candidate_model.entity_types
        for prop in entity.declared_properties
    }
    for finding in report.findings:
        if set(finding.question_ids) - question_ids or set(finding.semantic_ids) - semantic_ids:
            raise ValueError("assessment finding references unknown parent definitions")
    return windows


def _supplemental_locations(
    windows: tuple[Window, ...], request: RevisionRequest,
    *, ocr_cache: Path | None = None, ocr_identity: dict[str, Any] | None = None,
) -> tuple[SupplementalDesignLocation, ...]:
    by_unit: dict[str, list[Window]] = {}
    for window in windows:
        by_unit.setdefault(window.source_unit_id, []).append(window)
    locations = []
    for finding in request.accepted_findings:
        matching = [
            window for window in by_unit[finding.source_unit_id]
            if window.span_start <= finding.span_start < finding.span_end <= window.span_end
        ]
        if len(matching) != 1:
            raise ValueError("accepted finding requires one trusted assessment window")
        window = matching[0]
        start = finding.span_start - window.span_start
        end = finding.span_end - window.span_start
        if window.text[start:end] != finding.quote:
            raise ValueError("accepted finding quote differs from trusted source")
        locations.append(SupplementalDesignLocation(
            source_file_id=finding.source_file_id, source_ref=window.source_ref,
            source_text=window.text, source_text_start=window.span_start, page=window.page,
            span_start=finding.span_start, span_end=finding.span_end, quote=finding.quote,
            extraction_ref=window.extraction_ref,
            ocr_cache=ocr_cache if window.extraction_ref is not None else None,
            ocr_identity=ocr_identity if window.extraction_ref is not None else None,
        ))
    return tuple(locations)


def _definitions(contract: DomainContractV2) -> dict[str, dict[str, dict[str, Any]]]:
    entities = contract.candidate_model.entity_types
    return {
        "entity": {item.type_id: item.model_dump(mode="json") for item in entities},
        "relationship": {
            item.relationship_type_id: item.model_dump(mode="json")
            for item in contract.candidate_model.relationship_types
        },
        "property": {
            f"{entity.type_id}/{item.property_id}": item.model_dump(mode="json")
            for entity in entities for item in entity.declared_properties
        },
        "constraint": {
            f"{entity.type_id}/{item.constraint_id}": item.model_dump(mode="json")
            for entity in entities for item in entity.declared_constraints
        },
        "question": {item.id: item.model_dump(mode="json") for item in contract.competency_questions},
        "completeness_requirement": {
            item.requirement_id: item.model_dump(mode="json") for item in contract.completeness_requirements
        },
        "external_reference": {
            item.reference_id: item.model_dump(mode="json") for item in contract.approved_external_references
        },
    }


def _revision_changes(
    parent: DomainContractV2, child: DomainContractV2, request_hash: str,
) -> RevisionChanges:
    before, after = _definitions(parent), _definitions(child)
    protected = {
        "entity": (
            "semantic_key", "classification", "parent_type_id", "abstract",
            "identity_root_type_id", "identity_key_policy", "sibling_classification_policy", "tombstoned",
        ),
        "relationship": (
            "predicate_id", "source_type_ids", "target_type_ids", "direction",
            "endpoint_policy", "identity_policy",
        ),
        "property": ("value_type", "required"),
        "constraint": ("expression", "severity"),
        "completeness_requirement": (
            "requirement_kind", "scope_type_id", "scoped_subtype_id", "scoped_filter",
            "required_roles", "structured_fact_set",
        ),
    }
    changes = []
    for kind, definitions in before.items():
        missing = set(definitions) - set(after[kind])
        if missing:
            raise ValueError(f"revision removes existing {kind} IDs without breaking-change authorization: {sorted(missing)}")
        for identifier, definition in definitions.items():
            changed = [
                field for field in protected.get(kind, ())
                if definition[field] != after[kind][identifier][field]
            ]
            if changed:
                raise ValueError(f"revision changes protected {kind} definition {identifier}: {changed}; breaking-change authorization is unavailable")
        for identifier in sorted(set(definitions) | set(after[kind])):
            old, new = definitions.get(identifier), after[kind].get(identifier)
            if (
                kind == "property" and old is None and new is not None and new["required"]
                and identifier.rsplit("/", 1)[0] in before["entity"]
            ):
                raise ValueError(
                    f"revision adds required property {identifier} to an existing type; "
                    "breaking-change authorization is unavailable"
                )
            if old != new:
                changes.append({
                    "kind": kind, "id": identifier,
                    "operation": "added" if old is None else "modified",
                    "before": old, "after": new,
                })
    parent_payload, child_payload = parent.model_dump(mode="json"), child.model_dump(mode="json")
    for section in sorted(parent_payload):
        if parent_payload[section] != child_payload[section]:
            changes.append({
                "kind": "contract_section", "id": section, "operation": "modified",
                "before": parent_payload[section], "after": child_payload[section],
            })
    values = {
        "contract_version": "1.0.0",
        "parent_domain_contract_hash": compute_contract_hash(parent),
        "child_domain_contract_hash": compute_contract_hash(child),
        "request_hash": request_hash, "breaking_change_authorized": False,
        "changes": tuple(changes),
    }
    return RevisionChanges(**values, changes_hash=canonical_sha256(values))


def create_revision(
    *,
    parent_state: Path,
    source: Path,
    report: AssessmentReport,
    review: AssessmentReview,
    request: RevisionRequest,
    state_root: Path,
    domain_path: Path,
    candidates: dict[str, Any] | None = None,
    client: BoundedRevisionClient | None = None,
    dry_run: bool = False,
    max_prompt_chars: int = 64_000,
    ocr_cache: Path | None = None,
) -> dict[str, Any]:
    parent = load_prepared_l1_stage(state_root=parent_state)
    if parent.preflight.budget.budget_snapshot_hash != canonical_sha256(
        parent.preflight.budget.model_dump(mode="json", exclude={"budget_snapshot_hash"})
    ):
        raise ValueError("parent sampling budget snapshot is unavailable; regenerate parent design before revision")
    parent_hash = compute_contract_hash(parent.proposal.draft_contract)
    if not (
        report.domain_contract_hash == review.domain_contract_hash
        == request.parent_domain_contract_hash == parent_hash
    ):
        raise ValueError("revision parent domain hashes differ")
    rebuilt_review, rebuilt_request = review_assessment(
        report, actor=review.actor, decisions=review.decisions,
    )
    if rebuilt_review != review or rebuilt_request != request:
        raise ValueError("revision request differs from the reviewed assessment")
    if not request.accepted_findings:
        raise ValueError("revision requires at least one accepted finding")
    if len(request.accepted_findings) > 16:
        raise ValueError("revision supplemental findings are capped at 16")
    if not 256 <= max_prompt_chars <= 64_000:
        raise ValueError("revision max_prompt_chars must be between 256 and 64000")
    windows = _trusted_report_windows(
        source, report, parent.proposal.draft_contract, ocr_cache=ocr_cache,
    )
    supplemental = _supplemental_locations(
        windows, request, ocr_cache=ocr_cache, ocr_identity=report.ocr_identity,
    )
    parent_root = parent_state.resolve()
    target = state_root.resolve()
    destination = domain_path.resolve()
    if (
        target == parent_root or target.is_relative_to(parent_root)
        or parent_root.is_relative_to(target)
        or destination.is_relative_to(parent_root)
    ):
        raise ValueError("revision outputs must be separate from parent state")
    if not destination.is_relative_to(target):
        raise ValueError("revision domain output must be inside its new state directory")
    if target.exists():
        raise ValueError("revision state directory already exists")
    if client is not None:
        cache = client.response_cache.resolve()
        if any(
            cache == root or cache.is_relative_to(root) or root.is_relative_to(cache)
            for root in (parent_root, target)
        ):
            raise ValueError("revision response cache must be separate from parent and child state")
    result: dict[str, Any] = {
        "operation": "domain.revise",
        "status": "planned",
        "parent_domain_contract_hash": parent_hash,
        "request_hash": request.request_hash,
        "state_root": str(state_root),
        "domain_path": str(domain_path),
        "model_calls": 0,
        "approved": False,
        "parent_modified": False,
        "supplemental_finding_count": len(supplemental),
        "max_prompt_chars": min(max_prompt_chars, client.max_prompt_chars) if client else max_prompt_chars,
    }
    if dry_run:
        return result
    if (candidates is None) == (client is None):
        raise ValueError("choose exactly one revision client or candidate fixture")
    model_version = "offline-revision/1.0.0"
    model_hash = canonical_sha256({"fixture": candidates})
    if client is not None:
        model_version = "bounded-revision/1.0.0"
        model_hash = compute_model_hash(client, model_version)
    intake = parent.preflight.intake.model_dump(mode="json", exclude={
        "identity", "domain_intake_id", "intake_hash", "contract_version",
    })
    preflight = preflight_l1_inputs(
        source_path=source,
        intake_raw=intake,
        project_id=parent.preflight.base_identity.project_id,
        run_id=f"run:revision-{request.request_hash[:24]}",
        model_version=model_version, model_hash=model_hash,
        budget=parent.preflight.budget,
    )
    instruction = canonical_json({
        "kind": "reviewed-document-challenge",
        "request_hash": request.request_hash,
        "parent_domain_contract_hash": parent_hash,
        "parent_domain": parent.proposal.draft_contract.model_dump(mode="json"),
        "instruction": (
            "Consider these reviewed document challenges when revising the draft. "
            "They do not approve a schema change or mint evidence authority. "
            "Return the complete parent ontology with compatible reviewed additions, "
            "not only a fragment. Do not remove existing IDs or change identity, "
            "hierarchy or property types: breaking changes are not authorized. "
            "Parent evidence references are historical: cite only freshly supplied "
            "verified design evidence IDs in the new proposal. "
            "Preserve existing compatible semantic IDs and explain unsupported "
            "requested changes. Return candidates for a new explicit approval."
        ),
        "accepted_findings": [
            f.model_dump(mode="json") for f in request.accepted_findings
        ],
    })
    calls_before = client.calls if client else 0
    replays_before = client.replayed_calls if client else 0
    prepared = prepare_l1_stage(
        preflight, candidates=candidates, client=client,
        correction_instruction=instruction,
        parent_correction_context_id=f"domain-revision:{request.request_hash}",
        supplemental_design_locations=supplemental,
        max_prompt_chars=result["max_prompt_chars"],
    )
    if client is not None:
        prepared = replace(prepared, model_call_count=client.calls - calls_before)
    changes = _revision_changes(
        parent.proposal.draft_contract, prepared.proposal.draft_contract, request.request_hash,
    )
    # Reserve the new namespace after all preparation succeeds. Never overwrite
    # a previous attempt or the parent's current proposal/approval artifacts.
    state_root.mkdir(parents=True, exist_ok=False)
    save_new_artifact(state_root / "revision-request.json", request)
    save_new_artifact(state_root / "assessment-review.json", review)
    save_new_artifact(state_root / "assessment-report.json", report)
    save_new_artifact(state_root / "revision-changes.json", changes)
    outcome = finalize_l1_stage(
        prepared, decision=None, actor=None,
        state_root=state_root, domain_path=domain_path,
    )
    result.update({
        "status": outcome.status,
        "proposal_hash": prepared.proposal.proposal_hash,
        "model_calls": prepared.model_call_count,
        "replayed_calls": client.replayed_calls - replays_before if client else 0,
        "changes_hash": changes.changes_hash,
        "changes": list(changes.changes),
        "next_action": "domain approve with exact project/run/proposal anchors",
        "project_id": preflight.base_identity.project_id,
        "run_id": preflight.run_id,
    })
    return result
