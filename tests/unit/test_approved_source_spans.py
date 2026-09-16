"""Source segment IDs are a lossless selection adapter, not verified evidence."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import re
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.approved_few_shot import generate_examples, render_system_prompt
from fabric_kg_builder.enrichment.approved_input_budget import account_request, request_envelope
from fabric_kg_builder.enrichment.approved_quote_anchors import (
    SOURCE_SPANS_ADAPTER_VERSION, SOURCE_SPANS_MODE, SOURCE_SPANS_RULE,
    SourceSpanResolutionError, resolve_source_span_response, source_segments,
    source_span_response_schema,
)
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.schema2_extraction import RawCandidateResponse
from tests.unit.test_approved_donor_continuation import _snapshot
from tests.unit.test_approved_few_shot import _domain
from tests.unit.test_approved_quote_anchors import _records
from tests.unit.test_approved_reextraction import _no_source_reads, _options, _sdk
from tests.unit.test_approved_token_budgets import _args, _configure
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401


def _source(text="  α😀 e\u0301 \r\n\r\n <span>A&nbsp;\t B</span> \n  α😀 e\u0301 \u00a0", start=37):
    return dict(source_unit_id="source:one", source_text_hash="a" * 64,
                source_text=text, slice_start=start, slice_end=start + len(text))


def _anchor(catalog, first=0, last=0):
    if catalog["adapter_version"] == core.SOURCE_SPANS_V2_ADAPTER_VERSION:
        return dict(start_segment_id=catalog["segments"][first]["segment_id"],
                    end_segment_id=catalog["segments"][last]["segment_id"])
    return dict(source_unit_id=catalog["source_unit_id"], slice_id=catalog["slice_id"],
                start_segment_id=catalog["segments"][first]["segment_id"],
                end_segment_id=catalog["segments"][last]["segment_id"])


def _response(catalog, first=0, last=0):
    result = _records()
    for candidate in result["candidates"]:
        if candidate["candidate_kind"] == "entity":
            candidate["anchors"] = [_anchor(catalog, first, last)]
        else:
            candidate["anchor"] = _anchor(catalog, first, last)
    return result


def test_exact_repeated_unicode_html_and_multiline_ranges_preserve_all_values():
    source = _source()
    catalog = source_segments(**source)
    assert catalog == source_segments(**source)
    assert len(catalog["slice_id"]) == 64
    assert catalog["source_text_hash"] == source["source_text_hash"]
    for ordinal, segment in enumerate(catalog["segments"], 1):
        assert re.fullmatch(rf"seg:[0-9a-f]{{16}}:{ordinal}", segment["segment_id"])
        assert len(segment["segment_id"]) < 64
    assert [item["line_number"] for item in catalog["segments"]] == [1, 3, 4]
    assert catalog["segments"][0]["text"] == catalog["segments"][2]["text"] == "α😀 e\u0301"
    assert catalog["segments"][0]["segment_id"] != catalog["segments"][2]["segment_id"]
    assert catalog["slice_text_sha256"] == hashlib.sha256(source["source_text"].encode()).hexdigest()
    for first, last in ((0, 0), (2, 2), (0, 2), (1, 2)):
        raw = _response(catalog, first, last)
        before = deepcopy(raw)
        result = resolve_source_span_response(raw, **source)
        assert raw == before
        RawCandidateResponse.model_validate(result)
        start, end = catalog["segments"][first]["start"], catalog["segments"][last]["end"]
        expected = source["source_text"][start - source["slice_start"]:end - source["slice_start"]]
        for old, new in zip(raw["candidates"], result["candidates"]):
            key = "anchors" if old["candidate_kind"] == "entity" else "anchor"
            assert {k: v for k, v in old.items() if k != key} == {k: v for k, v in new.items() if k != key}
            anchor = new[key][0] if key == "anchors" else new[key]
            assert anchor == dict(quote=expected, span_start=start, span_end=end,
                                  model_authored_evidence_id=None)
        if first == 0 and last == 2:
            assert "\r\n\r\n <span>A&nbsp;\t B</span> \n  " in expected
            assert expected != expected.replace("e\u0301", "é")


def test_one_line_html_rows_and_cells_expose_small_owner_contexts():
    rows = (
        "<tr><td>Owner A</td><td>5</td></tr>",
        "<tr><td>Owner B</td><td>5</td></tr>",
    )
    text = "<table><tbody>" + "".join(rows) + "</tbody></table>"
    source = _source(text, start=73)
    catalog = source_segments(**source)
    segments = catalog["segments"]
    assert {item["line_number"] for item in segments} == {1}
    row_opens = [i for i, item in enumerate(segments) if item["segment_kind"] == "row_open"]
    row_closes = [i for i, item in enumerate(segments) if item["segment_kind"] == "row_close"]
    quantities = [i for i, item in enumerate(segments) if item["text"] == "5"]
    assert len(row_opens) == len(row_closes) == len(quantities) == 2
    assert segments[quantities[0]]["segment_id"] != segments[quantities[1]]["segment_id"]
    for number, (start, end, quantity) in enumerate(zip(row_opens, row_closes, quantities)):
        raw = _response(catalog, start, end)
        raw["candidates"][0]["label"] = f"Owner {'AB'[number]}"
        raw["candidates"][1]["anchor"] = _anchor(catalog, quantity, quantity)
        result = resolve_source_span_response(raw, **source)
        owner = result["candidates"][0]["anchors"][0]
        field = result["candidates"][1]["anchor"]
        assert owner["quote"] == rows[number]
        assert owner["quote"].count("<tr>") == 1
        assert field["quote"] == "5"
        assert owner["span_start"] <= field["span_start"] < field["span_end"] <= owner["span_end"]
        assert owner["span_start"] == source["slice_start"] + text.index(rows[number])
        assert owner["quote"] == text[owner["span_start"] - 73:owner["span_end"] - 73]
        if number == 0:
            assert field["span_end"] < segments[row_opens[1]]["start"]
    assert "smallest contiguous structural owning context" in SOURCE_SPANS_RULE
    assert "not the whole table or page" in SOURCE_SPANS_RULE


def test_html_tag_quotes_comments_case_and_line_breaks_are_lossless():
    text = (
        '<div title="<tr>ignored</tr>">Intro</div><!-- <tr>ignored</tr> -->'
        '<tr-data>not a row</tr-data><TR data-x=">">'
        "<TD\n title='a > b'>\u00a0α e\u0301\n\n<b>inside</b> \t</TD>"
        "<TD>9</TD></TR>"
    )
    source = _source(text)
    catalog = source_segments(**source)
    segments = catalog["segments"]
    assert sum(item["segment_kind"] == "row_open" for item in segments) == 1
    assert sum(item["segment_kind"] == "row_close" for item in segments) == 1
    first = next(i for i, item in enumerate(segments) if item["text"] == '<TR data-x=">">')
    last = next(i for i, item in enumerate(segments) if item["text"] == "</TR>")
    result = resolve_source_span_response(_response(catalog, first, last), **source)
    quote = result["candidates"][0]["anchors"][0]["quote"]
    assert quote == text[text.index('<TR data-x=">">'):]
    assert "\u00a0α e\u0301\n\n<b>inside</b> \t" in quote
    for segment in segments:
        assert text[segment["start"] - source["slice_start"]:segment["end"] - source["slice_start"]] == segment["text"]
    assert any(item["segment_kind"] == "cell_open" and item["line_number"] == 2 for item in segments)


def test_structural_segments_cannot_reuse_full_source_ids_in_bounded_cell_slice():
    complete = "<table><tr><td>left</td><td>right</td></tr></table>"
    source = _source(complete, start=0)
    original = source_segments(**source)
    start, end = complete.index("<td>right"), complete.index("</tr>")
    child = _source(complete[start:end], start=start)
    catalog = source_segments(**child)
    assert [item["segment_kind"] for item in catalog["segments"]] == ["cell_open", "content", "cell_close"]
    result = resolve_source_span_response(_response(catalog, 0, -1), **child)
    anchor = result["candidates"][0]["anchors"][0]
    assert anchor["quote"] == "<td>right</td>"
    assert anchor["span_start"] == start and anchor["span_end"] == end
    raw = _response(catalog)
    raw["candidates"][1]["anchor"]["end_segment_id"] = original["segments"][-1]["segment_id"]
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_response(raw, **child)


@pytest.mark.parametrize("change", [
    {"source_unit_id": "other"}, {"source_text_hash": "b" * 64},
    {"slice_start": 38, "slice_end": 39}, {"source_text": "Y"},
])
def test_ids_bind_source_hash_slice_offset_and_exact_slice_bytes(change):
    source = _source("X")
    catalog = source_segments(**source)
    changed = source_segments(**{**source, **change})
    assert catalog["slice_id"] != changed["slice_id"]
    assert catalog["segments"][0]["segment_id"] != changed["segments"][0]["segment_id"]
    with pytest.raises(SourceSpanResolutionError, match="UNRESOLVED"):
        resolve_source_span_response(_response(catalog), **{**source, **change})


def test_compact_prefix_collision_never_replaces_full_source_slice_authority(monkeypatch):
    from fabric_kg_builder.enrichment import approved_quote_anchors as adapter

    original_hash = adapter._exact_hash

    def colliding_segment_hash(value):
        return "c" * 64 if isinstance(value, list) else original_hash(value)

    monkeypatch.setattr(adapter, "_exact_hash", colliding_segment_hash)
    source = _source("first\nsecond")
    catalog = source_segments(**source)
    assert [item["segment_id"] for item in catalog["segments"]] == [
        "seg:cccccccccccccccc:1", "seg:cccccccccccccccc:2",
    ]
    result = resolve_source_span_response(_response(catalog, 1, 1), **source)
    assert result["candidates"][0]["anchors"][0]["quote"] == "second"
    for change in (
        {"source_unit_id": "another"},
        {"source_text_hash": "b" * 64},
        {"source_text": "other\nsecond"},
        {"slice_start": 50, "slice_end": 62},
    ):
        other_source = {**source, **change}
        foreign = source_segments(**other_source)
        assert foreign["segments"][0]["segment_id"] == catalog["segments"][0]["segment_id"]
        assert foreign["slice_id"] != catalog["slice_id"]
        with pytest.raises(SourceSpanResolutionError) as error:
            resolve_source_span_response(_response(foreign), **source)
        assert {item["reason"] for item in error.value.diagnostics} == {"SOURCE_SPAN_AUTHORITY_MISMATCH"}
    raw = _response(catalog)
    raw["candidates"][1]["anchor"]["end_segment_id"] = "seg:cccccccccccccccc:3"
    with pytest.raises(SourceSpanResolutionError) as error:
        resolve_source_span_response(raw, **source)
    assert error.value.diagnostics[0]["reason"] == "SOURCE_SPAN_ID_OUTSIDE_AUTHORIZED_SLICE"


@pytest.mark.parametrize(("change", "reason"), [
    ({"source_unit_id": "other"}, "SOURCE_SPAN_AUTHORITY_MISMATCH"),
    ({"slice_id": "other"}, "SOURCE_SPAN_AUTHORITY_MISMATCH"),
    ({"start_segment_id": "unknown"}, "SOURCE_SPAN_ID_OUTSIDE_AUTHORIZED_SLICE"),
    ({"end_segment_id": "unknown"}, "SOURCE_SPAN_ID_OUTSIDE_AUTHORIZED_SLICE"),
    ({"span_start": 37}, "SOURCE_SPAN_ANCHOR_SCHEMA_INVALID"),
    ({"quote": "α😀 e\u0301"}, "SOURCE_SPAN_ANCHOR_SCHEMA_INVALID"),
    ({"model_authored_evidence_id": None}, "SOURCE_SPAN_ANCHOR_SCHEMA_INVALID"),
    ({"start_segment_id": 0}, "SOURCE_SPAN_ANCHOR_SCHEMA_INVALID"),
    ({"end_segment_id": None}, "SOURCE_SPAN_ANCHOR_SCHEMA_INVALID"),
])
def test_invalid_anchor_rejects_entire_leaf_without_dropping_candidates(change, reason):
    source = _source()
    raw = _response(source_segments(**source))
    for candidate in raw["candidates"]:
        anchor = candidate["anchors"][0] if candidate["candidate_kind"] == "entity" else candidate["anchor"]
        anchor.update(change)
    before = deepcopy(raw)
    with pytest.raises(SourceSpanResolutionError) as error:
        resolve_source_span_response(raw, **source)
    assert raw == before
    assert len(error.value.diagnostics) == 3
    assert {item["reason"] for item in error.value.diagnostics} == {reason}


def test_reversed_and_cross_slice_segment_ids_rejected_even_with_current_slice_id():
    source = _source()
    catalog = source_segments(**source)
    with pytest.raises(SourceSpanResolutionError) as error:
        resolve_source_span_response(_response(catalog, 2, 0), **source)
    assert {item["reason"] for item in error.value.diagnostics} == {"SOURCE_SPAN_REVERSED"}
    foreign = source_segments(**_source(source["source_text"], start=0))
    raw = _response(catalog)
    raw["candidates"][0]["anchors"][0]["end_segment_id"] = foreign["segments"][0]["segment_id"]
    with pytest.raises(SourceSpanResolutionError) as error:
        resolve_source_span_response(raw, **source)
    assert error.value.diagnostics[0]["reason"] == "SOURCE_SPAN_ID_OUTSIDE_AUTHORIZED_SLICE"


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(extra=True),
    lambda raw: raw["candidates"].append(None),
    lambda raw: raw["candidates"][0].update(extra=True),
    lambda raw: raw["candidates"][1].update(value=None),
    lambda raw: raw["candidates"][1].pop("normalized_value"),
    lambda raw: raw["candidates"][2].update(direction="invented"),
    lambda raw: raw["candidates"][0].update(anchors=[]),
    lambda raw: raw["candidates"][1].update(anchor=None),
    lambda raw: raw["candidates"][2].update(candidate_kind="unknown"),
    lambda raw: raw["candidates"][2]["anchor"].pop("slice_id"),
])
def test_malformed_envelopes_candidates_unknown_fields_and_missing_anchors_fail_closed(mutation):
    source = _source()
    raw = _response(source_segments(**source))
    mutation(raw)
    before = deepcopy(raw)
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_response(raw, **source)
    assert raw == before


@pytest.mark.parametrize("text", ["", "\r\n \t\u00a0\n"])
def test_empty_and_blank_only_slices_have_no_selectable_segments(text):
    source = _source(text)
    assert source_segments(**source)["segments"] == []
    assert resolve_source_span_response({"candidates": []}, **source) == {"candidates": []}
    with pytest.raises(SourceSpanResolutionError):
        resolve_source_span_response(_response(source_segments(**_source("x"))), **source)


@pytest.mark.parametrize("change", [
    {"slice_start": -1}, {"slice_end": 999}, {"slice_start": True},
    {"source_unit_id": ""}, {"source_text_hash": ""},
])
def test_invalid_slice_contract(change):
    with pytest.raises(ValueError, match="SLICE_INVALID"):
        source_segments(**{**_source(), **change})


def test_schema_prompt_few_shot_projection_and_hashes_share_one_contract():
    contract = _domain()
    authority = core.prompt_authority(contract, anchor_mode=SOURCE_SPANS_MODE)
    schema = source_span_response_schema()
    fields = {"source_unit_id", "slice_id", "start_segment_id", "end_segment_id"}
    definition = schema["$defs"]["ProposedAnchor"]
    assert set(definition["properties"]) == set(definition["required"]) == fields
    assert definition["additionalProperties"] is False
    for name in ("RawEntityCandidate", "RawRelationshipCandidate", "RawPropertyCandidate"):
        key = "anchors" if name == "RawEntityCandidate" else "anchor"
        assert key in schema["$defs"][name]["required"]
    assert "span_start" not in json.dumps(schema)
    assert "quote" not in json.dumps(definition)
    supplied = generate_examples(contract)
    before = deepcopy(supplied)
    rendered, examples = render_system_prompt(
        authority["system_prompt_template"], contract, few_shot=supplied, source_spans=True,
    )
    assert supplied == before
    assert rendered == authority["system_prompt"]
    assert SOURCE_SPANS_RULE in rendered
    assert "Copy the quote exactly" not in rendered
    assert "span_start" not in rendered
    assert authority["few_shot_hash"] == core.response_hash(examples, SOURCE_SPANS_MODE)
    assert authority["system_prompt_sha256"] == hashlib.sha256(rendered.encode()).hexdigest()
    assert authority["response_schema_sha256"] == core.response_hash(schema, SOURCE_SPANS_MODE)
    assert authority["anchor_adapter_version"] == SOURCE_SPANS_ADAPTER_VERSION
    for example, canonical in zip(examples, supplied):
        resolved = resolve_source_span_response(
            example["response"], **example["source_identity"], source_text=example["source_text"],
        )
        assert resolved == canonical["response"]


def test_few_shot_projection_never_expands_subline_anchor():
    supplied = generate_examples(_domain())
    example = supplied[0]
    example["source_text"] = example["source_text"].replace("\n", " ")
    with pytest.raises(ValueError, match="complete source segment range"):
        core.prompt_authority(_domain(), anchor_mode=SOURCE_SPANS_MODE, few_shot=supplied)


def test_bounded_subworkunit_prompt_preserves_original_metadata_not_heading_evidence(integrated_case):
    options = _options(integrated_case)
    inputs, _, materialized, _ = core.prepare_approved_sources(
        **{key: options[key] for key in ("source_path", "l1_state_root", "domain_path", "window_run_path")},
    )
    root = core.plan_approved_work_units(
        materialized.source_units, contract=inputs.domain_contract,
        pass_name="schema-constrained-extraction", authority_fingerprint="0" * 64,
    )[0]
    child = replace(root, slice_start=root.slice_start + 3, slice_end=root.slice_end - 2,
                    anchor_text="INTERPRETATION_ONLY_HEADING")
    vocabulary = core.compile_closed_vocabulary(inputs.domain_contract)
    args = dict(document_name="source.pdf", section_path=["Section"], anchor_mode=SOURCE_SPANS_MODE)
    payload = json.loads(core.approved_prompt(child, vocabulary, **args))
    assert payload["source_text"] == child.text
    assert child.source_text == root.source_text
    assert payload["interpretation_context"]["inherited_heading"] == "INTERPRETATION_ONLY_HEADING"
    assert "INTERPRETATION_ONLY_HEADING" not in json.dumps(payload["source_segments"])
    catalog = payload["source_segments"]
    result = resolve_source_span_response(
        _response(catalog, 0, -1), source_text=child.text, **payload["source_identity"],
    )
    anchor = result["candidates"][1]["anchor"]
    assert child.slice_start <= anchor["span_start"] < anchor["span_end"] <= child.slice_end
    assert child.source_text[anchor["span_start"]:anchor["span_end"]] == anchor["quote"]
    next_payload = json.loads(core.approved_prompt(root, vocabulary, **args))
    assert next_payload["source_text"] == root.text
    assert next_payload["source_segments"]["slice_id"] != catalog["slice_id"]


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
def test_mocked_fresh_run_resume_and_budget_envelope(integrated_case, route):
    options = _options(integrated_case)
    config = options["foundry_config"].model_copy(update={"inference_api": route})
    options["foundry_config"] = config
    emitted = []

    def response(request):
        user = request["input"].split("\n", 1)[1] if route == "project_responses" else request["messages"][1]["content"]
        payload = json.loads(user)
        entity = next(item for item in payload["entity_types"] if not item["abstract"])
        candidate = dict(candidate_kind="entity", local_id="fresh", observed_type=entity["type_id"],
                         label="governed subject", anchors=[_anchor(payload["source_segments"], 0, -1)],
                         identity_key={key: "governed subject" for key in entity["identity_key_policy"]["business_key_fields"]})
        result = {"candidates": [candidate]}
        emitted.append(result)
        return result

    sdk, requests = _sdk(response)
    if route == "project_responses":
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(output_text=json.dumps(response(kwargs)), status="completed")
        sdk.responses = SimpleNamespace(create=create)
    plan = core.run_approved_reextraction(**options, anchor_mode=SOURCE_SPANS_MODE, dry_run=True)
    result = core.run_approved_reextraction(
        **options, anchor_mode=SOURCE_SPANS_MODE,
        client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    assert result["logical_calls"] == result["physical_calls"] == len(requests) == 1
    state = options["state_root"]
    saved = json.loads(next((state / "reextraction-responses").glob("*.json")).read_text())
    assert saved["response"] == emitted[0]
    resolved = json.loads(next((state / "reextraction-resolved-responses").glob("*.json")).read_text())
    assert resolved["adapter_version"] == SOURCE_SPANS_ADAPTER_VERSION
    assert resolved["source_segments_sha256"] == core.response_hash(resolved["source_segments"], SOURCE_SPANS_MODE)
    assert resolved["response"]["candidates"][0]["anchors"][0]["quote"]
    leaves = [json.loads(path.read_text()) for path in (state / "checkpoint-leaves").glob("*.json")]
    assert sum(leaf["raw_candidate_count"] for leaf in leaves) == 1
    authority = json.loads((state / "approved-reextraction-authority.json").read_text())
    request = requests[0]
    user = request["input"].split("\n", 1)[1] if route == "project_responses" else request["messages"][1]["content"]
    assert request_envelope(config, system=authority["system_prompt"], user=user,
                            schema=source_span_response_schema(), output=4096) == request
    assert account_request(request, authority["input_budget"])["estimated_total_tokens_upper_bound"] == (
        plan["input_budget"]["maxima"]["estimated_total_tokens_upper_bound"]
    )
    payload = json.loads(user)
    assert "source_offset_rule" not in payload and "source_quote_rule" not in payload
    assert payload["source_span_rule"] == SOURCE_SPANS_RULE
    before = _snapshot(state)
    assert core.run_approved_reextraction(
        **options, resume=True, client_factory=lambda: pytest.fail("resume called provider"),
    ) == result
    assert _snapshot(state) == before
    with pytest.raises(ValueError, match="ANCHOR_MODE_DRIFT"):
        core.run_approved_reextraction(**options, anchor_mode=core.QUOTE_MODE, resume=True, dry_run=True)


def test_bad_id_retains_raw_and_diagnostics_no_paid_resampling(integrated_case):
    options = _options(integrated_case)
    sdk, requests = _sdk(_response(source_segments(**_source())))
    with pytest.raises(SourceSpanResolutionError):
        core.run_approved_reextraction(
            **options, anchor_mode=SOURCE_SPANS_MODE,
            client_factory=lambda: FoundryClient(options["foundry_config"], _sdk_client=sdk),
        )
    state = options["state_root"]
    assert len(requests) == 1
    assert list((state / "reextraction-responses").glob("*.json"))
    diagnostics = json.loads(next((state / "reextraction-anchor-diagnostics").glob("*.json")).read_text())
    assert len(diagnostics["diagnostics"]) == 3
    assert "source_segments" in diagnostics
    before = _snapshot(state)
    with pytest.raises(SourceSpanResolutionError):
        core.run_approved_reextraction(
            **options, resume=True, client_factory=lambda: pytest.fail("no resampling"),
        )
    assert _snapshot(state) == before


def test_cli_explicit_mode_zero_remote_dryrun_and_input_cap(integrated_case, monkeypatch):
    options = _options(integrated_case)
    _configure(monkeypatch, options)
    _no_source_reads(monkeypatch)
    monkeypatch.setattr(core, "_bounded_client", lambda *_: pytest.fail("constructed transport"))
    args = [*_args(options), "--approved-anchor-mode", SOURCE_SPANS_MODE, "--dry-run"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["anchor_mode"] == SOURCE_SPANS_MODE
    assert plan["remote_calls"] == plan["writes"] == plan["original_source_reads"] == 0
    assert not options["state_root"].exists()
    limit = plan["input_budget"]["maxima"]["estimated_total_tokens_upper_bound"] - 1
    result = CliRunner().invoke(cli, [*args, "--max-context-tokens", str(limit)])
    assert result.exit_code != 0 and "INPUT_BUDGET_EXCEEDED" in result.output
    assert not options["state_root"].exists()


def test_source_spans_never_inherit_unreviewed_donor_proofs(integrated_case):
    from fabric_kg_builder.enrichment.approved_donor_continuation import run_approved_continuation
    from tests.unit.test_approved_donor_continuation import _child

    options = _options(integrated_case)
    sdk, _ = _sdk()
    core.run_approved_reextraction(
        **options, anchor_mode=SOURCE_SPANS_MODE,
        client_factory=lambda: FoundryClient(options["foundry_config"], _sdk_client=sdk),
    )
    before = _snapshot(options["state_root"])
    with pytest.raises(ValueError, match="DONOR_SOURCE_SPANS_UNSUPPORTED"):
        run_approved_continuation(**_child(options), dry_run=True)
    assert _snapshot(options["state_root"]) == before
