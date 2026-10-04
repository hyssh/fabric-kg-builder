"""Request-local selections do not ask the model to retype source authority."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.approved_few_shot import generate_examples, render_system_prompt
from fabric_kg_builder.enrichment.approved_input_budget import account_request, request_envelope
from fabric_kg_builder.enrichment.approved_quote_anchors import (
    SOURCE_SPANS_MODE, SOURCE_SPANS_V2_MODE, SOURCE_SPANS_ADAPTER_VERSION,
    SOURCE_SPANS_V2_ADAPTER_VERSION, SOURCE_SPANS_V2_RULE, SourceSpanResolutionError,
    source_segments, source_segments_v2, resolve_source_span_response,
    resolve_source_span_v2_response, source_span_v2_response_schema,
)
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.schema2_extraction import RawCandidateResponse
from tests.unit.test_approved_donor_continuation import _snapshot
from tests.unit.test_approved_few_shot import _domain
from tests.unit.test_approved_reextraction import _options, _sdk, _no_source_reads
from tests.unit.test_approved_source_spans import _source, _response, _anchor
from tests.unit.test_approved_token_budgets import _args, _configure
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401


def test_v1_catalog_prompt_schema_examples_and_resolution_remain_exactly_unchanged():
    source = _source()
    catalog = source_segments(**source)
    assert SOURCE_SPANS_ADAPTER_VERSION == "source-bound-lines-html-table-codepoints/1.2.0"
    assert core.response_hash(catalog, SOURCE_SPANS_MODE) == "94b81c5656553bcef43c2d74dbab2cce3dcc124c3d619e25fc835db6e5c2e546"
    assert core.response_hash(core.prompt_authority(_domain(), anchor_mode=SOURCE_SPANS_MODE), SOURCE_SPANS_MODE) == (
        "a0e195e828ed47dbe0a569f44a4f1f1fd6eee65db4294b74dcd5ac00fb961bca"
    )
    assert core.response_hash(resolve_source_span_response(_response(catalog, 0, 2), **source), SOURCE_SPANS_MODE) == (
        "bdee7e116575747e50a2653103976921571ce42c7d155317d65b54012d9c50a6"
    )


def test_local_ids_preserve_exact_structural_partitions_and_repeated_unicode_lines():
    source = _source()
    first = source_segments(**source)
    second = source_segments_v2(**source)
    assert second == source_segments_v2(**source)
    assert second["adapter_version"] == SOURCE_SPANS_V2_ADAPTER_VERSION
    assert second["slice_id"] != first["slice_id"] and len(second["slice_id"]) == 64
    assert second["source_unit_id"] == source["source_unit_id"]
    assert second["source_text_hash"] == source["source_text_hash"]
    assert second["slice_text_sha256"] == hashlib.sha256(source["source_text"].encode()).hexdigest()
    assert [s["segment_id"] for s in second["segments"]] == ["s1", "s2", "s3"]
    for old, new in zip(first["segments"], second["segments"]):
        assert {k: v for k, v in old.items() if k != "segment_id"} == {k: v for k, v in new.items() if k != "segment_id"}
    for start, end in ((0, 0), (2, 2), (0, 2)):
        raw = _response(second, start, end)
        before = deepcopy(raw)
        resolved = resolve_source_span_v2_response(raw, **source)
        assert raw == before
        assert resolved == resolve_source_span_response(_response(first, start, end), **source)
        RawCandidateResponse.model_validate(resolved)
    assert resolve_source_span_v2_response(_response(second, 0, 2), **source)["candidates"][0]["anchors"][0]["quote"] == source["source_text"].strip()


def test_reported_candidate_17_omitted_slice_substring_failure_is_not_a_v2_model_field():
    # Minimized synthetic reproduction of the paid v1 failure, not an old-run repair.
    source = _source("first\nsecond\nthird\nfourth\nfifth\nselected field")
    v1 = source_segments(**source)
    good_property = _response(v1, 5, 5)["candidates"][1]
    raw_v1 = {"candidates": [deepcopy(good_property) for _ in range(18)]}
    full_slice_id = v1["slice_id"]
    raw_v1["candidates"][17]["anchor"]["slice_id"] = full_slice_id[:20] + full_slice_id[28:]
    before = deepcopy(raw_v1)
    with pytest.raises(SourceSpanResolutionError) as error:
        resolve_source_span_response(raw_v1, **source)
    assert error.value.diagnostics == [{
        "path": "$.candidates[17].anchor", "reason": "SOURCE_SPAN_AUTHORITY_MISMATCH",
    }]
    assert raw_v1 == before
    v2 = source_segments_v2(**source)
    proposed = _response(v2, 5, 5)["candidates"][1]
    raw_v2 = {"candidates": [deepcopy(proposed) for _ in range(18)]}
    assert raw_v2["candidates"][17]["anchor"] == {"start_segment_id": "s6", "end_segment_id": "s6"}
    resolved = resolve_source_span_v2_response(raw_v2, **source)
    assert len(resolved["candidates"]) == 18
    assert all(candidate["anchor"]["quote"] == "selected field" for candidate in resolved["candidates"])
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_v2_response(raw_v1, **source)


@pytest.mark.parametrize("bad", [
    {"start_segment_id": "s0", "end_segment_id": "s1"},
    {"start_segment_id": "s01", "end_segment_id": "s1"},
    {"start_segment_id": "s999", "end_segment_id": "s999"},
    {"start_segment_id": "s1", "end_segment_id": "s999"},
    {"start_segment_id": "s2", "end_segment_id": "s1"},
    {"start_segment_id": " s1", "end_segment_id": "s1"},
    {"start_segment_id": 1, "end_segment_id": "s1"},
    {"start_segment_id": True, "end_segment_id": "s1"},
    {"start_segment_id": "s1"},
    {"start_segment_id": "s1", "end_segment_id": None},
    {"start_segment_id": "s1", "end_segment_id": "s1", "slice_id": "a" * 64},
    {"start_segment_id": "s1", "end_segment_id": "s1", "source_unit_id": "source:one"},
    {"start_segment_id": "s1", "end_segment_id": "s1", "quote": "first"},
    {"start_segment_id": "s1", "end_segment_id": "s1", "span_start": 0},
    {"start_segment_id": "s1", "end_segment_id": "s1", "model_authored_evidence_id": None},
    None,
])
def test_local_anchors_reject_all_extra_authority_and_malformed_selections(bad):
    source = _source()
    raw = _response(source_segments_v2(**source))
    raw["candidates"][1]["anchor"] = bad
    before = deepcopy(raw)
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_v2_response(raw, **source)
    assert raw == before


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(extra=[]),
    lambda raw: raw["candidates"].append(None),
    lambda raw: raw["candidates"][0].update(anchors=[]),
    lambda raw: raw["candidates"][0].update(extra="unknown"),
    lambda raw: raw["candidates"][1].pop("value"),
    lambda raw: raw["candidates"][2].update(direction="invented"),
])
def test_local_schema_never_silently_drops_candidates(mutation):
    source = _source()
    raw = _response(source_segments_v2(**source))
    mutation(raw)
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_v2_response(raw, **source)


@pytest.mark.parametrize("text", ["", "\r\n \t\u00a0\n"])
def test_local_empty_slices_have_no_selectable_ids(text):
    source = _source(text)
    assert source_segments_v2(**source)["segments"] == []
    assert resolve_source_span_v2_response({"candidates": []}, **source) == {"candidates": []}
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_v2_response(_response(source_segments_v2(**_source("x"))), **source)


def test_local_table_row_and_cell_selection_stays_inside_bounded_source():
    text = "<table><tr><td>A</td><td>5</td></tr><tr><td>B</td><td>5</td></tr></table>"
    start = text.index("<tr><td>B")
    end = text.index("</table>")
    source = _source(text[start:end], start=start)
    catalog = source_segments_v2(**source)
    quantity = next(i for i, item in enumerate(catalog["segments"]) if item["text"] == "5")
    raw = _response(catalog, 0, -1)
    raw["candidates"][1]["anchor"] = _anchor(catalog, quantity, quantity)
    result = resolve_source_span_v2_response(raw, **source)
    owner = result["candidates"][0]["anchors"][0]
    field = result["candidates"][1]["anchor"]
    assert owner["quote"] == "<tr><td>B</td><td>5</td></tr>"
    assert text[owner["span_start"]:owner["span_end"]] == owner["quote"]
    assert start <= field["span_start"] < field["span_end"] <= end
    assert field["quote"] == "5"


@pytest.mark.parametrize("change", [
    {"source_unit_id": "another"}, {"source_text_hash": "b" * 64},
    {"source_text": "y"}, {"slice_start": 100, "slice_end": 101},
])
def test_local_ids_repeat_but_full_request_catalog_binding_changes(change):
    source = _source("x")
    first = source_segments_v2(**source)
    second = source_segments_v2(**{**source, **change})
    assert first["segments"][0]["segment_id"] == second["segments"][0]["segment_id"] == "s1"
    assert first["slice_id"] != second["slice_id"]
    assert core.response_hash(first, SOURCE_SPANS_V2_MODE) != core.response_hash(second, SOURCE_SPANS_V2_MODE)


def test_local_schema_and_fewshots_use_only_selection_fields():
    contract = _domain()
    authority = core.prompt_authority(contract, anchor_mode=SOURCE_SPANS_V2_MODE)
    schema = source_span_v2_response_schema()
    definition = schema["$defs"]["ProposedAnchor"]
    assert set(definition["properties"]) == set(definition["required"]) == {"start_segment_id", "end_segment_id"}
    assert definition["additionalProperties"] is False
    assert authority["anchor_adapter_version"] == SOURCE_SPANS_V2_ADAPTER_VERSION
    assert authority["response_schema"] == schema
    supplied = generate_examples(contract)
    before = deepcopy(supplied)
    system, examples = render_system_prompt(
        authority["system_prompt_template"], contract, few_shot=supplied,
        source_spans=True, source_span_mode=SOURCE_SPANS_V2_MODE,
    )
    assert supplied == before
    assert system == authority["system_prompt"] and SOURCE_SPANS_V2_RULE in system
    assert authority["few_shot_hash"] == core.response_hash(examples, SOURCE_SPANS_V2_MODE)
    assert authority["response_schema_sha256"] == core.response_hash(schema, SOURCE_SPANS_V2_MODE)
    assert authority["system_prompt_sha256"] == hashlib.sha256(system.encode()).hexdigest()
    for example, canonical in zip(examples, supplied):
        resolved = resolve_source_span_v2_response(
            example["response"], **example["source_identity"], source_text=example["source_text"],
        )
        assert resolved == canonical["response"]
        for candidate in example["response"]["candidates"]:
            anchors = candidate["anchors"] if candidate["candidate_kind"] == "entity" else [candidate["anchor"]]
            assert all(set(anchor) == {"start_segment_id", "end_segment_id"} for anchor in anchors)


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
def test_v2_fresh_mocked_request_exact_resume_and_full_budget(integrated_case, route):
    options = _options(integrated_case)
    config = options["foundry_config"].model_copy(update={"inference_api": route})
    options["foundry_config"] = config

    def response(request):
        user = request["input"].split("\n", 1)[1] if route == "project_responses" else request["messages"][1]["content"]
        payload = json.loads(user)
        entity = next(item for item in payload["entity_types"] if not item["abstract"])
        return {"candidates": [{
            "candidate_kind": "entity", "local_id": "fresh", "observed_type": entity["type_id"],
            "label": "governed subject", "anchors": [_anchor(payload["source_segments"], 0, -1)],
            "identity_key": {key: "governed subject" for key in entity["identity_key_policy"]["business_key_fields"]},
        }]}

    sdk, requests = _sdk(response)
    if route == "project_responses":
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(output_text=json.dumps(response(kwargs)), status="completed")
        sdk.responses = SimpleNamespace(create=create)
    plan = core.run_approved_reextraction(**options, anchor_mode=SOURCE_SPANS_V2_MODE, dry_run=True)
    result = core.run_approved_reextraction(
        **options, anchor_mode=SOURCE_SPANS_V2_MODE,
        client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    assert result["logical_calls"] == result["physical_calls"] == len(requests) == 1
    state = options["state_root"]
    authority = json.loads((state / "approved-reextraction-authority.json").read_text())
    request = requests[0]
    user = request["input"].split("\n", 1)[1] if route == "project_responses" else request["messages"][1]["content"]
    assert request_envelope(config, system=authority["system_prompt"], user=user,
                            schema=source_span_v2_response_schema(), output=4096) == request
    assert account_request(request, authority["input_budget"])["estimated_total_tokens_upper_bound"] == plan["input_budget"]["maxima"]["estimated_total_tokens_upper_bound"]
    proof = json.loads(next((state / "reextraction-resolved-responses").glob("*.json")).read_text())
    assert proof["adapter_version"] == SOURCE_SPANS_V2_ADAPTER_VERSION
    assert proof["source_segments"] == json.loads(user)["source_segments"]
    assert proof["source_segments_sha256"] == core.response_hash(proof["source_segments"], SOURCE_SPANS_V2_MODE)
    assert proof["source_unit_id"] == json.loads(user)["source_identity"]["source_unit_id"]
    assert proof["response"]["candidates"][0]["anchors"][0]["quote"]
    before = _snapshot(state)
    assert core.run_approved_reextraction(
        **options, resume=True, client_factory=lambda: pytest.fail("resume requested provider"),
    ) == result
    assert _snapshot(state) == before
    with pytest.raises(ValueError, match="ANCHOR_MODE_DRIFT"):
        core.run_approved_reextraction(**options, anchor_mode=SOURCE_SPANS_MODE, resume=True, dry_run=True)
    from fabric_kg_builder.enrichment.approved_donor_continuation import run_approved_continuation
    from tests.unit.test_approved_donor_continuation import _child
    continuation = run_approved_continuation(**_child(options), dry_run=True)
    assert continuation["anchor_mode"] == SOURCE_SPANS_V2_MODE
    assert continuation["remaining_work_units"] == continuation["remote_calls"] == 0


def test_v2_recursive_request_cache_cannot_move_response_envelope_between_local_id_contexts(
    integrated_case, monkeypatch,
):
    from fabric_kg_builder.enrichment import schema2_stage

    options = _options(integrated_case)
    options["max_calls"] = 2
    inputs, _, materialized, _ = core.prepare_approved_sources(
        **{key: options[key] for key in ("source_path", "l1_state_root", "domain_path", "window_run_path")},
    )
    root = core.plan_approved_work_units(
        materialized.source_units, contract=inputs.domain_contract,
        pass_name="schema-constrained-extraction", authority_fingerprint="0" * 64,
    )[0]
    mid = (root.slice_start + root.slice_end) // 2
    children = (replace(root, slice_end=mid), replace(root, slice_start=mid))
    vocabulary = core.compile_closed_vocabulary(inputs.domain_contract)

    class StopAfterRequests(Exception):
        pass

    def run_requests(**kwargs):
        for child in children:
            prompt = core.render_extraction_prompt(
                vocabulary, source_unit_id=child.source_unit_id, source_text_hash=child.source_text_hash,
                source_text=child.anchored_text, slice_start=child.slice_start, slice_end=child.slice_end,
            )
            kwargs["service"].complete(prompt=prompt, work_unit=child)
        raise StopAfterRequests

    monkeypatch.setattr(schema2_stage, "run_l2", run_requests)
    sdk, requests = _sdk(lambda request: _response(json.loads(request["messages"][1]["content"])["source_segments"]))
    with pytest.raises(StopAfterRequests):
        core.run_approved_reextraction(
            **options, anchor_mode=SOURCE_SPANS_V2_MODE,
            client_factory=lambda: FoundryClient(options["foundry_config"], _sdk_client=sdk),
        )
    assert len(requests) == 2
    payloads = [json.loads(request["messages"][1]["content"]) for request in requests]
    assert all(payload["source_segments"]["segments"][0]["segment_id"] == "s1" for payload in payloads)
    assert payloads[0]["source_segments"]["slice_id"] != payloads[1]["source_segments"]["slice_id"]
    state = options["state_root"]
    paths = sorted((state / "reextraction-responses").glob("*.json"))
    assert len(paths) == 2 and paths[0].stem != paths[1].stem
    # Even resealing inventory cannot authorize another request's response envelope.
    contents = [path.read_bytes() for path in paths]
    paths[0].write_bytes(contents[1])
    paths[1].write_bytes(contents[0])
    core._write_state(state / "reextraction-integrity.json", core._inventory(state))
    with pytest.raises(ValueError, match="RESPONSE_DRIFT"):
        core.run_approved_reextraction(
            **options, resume=True, client_factory=lambda: pytest.fail("cache drift called provider"),
        )


def test_v2_cli_zero_remote_dryrun(integrated_case, monkeypatch):
    options = _options(integrated_case)
    _configure(monkeypatch, options)
    _no_source_reads(monkeypatch)
    monkeypatch.setattr(core, "_bounded_client", lambda *_: pytest.fail("dry-run constructed transport"))
    result = CliRunner().invoke(cli, [
        *_args(options), "--approved-anchor-mode", SOURCE_SPANS_V2_MODE, "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["anchor_mode"] == SOURCE_SPANS_V2_MODE
    assert plan["remote_calls"] == plan["writes"] == plan["original_source_reads"] == 0
    assert not options["state_root"].exists()
