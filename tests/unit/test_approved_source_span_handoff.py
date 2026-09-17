"""Offline source-span handoff preserves provider proofs and honest partial scope."""

import base64
import hashlib
import json

import pytest

from fabric_kg_builder.cli import cli  # noqa: F401
from fabric_kg_builder.enrichment import approved_partial_handoff as partial
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.schema2_validation_stage import load_l3_inputs
from tests.unit.test_approved_donor_continuation import partial_donor, _snapshot  # noqa: F401
from tests.unit.test_approved_partial_handoff import _approve, _deny_network, _options
from tests.unit.test_approved_reextraction import _sdk
from tests.unit.test_approved_source_spans import _anchor
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


@pytest.fixture(params=core.SOURCE_SPAN_MODES)
def span_partial(partial_donor, request):
    mode = request.param
    options = {**partial_donor, "state_root": partial_donor["state_root"].with_name("source-span-donor")}

    class UnsupportedSchema(Exception):
        status_code = 400

    def response(request):
        if request["response_format"]["type"] == "json_schema":
            raise UnsupportedSchema("offline schema fallback")
        payload = json.loads(request["messages"][1]["content"])
        entity = next(item for item in payload["entity_types"] if not item["abstract"])
        label = payload["source_segments"]["segments"][0]["text"].split()[0]
        return {"candidates": [{
            "candidate_kind": "entity", "local_id": "fresh", "observed_type": entity["type_id"],
            "label": label, "anchors": [_anchor(payload["source_segments"])],
            "identity_key": {key: label for key in entity["identity_key_policy"]["business_key_fields"]},
        }]}

    sdk, requests = _sdk(response)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        core.run_approved_reextraction(
            **options, anchor_mode=mode,
            client_factory=lambda: FoundryClient(options["foundry_config"], _sdk_client=sdk),
        )
    assert len(requests) == 3
    return options


def _provider(state):
    for path in (state / "reextraction-provider-responses").glob("*.json"):
        artifact = json.loads(path.read_text())
        if artifact["raw_output_utf8_base64"]:
            return path, artifact
    pytest.fail("missing fixture provider output")


def _refresh(state):
    core._write_state(state / "reextraction-integrity.json", core._inventory(state))


def test_source_span_partial_plan_seal_and_l3_input_have_exact_provider_and_adapter_proofs(
    span_partial, monkeypatch,
):
    _deny_network(monkeypatch)
    options = _options(span_partial)
    before = _snapshot(span_partial["state_root"])
    plan = partial.run_partial_handoff(**options)
    mode = plan["anchor_mode"]
    assert mode in core.SOURCE_SPAN_MODES
    assert plan["completed_root_count"] == plan["selected_root_count"] == 1
    assert plan["approved_contract_root_count"] == 3
    assert plan["missing_root_count"] == 2
    assert plan["operator_excluded_completed_root_count"] == 0
    assert plan["new_logical_calls"] == plan["new_physical_calls"] == plan["writes"] == 0
    assert not options["state_root"].exists()
    result = _approve(options, plan)
    assert result["status"] == "succeeded" and result["remote_calls"] == 0
    retained = json.loads(next((options["state_root"] / "retained-responses").glob("*.json")).read_text())
    raw = retained["response"]
    decoded = base64.b64decode(retained["provider_output"]["raw_output_utf8_base64"]).decode()
    assert json.loads(decoded) == raw
    assert "quote" not in raw["candidates"][0]["anchors"][0]
    assert retained["anchor_adapter_version"] == core.source_span_adapter(mode).version
    assert retained["source_segments_sha256"] == core.response_hash(retained["source_segments"], mode)
    assert retained["resolved_response_hash"] == core.response_hash(retained["resolved_response"], mode)
    assert retained["resolved_response"]["candidates"][0]["anchors"][0]["quote"]
    inputs = load_l3_inputs(
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    assert inputs.l2_metrics.foundry_calls == 0
    assert inputs.partial_extraction_scope["plan"]["plan_hash"] == plan["plan_hash"]
    assert _snapshot(span_partial["state_root"]) == before


@pytest.mark.parametrize("change", ["missing", "different_decoded_response", "invalid_hash"])
def test_source_span_handoff_requires_exact_provider_bytes_not_just_raw_json(
    span_partial, monkeypatch, change,
):
    state = span_partial["state_root"]
    path, artifact = _provider(state)
    if change == "missing":
        path.unlink()
    else:
        output = b'{"candidates":[]}'
        artifact["raw_output_utf8_base64"] = base64.b64encode(output).decode()
        artifact["raw_output_sha256"] = (
            hashlib.sha256(output).hexdigest() if change != "invalid_hash" else "0" * 64
        )
        path.write_text(json.dumps(artifact))
    _refresh(state)
    before = _snapshot(state)
    _deny_network(monkeypatch)
    options = _options(span_partial)
    with pytest.raises(ValueError, match="PROVIDER_RESPONSE"):
        partial.run_partial_handoff(**options)
    assert not options["state_root"].exists()
    assert _snapshot(state) == before


def test_valid_provider_proof_with_invalid_source_ids_fails_whole_handoff(span_partial, monkeypatch):
    state = span_partial["state_root"]
    path = next((state / "reextraction-responses").glob("*.json"))
    cached = json.loads(path.read_text())
    cached["response"]["candidates"][0]["anchors"][0]["slice_id"] = "0" * 64
    cached["response_hash"] = core.response_hash(cached["response"], core.SOURCE_SPANS_MODE)
    path.write_text(json.dumps(cached))
    provider_path, artifact = _provider(state)
    output = json.dumps(cached["response"]).encode()
    artifact["raw_output_utf8_base64"] = base64.b64encode(output).decode()
    artifact["raw_output_sha256"] = hashlib.sha256(output).hexdigest()
    provider_path.write_text(json.dumps(artifact))
    _refresh(state)
    before = _snapshot(state)
    _deny_network(monkeypatch)
    options = _options(span_partial)
    with pytest.raises(ValueError, match="CANONICAL_PARSE_FAILED.*SOURCE_SPAN_UNRESOLVED"):
        partial.run_partial_handoff(**options)
    assert not options["state_root"].exists()
    assert _snapshot(state) == before


def test_source_span_handoff_does_not_accept_quote_review(span_partial, monkeypatch):
    _deny_network(monkeypatch)
    options = _options(span_partial)
    with pytest.raises(ValueError, match="SOURCE_SPAN_QUOTE_REVIEW_UNSUPPORTED"):
        partial.run_partial_handoff(**options, approved_quote_review=options["state_root"].parent / "unused-review.json")
    assert not options["state_root"].exists()
