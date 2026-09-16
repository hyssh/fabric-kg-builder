"""Compact ordered requirements must reach C0 without inventing member order."""

import copy
import json

import pytest

from fabric_kg_builder.domain.compact import COMPACT_TRANSFORMATION_VERSION
from fabric_kg_builder.domain.design import (
    DESIGN_COMPILER_VERSION, compile_domain_design, evaluate_domain_design,
)
from fabric_kg_builder.domain.stage import finalize_l1_stage
from fabric_kg_builder.enrichment.schema2_extraction import (
    build_required_member_set_proposals, derive_collection_member_fragments,
    extraction_leaf_from_dict, extraction_leaf_to_dict,
)
from fabric_kg_builder.enrichment.schema2_sources import L2StageError
from tests.unit.test_domain_design import Client, _ordered_sketch, _preflight, generate_domain_design
from tests.unit.test_schema2_extraction import _authority, _build, _identity, _response


@pytest.fixture
def ordered_contract(tmp_path):
    preflight = _preflight(tmp_path)
    raw = _ordered_sketch()
    raw["types"][1]["identity_property_keys"] = ["label"]
    raw["properties"][1]["required"] = True
    draft = generate_domain_design(preflight, client=Client(raw))
    before = draft.model_dump_json()
    prepared = compile_domain_design(draft, evaluate_domain_design(draft), preflight=preflight)
    assert draft.model_dump_json() == before
    assert COMPACT_TRANSFORMATION_VERSION == "compact-to-schema2-1.8.0"
    assert DESIGN_COMPILER_VERSION == "domain-design-compiler/1.5.0"
    assert any(
        "approval-reviewed full-sequence requirement, not observed order" in item
        for item in prepared.candidates.assumptions
    )
    contract = finalize_l1_stage(prepared, decision="approve", actor="offline-reviewer", persist=False).contract
    assert contract.approval.status == "approved"
    fact_set = contract.completeness_requirements[0].structured_fact_set
    assert fact_set.cardinality is None
    assert fact_set.ordering_policy.model_dump() == {
        "mode": "ordered", "ordinal_property_id": "property:subject.ordinal",
        "ordinal_value_type": "integer", "direction": "ascending",
        "unique_ordinals": True, "contiguous": True,
    }
    assert fact_set.collection_identity_policy.ordinals_included is True
    assert fact_set.collection_identity_policy.preserve_member_order is True
    return contract


def _observations(orders):
    response = _response()[:3]
    aggregate, member, membership = response
    aggregate.update(observed_type="Record", identity_key={"property:record.text": "record-a"})
    member.update(observed_type="Subject")
    membership.update(observed_predicate="Describes")
    result = [aggregate]
    for index, order in enumerate(orders):
        observed_member = copy.deepcopy(member)
        observed_member.update(local_id=f"member-{index}", identity_key={"property:subject.label": f"member-{index}"})
        observed_membership = copy.deepcopy(membership)
        observed_membership.update(target_local_id=f"member-{index}", member_order=order)
        result.extend([observed_member, observed_membership])
        if order is not None:
            result.append({
                "candidate_kind": "property", "owner_local_id": f"member-{index}",
                "observed_property": "Ordinal", "value": order, "normalized_value": order,
                "temporal_key": None, "anchor": None,
            })
    return result


def _proposals(contract, observations):
    leaf = _build(contract, observations)
    assert all(item.approved_semantic_id is not None for item in leaf.proposed_candidates)
    fragments = derive_collection_member_fragments((leaf,), contract=contract)
    expected_orders = [item.get("member_order") for item in observations if item["candidate_kind"] == "relationship"]
    assert len(fragments) == len(expected_orders)
    assert sorted((item.member_order for item in fragments), key=str) == sorted(expected_orders, key=str)
    return build_required_member_set_proposals(
        fragments, leaves=(leaf,), contract=contract,
        authority_factory=lambda _requirement: _authority(contract), base_identity=_identity(),
    )


@pytest.mark.parametrize("orders", [(0,), (0, 1), (1, 0)])
def test_compiled_order_requirement_reaches_c0_without_claiming_cardinality(ordered_contract, orders):
    observations = _observations(orders)
    before = copy.deepcopy(observations)
    views = _proposals(ordered_contract, observations)
    assert observations == before
    assert len(views) == 1
    proposal = views[0].proposal
    assert proposal.identity.contract_version == "1.1.0"
    assert proposal.ordering_policy.member_order_encoding == "zero_based_contiguous"
    assert [member.member_order for member in proposal.members] == sorted(orders)
    assert proposal.expected_cardinality is proposal.minimum_cardinality is proposal.maximum_cardinality is None
    assert all(member.supporting_evidence_span_ids == () for member in proposal.members)
    assert "completeness_status" not in proposal.model_dump()


@pytest.mark.parametrize("orders", [(None,), (1,), (0, 2), (0, 0)])
def test_compiled_order_requirement_never_repairs_missing_shifted_duplicate_or_gapped_orders(ordered_contract, orders):
    observations = _observations(orders)
    before = copy.deepcopy(observations)
    with pytest.raises(L2StageError, match="observed unique contiguous zero-based positions"):
        _proposals(ordered_contract, observations)
    assert observations == before


def test_compiled_order_requirement_does_not_invent_absent_collection_members(ordered_contract):
    assert _proposals(ordered_contract, _observations(())) == ()


@pytest.mark.parametrize("orders", [(1, 2, 3, 4, 5, 6, 7), (1, 2, 3, 5)])
def test_partial_order_checkpoint_preserves_atomic_accounting_but_cannot_mint_c0_proposal(ordered_contract, orders):
    leaf = _build(ordered_contract, _observations(orders))
    checkpoint = json.dumps(extraction_leaf_to_dict(leaf), sort_keys=True)
    restored = extraction_leaf_from_dict(json.loads(checkpoint))
    assert restored == leaf
    assert restored.batch.retained_candidate_count == 1 + 3 * len(orders)
    assert restored.batch.input_candidate_count == restored.batch.retained_candidate_count
    assert len(restored.batch.candidate_dispositions) == restored.batch.input_candidate_count
    fragments = derive_collection_member_fragments((restored,), contract=ordered_contract)
    assert sorted(fragment.member_order for fragment in fragments) == list(orders)
    with pytest.raises(L2StageError, match="observed unique contiguous zero-based positions"):
        build_required_member_set_proposals(
            fragments, leaves=(restored,), contract=ordered_contract,
            authority_factory=lambda _requirement: _authority(ordered_contract),
            base_identity=_identity(),
        )
    assert json.dumps(extraction_leaf_to_dict(restored), sort_keys=True) == checkpoint


def test_unordered_compact_collection_keeps_unspecified_order_policy(tmp_path):
    preflight = _preflight(tmp_path)
    raw = _ordered_sketch()
    raw["completeness"][0].update(ordered=False, ordinal_property_key=None)
    draft = generate_domain_design(preflight, client=Client(raw))
    prepared = compile_domain_design(draft, evaluate_domain_design(draft), preflight=preflight)
    fact_set = prepared.proposal.draft_contract.completeness_requirements[0].structured_fact_set
    assert fact_set.ordering_policy.mode == "unordered"
    assert fact_set.ordering_policy.contiguous is None
    assert fact_set.ordering_policy.unique_ordinals is None
    assert fact_set.cardinality is None
    assert not any("full-sequence" in item for item in prepared.candidates.assumptions)
