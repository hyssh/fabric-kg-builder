"""Integrated-run design derivation and explicitly reviewed, zero-call replay."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import model_serializer

from fabric_kg_builder.contracts.base import ContractModel, RequiredText, Sha256
from fabric_kg_builder.domain.discovery import _Hashed, _seal, _write, prepared_design_artifacts
from fabric_kg_builder.domain.models import DomainContractV2, WindowRunBinding
from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract
from fabric_kg_builder.domain import window_schema
from fabric_kg_builder.domain.window_run_acceptance import WindowRunCoverageAcceptance, WindowRunPrefixAcceptance, validate_window_run_acceptance
from fabric_kg_builder.sources.corpus import validate_corpus_manifest_against_source

from .window_mapping import ReplayMapping


class WindowRunReuseError(ValueError):
    """The integrated source, design or human review does not authorize replay."""


def projected_id_only_relationship_aliases(contract):
    """Keep scoped canonical IDs, never resolve an ambiguous human label."""
    from fabric_kg_builder.contracts.base import normalize_nfc

    # Native window designs also expand polymorphic relations into disjoint
    # endpoint pairs; they need the same canonical-ID-only label handling.
    if contract.window_schema_projection is None and contract.window_run_binding is None:
        return set()
    groups, reserved = {}, set()
    for relation in contract.candidate_model.relationship_types:
        groups.setdefault(normalize_nfc(relation.display_name).casefold(), []).append(relation)
        reserved.update(normalize_nfc(value).casefold() for value in (
            relation.relationship_type_id, relation.predicate_id,
        ))
    closure = contract.hierarchy_closure
    sources = closure.compatible_source_type_ids_by_relationship
    targets = closure.compatible_target_type_ids_by_relationship
    result = set()
    for alias, relations in groups.items():
        if len(relations) < 2 or alias in reserved or len({item.display_name for item in relations}) != 1:
            continue
        if any(
            set(sources[left.relationship_type_id]) & set(sources[right.relationship_type_id])
            and set(targets[left.relationship_type_id]) & set(targets[right.relationship_type_id])
            for index, left in enumerate(relations) for right in relations[index + 1:]
        ):
            continue
        result.add(alias)
    return result


def checked_window_run(run, acceptance=None, *, _validation=None):
    from fabric_kg_builder.domain.window_validation import WindowValidationOperation

    operation = _validation or WindowValidationOperation()
    checked = operation.run(run)
    if acceptance is not None:
        validate_window_run_acceptance(acceptance, checked, _validation=operation)
    elif checked.state != "complete":
        raise WindowRunReuseError("WINDOW_RUN_INCOMPLETE: finish the corpus or explicitly review >=99% coverage or a limited committed-prefix scope")
    if (acceptance is None and any(item.response is None for item in checked.chunks)) or any(
        item.status not in {"processed", "no_candidates"} for item in checked.prepared.sources
    ):
        raise WindowRunReuseError("WINDOW_RUN_COVERAGE_INCOMPLETE")
    return checked


def window_run_binding(run, acceptance=None, *, _validation=None) -> WindowRunBinding:
    run = checked_window_run(run, acceptance, _validation=_validation)
    return WindowRunBinding(
        window_run_hash=run.artifact_hash,
        prepared_corpus_hash=run.prepared.prepared_hash,
        context_hash=run.context.artifact_hash,
        snapshot_hash=run.final_snapshot.artifact_hash,
        final_mapping_hash=run.final_mapping.artifact_hash,
        coverage_acceptance_hash=acceptance.acceptance_hash if isinstance(acceptance, WindowRunCoverageAcceptance) else None,
        scope_acceptance_hash=acceptance.acceptance_hash if isinstance(acceptance, WindowRunPrefixAcceptance) else None,
    )


def window_run_design_artifacts(run, *, preflight, verified_at_utc, acceptance=None, _validation=None):
    run = checked_window_run(run, acceptance, _validation=_validation)
    if window_run_intake(run, preflight.base_identity).intake_hash != preflight.intake.intake_hash:
        raise WindowRunReuseError("WINDOW_RUN_CONTEXT_DRIFT: design must retain the original full intake")
    validate_corpus_manifest_against_source(
        run.prepared.corpus, preflight.source_path, identity=run.prepared.base_identity,
    )
    return prepared_design_artifacts(
        run.prepared, preflight=preflight, verified_at_utc=verified_at_utc,
        **({"selected_chunks": acceptance.selected_chunks, "scope_notice": acceptance.scope_notice}
           if isinstance(acceptance, WindowRunPrefixAcceptance) else {}),
    )


def window_run_intake(run, identity):
    from fabric_kg_builder.domain.stage import seal_domain_intake

    return seal_domain_intake(run.context.intake_raw, identity=identity)


class WindowRunMappingReview(_Hashed):
    artifact_kind: Literal["domain.window_run_mapping_review"] = "domain.window_run_mapping_review"
    artifact_version: Literal["1.0.0"] = "1.0.0"
    authority: Literal["reviewed_schema_alignment_only"] = "reviewed_schema_alignment_only"
    binding: WindowRunBinding
    domain_contract_hash: Sha256
    concept_targets: dict[str, str]
    pending_concept_ids: list[str]
    actor: RequiredText
    rationale: RequiredText
    accepted: Literal[True] = True
    policy: Literal["exact_names_approved_scopes_v1"] = "exact_names_approved_scopes_v1"
    ontology_approved: Literal[False] = False
    evidence_approved: Literal[False] = False
    coverage_acceptance: WindowRunCoverageAcceptance | None = None
    scope_acceptance: WindowRunPrefixAcceptance | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        values = handler(self)
        if self.coverage_acceptance is None:
            values.pop("coverage_acceptance", None)
        if self.scope_acceptance is None:
            values.pop("scope_acceptance", None)
        return values


class WindowRunMappingPreview(ContractModel):
    artifact_kind: Literal["domain.window_run_mapping_preview"] = "domain.window_run_mapping_preview"
    authority: Literal["review_required"] = "review_required"
    binding: WindowRunBinding
    domain_contract_hash: Sha256
    concept_targets: dict[str, str]
    pending_concept_ids: list[str]
    actor: RequiredText
    rationale: RequiredText
    accepted: Literal[False] = False
    ontology_approved: Literal[False] = False
    evidence_approved: Literal[False] = False
    coverage_acceptance: WindowRunCoverageAcceptance | None = None
    scope_acceptance: WindowRunPrefixAcceptance | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        values = handler(self)
        if self.coverage_acceptance is None:
            values.pop("coverage_acceptance", None)
        if self.scope_acceptance is None:
            values.pop("scope_acceptance", None)
        return values


def _concept_targets(snapshot, contract):
    """Only unique exact labels and translated owners/endpoints; never fuzzy aliases."""
    approved = window_schema.seed_snapshot(contract).concepts
    targets = {}
    entities = [item for item in snapshot.concepts if item.kind == "entity"]
    while entities:
        remaining = []
        for concept in entities:
            if concept.parent_type_id is not None and concept.parent_type_id not in targets:
                remaining.append(concept)
                continue
            matches = [
                target for target in approved
                if target.kind == "entity" and target.name == concept.name
                and target.parent_type_id == targets.get(concept.parent_type_id)
                and (concept.identity_policy == {"mode": "unresolved"}
                     or concept.identity_policy == target.identity_policy)
            ]
            if len(matches) == 1:
                targets[concept.concept_id] = matches[0].concept_id
        if len(remaining) == len(entities):
            break
        entities = remaining
    for concept in snapshot.concepts:
        if concept.kind == "entity":
            continue
        scope_fields = ("owner_type_ids",) if concept.kind == "property" else ("source_type_ids", "target_type_ids")
        if any(item not in targets for field in scope_fields for item in getattr(concept, field)):
            continue
        matches = [
            target for target in approved
            if target.kind == concept.kind and target.name == concept.name
            and target.direction == concept.direction and target.value_type == concept.value_type
            and all(
                set(getattr(target, field)) == {targets[item] for item in getattr(concept, field)}
                for field in scope_fields
            )
        ]
        if len(matches) == 1:
            targets[concept.concept_id] = matches[0].concept_id
    projection = contract.window_schema_projection
    if projection is not None:
        source = {concept.concept_id: concept for concept in snapshot.concepts}
        declared = set(projection.retained_concept_keys) | set(projection.unsupported_concepts)
        if declared != set(source):
            raise WindowRunReuseError("WINDOW_PROJECTION_CONCEPT_ACCOUNTING_DRIFT")
        approved_by_id = {concept.concept_id: concept for concept in approved}
        prefixes = {"entity": "semantic-type:", "relationship": "relationship-type:", "property": "property:"}
        targets = {
            key: target for key, target in targets.items()
            if key in projection.retained_concept_keys
            and target == prefixes[source[key].kind] + projection.retained_concept_keys[key]
            and (source[key].kind == "property" or approved_by_id[target].definition == source[key].definition)
        }
    return targets


def _bindings(window_run_path, domain_path):
    from fabric_kg_builder.domain.window_run import load_windowed_run

    contract = load_domain_contract(domain_path)
    if not isinstance(contract, DomainContractV2):
        raise WindowRunReuseError("WINDOW_RUN_REQUIRES_SCHEMA2")
    run = checked_window_run(load_windowed_run(window_run_path), contract.window_run_acceptance)
    binding = window_run_binding(run, contract.window_run_acceptance)
    if contract.window_run_binding != binding:
        raise WindowRunReuseError("WINDOW_RUN_APPROVED_BINDING_DRIFT")
    if contract.approval.status != "approved" or contract.approval.contract_hash != compute_contract_hash(contract):
        raise WindowRunReuseError("WINDOW_RUN_DOMAIN_NOT_APPROVED")
    targets = _concept_targets(run.final_snapshot, contract)
    return run, contract, {
        "binding": binding,
        "coverage_acceptance": contract.window_run_acceptance if isinstance(contract.window_run_acceptance, WindowRunCoverageAcceptance) else None,
        "scope_acceptance": contract.window_run_acceptance if isinstance(contract.window_run_acceptance, WindowRunPrefixAcceptance) else None,
        "domain_contract_hash": compute_contract_hash(contract),
        "concept_targets": targets,
        "pending_concept_ids": sorted(
            item.concept_id for item in run.final_snapshot.concepts if item.concept_id not in targets
        ),
    }


def review_window_run_mapping(*, window_run_path, domain_path, actor, rationale, output=None):
    _, _, bindings = _bindings(window_run_path, domain_path)
    review = _seal(WindowRunMappingReview, **bindings, actor=actor, rationale=rationale)
    if output is not None:
        _write(output, review)
    return review


def preview_window_run_mapping(*, window_run_path, domain_path, actor, rationale):
    _, _, bindings = _bindings(window_run_path, domain_path)
    return WindowRunMappingPreview(**bindings, actor=actor, rationale=rationale)


def load_window_run_replay_mapping(path, *, window_run_path, domain_path):
    review = WindowRunMappingReview.model_validate_json(Path(path).read_text(encoding="utf-8"))
    run, _, expected = _bindings(window_run_path, domain_path)
    for field, value in expected.items():
        if getattr(review, field) != value:
            raise WindowRunReuseError(f"WINDOW_RUN_MAPPING_BINDING_DRIFT: {field}")
    return ReplayMapping(review, run.final_mapping, run.final_snapshot), {
        "window_mapping": {
            "review_hash": review.artifact_hash,
            **review.binding.model_dump(mode="json"),
            "policy": review.policy,
        },
    }


def prepare_window_run_reuse(window_run_path, source_path, l1_state_root, domain_path):
    from .discovery_reuse import materialize_reuse_sources
    from .schema2_sources import load_l2_inputs

    inputs = load_l2_inputs(l1_state_root=l1_state_root, domain_path=domain_path)
    run, _, _ = _bindings(window_run_path, domain_path)
    if run.config.discovery_mode == "whole-document":
        raise WindowRunReuseError(
            "WHOLE_DOCUMENT_SCHEMA_REQUIRES_REEXTRACTION: schema discovery contains no instance candidates; "
            "use --reextract-approved after L1 approval")
    if (
        inputs.corpus_manifest.corpus_hash != run.prepared.corpus.corpus_hash
        or inputs.l1_receipt.identity.project_id != run.prepared.base_identity.project_id
    ):
        raise WindowRunReuseError("WINDOW_RUN_APPROVED_SOURCE_DRIFT")
    validate_corpus_manifest_against_source(
        run.prepared.corpus, source_path, identity=run.prepared.base_identity,
    )
    reader, materialized = materialize_reuse_sources(inputs, run, source_path)
    return inputs, run, reader, materialized, inputs.domain_contract.window_run_acceptance


def window_run_ledger_accounting(run):
    entries = sum(len(item.candidate_grounding) for item in run.chunks)
    sizes = [len((item.raw_response or {}).get("candidates", [])) for item in run.chunks]
    incomplete = sum(
        {entry.candidate_index for entry in item.candidate_grounding} != set(range(size))
        for item, size in zip(run.chunks, sizes)
    )
    return {
        "candidate_ledger_entry_count": entries,
        "unaccounted_raw_candidate_count": sum(sizes) - entries,
        "unaccounted_raw_array_count": incomplete,
        "received_array_ledger_complete": incomplete == 0,
    }
