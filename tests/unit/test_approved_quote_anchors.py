"""Quote-first is a lossless model adapter, never an evidence-validation bypass."""

import base64
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.approved_donor_continuation import run_approved_continuation
from fabric_kg_builder.enrichment.approved_few_shot import generate_examples, render_system_prompt
from fabric_kg_builder.enrichment.approved_quote_anchors import (
    ADAPTER_VERSION, OFFSET_MODE, QUOTE_MODE, QuoteAnchorResolutionError,
    quote_response_schema, resolve_quote_response,
)
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.schema2_extraction import ProposedAnchor, RawCandidateResponse
from tests.unit.test_approved_donor_continuation import _child, _snapshot
from tests.unit.test_approved_few_shot import _domain
from tests.unit.test_approved_reextraction import _config, _options, _sdk
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401


def _records(quote="α😀 e\u0301"):
    return {"candidates": [
        {"candidate_kind": "entity", "local_id": " e ", "observed_type": "type:x",
         "label": "α😀", "identity_key": {"name": " e\u0301 "}, "anchors": [{"quote": quote}]},
        {"candidate_kind": "property", "owner_local_id": " e ", "observed_property": "property:x",
         "value": " e\u0301 ", "normalized_value": " e\u0301 ", "anchor": {"quote": quote}},
        {"candidate_kind": "relationship", "source_local_id": " e ", "target_local_id": " e ",
         "observed_predicate": "relationship:x", "direction": "source_to_target",
         "governed_context": {"text": " e\u0301 "}, "anchor": {"quote": quote}},
    ]}


def test_exact_codepoints_and_all_candidate_fields_preserved():
    raw = _records()
    before = deepcopy(raw)
    text = "章 | α😀 e\u0301 | конец"
    result = resolve_quote_response(raw, source_text=text, slice_start=37, slice_end=37 + len(text))
    assert raw == before
    for old, new in zip(raw["candidates"], result["candidates"]):
        field = "anchors" if old["candidate_kind"] == "entity" else "anchor"
        assert {k: v for k, v in old.items() if k != field} == {k: v for k, v in new.items() if k != field}
        anchor = new[field][0] if field == "anchors" else new[field]
        assert anchor == {"quote": "α😀 e\u0301", "span_start": 41, "span_end": 46,
                          "model_authored_evidence_id": None}
        assert ProposedAnchor.model_validate(anchor).quote == "α😀 e\u0301"
    RawCandidateResponse.model_validate(result)


@pytest.mark.parametrize(("quote", "text", "reason"), [
    ("missing", "present", "QUOTE_NOT_FOUND_IN_AUTHORIZED_SLICE"),
    ("x", "x and x", "QUOTE_AMBIGUOUS_IN_AUTHORIZED_SLICE"),
    ("aa", "aaa", "QUOTE_AMBIGUOUS_IN_AUTHORIZED_SLICE"),
    ("é", "e\u0301", "QUOTE_NOT_FOUND_IN_AUTHORIZED_SLICE"),
    (" x", " x", "QUOTE_BOUNDARY_WHITESPACE_UNSUPPORTED"),
    ("x\n", "x\n", "QUOTE_BOUNDARY_WHITESPACE_UNSUPPORTED"),
    ("\u00a0x", "\u00a0x", "QUOTE_BOUNDARY_WHITESPACE_UNSUPPORTED"),
    ("", "", "QUOTE_EMPTY_OR_INVALID"),
])
def test_invalid_quotes_fail_closed_with_every_candidate_accounted(quote, text, reason):
    raw = _records(quote)
    before = deepcopy(raw)
    with pytest.raises(QuoteAnchorResolutionError) as error:
        resolve_quote_response(raw, source_text=text, slice_start=5, slice_end=5 + len(text))
    assert len(error.value.diagnostics) == 3
    assert {item["reason"] for item in error.value.diagnostics} == {reason}
    assert raw == before


def test_only_authorized_slice_counts_and_interior_whitespace_is_exact():
    source = "same outside | same\t\ninside | same outside"
    start, end = source.index("same\t"), source.index(" | same outside")
    result = resolve_quote_response(
        _records("same"), source_text=source[start:end], slice_start=start, slice_end=end,
    )
    assert result["candidates"][0]["anchors"][0]["span_start"] == start
    result = resolve_quote_response(
        _records("same\t\ninside"), source_text=source[start:end], slice_start=start, slice_end=end,
    )
    assert result["candidates"][0]["anchors"][0]["quote"] == "same\t\ninside"
    with pytest.raises(QuoteAnchorResolutionError):
        resolve_quote_response(_records("outside"), source_text=source[start:end],
                               slice_start=start, slice_end=end)


@pytest.mark.parametrize("anchor", [{"quote": "x", "span_start": 0}, {"quote": "x", "model_authored_evidence_id": None}, None])
def test_offset_or_evidence_fields_never_accepted_as_quote_only(anchor):
    raw = _records("x")
    raw["candidates"][1]["anchor"] = anchor
    with pytest.raises(QuoteAnchorResolutionError, match="UNRESOLVED"):
        resolve_quote_response(raw, source_text="x", slice_start=0, slice_end=1)


def test_missing_entity_and_property_quotes_fail_closed():
    raw = _records("x")
    raw["candidates"][0]["anchors"] = []
    del raw["candidates"][1]["anchor"]
    with pytest.raises(QuoteAnchorResolutionError) as error:
        resolve_quote_response(raw, source_text="x", slice_start=0, slice_end=1)
    assert len(error.value.diagnostics) == 2


def test_schema_prompt_and_replaceable_examples_agree_and_hash_rendered_values():
    contract = _domain()
    authority = core.prompt_authority(contract, anchor_mode=QUOTE_MODE)
    schema = quote_response_schema()
    assert schema["$defs"]["ProposedAnchor"]["properties"] == {"quote": {"type": "string", "minLength": 1}}
    assert "span_start" not in json.dumps(schema)
    assert "model_authored_evidence_id" not in json.dumps(schema)
    assert authority["anchor_adapter_version"] == ADAPTER_VERSION
    assert authority["system_prompt_sha256"] == hashlib.sha256(authority["system_prompt"].encode()).hexdigest()
    assert "span_start" not in authority["system_prompt"]
    assert "recompute every anchor" not in authority["system_prompt"]
    supplied = generate_examples(contract, unique_quotes=True)
    before = deepcopy(supplied)
    rendered, examples = render_system_prompt(
        authority["system_prompt_template"], contract, few_shot=supplied, quote_only=True,
    )
    assert supplied == before
    assert rendered == authority["system_prompt"]
    assert authority["few_shot_hash"] == core.response_hash(examples, QUOTE_MODE)
    assert authority["few_shot_hash"] != core.response_hash(supplied, QUOTE_MODE)
    assert {c["candidate_kind"] for c in examples[0]["response"]["candidates"]} == {"entity", "property", "relationship"}
    for example in examples:
        response = resolve_quote_response(
            example["response"], source_text=example["source_text"],
            slice_start=example["slice_start"], slice_end=example["slice_end"],
        )
        RawCandidateResponse.model_validate(response)
    assert "enrichment/approved_quote_anchors.py" in core._code_identity()
    legacy = core.prompt_authority(contract)
    assert "anchor_mode" not in legacy
    assert legacy["response_schema"] == core.raw_candidate_response_schema()


def test_custom_example_boundary_whitespace_is_not_silently_stripped():
    contract = _domain()
    supplied = generate_examples(contract, unique_quotes=True)
    supplied[0]["response"]["candidates"][0]["anchors"][0]["quote"] += "\n"
    with pytest.raises(ValueError, match="quote boundary whitespace"):
        render_system_prompt(core.SYSTEM_PROMPT, contract, few_shot=supplied, quote_only=True)


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
def test_lossless_provider_output_capture_and_separate_canonical_adapter(tmp_path, route):
    raw = _records()
    wire_text = " \n" + json.dumps(raw, ensure_ascii=False, indent=3) + "\n "
    sdk, _ = _sdk()
    sdk.chat.completions.create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=wire_text), finish_reason="stop")],
    ))
    sdk.responses = SimpleNamespace(create=Mock(return_value=SimpleNamespace(
        output_text=wire_text, status="completed",
    )))
    config = _config().model_copy(update={"inference_api": route})
    ledger = core._BudgetLedger(tmp_path, max_calls=1, max_physical_calls=1, max_output_tokens=4096)
    ledger.request_hash = "abc"
    client = core._bounded_client(config, ledger, lambda: FoundryClient(config, _sdk_client=sdk))
    response = client.complete_json(system="extract", user="source", json_schema=quote_response_schema(),
                                    max_completion_tokens=4096, max_attempts=1)
    path = tmp_path / "reextraction-responses" / "abc.json"
    core.persist_response(path, {"response": response}, QUOTE_MODE)
    unit = SimpleNamespace(work_unit_id="w", source_unit_id="s", source_text_hash="h",
                           text="α😀 e\u0301", slice_start=9, slice_end=14)
    enriched = core.adapt_response(response, work_unit=unit, state=tmp_path,
                                  request_hash="abc", anchor_mode=QUOTE_MODE)
    original = json.loads(path.read_text())["response"]
    assert original == raw
    assert original["candidates"][0]["anchors"][0] == {"quote": "α😀 e\u0301"}
    artifact = json.loads(next((tmp_path / "reextraction-provider-responses").glob("*.json")).read_text())
    assert base64.b64decode(artifact["raw_output_utf8_base64"]).decode() == wire_text
    assert artifact["raw_output_sha256"] == hashlib.sha256(wire_text.encode()).hexdigest()
    resolved = json.loads(next((tmp_path / "reextraction-resolved-responses").glob("*.json")).read_text())
    assert resolved["response"] == enriched
    assert enriched["candidates"][0]["anchors"][0]["span_start"] == 9
    assert original["candidates"][1]["value"] == " e\u0301 "


@pytest.mark.parametrize("kind", ["entity", "property", "relationship"])
def test_unresolved_response_is_retained_and_resumable_without_paid_resampling(integrated_case, kind):
    options = _options(integrated_case)
    raw = _records("not present in this approved source")
    raw["candidates"] = [c for c in raw["candidates"] if c["candidate_kind"] == kind]
    sdk, requests = _sdk(raw)
    with pytest.raises(QuoteAnchorResolutionError):
        core.run_approved_reextraction(
            **options, anchor_mode=QUOTE_MODE,
            client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    assert len(requests) == 1
    state = options["state_root"]
    saved = json.loads(next((state / "reextraction-responses").glob("*.json")).read_text())
    assert saved["response"] == raw
    diagnostics = json.loads(next((state / "reextraction-anchor-diagnostics").glob("*.json")).read_text())
    assert diagnostics["status"] == "unresolved_fail_closed"
    assert len(diagnostics["diagnostics"]) == 1
    assert not (state / "approved-reextraction-result.json").exists()
    snapshot = _snapshot(state)
    with pytest.raises(QuoteAnchorResolutionError):
        core.run_approved_reextraction(
            **options, resume=True, client_factory=lambda: pytest.fail("No paid resampling"),
        )
    assert _snapshot(state) == snapshot


def test_quote_run_canonical_processing_resume_and_donor_inherit_mode(integrated_case):
    options = _options(integrated_case)
    emitted = []

    def response(request):
        payload = json.loads(request["messages"][1]["content"])
        assert "source_offset_rule" not in payload
        assert not any("add slice_start" in rule for rule in payload["rules"])
        entity = next(item for item in payload["entity_types"] if not item["abstract"])
        quote = payload["source_text"].strip()
        raw = {"candidates": [{
            "candidate_kind": "entity", "local_id": "fresh", "observed_type": entity["type_id"],
            "label": "governed subject", "identity_key": {
                field: "governed subject" for field in entity["identity_key_policy"]["business_key_fields"]
            }, "anchors": [{"quote": quote}],
        }]}
        emitted.append(raw)
        return raw

    sdk, requests = _sdk(response)
    result = core.run_approved_reextraction(
        **options, anchor_mode=QUOTE_MODE, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert result["anchor_mode"] == QUOTE_MODE
    assert len(requests) == 1
    state = options["state_root"]
    raw = json.loads(next((state / "reextraction-responses").glob("*.json")).read_text())
    assert raw["response"] == emitted[0]
    leaves = [json.loads(p.read_text()) for p in (state / "checkpoint-leaves").glob("*.json")]
    assert sum(leaf["raw_candidate_count"] for leaf in leaves) == 1
    before = _snapshot(state)
    assert core.run_approved_reextraction(
        **options, resume=True, client_factory=lambda: pytest.fail("Resume called provider"),
    ) == result
    child = _child(options)
    reused = run_approved_continuation(**child, client_factory=lambda: pytest.fail("Donor called provider"))
    assert reused["anchor_mode"] == QUOTE_MODE
    assert reused["logical_calls"] == reused["physical_calls"] == 0
    assert _snapshot(state) == before
    assert run_approved_continuation(
        **child, resume=True, client_factory=lambda: pytest.fail("Child resume called provider"),
    ) == reused
    with pytest.raises(ValueError, match="ANCHOR_MODE_DRIFT"):
        core.run_approved_reextraction(**options, resume=True, anchor_mode=OFFSET_MODE, dry_run=True)
    with pytest.raises(ValueError, match="ANCHOR_MODE_DRIFT"):
        run_approved_continuation(**_child(options, state_root=state.parent / "wrong-mode"),
                                  anchor_mode=OFFSET_MODE, dry_run=True)


def test_legacy_authority_never_silently_upgraded(integrated_case):
    options = _options(integrated_case)
    sdk, _ = _sdk()
    result = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    state = options["state_root"]
    authority = json.loads((state / "approved-reextraction-authority.json").read_text())
    assert "anchor_mode" not in authority
    assert core.resolve_anchor_mode(sealed_state=state) == OFFSET_MODE
    assert result["anchor_mode"] == OFFSET_MODE
    with pytest.raises(ValueError, match="ANCHOR_MODE_DRIFT"):
        run_approved_continuation(**_child(options), anchor_mode=QUOTE_MODE, dry_run=True)
    # Actual historical code identities cannot be reinterpreted by changed code.
    authority["code_identity"]["enrichment/approved_reextraction.py"] = "historical"
    core._write_state(state / "approved-reextraction-authority.json", authority)
    core._write_state(state / "reextraction-integrity.json", core._inventory(state))
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        core.run_approved_reextraction(**options, resume=True, dry_run=True)


def test_anchor_cli_option_requires_approved_mode(tmp_path):
    result = CliRunner().invoke(cli, ["enrich", "--input", str(tmp_path),
                                     "--approved-anchor-mode", QUOTE_MODE])
    assert result.exit_code != 0
    assert "--approved-anchor-mode" in result.output


@pytest.mark.parametrize(("model", "explicit", "expected"), [
    ("gpt-5.4", None, QUOTE_MODE),
    ("gpt-4.1", None, OFFSET_MODE),
    ("gpt-5.4", OFFSET_MODE, OFFSET_MODE),
    ("gpt-4.1", QUOTE_MODE, QUOTE_MODE),
])
def test_cli_mode_is_explicit_or_safe_fresh_default(integrated_case, monkeypatch, model, explicit, expected):
    from tests.unit.test_approved_token_budgets import _args, _configure

    options = _options(integrated_case)
    _configure(monkeypatch, options, model=model)
    args = [*_args(options), "--dry-run"]
    if explicit is not None:
        args += ["--approved-anchor-mode", explicit]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["anchor_mode"] == expected


def test_quote_donor_requires_original_provider_output(integrated_case):
    options = _options(integrated_case)
    sdk, _ = _sdk()
    core.run_approved_reextraction(
        **options, anchor_mode=QUOTE_MODE, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    state = options["state_root"]
    provider = next((state / "reextraction-provider-responses").glob("*.json"))
    provider.unlink()
    core._write_state(state / "reextraction-integrity.json", core._inventory(state))
    with pytest.raises(ValueError, match="PROVIDER_RESPONSE_DRIFT"):
        run_approved_continuation(**_child(options), dry_run=True)


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
@pytest.mark.parametrize("mode", [QUOTE_MODE, OFFSET_MODE])
def test_entire_sdk_request_has_one_consistent_anchor_contract(integrated_case, route, mode):
    from fabric_kg_builder.enrichment.approved_input_budget import account_request, request_envelope
    from fabric_kg_builder.enrichment.approved_quote_anchors import QUOTE_RULE

    options = _options(integrated_case)
    config = options["foundry_config"].model_copy(update={"inference_api": route})
    options.update(foundry_config=config, max_output_tokens=64000, max_context_tokens=200000)
    sdk, calls = _sdk()
    if route == "project_responses":
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text='{"candidates":[]}', status="completed")
        sdk.responses = SimpleNamespace(create=create)

    plan = core.run_approved_reextraction(**options, anchor_mode=mode, dry_run=True)
    core.run_approved_reextraction(
        **options, anchor_mode=mode,
        client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    authority = json.loads(
        (options["state_root"] / "approved-reextraction-authority.json").read_text()
    )
    assert len(calls) == 1
    request = calls[0]
    user = request["input"].split("\n", 1)[1] if route == "project_responses" else request["messages"][1]["content"]
    payload = json.loads(user)
    full_request = json.dumps(request, ensure_ascii=False)
    schema = quote_response_schema() if mode == QUOTE_MODE else core.raw_candidate_response_schema()
    assert core.response_hash(schema, mode) == core.response_hash(authority["response_schema"], mode)
    expected = request_envelope(
        config, system=authority["system_prompt"], user=user,
        schema=schema, output=64000,
    )
    assert json.loads(full_request) == json.loads(json.dumps(expected))
    accounting = account_request(request, authority["input_budget"])
    assert accounting["estimated_total_tokens_upper_bound"] == (
        plan["input_budget"]["maxima"]["estimated_total_tokens_upper_bound"]
    )
    assert payload["source_identity"]["slice_start"] >= 0
    assert payload["source_identity"]["slice_end"] > payload["source_identity"]["slice_start"]
    numeric_rule = "Proposed source anchors use Unicode codepoint offsets and are not verified evidence."
    if mode == QUOTE_MODE:
        assert payload["source_quote_rule"] == QUOTE_RULE
        assert QUOTE_RULE in payload["rules"]
        for forbidden in (
            "span_start", "span_end", "source_offset_rule", numeric_rule,
            "add slice_start", "absolute Unicode codepoint offsets",
            "recompute every anchor",
        ):
            assert forbidden not in full_request
        assert 'Do not output or calculate character offsets' in full_request
    else:
        assert numeric_rule in payload["rules"]
        assert "source_offset_rule" in payload
        assert "source_quote_rule" not in payload
        assert "span_start" in full_request and "span_end" in full_request
