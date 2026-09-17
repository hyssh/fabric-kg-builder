"""Local-reference repair is a scoped, reversible proof, not semantic deduplication."""

from copy import deepcopy
import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_local_identifiers as ids
from fabric_kg_builder.enrichment import approved_partial_handoff as partial
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.schema2_validation_stage import load_l3_inputs
from tests.unit.test_approved_donor_continuation import partial_donor, _snapshot  # noqa: F401
from tests.unit.test_approved_budget_limited_partial import bounded_child, _handoff_options  # noqa: F401
from tests.unit.test_approved_partial_handoff import _approve, _deny_network, _options
from tests.unit.test_approved_source_span_handoff import span_partial  # noqa: F401
from tests.unit.test_approved_source_span_continuation import v2_partial  # noqa: F401
from tests.unit.test_schema2_extraction import _build, _domain, _response, _source_unit
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


def _scope(start=0, end=36, source=None):
    return {
        "source_unit_id": source or _source_unit().source_unit_id,
        "slice_start": start, "slice_end": end,
    }


def _raw():
    candidates = _response()[:3]
    for candidate in candidates:
        if candidate["candidate_kind"] == "entity":
            candidate["identity_key"] = {}
    candidates.append({
        "candidate_kind": "property", "owner_local_id": "equipment-1",
        "observed_property": "Reading", "value": "  Cafe\u0301\t<&nbsp;>\r\n ",
        "normalized_value": "  Cafe\u0301\t<&nbsp;>\r\n ", "temporal_key": "test",
    })
    return {"candidates": candidates}


def _stable_domain():
    contract = _domain()
    entities = [
        entity.model_copy(update={"identity_key_policy": entity.identity_key_policy.model_copy(update={
            "key_mode": "stable_source_identity", "business_key_fields": [],
        })})
        for entity in contract.candidate_model.entity_types
    ]
    return contract.model_copy(update={
        "candidate_model": contract.candidate_model.model_copy(update={"entity_types": entities}),
    })


def _records(response, contract=None):
    return _build(contract or _stable_domain(), response["candidates"]).proposed_candidates


def test_same_source_same_type_reused_local_ids_are_distinct_only_across_slice_bounds():
    raw = _raw()
    first, proof = ids.qualify_response(raw, **_scope(0, 18))
    second, _ = ids.qualify_response(raw, **_scope(18, 36))
    first_again, _ = ids.qualify_response(raw, **_scope(0, 18))
    original = _records(raw)
    left, right = _records(first), _records(second)
    assert first == first_again
    for kind in ("entity", "relationship", "property"):
        before = {r.semantic_id for r in original if r.candidate_kind == kind}
        a = {r.semantic_id for r in left if r.candidate_kind == kind}
        b = {r.semantic_id for r in right if r.candidate_kind == kind}
        assert a and a.isdisjoint(b) and a.isdisjoint(before)
    assert proof["scope"] == _scope(0, 18)
    assert all(a.candidate_id != b.candidate_id for a, b in zip(left, right))


def test_all_endpoints_and_owners_are_rewired_by_canonical_materializer():
    projected, _ = ids.qualify_response(_raw(), **_scope())
    records = _records(projected)
    entities = {record.local_reference: record for record in records if record.candidate_kind == "entity"}
    raw = projected["candidates"]
    relation = next(record for record in records if record.candidate_kind == "relationship")
    prop = next(record for record in records if record.candidate_kind == "property")
    assert relation.proposed_source_entity_id == entities[raw[2]["source_local_id"]].semantic_id
    assert relation.proposed_target_entity_id == entities[raw[2]["target_local_id"]].semantic_id
    assert prop.proposed_owner_entity_id == entities[raw[3]["owner_local_id"]].semantic_id


def test_input_and_every_non_reference_field_are_lossless_and_unchanged():
    original = _raw()
    original["candidates"][0]["label"] = " Stra\u00dfe Cafe\u0301 <td> "
    before = deepcopy(original)
    projected, proof = ids.qualify_response(original, **_scope())
    assert original == before
    for a, b in zip(original["candidates"], projected["candidates"]):
        fields = ids.REFERENCE_FIELDS[a["candidate_kind"]]
        assert ids.lossless_hash({k: v for k, v in a.items() if k not in fields}) == ids.lossless_hash(
            {k: v for k, v in b.items() if k not in fields},
        )
    assert proof["input_response_sha256"] == ids.lossless_hash(original)
    assert proof["output_response_sha256"] == ids.lossless_hash(projected)
    assert ids.verify_projection(original, {"response": projected, "proof": proof}, **_scope()) == projected


def test_casefold_duplicates_stay_duplicates_unknown_references_stay_unresolved():
    original = _raw()
    original["candidates"][0]["local_id"] = " Stra\u00dfe "
    duplicate = deepcopy(original["candidates"][0])
    duplicate["local_id"] = "STRASSE"
    original["candidates"].append(duplicate)
    original["candidates"][2]["source_local_id"] = "strasse"
    original["candidates"][2]["target_local_id"] = "unknown"
    original["candidates"][3]["owner_local_id"] = " UNKNOWN "
    projected, proof = ids.qualify_response(original, **_scope())
    records = _records(projected)
    raw = projected["candidates"]
    assert raw[0]["local_id"].casefold() == raw[-1]["local_id"].casefold() == raw[2]["source_local_id"].casefold()
    assert raw[0]["local_id"] != raw[-1]["local_id"]
    assert raw[2]["target_local_id"].casefold() == raw[3]["owner_local_id"].casefold()
    assert len(raw) == len(original["candidates"])
    assert len(proof["mapping"]) == 3
    entities = [r for r in records if r.candidate_kind == "entity"]
    before = _build(_stable_domain(), original["candidates"])
    after = _build(_stable_domain(), projected["candidates"])
    assert before.raw_candidate_count == after.raw_candidate_count == 5
    assert dict(before.audit_reason_counts)["CANDIDATE_PAYLOAD_CONFLICT"] == dict(
        after.audit_reason_counts,
    )["CANDIDATE_PAYLOAD_CONFLICT"] == 1
    assert len(entities) == 2 and len({r.local_reference.casefold() for r in entities}) == 2
    known_ids = {r.semantic_id for r in entities}
    assert next(r for r in records if r.candidate_kind == "relationship").proposed_target_entity_id not in known_ids
    assert next(r for r in records if r.candidate_kind == "property").proposed_owner_entity_id not in known_ids


def test_business_key_and_explicit_native_stable_ids_and_admission_are_unchanged():
    business = {"candidates": _response()[:3]}
    projected, _ = ids.qualify_response(business, **_scope())
    assert {r.semantic_id for r in _records(business, _domain())} == {
        r.semantic_id for r in _records(projected, _domain())
    }
    native = _raw()
    for candidate in native["candidates"]:
        if candidate["candidate_kind"] == "entity":
            candidate["stable_source_identity"] = f"{_scope()['source_unit_id']}:{candidate['local_id']}"
    projected, proof = ids.qualify_response(native, **_scope())
    assert projected == native
    assert all(row["native_stable_identity"] for row in proof["mapping"])
    before, after = _records(native), _records(projected)
    assert [(r.semantic_id, r.approved_semantic_id) for r in before] == [
        (r.semantic_id, r.approved_semantic_id) for r in after
    ]
    assert all(r.approved_semantic_id for r in after if r.candidate_kind == "entity")


def test_native_group_preserves_casefold_duplicates_and_unknown_aliases_without_waiver():
    native = _raw()
    native["candidates"][0]["stable_source_identity"] = "invalid-native-id-stays-invalid"
    duplicate = deepcopy(native["candidates"][0])
    duplicate["local_id"] = "FACILITY-A"
    duplicate["stable_source_identity"] = None
    native["candidates"].append(duplicate)
    projected, proof = ids.qualify_response(native, **_scope())
    assert projected["candidates"][0] == native["candidates"][0]
    assert projected["candidates"][-1] == native["candidates"][-1]
    assert projected["candidates"][2]["source_local_id"] == "facility-a"
    assert next(r for r in proof["mapping"] if r["reference"] == "facility-a")["native_stable_identity"]
    rejected_before = [r for r in _records(native) if r.local_reference == "facility-a"]
    rejected_after = [r for r in _records(projected) if r.local_reference == "facility-a"]
    assert rejected_before[0].approved_semantic_id is rejected_after[0].approved_semantic_id is None
    assert rejected_before[0].semantic_id == rejected_after[0].semantic_id


def test_namespace_depends_only_on_exact_source_and_bounds_not_type_or_response_metadata():
    first, a = ids.qualify_response(_raw(), **_scope())
    other = _raw()
    other["candidates"][0]["observed_type"] = "Other type"
    other["candidates"][0]["label"] = "Different semantic label"
    second, b = ids.qualify_response(other, **_scope())
    assert a["namespace"] == b["namespace"]
    assert first["candidates"][0]["local_id"] == second["candidates"][0]["local_id"]
    for scope in (_scope(1, 36), _scope(0, 35), _scope(source="another:source")):
        _, different = ids.qualify_response(_raw(), **scope)
        assert a["namespace"] != different["namespace"]


def test_fixed_length_nfc_casefold_classes_retain_duplicates_without_false_conflicts():
    original = {"candidates": [
        {**_raw()["candidates"][0], "local_id": value}
        for value in (" \u00c91 ", " E\u03011 ", "\u00e91")
    ]}
    projected, proof = ids.qualify_response(original, **_scope())
    references = [row["local_id"] for row in projected["candidates"]]
    assert len(references) == 3
    assert len({value.casefold() for value in references}) == len(proof["mapping"]) == 1
    assert references[0] == references[1] != references[2]
    assert all(len(value) == 55 and value.isascii() for value in references)
    assert ids.normalize_reference(" \u00c91 ") == ids.normalize_reference(" E\u03011 ") == "\u00e91"
    before = _build(_stable_domain(), original["candidates"])
    after = _build(_stable_domain(), projected["candidates"])
    assert before.raw_candidate_count == after.raw_candidate_count == 3
    assert dict(before.audit_reason_counts)["CANDIDATE_PAYLOAD_CONFLICT"] > 0
    assert dict(after.audit_reason_counts)["CANDIDATE_PAYLOAD_CONFLICT"] > 0
    for response in (original["candidates"][:2], projected["candidates"][:2]):
        assert "CANDIDATE_PAYLOAD_CONFLICT" not in dict(_build(_stable_domain(), response).audit_reason_counts)
    long = {"candidates": [{**original["candidates"][0], "local_id": "e" * 100000}]}
    bounded, _ = ids.qualify_response(long, **_scope(source="source" * 10000))
    assert len(bounded["candidates"][0]["local_id"]) == 55


def test_same_normalized_refs_rewire_nfc_endpoint_and_owner_aliases():
    original = _raw()
    original["candidates"][0]["local_id"] = "\u00c91"
    original["candidates"][2]["source_local_id"] = " E\u03011 "
    original["candidates"][3]["owner_local_id"] = "\u00e91"
    projected, _ = ids.qualify_response(original, **_scope())
    references = [
        projected["candidates"][0]["local_id"],
        projected["candidates"][2]["source_local_id"],
        projected["candidates"][3]["owner_local_id"],
    ]
    assert len({value.casefold() for value in references}) == 1
    records = _records(projected)
    owner = next(r.semantic_id for r in records if r.local_reference == references[0])
    assert next(r for r in records if r.candidate_kind == "relationship").proposed_source_entity_id == owner
    assert next(r for r in records if r.candidate_kind == "property").proposed_owner_entity_id == owner


def test_digest_and_spelling_collisions_fail_closed(monkeypatch):
    original = _raw()
    with monkeypatch.context() as patch:
        patch.setattr(ids, "_reference_digest", lambda *args: "lq:" + "a" * 52)
        with pytest.raises(ValueError, match="NAMESPACE_COLLISION"):
            ids.qualify_response(original, **_scope())
        single = {"candidates": [original["candidates"][0]]}
        _, first = ids.qualify_response(single, **_scope(0, 18))
        _, second = ids.qualify_response(single, **_scope(18, 36))
        ids.verify_namespace_collisions([first, first])
        with pytest.raises(ValueError, match="NAMESPACE_COLLISION"):
            ids.verify_namespace_collisions([first, second])
    original["candidates"].append({**original["candidates"][0], "local_id": "FACILITY-A"})
    monkeypatch.setattr(ids, "_spelling_variant", lambda reference, spelling: reference)
    with pytest.raises(ValueError, match="SPELLING_COLLISION"):
        ids.qualify_response(original, **_scope())


def test_cross_response_native_literal_collision_is_not_silently_merged():
    single = {"candidates": [_raw()["candidates"][0]]}
    projected, first = ids.qualify_response(single, **_scope(0, 18))
    native = {"candidates": [{
        **single["candidates"][0], "local_id": projected["candidates"][0]["local_id"],
        "stable_source_identity": "native",
    }]}
    _, second = ids.qualify_response(native, **_scope(18, 36))
    for proofs in ([first, second], [second, first]):
        with pytest.raises(ValueError, match="NAMESPACE_COLLISION"):
            ids.verify_namespace_collisions(proofs)


@pytest.mark.parametrize("change", ["mapping", "scope", "input_hash", "output_hash", "assertions", "extra", "value", "reference"])
def test_forged_projections_fail_even_with_self_consistent_output_hash(change):
    original = _raw()
    projected, proof = ids.qualify_response(original, **_scope())
    artifact = {"response": projected, "proof": proof}
    if change == "mapping":
        proof["mapping"][0]["qualified_reference"] = "forged"
    elif change == "scope":
        proof["scope"]["slice_end"] += 1
    elif change == "input_hash":
        proof["input_response_sha256"] = "0" * 64
    elif change == "output_hash":
        proof["output_response_sha256"] = "0" * 64
    elif change == "assertions":
        proof["assertions"]["only_reference_fields_changed"] = 1
    elif change == "extra":
        artifact["ignored"] = True
    elif change == "value":
        projected["candidates"][3]["value"] = "forged"
        proof["output_response_sha256"] = ids.lossless_hash(projected)
    else:
        projected["candidates"][0]["local_id"] = "forged"
        proof["output_response_sha256"] = ids.lossless_hash(projected)
    with pytest.raises(ValueError, match="PROJECTION_DRIFT"):
        ids.verify_projection(original, artifact, **_scope())


@pytest.mark.parametrize("scope", [
    _scope(-1, 36), _scope(36, 36), _scope(37, 36), _scope(True, 36),
    _scope(0, 36.0), {**_scope(), "source_unit_id": ""},
])
def test_invalid_authoritative_scope_is_not_coerced(scope):
    with pytest.raises(ValueError, match="SCOPE_INVALID"):
        ids.qualify_response(_raw(), **scope)


def test_collision_with_native_literal_namespace_is_rejected_not_merged():
    original = _raw()
    projected, _ = ids.qualify_response(original, **_scope())
    native = deepcopy(original["candidates"][0])
    native["local_id"] = projected["candidates"][0]["local_id"]
    native["stable_source_identity"] = "native"
    original["candidates"].append(native)
    with pytest.raises(ValueError, match="NAMESPACE_COLLISION"):
        ids.qualify_response(original, **_scope())


def test_cli_optin_is_visible_and_default_false():
    from fabric_kg_builder.cli.partial_handoff_cmd import handoff_partial_cmd

    help_result = CliRunner().invoke(cli, ["handoff-partial", "--help"])
    assert help_result.exit_code == 0
    assert "--qualify-local-identifiers" in help_result.output
    option = next(p for p in handoff_partial_cmd.params if p.name == "qualify_local_identifiers")
    assert option.default is False


def test_qualification_of_bounded_continuation_retains_ancestry_spend_and_missing_roots(
    bounded_child, monkeypatch,
):
    _deny_network(monkeypatch)
    before = _snapshot(bounded_child["state_root"])
    options = {**_handoff_options(bounded_child), "qualify_local_identifiers": True}
    plan = partial.run_partial_handoff(**options)
    assert plan["source_execution_profile"]["completion_expectation"] == "budget_limited_partial"
    assert plan["completed_root_count"] == plan["completed_leaf_count"] == 2
    assert plan["missing_root_count"] == 1 and plan["approved_contract_root_count"] == 3
    assert plan["prior_spent"]["physical_calls"] == 4
    _approve(options, plan)
    inputs = load_l3_inputs(
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    assert inputs.l2_metrics.foundry_calls == 0
    retained = [
        json.loads(path.read_bytes()) for path in (options["state_root"] / "retained-responses").glob("*.json")
    ]
    assert len(retained) == 2
    assert len({row["provenance"]["donor_fingerprint"] for row in retained}) == 2
    assert all(ids.ARTIFACT_KEY in row for row in retained)
    assert _snapshot(bounded_child["state_root"]) == before


@pytest.mark.parametrize("response", [
    [], {"candidates": None}, {"candidates": [], "extra": True},
    {"candidates": [{"candidate_kind": []}]},
    {"candidates": [{"candidate_kind": "entity", "local_id": " "}]},
    {"candidates": [{"candidate_kind": "relationship", "source_local_id": "e1"}]},
])
def test_malformed_projection_inputs_are_never_coerced_or_dropped(response):
    with pytest.raises(ValueError, match="APPROVED_LOCAL_IDENTIFIERS_"):
        ids.qualify_response(response, **_scope())


def test_qualified_source_handoff_plan_binding_raw_proof_and_canonical_l3_input(span_partial, monkeypatch):
    _deny_network(monkeypatch)
    original_options = _options(span_partial)
    options = {**original_options, "qualify_local_identifiers": True}
    before = _snapshot(span_partial["state_root"])
    unqualified = partial.run_partial_handoff(**original_options)
    assert ids.PROFILE_KEY not in unqualified
    assert ids.CODE_PATH not in unqualified["partial_code_identity"]
    plan = partial.run_partial_handoff(**options)
    assert plan["plan_hash"] != unqualified["plan_hash"]
    assert plan[ids.PROFILE_KEY] == ids.profile()
    assert plan["partial_code_identity"][ids.CODE_PATH] == partial._local_identifier_code_hash()
    assert plan["writes"] == plan["remote_calls"] == 0
    assert plan["missing_root_count"] == unqualified["missing_root_count"] == 2
    with pytest.raises(ValueError, match="EXACT_PLAN_APPROVAL_REQUIRED"):
        _approve(original_options, plan)
    with pytest.raises(ValueError, match="EXACT_PLAN_APPROVAL_REQUIRED"):
        _approve(options, unqualified)
    assert not options["state_root"].exists()
    _approve(options, plan)
    inputs = load_l3_inputs(
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    assert inputs.l2_metrics.foundry_calls == 0
    assert inputs.partial_extraction_scope["plan"][ids.PROFILE_KEY] == ids.profile()
    retained = json.loads(next((options["state_root"] / "retained-responses").glob("*.json")).read_bytes())
    assert retained["response"]["candidates"][0]["local_id"] == "fresh"
    artifact = retained[ids.ARTIFACT_KEY]
    qualified = artifact["response"]["candidates"][0]["local_id"]
    assert qualified.startswith("lq:") and len(qualified) == 55
    assert retained["provenance"]["response_hash"] == core.response_hash(retained["response"], plan["anchor_mode"])
    assert artifact["proof"]["input_response_sha256"] == ids.lossless_hash(retained["resolved_response"])
    records = [row for partition in inputs.proposed_partitions.values() for row in partition]
    assert any(row.local_reference == qualified for row in records)
    assert _snapshot(span_partial["state_root"]) == before
    for change in ("flag", "version", "helper", "missing-profile", "extra"):
        forged = deepcopy(plan)
        if change == "flag":
            forged[ids.PROFILE_KEY]["qualify_local_identifiers"] = 1
        elif change == "version":
            forged[ids.PROFILE_KEY]["version"] = "unreviewed"
        elif change == "helper":
            forged["partial_code_identity"][ids.CODE_PATH] = "0" * 64
        elif change == "missing-profile":
            del forged[ids.PROFILE_KEY]
        else:
            forged[ids.PROFILE_KEY]["ignored"] = True
        with pytest.raises(ValueError, match="LOCAL_IDENTIFIER_PROFILE_DRIFT"):
            partial._verify_local_identifier_projections(options["state_root"], forged)
    monkeypatch.setattr(partial, "_local_identifier_code_hash", lambda: "0" * 64)
    with pytest.raises(ValueError, match="LOCAL_IDENTIFIER_PROFILE_DRIFT"):
        load_l3_inputs(
            l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
            domain_path=options["domain_path"],
        )


@pytest.mark.parametrize("change", ["projection", "resolved", "provider", "range", "catalog", "missing"])
def test_read_scope_rederives_projections_not_just_inventory_hashes(span_partial, monkeypatch, change):
    _deny_network(monkeypatch)
    options = {**_options(span_partial), "qualify_local_identifiers": True}
    plan = partial.run_partial_handoff(**options)
    _approve(options, plan)
    state = options["state_root"]
    path = next((state / "retained-responses").glob("*.json"))
    retained = json.loads(path.read_bytes())
    if change == "projection":
        retained[ids.ARTIFACT_KEY]["response"]["candidates"][0]["local_id"] = "forged"
        retained[ids.ARTIFACT_KEY]["proof"]["output_response_sha256"] = ids.lossless_hash(
            retained[ids.ARTIFACT_KEY]["response"],
        )
    elif change == "resolved":
        retained["resolved_response"]["candidates"][0]["label"] = "forged"
        retained["resolved_response_hash"] = core.response_hash(retained["resolved_response"], plan["anchor_mode"])
    elif change == "provider":
        retained["provider_output"]["request_hash"] = "0" * 64
    elif change == "range":
        retained["range"]["slice_start"] += 1
    elif change == "catalog":
        retained["source_segments"]["slice_id"] = "0" * 64
        retained["source_segments_sha256"] = core.response_hash(retained["source_segments"], plan["anchor_mode"])
    else:
        del retained[ids.ARTIFACT_KEY]
    path.write_text(json.dumps(retained, ensure_ascii=False), encoding="utf-8")
    core._write_state(state / partial.INTEGRITY, partial._inventory(state))
    with pytest.raises(ValueError, match="LOCAL_IDENTIFIER_PROOF_INVALID"):
        load_l3_inputs(
            l2_state_root=state, l1_state_root=options["l1_state_root"], domain_path=options["domain_path"],
        )
