"""Reviewed recovery is an opt-in projection, not rewritten extraction evidence."""

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
from fabric_kg_builder.enrichment import approved_quote_review as review
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.approved_quote_anchors import QUOTE_MODE, QuoteAnchorResolutionError
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from tests.unit.test_approved_donor_continuation import _child, _cli_args, _snapshot
from tests.unit.test_approved_quote_anchors import _records
from tests.unit.test_approved_reextraction import _config, _options, _sdk
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _document(authority=None, state=None):
    return {
        "version": review.VERSION, "actor": "offline-reviewer",
        "rationale": "Reviewed contiguous quotation including intervening OCR text.",
        "reviewed_at": "2026-09-13T06:30:00Z",
        "donor_fingerprint": authority["fingerprint"] if authority else "a" * 64,
        "donor_authority_sha256": _sha((state / continuation.AUTHORITY).read_bytes()) if state else "b" * 64,
        "donor_integrity_sha256": _sha((state / continuation.INTEGRITY).read_bytes()) if state else "c" * 64,
        "producer_code_identity_sha256": review.KNOWN_PRODUCER,
        "corrections": [],
    }


def _correction(raw, unit, request_hash, path, new_quote, old_quote="not contiguous"):
    return {
        "request_hash": request_hash, "response_hash": core.response_hash(raw, QUOTE_MODE),
        "source_unit_id": unit.source_unit_id, "source_text_hash": unit.source_text_hash,
        "slice_start": unit.slice_start, "slice_end": unit.slice_end,
        "candidate_path": path, "old_quote_sha256": _sha(old_quote.encode()),
        "new_quote": new_quote,
    }


def _write(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_documented_review_schema_matches_accepted_fields():
    document = (Path(__file__).resolve().parents[2] / "docs/specs/SPEC-APPROVED-EXTRACTION-PROMPT.md").read_text()
    section = document.split("## Explicit reviewed exact-quote recovery", 1)[1]
    example = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert set(example) == review.REVIEW_FIELDS
    assert set(example["corrections"][0]) == review.CORRECTION_FIELDS
    assert example["producer_code_identity_sha256"] == review.KNOWN_PRODUCER


@pytest.fixture
def failed_donor(integrated_case, legacy_quote_producer):
    options = _options(integrated_case)
    emitted = []

    def response(request):
        payload = json.loads(request["messages"][1]["content"])
        raw = _records("not contiguous")
        raw["candidates"] = raw["candidates"][:1]
        emitted.append((payload, raw))
        return raw

    sdk, requests = _sdk(response)
    with pytest.raises(QuoteAnchorResolutionError):
        core.run_approved_reextraction(
            **options, anchor_mode=QUOTE_MODE,
            client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    assert len(requests) == 1
    state = options["state_root"]
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    cached = json.loads(next((state / "reextraction-responses").glob("*.json")).read_text())
    payload, raw = emitted[0]
    document = _document(authority, state)
    document["corrections"] = [_correction(
        raw, SimpleNamespace(**payload["source_identity"]), cached["request_hash"],
        "candidates[0].anchors[0]", payload["source_text"].strip(),
    )]
    path = _write(state.parent / "review.json", document)
    return options, path, document


def test_reviewed_child_reuses_original_and_replays_canonical_validation(failed_donor):
    options, path, document = failed_donor
    state = options["state_root"]
    before = _snapshot(state)
    child = _child(options, approved_quote_review=path)
    factory = lambda: pytest.fail("Reviewed replay must not call provider")
    plan = continuation.run_approved_continuation(**child, dry_run=True, client_factory=factory)
    assert plan["remaining_work_units"] == 0
    assert plan["reused_responses"] == 1
    assert plan["quote_review"]["corrected_requests"] == 1
    assert plan["quote_review"]["correction_count"] == 1
    assert not child["state_root"].exists()
    result = continuation.run_approved_continuation(**child, client_factory=factory)
    assert result["logical_calls"] == result["physical_calls"] == 0
    assert result["lineage_spent"]["logical_calls"] == 1
    assert result["lineage_spent"]["physical_calls"] == 1
    assert _snapshot(state) == before
    saved = json.loads(next((child["state_root"] / "reextraction-responses").glob("*.json")).read_text())
    original = json.loads(next((state / "reextraction-responses").glob("*.json")).read_text())
    assert saved["response"] == original["response"]
    projection = json.loads(next((child["state_root"] / "reextraction-reviewed-responses").glob("*.json")).read_text())
    assert projection["original_response_hash"] == original["response_hash"]
    assert projection["reviewed_response_hash"] != original["response_hash"]
    assert projection["status"] == "reviewed_quote_projection_pending_canonical_validation"
    expected = deepcopy(original["response"])
    expected["candidates"][0]["anchors"][0]["quote"] = document["corrections"][0]["new_quote"]
    assert projection["reviewed_response"] == expected
    output = base64.b64decode(projection["original_provider_output"]["raw_output_utf8_base64"])
    assert json.loads(output) == original["response"]
    source_output = json.loads(next((state / "reextraction-provider-responses").glob("*.json")).read_text())
    assert projection["original_provider_output"] == source_output
    retained = json.loads((child["state_root"] / "approved-quote-correction-review.json").read_text())
    assert base64.b64decode(retained["review_file_utf8_base64"]) == path.read_bytes()
    leaves = [json.loads(p.read_text()) for p in (child["state_root"] / "checkpoint-leaves").glob("*.json")]
    assert sum(leaf["raw_candidate_count"] for leaf in leaves) == 1
    assert any(
        reason == "UNKNOWN_ENTITY_TYPE"
        for leaf in leaves for reason, count in leaf["audit_reason_counts"] if count
    )
    assert (child["state_root"] / "approved-reextraction-result.json").is_file()
    snapshot = _snapshot(child["state_root"])
    assert continuation.run_approved_continuation(**child, resume=True, client_factory=factory) == result
    assert _snapshot(child["state_root"]) == snapshot
    assert _snapshot(state) == before
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        continuation.run_approved_continuation(**_child(options), resume=True, dry_run=True)
    with pytest.raises(ValueError, match="REVIEWED_CHILD_DONOR_UNSUPPORTED"):
        continuation.run_approved_continuation(
            **_child(options, reuse_approved_run=child["state_root"],
                     state_root=state.parent / "grandchild"), dry_run=True,
        )


@pytest.mark.parametrize(("field", "value", "error"), [
    ("request_hash", "d" * 64, "REQUEST_NOT_FOUND"),
    ("response_hash", "d" * 64, "RESPONSE_OR_SOURCE_BINDING_DRIFT"),
    ("source_text_hash", "d" * 64, "RESPONSE_OR_SOURCE_BINDING_DRIFT"),
    ("source_unit_id", "source-unit:wrong", "RESPONSE_OR_SOURCE_BINDING_DRIFT"),
    ("slice_start", 1, "RESPONSE_OR_SOURCE_BINDING_DRIFT"),
    ("candidate_path", "candidates[1].anchors[0]", "RESPONSE_OR_SOURCE_BINDING_DRIFT"),
    ("old_quote_sha256", "d" * 64, "OLD_QUOTE_DRIFT"),
    ("new_quote", "still not in source", "QUOTE_ANCHOR_UNRESOLVED"),
    ("new_quote", " boundary whitespace ", "QUOTE_ANCHOR_UNRESOLVED"),
    ("candidate_path", "candidates[0].label", "SCHEMA_INVALID"),
    ("slice_start", False, "SCHEMA_INVALID"),
])
def test_bad_reviews_fail_before_child_or_provider(failed_donor, field, value, error):
    options, path, document = failed_donor
    before = _snapshot(options["state_root"])
    document["corrections"][0][field] = value
    _write(path, document)
    child = _child(options, approved_quote_review=path)
    with pytest.raises(ValueError, match=error):
        continuation.run_approved_continuation(
            **child, client_factory=lambda: pytest.fail("Bad review called provider"),
        )
    assert not child["state_root"].exists()
    assert _snapshot(options["state_root"]) == before


@pytest.mark.parametrize(("field", "value", "error"), [
    ("actor", "", "SCHEMA_INVALID"),
    ("rationale", " ", "SCHEMA_INVALID"),
    ("reviewed_at", "2026-09-13", "REVIEWED_AT_INVALID"),
    ("producer_code_identity_sha256", "d" * 64, "PRODUCER_NOT_APPROVED"),
    ("donor_fingerprint", "d" * 64, "DONOR_BINDING_DRIFT"),
    ("donor_authority_sha256", "d" * 64, "DONOR_BINDING_DRIFT"),
    ("donor_integrity_sha256", "d" * 64, "DONOR_BINDING_DRIFT"),
    ("corrections", [], "SCHEMA_INVALID"),
])
def test_review_authority_must_be_explicit_and_bound(failed_donor, field, value, error):
    options, path, document = failed_donor
    document[field] = value
    _write(path, document)
    with pytest.raises(ValueError, match=error):
        continuation.run_approved_continuation(**_child(options, approved_quote_review=path), dry_run=True)


def test_producer_guard_is_not_a_code_drift_waiver(failed_donor, monkeypatch):
    options, path, _ = failed_donor
    changed = {**core._code_identity(), "enrichment/schema2_evidence.py": "d" * 64}
    monkeypatch.setattr(core, "_code_identity", lambda: changed)
    with pytest.raises(ValueError, match="PRODUCER_CODE_DRIFT"):
        continuation.run_approved_continuation(**_child(options, approved_quote_review=path), dry_run=True)


def test_unknown_matching_producer_cannot_replace_real_historical_pin(monkeypatch):
    unknown = {"enrichment/unknown_producer.py": "f" * 64}
    monkeypatch.setattr(core, "_code_identity", lambda: dict(unknown))
    assert review.KNOWN_PRODUCER == "be1e3eca73f49c95043f55d19524b3edaf3795dcd5847aca02331dfcad0469ed"
    with pytest.raises(ValueError, match="PRODUCER_CODE_DRIFT"):
        review.verify_helper_identity(
            {**unknown, **review.LEGACY_HELPERS}, current_helpers=review.LEGACY_HELPERS,
            review_version=review.VERSION, has_chain=False,
        )


def test_review_resume_cannot_change_review_bytes(failed_donor):
    options, path, document = failed_donor
    child = _child(options, approved_quote_review=path)
    continuation.run_approved_continuation(**child, client_factory=lambda: pytest.fail("No calls"))
    before = _snapshot(child["state_root"])
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        continuation.run_approved_continuation(**child, resume=True, dry_run=True)
    assert _snapshot(child["state_root"]) == before


def test_duplicate_or_extended_review_cannot_patch_other_values(failed_donor):
    options, path, document = failed_donor
    duplicate = deepcopy(document)
    duplicate["corrections"].append(deepcopy(duplicate["corrections"][0]))
    _write(path, duplicate)
    with pytest.raises(ValueError, match="DUPLICATE_CORRECTION"):
        continuation.run_approved_continuation(**_child(options, approved_quote_review=path), dry_run=True)
    document["corrections"][0]["label"] = "invented replacement"
    _write(path, document)
    with pytest.raises(ValueError, match="SCHEMA_INVALID"):
        continuation.run_approved_continuation(**_child(options, approved_quote_review=path), dry_run=True)
    path.write_text('{"actor":"one","actor":"two"}')
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        review.QuoteReview(path)


def test_all_kinds_unicode_and_exact_slice_projection(tmp_path):
    raw = _records("not contiguous")
    text = "outside repeated | α😀 e\u0301 <figure> OCR </figure> конец | outside repeated"
    start = text.index("α")
    end = text.index(" | outside", start)
    unit = SimpleNamespace(
        source_unit_id="source-unit:x", source_text_hash=_sha(text.encode()),
        text=text[start:end], slice_start=start, slice_end=end, work_unit_id="child-unit",
    )
    paid = continuation.PaidResponse(raw, "a" * 64, "d" * 64, core.response_hash(raw, QUOTE_MODE))
    document = _document()
    document["corrections"] = [
        _correction(raw, unit, paid.request_hash, path, unit.text)
        for path in ("candidates[0].anchors[0]", "candidates[1].anchor", "candidates[2].anchor")
    ]
    reviewed = review.QuoteReview(_write(tmp_path / "review.json", document))
    before = deepcopy(raw)
    reviewed.verify_response(paid, unit, {"offline": True})
    reviewed.finish()
    projection = reviewed.projections[paid.request_hash]
    for old, corrected, resolved in zip(raw["candidates"], projection["reviewed_response"]["candidates"],
                                         projection["resolved_response"]["candidates"]):
        key = "anchors" if old["candidate_kind"] == "entity" else "anchor"
        assert {k: v for k, v in old.items() if k != key} == {k: v for k, v in corrected.items() if k != key}
        anchor = resolved[key][0] if key == "anchors" else resolved[key]
        assert anchor["quote"] == unit.text
        assert anchor["span_start"] == start and anchor["span_end"] == end
    assert raw == before
    document["corrections"].pop()
    incomplete = review.QuoteReview(_write(tmp_path / "incomplete.json", document))
    with pytest.raises(QuoteAnchorResolutionError):
        incomplete.verify_response(paid, unit, {"offline": True})


@pytest.mark.parametrize("new_quote", ["x", "é", "outside"])
def test_no_ambiguous_normalized_or_out_of_slice_quote(tmp_path, new_quote):
    raw = _records("not contiguous")
    raw["candidates"] = raw["candidates"][:1]
    unit = SimpleNamespace(source_unit_id="source-unit:x", source_text_hash="a" * 64,
                           text="x x e\u0301", slice_start=100, slice_end=106)
    paid = continuation.PaidResponse(raw, "a" * 64, "d" * 64, core.response_hash(raw, QUOTE_MODE))
    document = _document()
    document["corrections"] = [_correction(raw, unit, paid.request_hash, "candidates[0].anchors[0]", new_quote)]
    reviewed = review.QuoteReview(_write(tmp_path / "review.json", document))
    with pytest.raises(QuoteAnchorResolutionError):
        reviewed.verify_response(paid, unit, {"offline": True})


def test_review_cannot_replace_an_already_resolvable_anchor(tmp_path):
    raw = _records("already exact")
    raw["candidates"] = raw["candidates"][:1]
    unit = SimpleNamespace(source_unit_id="source-unit:x", source_text_hash="a" * 64,
                           text="already exact", slice_start=100, slice_end=113)
    paid = continuation.PaidResponse(raw, "a" * 64, "d" * 64, core.response_hash(raw, QUOTE_MODE))
    document = _document()
    document["corrections"] = [_correction(
        raw, unit, paid.request_hash, "candidates[0].anchors[0]", "exact", old_quote="already exact",
    )]
    reviewed = review.QuoteReview(_write(tmp_path / "review.json", document))
    with pytest.raises(ValueError, match="ORIGINAL_ALREADY_RESOLVABLE"):
        reviewed.verify_response(paid, unit, {"offline": True})


def test_reviewed_partial_donor_calls_only_missing_units(prefix_case, tmp_path, legacy_quote_producer):
    from tests.unit.test_window_run_prefix_acceptance_cli import _accept
    from tests.unit.test_window_run_approved_cli import _approve_integrated
    from tests.unit.test_window_run_coverage_acceptance_cli import CoverageModel

    source, intake, windows = prefix_case
    acceptance = tmp_path / "acceptance.json"
    accepted = _accept(windows, acceptance, accept=True)
    assert accepted.exit_code == 0, accepted.output
    l1, domain, _ = _approve_integrated(
        tmp_path, source, intake, windows, CoverageModel(), acceptance=acceptance,
    )
    options = dict(
        source_path=source, window_run_path=windows, l1_state_root=l1, domain_path=domain,
        state_root=tmp_path / "donor-l2", foundry_config=_config(),
        max_calls=10, max_physical_calls=10, max_output_tokens=4096,
    )
    emitted = []

    def original_response(request):
        payload = json.loads(request["messages"][1]["content"])
        raw = {"candidates": []} if not emitted else _records("not contiguous")
        if raw["candidates"]:
            raw["candidates"] = raw["candidates"][:1]
        emitted.append((payload, raw))
        return raw

    sdk, original_calls = _sdk(original_response)
    with pytest.raises(QuoteAnchorResolutionError):
        core.run_approved_reextraction(
            **options, anchor_mode=QUOTE_MODE,
            client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    assert len(original_calls) == 2
    state = options["state_root"]
    before = _snapshot(state)
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    payload, raw = emitted[-1]
    cached = next(
        json.loads(p.read_text()) for p in (state / "reextraction-responses").glob("*.json")
        if json.loads(p.read_text())["response"] == raw
    )
    document = _document(authority, state)
    document["corrections"] = [_correction(
        raw, SimpleNamespace(**payload["source_identity"]), cached["request_hash"],
        "candidates[0].anchors[0]", payload["source_text"].strip(),
    )]
    path = _write(tmp_path / "review.json", document)
    child = _child(options, approved_quote_review=path, max_calls=10, max_physical_calls=10)
    plan = continuation.run_approved_continuation(**child, dry_run=True)
    assert plan["reused_responses"] == 2
    assert plan["remaining_work_units"] > 0
    paid_prompts = {call["messages"][1]["content"] for call in original_calls}

    def missing_only(request):
        assert request["messages"][1]["content"] not in paid_prompts
        return {"candidates": []}

    sdk, new_calls = _sdk(missing_only)
    result = continuation.run_approved_continuation(
        **child, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert len(new_calls) == plan["remaining_work_units"]
    assert result["lineage_spent"]["physical_calls"] == 2 + len(new_calls)
    assert result["lineage_spent"]["logical_calls"] == 2 + len(new_calls)
    assert _snapshot(state) == before


def test_cli_requires_explicit_donor_and_forwards_review(failed_donor, monkeypatch):
    options, path, _ = failed_donor
    child = _child(options)
    import fabric_kg_builder.config.loader as loader
    monkeypatch.setattr(loader, "load_config", lambda **kw: SimpleNamespace(foundry=_config()))
    args = _cli_args(child) + ["--approved-quote-review", str(path), "--dry-run"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["quote_review"]["corrected_requests"] == 1
    at = args.index("--reuse-approved-run")
    del args[at:at + 2]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "--approved-quote-review requires --reuse-approved-run" in result.output
