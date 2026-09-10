"""Human-reviewed working-schema mappings, never source or ontology authority."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Literal

from fabric_kg_builder.contracts.base import RequiredText, Sha256, canonical_sha256
from fabric_kg_builder.domain.discovery import _Hashed, _seal, _write, load_discovery
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.hierarchy import resolve_identity_root_policy
from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract
from fabric_kg_builder.domain import window_schema as core


class WindowMappingReview(_Hashed):
    artifact_kind: Literal["domain.window_mapping_review"] = "domain.window_mapping_review"
    artifact_version: Literal["1.0.0"] = "1.0.0"
    authority: Literal["reviewed_schema_alignment_only"] = "reviewed_schema_alignment_only"
    discovery_hash: Sha256
    discovery_file_sha256: Sha256
    domain_contract_hash: Sha256
    window_run_hash: Sha256
    snapshot_hash: Sha256
    final_mapping_hash: Sha256
    concept_targets: dict[str, str]
    pending_concept_ids: list[str]
    actor: RequiredText
    rationale: RequiredText
    accepted: Literal[True] = True
    policy: Literal["exact_approved_ids_scoped_v1"] = "exact_approved_ids_scoped_v1"
    ontology_approved: Literal[False] = False
    evidence_approved: Literal[False] = False


def _bindings(state_root, discovery_path, domain_path):
    run = core.load_window_run(state_root)
    if run.state != "complete":
        raise ValueError("WINDOW_MAPPING_INCOMPLETE: finish every window before review/replay")
    discovery = load_discovery(discovery_path)
    if discovery.run_hash != run.discovery_hash:
        raise ValueError("WINDOW_MAPPING_DISCOVERY_DRIFT")
    contract = load_domain_contract(domain_path)
    if not isinstance(contract, DomainContractV2):
        raise ValueError("Window mapping requires a Schema-2 DomainContractV2 target")
    approved = {c.concept_id: c for c in core.seed_snapshot(contract).concepts}
    targets = {}
    for concept in run.final_snapshot.concepts:
        target = approved.get(concept.concept_id)
        if target is None:
            continue
        # Aliases are alignment proposals; identity, ownership and direction may
        # never be rewritten by a schema-only review.
        fields = ("kind", "source_type_ids", "target_type_ids", "owner_type_ids",
                  "direction", "identity_policy", "value_type", "parent_type_id", "endpoint_policy")
        if all(getattr(concept, field) == getattr(target, field) for field in fields):
            targets[concept.concept_id] = target.concept_id
    return run, {
        "discovery_hash": run.discovery_hash,
        "discovery_file_sha256": hashlib.sha256(Path(discovery_path).read_bytes()).hexdigest(),
        "domain_contract_hash": compute_contract_hash(contract),
        "window_run_hash": run.artifact_hash,
        "snapshot_hash": run.final_snapshot.artifact_hash,
        "final_mapping_hash": run.final_mapping_hash,
        "concept_targets": targets,
        "pending_concept_ids": sorted(c.concept_id for c in run.final_snapshot.concepts if c.concept_id not in targets),
    }


def review_window_mapping(*, state_root, discovery_path, domain_path, actor, rationale, output=None):
    _, bindings = _bindings(state_root, discovery_path, domain_path)
    review = _seal(WindowMappingReview, **bindings, actor=actor, rationale=rationale)
    if output is not None:
        _write(output, review)
    return review


class ReplayMapping:
    def __init__(self, review, mapping, snapshot):
        self.review = review
        self.records = mapping.records
        self.snapshot = snapshot


def load_replay_mapping(path, *, state_root, discovery_path, domain_path):
    review = WindowMappingReview.model_validate_json(Path(path).read_text(encoding="utf-8"))
    run, expected = _bindings(state_root, discovery_path, domain_path)
    for field, value in expected.items():
        if getattr(review, field) != value:
            raise ValueError(f"WINDOW_MAPPING_BINDING_DRIFT: {field}")
    mapping = core.load_final_mapping(state_root)
    return ReplayMapping(review, mapping, run.final_snapshot), {"window_mapping": {
        "review_hash": review.artifact_hash,
        "window_run_hash": review.window_run_hash,
        "snapshot_hash": review.snapshot_hash,
        "final_mapping_hash": review.final_mapping_hash,
        "discovery_file_sha256": review.discovery_file_sha256,
        "policy": review.policy,
    }}


def align_verified_candidates(candidates, mapping, *, chunk_id, contract):
    """Copy schema labels only; original facts, values, IDs and anchors survive."""
    targets = mapping.review.concept_targets
    records = {}
    for record in mapping.records:
        if record.chunk_id == chunk_id and record.status == "mapped" and record.concept_id in targets:
            prior = records.get(record.verified_candidate_hash)
            if prior is not None and prior.concept_id != record.concept_id:
                raise ValueError("WINDOW_MAPPING_AMBIGUOUS_OBSERVATION")
            records[record.verified_candidate_hash] = record
    aligned, trace = [], []
    for original in candidates:
        candidate = copy.deepcopy(original)
        digest = canonical_sha256(original)
        record = records.get(digest)
        if record is not None:
            field = {"entity": "observed_type", "property": "observed_property",
                     "relationship": "observed_predicate"}[candidate["candidate_kind"]]
            if candidate[field] != record.observed_term or candidate["candidate_kind"] != record.kind:
                raise ValueError("WINDOW_MAPPING_CANDIDATE_DRIFT")
            candidate[field] = targets[record.concept_id]
            if candidate["candidate_kind"] == "entity":
                policy = resolve_identity_root_policy(
                    targets[record.concept_id], contract.candidate_model.entity_types,
                )
                keys = {}
                for key, value in candidate["identity_key"].items():
                    matches = {key} if key in policy.business_key_fields else {
                        approved_field
                        for concept in mapping.snapshot.concepts
                        if concept.kind == "property" and concept.concept_id in targets
                        and core._allowed(
                            record.concept_id, concept.owner_type_ids, concept, mapping.snapshot,
                        )
                        and key in {concept.concept_id, concept.name, *concept.aliases}
                        for approved_field in policy.business_key_fields
                        if approved_field in {
                            targets[concept.concept_id], concept.name, *concept.aliases,
                        }
                    }
                    mapped_key = next(iter(matches)) if len(matches) == 1 else key
                    if mapped_key in keys and keys[mapped_key] != value:
                        raise ValueError("WINDOW_MAPPING_IDENTITY_CONFLICT")
                    keys[mapped_key] = value
                candidate["identity_key"] = keys
            trace.append({
                "binding_kind": "reviewed_window_schema",
                "review_hash": mapping.review.artifact_hash,
                "observation_id": record.observation_id,
                "raw_candidate_hash": record.raw_candidate_hash,
                "verified_candidate_hash": digest,
                "aligned_candidate_hash": canonical_sha256(candidate),
                "field": field, "original_term": original[field],
                "approved_term": candidate[field],
                "source_type_id": record.source_type_id,
                "target_type_id": record.target_type_id,
                "owner_type_id": record.owner_type_id,
                "direction": record.direction,
                "original_identity_key": original.get("identity_key"),
                "aligned_identity_key": candidate.get("identity_key"),
            })
        aligned.append(candidate)
    return aligned, trace


def load_design_window_context(state_root, discovery_path):
    run = core.load_window_run(state_root)
    discovery = load_discovery(discovery_path)
    if run.state != "complete" or run.discovery_hash != discovery.run_hash:
        raise ValueError("WINDOW_DESIGN_REFERENCE_INCOMPLETE_OR_FOREIGN")
    return {
        "authority": "working_reference_only",
        "window_run_hash": run.artifact_hash,
        "discovery_hash": discovery.run_hash,
        "snapshot": run.final_snapshot.model_dump(mode="json"),
        "ontology_approved": False, "evidence_approved": False,
    }
