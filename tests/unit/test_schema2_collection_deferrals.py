"""Lossless partial-order exclusions across persisted, zero-call L2/L3 handoffs."""

from __future__ import annotations

import copy
import dataclasses
import json
from collections import Counter

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, deterministic_contract_id
from fabric_kg_builder.contracts.lifecycle import AssertionState
from fabric_kg_builder.enrichment.schema2_collections import (
    COLLECTION_DEFERRAL_KIND,
    CollectionDeferral,
)
from fabric_kg_builder.enrichment.schema2_evidence import L3StageError
from fabric_kg_builder.enrichment.schema2_extraction import (
    build_required_member_set_proposals,
    derive_collection_member_fragments,
)
from fabric_kg_builder.enrichment.schema2_sources import L2StageError
from fabric_kg_builder.enrichment.schema2_validation_stage import (
    _reconcile_collection_partition,
    load_l3_inputs,
    validate_collection_partition,
)
from fabric_kg_builder.serving.lifecycle_projection import build_l4_projection
from tests.unit import test_schema2_validation_stage as fixtures
from tests.unit.test_compact_ordered_c0 import ordered_contract, _observations
from tests.unit.test_schema2_extraction import _authority, _build, _identity


def _pipeline(tmp_path, monkeypatch, orders, *, roles=False, role=None):
    sentence = "A governed record describes " + "; ".join(
        f"governed subject {index}" for index in range(len(orders))
    ) + "."
    monkeypatch.setattr(fixtures, "_SENTENCE", sentence)

    def mutate(candidates, work_unit):
        records = [candidates[0]]
        for index, order in enumerate(orders):
            member = copy.deepcopy(candidates[1])
            member["local_id"] = f"subject-{index}"
            quote = f"governed subject {index}"
            start = work_unit.slice_start + work_unit.text.index(quote)
            member["anchors"] = [{
                "span_start": start, "span_end": start + len(quote),
                "quote": quote, "model_authored_evidence_id": None,
            }]
            relationship = {
                **candidates[2], "target_local_id": member["local_id"],
                "member_order": order, "member_role_id": role,
            }
            records.extend((member, relationship))
        return records

    return fixtures._pipeline(
        tmp_path, "records",
        fact_set=fixtures._fact_set("records", ordered=True, roles=roles, expected_count=None),
        mutate=mutate,
        member_properties=({
            "property_id": "property:records.member-order",
            "display_name": "Member Order", "value_type": "integer", "required": False,
        },),
    )


def _load(tmp_path, l1, domain):
    return load_l3_inputs(
        l2_state_root=tmp_path / ".fkg/l2",
        l1_state_root=l1, domain_path=domain,
    )


@pytest.mark.parametrize("orders", [
    (1, 2, 3, 4, 5, 6, 7), (1, 2, 3, 5), (None,), (0, 0), (0, 2),
])
def test_partial_positions_preserve_atoms_and_block_collection_through_l4(tmp_path, monkeypatch, orders):
    l1, domain, l2 = _pipeline(tmp_path, monkeypatch, orders)
    assert not l2.required_member_sets
    assert len(l2.collection_deferrals) == 1
    deferred = l2.collection_deferrals[0]
    assert Counter(item.member_order for item in deferred.observations) == Counter(orders)
    assert len({item.membership_relationship_candidate_id for item in deferred.observations}) == len(orders)
    assert sum(leaf.batch.retained_candidate_count for leaf in l2.leaves) == 1 + 2 * len(orders)
    assert sum(leaf.batch.input_candidate_count for leaf in l2.leaves) == 1 + 2 * len(orders)
    assert CollectionDeferral.model_validate_json(canonical_json(deferred)) == deferred
    l3 = fixtures._l3(tmp_path, l1, domain)
    assert l3.inputs.collection_deferrals == l2.collection_deferrals
    assert len(l3.candidate_results) == 1 + 2 * len(orders)
    assert all(item.current_state == AssertionState.ASSERTED.value for item in l3.candidate_results)
    assert all(item.evidence_span_ids for item in l3.candidate_results)
    assert not l3.required_member_manifests
    assert l3.blocked_completeness_scopes == (
        (deferred.authority.completeness_requirement_id, deferred.scope_canonical_id),
    )
    record = l3.required_member_outcomes[0]
    assert record.contract_version == "1.1.0"
    assert record.manifest is None
    assert record.outcome.completeness_state == "unresolved"
    assert record.outcome.readiness_state == "blocked"
    assert record.outcome.deferral_hash == deferred.deferral_hash
    payload = json.loads(next((l3.run_root / "required-member-outcomes").glob("*.json")).read_text())
    assert payload["readiness_state"] == "blocked"
    assert "required_member_set_proposal_id" not in payload
    assert not list((l3.run_root / "required-member-manifests").glob("*.json"))
    rows, *_ = build_l4_projection(l3)
    assert len(rows.semantic_asserted_entities) == 1 + len(orders)
    assert len(rows.semantic_asserted_relationships) == len(orders)
    assert not rows.semantic_required_member_manifests
    assert not rows.semantic_required_members


def test_deferrals_resume_without_calls_or_revalidation_and_reject_corrupt_storage(tmp_path, monkeypatch):
    l1, domain, l2 = _pipeline(tmp_path, monkeypatch, (1, 2, 3, 5))
    l3 = fixtures._l3(tmp_path, l1, domain)
    source_bytes = domain.read_bytes()
    deferral_path = next((tmp_path / ".fkg/l2/collection-deferrals").glob("*.json"))
    prior_bytes = deferral_path.read_bytes()

    class NoCalls:
        def complete(self, **_kwargs):
            pytest.fail("replay/resume must not call a model")

    resumed = fixtures._run_l2(tmp_path, "records", NoCalls(), l1, domain)
    assert resumed.receipt == l2.receipt
    assert resumed.collection_deferrals == l2.collection_deferrals
    assert deferral_path.read_bytes() == prior_bytes
    resumed_l3 = fixtures._l3(tmp_path, l1, domain)
    assert resumed_l3.recomputed_leaf_count == 0
    assert resumed_l3.reused_leaf_count == len(l3.leaves)
    assert resumed_l3.receipt == l3.receipt
    assert resumed_l3.required_member_outcomes == l3.required_member_outcomes
    assert domain.read_bytes() == source_bytes
    payload = json.loads(prior_bytes)
    payload["observations"][0]["member_order"] = 42
    deferral_path.write_text(canonical_json(payload) + "\n")
    with pytest.raises(L3StageError, match="hash does not recompute"):
        _load(tmp_path, l1, domain)
    with pytest.raises(ValueError, match="immutable L2 artifact collision"):
        fixtures._run_l2(tmp_path, "records", NoCalls(), l1, domain)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "rehashed-order", "reference", "batch-hash", "authority-hash"])
def test_l3_rederives_exhaustive_partition_instead_of_trusting_hashes(tmp_path, monkeypatch, mutation):
    l1, domain, _ = _pipeline(tmp_path, monkeypatch, (1, 2, 3, 5))
    inputs = _load(tmp_path, l1, domain)
    deferred = inputs.collection_deferrals[0]
    if mutation == "missing":
        records = ()
    elif mutation == "duplicate":
        records = (deferred, deferred)
    else:
        values = deferred.model_dump(mode="json", exclude={"deferral_hash", "collection_deferral_id"})
        observation = values["observations"][0]
        if mutation == "rehashed-order":
            observation["member_order"] = 42
        elif mutation == "reference":
            observation["membership_relationship_candidate_id"] = "relationship-candidate:foreign"
        elif mutation == "batch-hash":
            values["candidate_batch_hashes"][0][1] = "f" * 64
        else:
            values["authority"]["completeness_requirement_hash"] = "f" * 64
        values["observations"].sort(key=canonical_json)
        values["collection_deferral_id"] = deterministic_contract_id("collection-deferral", values)
        values["deferral_hash"] = canonical_sha256(values)
        forged = CollectionDeferral.model_validate_json(canonical_json(values))
        records = (forged,)
    with pytest.raises(L3StageError, match="derived proposal or deferral"):
        validate_collection_partition(dataclasses.replace(inputs, collection_deferrals=records))


def test_l3_deferral_outcome_cannot_be_upgraded_or_dropped(tmp_path, monkeypatch):
    l1, domain, _ = _pipeline(tmp_path, monkeypatch, (1,))
    l3 = fixtures._l3(tmp_path, l1, domain)
    original = l3.required_member_outcomes[0]
    for outcomes in (
        (),
        (original, original),
        (dataclasses.replace(original, outcome=dataclasses.replace(original.outcome, readiness_state="ready")),),
        (dataclasses.replace(original, outcome=dataclasses.replace(original.outcome, completeness_state="complete")),),
    ):
        with pytest.raises(L3StageError):
            _reconcile_collection_partition(
                proposals=(), deferrals=l3.inputs.collection_deferrals, outcomes=outcomes,
            )
    path = next((l3.run_root / "required-member-outcomes").glob("*.json"))
    values = json.loads(path.read_text())
    values["readiness_state"] = "ready"
    path.write_text(canonical_json(values) + "\n")
    with pytest.raises(L3StageError, match="immutable L3 artifact collision"):
        fixtures._l3(tmp_path, l1, domain)


def test_valid_collection_cannot_be_omitted_from_partition(tmp_path, monkeypatch):
    l1, domain, _ = _pipeline(tmp_path, monkeypatch, (0,))
    inputs = _load(tmp_path, l1, domain)
    with pytest.raises(L3StageError, match="derived proposal or deferral"):
        validate_collection_partition(dataclasses.replace(inputs, required_member_proposals=()))


@pytest.mark.parametrize("field,value", [
    ("member_role_id", "role:unknown"),
    ("member_candidate_id", "entity-candidate:foreign"),
    ("member_semantic_type_id", "semantic-type:unknown"),
])
def test_bad_references_and_roles_are_not_order_deferrals(ordered_contract, field, value):
    leaf = _build(ordered_contract, _observations((None,)))
    fragments = derive_collection_member_fragments((leaf,), contract=ordered_contract)
    broken = dataclasses.replace(fragments[0], **{field: value})
    deferrals = []
    with pytest.raises(L2StageError, match="structural violations"):
        build_required_member_set_proposals(
            (broken,), leaves=(leaf,), contract=ordered_contract,
            authority_factory=lambda _requirement: _authority(ordered_contract),
            base_identity=_identity(), deferrals=deferrals,
        )
    assert deferrals == []


@pytest.mark.parametrize("order", [0, 1, None])
@pytest.mark.parametrize("role", [
    "not-a-role", "role:unspecified", "role:default", "role:none",
    "role:placeholder", "role:unknown", "role:step:unspecified",
])
def test_approved_malformed_roles_fail_before_order_partition(ordered_contract, order, role):
    requirement = ordered_contract.completeness_requirements[0]
    fact_set = requirement.structured_fact_set.model_copy(update={"member_role_ids": [role]})
    contract = ordered_contract.model_copy(update={
        "completeness_requirements": [
            requirement.model_copy(update={"structured_fact_set": fact_set}),
        ],
    })
    observations = _observations((order,))
    for observation in observations:
        if observation["candidate_kind"] == "relationship":
            observation["member_role_id"] = role
    leaf = _build(contract, observations)
    fragments = derive_collection_member_fragments((leaf,), contract=contract)
    assert fragments[0].member_role_id == role
    deferrals = []
    with pytest.raises(ValidationError, match="member_role_id"):
        build_required_member_set_proposals(
            fragments, leaves=(leaf,), contract=contract,
            authority_factory=lambda _requirement: _authority(contract),
            base_identity=_identity(), deferrals=deferrals,
        )
    assert not deferrals


def test_missing_required_role_still_fails_before_collection_deferral(tmp_path, monkeypatch):
    with pytest.raises(L2StageError, match="approved required roles"):
        _pipeline(tmp_path, monkeypatch, (None,), roles=True)
    assert not (tmp_path / ".fkg/l2/stage-receipt.json").exists()


def test_identity_conflict_is_not_a_partial_order_deferral(ordered_contract):
    leaf = _build(ordered_contract, _observations((1,)))
    fragments = derive_collection_member_fragments((leaf,), contract=ordered_contract)
    deferrals = []
    with pytest.raises(L2StageError, match="L2_REQUIRED_MEMBER_IDENTITY_CONFLICT"):
        build_required_member_set_proposals(
            (*fragments, dataclasses.replace(fragments[0], member_order=2)),
            leaves=(leaf,), contract=ordered_contract,
            authority_factory=lambda _requirement: _authority(ordered_contract),
            base_identity=_identity(), deferrals=deferrals,
        )
    assert not deferrals


def test_valid_and_deferred_groups_partition_without_changing_observations(ordered_contract):
    observations = _observations((0, 1))
    second = _observations((1, 2, 3, 5))
    for item in second:
        for field in ("local_id", "source_local_id", "target_local_id", "owner_local_id"):
            if field in item:
                item[field] = "other-" + item[field]
        if item["candidate_kind"] == "entity":
            item["identity_key"] = {key: "other-" + value for key, value in item["identity_key"].items()}
    observations.extend(second)
    before = copy.deepcopy(observations)
    leaf = _build(ordered_contract, observations)
    fragments = derive_collection_member_fragments((leaf,), contract=ordered_contract)
    deferrals = []
    views = build_required_member_set_proposals(
        fragments, leaves=(leaf,), contract=ordered_contract,
        authority_factory=lambda _requirement: _authority(ordered_contract),
        base_identity=_identity(), deferrals=deferrals,
    )
    assert observations == before
    assert len(views) == len(deferrals) == 1
    assert {views[0].aggregate_entity_id, deferrals[0].scope_canonical_id} == {
        item.aggregate_entity_id for item in fragments
    }
    assert sum(len(item.proposal.members) for item in views) + sum(len(item.observations) for item in deferrals) == len(fragments)


def test_legacy_successful_handoff_retains_versions_and_serialized_carriers(tmp_path, monkeypatch):
    from fabric_kg_builder.enrichment import schema2_stage
    from fabric_kg_builder.enrichment.schema2_validation_stage import L3_ACCEPTED_VERSIONS

    legacy = {key: value for key, value in schema2_stage.L2_OUTPUT_ACCEPTED_VERSIONS.items()
              if key != COLLECTION_DEFERRAL_KIND}
    monkeypatch.setattr(schema2_stage, "L2_OUTPUT_ACCEPTED_VERSIONS", legacy)
    monkeypatch.setattr(schema2_stage, "COLLECTION_PARTITION_VERSION", None)
    l1, domain, l2 = _pipeline(tmp_path, monkeypatch, (0,))
    assert not l2.collection_deferrals
    before = {path: path.read_bytes() for path in (tmp_path / ".fkg/l2").rglob("*.json")}
    l3 = fixtures._l3(tmp_path, l1, domain)
    assert l3.receipt.accepted_contract_versions == L3_ACCEPTED_VERSIONS
    assert len(l3.required_member_manifests) == 1
    assert l3.required_member_outcomes[0].contract_version == "1.0.0"
    assert "collection_deferral_id" not in l3.required_member_outcomes[0].payload()
    assert not l3.blocked_completeness_scopes
    build_l4_projection(l3)
    assert all(path.read_bytes() == value for path, value in before.items())


def test_prior_partition_deferral_remains_readable_under_stricter_validation(tmp_path, monkeypatch):
    from fabric_kg_builder.enrichment import schema2_stage

    assert schema2_stage.COLLECTION_PARTITION_VERSION == "l2-collection-partition/1.0.1"
    with monkeypatch.context() as previous:
        previous.setattr(schema2_stage, "COLLECTION_PARTITION_VERSION", "l2-collection-partition/1.0.0")
        l1, domain, l2 = _pipeline(tmp_path, previous, (1,))
    before = {path: path.read_bytes() for path in (tmp_path / ".fkg/l2").rglob("*.json")}
    l3 = fixtures._l3(tmp_path, l1, domain)
    assert l3.inputs.collection_deferrals == l2.collection_deferrals
    assert len(l3.blocked_completeness_scopes) == 1
    assert not l3.required_member_manifests
    build_l4_projection(l3)
    assert all(path.read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize("role", ["role:unspecified", "not-a-role"])
def test_previous_malformed_role_deferral_fails_l3_rederivation(tmp_path, monkeypatch, role):
    from fabric_kg_builder.enrichment import schema2_extraction, schema2_stage

    original_fact_set = fixtures._fact_set

    def malformed_fact_set(*args, **kwargs):
        values = original_fact_set(*args, **kwargs)
        values["member_role_ids"] = [role]
        return values

    # Emulate the previous branch's skipped C0 member validation only while
    # generating this synthetic partial-order checkpoint.
    with monkeypatch.context() as previous:
        previous.setattr(fixtures, "_fact_set", malformed_fact_set)
        previous.setattr(schema2_stage, "COLLECTION_PARTITION_VERSION", "l2-collection-partition/1.0.0")
        previous.setattr(
            schema2_extraction.RequiredMemberReferenceV1_1,
            "seal", lambda **values: None,
        )
        l1, domain, l2 = _pipeline(tmp_path, previous, (1,), roles=True, role=role)
    assert len(l2.collection_deferrals) == 1
    with pytest.raises(L3StageError, match="member_role_id"):
        fixtures._l3(tmp_path, l1, domain)


def test_legacy_capability_cannot_turn_invalid_order_into_success(tmp_path, monkeypatch):
    l1, domain, _ = _pipeline(tmp_path, monkeypatch, (1,))
    inputs = _load(tmp_path, l1, domain)
    legacy_versions = {
        key: value for key, value in inputs.l2_receipt.accepted_contract_versions.items()
        if key != COLLECTION_DEFERRAL_KIND
    }
    values = inputs.l2_receipt.model_dump(mode="python", exclude={"receipt_hash"})
    values["accepted_contract_versions"] = legacy_versions
    values["receipt_hash"] = canonical_sha256({
        key: value for key, value in values.items()
        if key not in {"started_at_utc", "completed_at_utc"}
    })
    legacy = dataclasses.replace(
        inputs, collection_deferrals=(),
        l2_receipt=type(inputs.l2_receipt).model_validate(values),
    )
    with pytest.raises(L3StageError, match="observed unique contiguous zero-based positions"):
        validate_collection_partition(legacy)


def test_interrupted_l2_collection_storage_resumes_from_atomic_checkpoints(tmp_path, monkeypatch):
    from fabric_kg_builder.enrichment import schema2_stage

    persist = schema2_stage._persist_json

    def interrupt(path, payload):
        if path.parent.name == "collection-deferrals":
            raise OSError("injected collection-storage interruption")
        return persist(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(schema2_stage, "_persist_json", interrupt)
        with pytest.raises(OSError, match="injected"):
            _pipeline(tmp_path, monkeypatch, (1, 2, 3, 5))
    assert not (tmp_path / ".fkg/l2/stage-receipt.json").exists()
    assert list((tmp_path / ".fkg/l2/checkpoint-leaves").glob("*.json"))

    class NoCalls:
        def complete(self, **kwargs):
            pytest.fail("interrupted extraction must reuse atomic checkpoints")

    l1, domain = tmp_path / ".fkg/l1", tmp_path / "domain.yaml"
    resumed = fixtures._run_l2(tmp_path, "records", NoCalls(), l1, domain)
    assert resumed.metrics.foundry_calls == 0
    assert len(resumed.collection_deferrals) == 1
    l3 = fixtures._l3(tmp_path, l1, domain)
    assert l3.blocked_completeness_scopes


def test_public_window_replay_defers_without_model_calls_or_domain_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from click.testing import CliRunner
    from fabric_kg_builder.cli import cli, domain_design_cmd
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from tests.unit import test_window_run_approved_cli as integrated
    from tests.unit.test_domain_discovery_cli import _invoke

    class OrderedModel(integrated.IntegratedModel):
        def complete_json(self, **request):
            result = super().complete_json(**request)
            if "types" in result:
                result["properties"].append({
                    "owner_key": "subject", "key": "ordinal", "display_name": "Ordinal",
                    "value_type": "integer", "required": False,
                })
                result["completeness"][0].update(
                    kind="collection", ordered=True, ordinal_property_key="subject.ordinal",
                )
            elif "candidates" in result and result["candidates"]:
                # This synthetic fixture emits only its grounded observations.
                result["candidates"] = result["candidates"][:4]
                result["candidates"][2]["member_order"] = 1
            return result

    monkeypatch.setattr(integrated, "IntegratedModel", OrderedModel)
    root, source, _, windows, model, l1, domain, _ = integrated.integrated_case.__wrapped__(
        tmp_path, SimpleNamespace(param="concepts"),
    )
    review = root / "review.json"
    integrated._review(windows, domain, review)
    before = {path: path.read_bytes() for path in [domain, review, *windows.rglob("*.json")]}
    call_count = len(model.calls)
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("no model during replay"))
    l2 = root / "l2-deferred"
    args = integrated._replay_args(source, windows, domain, l1, l2, review)
    first = _invoke(args)
    resumed = _invoke(args)
    assert first == resumed
    assert first["l2_model_calls"] == first["targeted_model_calls"] == 0
    assert first["collection_deferral_count"] == 1
    assert first["original_candidate_ledger_complete"]
    assert len(model.calls) == call_count
    assert all(path.read_bytes() == value for path, value in before.items())
    l3 = root / "l3-deferred"
    result = CliRunner().invoke(cli, [
        "validate-evidence", "--state", str(l3), "--l2-state", str(l2),
        "--l1-state", str(l1), "--domain", str(domain),
    ])
    assert result.exit_code == 0, result.output
    assert "completeness/readiness blocked: 1" in result.output
    validated = run_l3(state_root=l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    assert validated.inputs.collection_deferrals[0].observations[0].member_order == 1
    assert not validated.required_member_manifests
    rows, *_ = build_l4_projection(validated)
    assert rows.semantic_asserted_entities
    assert rows.semantic_asserted_relationships
    assert rows.semantic_asserted_properties
    assert not rows.semantic_required_member_manifests
