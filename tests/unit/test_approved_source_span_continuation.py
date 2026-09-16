"""Exact v2 continuation uses paid selections without resetting physical spending."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_donor_continuation as continuation
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from tests.unit.test_approved_donor_continuation import (
    _child, _cli_args, _seal, _snapshot, partial_donor,  # noqa: F401
)
from tests.unit.test_approved_reextraction import _options, _sdk, _no_source_reads
from tests.unit.test_approved_source_spans import _anchor
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401

MODE = core.SOURCE_SPANS_V2_MODE


def _response(request):
    payload = json.loads(request["messages"][1]["content"])
    entity = next(item for item in payload["entity_types"] if not item["abstract"])
    label = payload["source_segments"]["segments"][0]["text"].split()[0]
    return {"candidates": [{
        "candidate_kind": "entity", "local_id": "fresh", "observed_type": entity["type_id"],
        "label": label, "anchors": [_anchor(payload["source_segments"])],
        "identity_key": {key: label for key in entity["identity_key_policy"]["business_key_fields"]},
    }]}


def _factory(options, sdk):
    return lambda: FoundryClient(options["foundry_config"], _sdk_client=sdk)


@pytest.fixture
def v2_donor(integrated_case):
    options = _options(integrated_case)
    sdk, requests = _sdk(_response)
    core.run_approved_reextraction(**options, anchor_mode=MODE, client_factory=_factory(options, sdk))
    assert len(requests) == 1
    return options


@pytest.fixture
def v2_partial(partial_donor):
    options = {**partial_donor, "state_root": partial_donor["state_root"].with_name("v2-partial")}

    class UnsupportedSchema(Exception):
        status_code = 400

    def response(request):
        if request["response_format"]["type"] == "json_schema":
            raise UnsupportedSchema("offline schema fallback")
        return _response(request)

    sdk, requests = _sdk(response)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        core.run_approved_reextraction(**options, anchor_mode=MODE, client_factory=_factory(options, sdk))
    assert len(requests) == 3
    return options


def _provider(state, request_hash=None):
    for path in (state / "reextraction-provider-responses").glob("*.json"):
        artifact = json.loads(path.read_text())
        if request_hash is None or artifact["request_hash"] == request_hash:
            return path, artifact
    pytest.fail("fixture provider artifact missing")


def _alter_provider(state, *, request_hash=None, raw=None, bad_hash=False):
    path, artifact = _provider(state, request_hash)
    data = json.dumps({"candidates": []} if raw is None else raw).encode()
    artifact["raw_output_utf8_base64"] = base64.b64encode(data).decode()
    artifact["raw_output_sha256"] = "0" * 64 if bad_hash else hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(artifact))
    _seal(state)


def test_completed_v2_root_reuse_exact_resume_and_ancestor_accounting(v2_donor, monkeypatch):
    _no_source_reads(monkeypatch)
    before = _snapshot(v2_donor["state_root"])
    child = _child(v2_donor)
    no_client = lambda: pytest.fail("fully cached continuation constructed a model")
    plan = continuation.run_approved_continuation(**child, dry_run=True, client_factory=no_client)
    assert plan["anchor_mode"] == MODE
    assert plan["remaining_work_units"] == plan["remote_calls"] == plan["writes"] == 0
    assert plan["reused_responses"] == 1 and plan["prior_spent"]["physical_calls"] == 1
    assert not child["state_root"].exists()
    result = continuation.run_approved_continuation(**child, client_factory=no_client)
    assert result["logical_calls"] == result["physical_calls"] == 0
    authority = json.loads((child["state_root"] / continuation.AUTHORITY).read_text())
    assert authority["code_identity"][continuation.CODE_PATH] == hashlib.sha256(Path(continuation.__file__).read_bytes()).hexdigest()
    assert {key: value for key, value in authority["code_identity"].items() if key != continuation.CODE_PATH} == core._code_identity()
    assert authority["anchor_adapter_version"] == core.SOURCE_SPANS_V2_ADAPTER_VERSION
    cached = json.loads(next((child["state_root"] / "reextraction-responses").glob("*.json")).read_text())
    original = json.loads(next((v2_donor["state_root"] / "reextraction-responses").glob("*.json")).read_text())
    assert cached["response"] == original["response"]
    assert cached["imported_from"]["request_hash"] == original["request_hash"]
    proof = json.loads(next((child["state_root"] / "reextraction-resolved-responses").glob("*.json")).read_text())
    assert proof["source_segments_sha256"] == core.response_hash(proof["source_segments"], MODE)
    assert proof["adapter_version"] == core.SOURCE_SPANS_V2_ADAPTER_VERSION
    child_before = _snapshot(child["state_root"])
    assert continuation.run_approved_continuation(**child, resume=True, client_factory=no_client) == result
    assert _snapshot(child["state_root"]) == child_before
    grandchild = _child(child, state_root=child["state_root"].with_name("grandchild"))
    reused = continuation.run_approved_continuation(**grandchild, client_factory=no_client)
    assert reused["physical_calls"] == 0
    assert reused["prior_spent"]["physical_calls"] == reused["lineage_spent"]["physical_calls"] == 1
    assert _snapshot(v2_donor["state_root"]) == before
    assert _snapshot(child["state_root"]) == child_before


def test_missing_roots_use_only_explicit_additional_budget_including_ancestor_retries(v2_partial):
    child = _child(v2_partial, max_calls=2, max_physical_calls=2)
    before = _snapshot(v2_partial["state_root"])
    plan = continuation.run_approved_continuation(**child, dry_run=True)
    assert plan["planned_chunks"] == 3 and plan["remaining_work_units"] == 2
    assert plan["prior_spent"] == {"logical_calls": 2, "physical_calls": 3, "reserved_output_tokens": 3 * 4096}
    assert plan["lineage_budget_ceiling"]["physical_calls"] == 5
    with pytest.raises(ValueError, match="BUDGET_TOO_SMALL"):
        continuation.run_approved_continuation(**{**child, "max_physical_calls": 1}, dry_run=True)
    assert not child["state_root"].exists()
    sdk, requests = _sdk(_response)
    result = continuation.run_approved_continuation(**child, client_factory=_factory(child, sdk))
    assert len(requests) == result["logical_calls"] == result["physical_calls"] == 2
    assert result["lineage_spent"]["logical_calls"] == 4
    assert result["lineage_spent"]["physical_calls"] == 5
    assert result["lineage_spent"]["reserved_output_tokens"] == 5 * 4096
    grandchild = _child(child, state_root=child["state_root"].with_name("next-v2"))
    next_plan = continuation.run_approved_continuation(**grandchild, dry_run=True)
    assert next_plan["prior_spent"] == result["lineage_spent"]
    assert next_plan["lineage_budget_ceiling"]["physical_calls"] == 5
    assert next_plan["additional_budget"]["physical_calls"] == 0
    assert _snapshot(v2_partial["state_root"]) == before


def test_split_parent_is_decoded_and_replayed_but_only_missing_children_are_paid(integrated_case):
    from fabric_kg_builder.enrichment.schema2_sources import load_l2_inputs

    options = _options(integrated_case)
    inputs = load_l2_inputs(l1_state_root=options["l1_state_root"], domain_path=options["domain_path"])
    count = inputs.domain_contract.reasoning_policy.max_relations_per_work_unit + 1

    def overflow(request):
        payload = json.loads(request["messages"][1]["content"])
        raw = _response(request)
        relationship = {
            "candidate_kind": "relationship", "source_local_id": "fresh", "target_local_id": "fresh",
            "observed_predicate": "relationship:x", "direction": "source_to_target",
            "anchor": _anchor(payload["source_segments"]),
        }
        raw["candidates"] += [deepcopy(relationship) for _ in range(count)]
        return raw

    sdk, calls = _sdk(overflow)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        core.run_approved_reextraction(**options, anchor_mode=MODE, client_factory=_factory(options, sdk))
    assert len(calls) == 1
    before = _snapshot(options["state_root"])
    child = _child(options, max_calls=2, max_physical_calls=2)
    plan = continuation.run_approved_continuation(**child, dry_run=True)
    assert plan["remaining_work_units"] == 2 and plan["reused_responses"] == 1
    sdk, requests = _sdk(_response)
    result = continuation.run_approved_continuation(**child, client_factory=_factory(child, sdk))
    assert len(requests) == result["logical_calls"] == result["physical_calls"] == 2
    assert result["lineage_spent"]["physical_calls"] == 3
    assert all(json.loads(request["messages"][1]["content"])["source_segments"]["segments"][0]["segment_id"] == "s1"
               for request in requests)
    imported = [json.loads(path.read_text()) for path in (child["state_root"] / "reextraction-responses").glob("*.json")
                if "imported_from" in json.loads(path.read_text())]
    assert len(imported) == 1 and len(imported[0]["response"]["candidates"]) == count + 1
    assert continuation.run_approved_continuation(
        **child, resume=True, client_factory=lambda: pytest.fail("split resume repaid"),
    ) == result
    assert _snapshot(options["state_root"]) == before


@pytest.mark.parametrize("change", ["missing", "bad_hash", "different_response"])
def test_provider_bytes_are_mandatory_even_after_inventory_reseal(v2_donor, change):
    state = v2_donor["state_root"]
    if change == "missing":
        path, _ = _provider(state)
        path.unlink()
        _seal(state)
    else:
        _alter_provider(state, bad_hash=change == "bad_hash")
    before = _snapshot(state)
    child = _child(v2_donor)
    with pytest.raises(ValueError, match="PROVIDER_RESPONSE"):
        continuation.run_approved_continuation(
            **child, dry_run=True, client_factory=lambda: pytest.fail("tamper called provider"),
        )
    assert not child["state_root"].exists()
    assert _snapshot(state) == before


@pytest.mark.parametrize("selection", ["unknown", "extra_authority"])
def test_provider_proven_response_with_bad_selection_is_never_silently_dropped(v2_donor, selection):
    state = v2_donor["state_root"]
    path = next((state / "reextraction-responses").glob("*.json"))
    cached = json.loads(path.read_text())
    anchor = cached["response"]["candidates"][0]["anchors"][0]
    if selection == "unknown":
        anchor["end_segment_id"] = "s999999"
    else:
        anchor["slice_id"] = "a" * 64
    cached["response_hash"] = core.response_hash(cached["response"], MODE)
    path.write_text(json.dumps(cached))
    _alter_provider(state, request_hash=cached["request_hash"], raw=cached["response"])
    before = _snapshot(state)
    child = _child(v2_donor)
    with pytest.raises(ValueError, match="SOURCE_SPAN_UNRESOLVED"):
        continuation.run_approved_continuation(**child, dry_run=True)
    assert not child["state_root"].exists()
    assert _snapshot(state) == before


@pytest.mark.parametrize("change", ["code", "scope", "catalog", "mode"])
def test_code_source_scope_catalog_and_mode_drift_fail_before_child(v2_donor, monkeypatch, change):
    child = _child(v2_donor)
    before = _snapshot(v2_donor["state_root"])
    if change == "code":
        identity = core._code_identity()
        monkeypatch.setattr(core, "_code_identity", lambda: {**identity, "enrichment/schema2_evidence.py": "d" * 64})
    elif change == "scope":
        base = continuation._base_authority

        def altered(*args, **kwargs):
            value = base(*args, **kwargs)
            value["scope"][0]["source_text_hash"] = "d" * 64
            return value
        monkeypatch.setattr(continuation, "_base_authority", altered)
    elif change == "catalog":
        prompt = continuation._prompt

        def altered(*args, **kwargs):
            value = json.loads(prompt(*args, **kwargs))
            value["source_segments"]["slice_id"] = "d" * 64
            return json.dumps(value)
        monkeypatch.setattr(continuation, "_prompt", altered)
    else:
        child["anchor_mode"] = core.SOURCE_SPANS_MODE
    with pytest.raises(ValueError, match="(SEMANTICS_DRIFT|REQUEST_EQUIVALENCE_UNPROVEN|ANCHOR_MODE_DRIFT)"):
        continuation.run_approved_continuation(**child, dry_run=True)
    assert not child["state_root"].exists()
    assert _snapshot(v2_donor["state_root"]) == before


def test_completed_child_resume_rechecks_own_provider_proofs(v2_partial):
    child = _child(v2_partial, max_calls=2, max_physical_calls=2)
    sdk, _ = _sdk(_response)
    continuation.run_approved_continuation(**child, client_factory=_factory(child, sdk))
    _alter_provider(child["state_root"], bad_hash=True)
    before = _snapshot(child["state_root"])
    with pytest.raises(ValueError, match="PROVIDER_RESPONSE"):
        continuation.run_approved_continuation(
            **child, resume=True, dry_run=True, client_factory=lambda: pytest.fail("resume paid again"),
        )
    assert _snapshot(child["state_root"]) == before


def test_partial_child_resume_uses_paid_local_failure_response_without_resampling(v2_partial, monkeypatch):
    from fabric_kg_builder.enrichment import schema2_stage

    child = _child(v2_partial, max_calls=2, max_physical_calls=2)
    sdk, requests = _sdk(_response)
    build = schema2_stage.build_candidate_batch

    def fail_after_paid_response(*args, **kwargs):
        if requests:
            raise ValueError("offline local processing failure")
        return build(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(schema2_stage, "build_candidate_batch", fail_after_paid_response)
        with pytest.raises(ValueError, match="offline local processing failure"):
            continuation.run_approved_continuation(**child, client_factory=_factory(child, sdk))
    assert len(requests) == 1
    paid = {
        path: path.read_bytes() for path in (child["state_root"] / "reextraction-responses").glob("*.json")
        if "imported_from" not in json.loads(path.read_text())
    }
    plan = continuation.run_approved_continuation(**child, resume=True, dry_run=True)
    assert plan["current_child_spent"]["physical_calls"] == 1
    assert plan["remaining_child_budget"]["physical_calls"] == 1
    result = continuation.run_approved_continuation(**child, resume=True, client_factory=_factory(child, sdk))
    assert result["physical_calls"] == result["logical_calls"] == len(requests) == 2
    assert result["lineage_spent"]["physical_calls"] == 5
    assert {path: path.read_bytes() for path in paid} == paid


def test_resume_snapshot_is_rechecked_after_provider_proof(v2_donor, monkeypatch):
    child = _child(v2_donor)
    continuation.run_approved_continuation(**child)
    original = continuation._run_child

    def changed_after_proof(**kwargs):
        (child["state_root"] / "new-file.json").write_text("{}")
        _seal(child["state_root"])
        return original(**kwargs)

    monkeypatch.setattr(continuation, "_run_child", changed_after_proof)
    with pytest.raises(ValueError, match="RESUME_PROVIDER_SNAPSHOT_DRIFT"):
        continuation.run_approved_continuation(**child, resume=True, dry_run=True)


def test_child_import_provenance_and_ancestor_spending_cannot_be_resealed_away(v2_donor):
    child = _child(v2_donor)
    continuation.run_approved_continuation(**child)
    path = next((child["state_root"] / "reextraction-responses").glob("*.json"))
    cached = json.loads(path.read_text())
    cached["imported_from"]["request_hash"] = "a" * 64
    path.write_text(json.dumps(cached))
    _seal(child["state_root"])
    with pytest.raises(ValueError, match="IMPORT_PROOF_INVALID"):
        continuation.run_approved_continuation(**child, resume=True, dry_run=True)


def test_recursive_ancestor_spending_cannot_be_reset_in_child_authority(v2_donor):
    from fabric_kg_builder.contracts.base import canonical_sha256

    child = _child(v2_donor)
    continuation.run_approved_continuation(**child)
    path = child["state_root"] / continuation.AUTHORITY
    authority = json.loads(path.read_text())
    authority["continuation"]["prior_spent"]["physical_calls"] = 0
    authority["fingerprint"] = canonical_sha256({
        key: value for key, value in authority.items() if key != "fingerprint"
    })
    path.write_text(json.dumps(authority))
    _seal(child["state_root"])
    grandchild = _child(child, state_root=child["state_root"].with_name("tampered-budget-grandchild"))
    with pytest.raises(ValueError, match="SEMANTICS_DRIFT"):
        continuation.run_approved_continuation(**grandchild, dry_run=True)
    assert not grandchild["state_root"].exists()


def test_v1_remains_unsupported_and_no_cross_mode_migration(integrated_case):
    options = _options(integrated_case)
    sdk, _ = _sdk()
    core.run_approved_reextraction(
        **options, anchor_mode=core.SOURCE_SPANS_MODE, client_factory=_factory(options, sdk),
    )
    before = _snapshot(options["state_root"])
    with pytest.raises(ValueError, match="DONOR_SOURCE_SPANS_UNSUPPORTED"):
        continuation.run_approved_continuation(**_child(options), dry_run=True)
    with pytest.raises(ValueError, match="ANCHOR_MODE_DRIFT"):
        continuation.run_approved_continuation(**_child(options), anchor_mode=MODE, dry_run=True)
    assert _snapshot(options["state_root"]) == before


def test_existing_cli_explicit_additional_zero_budget_plan_run_and_resume(v2_donor, monkeypatch):
    from fabric_kg_builder.config import loader

    options = _child(v2_donor)
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=options["foundry_config"]))
    args = [*_cli_args(options), "--approved-anchor-mode", MODE]
    for suffix in (["--dry-run"], [], ["--resume", "--dry-run"], ["--resume"]):
        result = CliRunner().invoke(cli, [*args, *suffix])
        assert result.exit_code == 0, result.output
        summary = json.loads(result.output)
        assert summary["anchor_mode"] == MODE
        assert summary["prior_spent"]["physical_calls"] == 1
        assert summary["additional_budget"]["logical_calls"] == summary["additional_budget"]["physical_calls"] == 0
