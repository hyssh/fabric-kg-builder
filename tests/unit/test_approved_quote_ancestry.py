"""Repeated reviewed continuations preserve paid ancestry and original output."""

import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.enrichment import schema2_stage
from tests.unit.test_approved_quote_review import (
    QUOTE_MODE, QuoteAnchorResolutionError, FoundryClient, continuation, core, review,
    _child, _config, _correction, _document, _records, _sdk, _sha, _snapshot, _write,
    failed_donor,  # noqa: F401
)
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


def _review_for(state, emitted, path, *, version=review.ANCESTRY_VERSION):
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    payload, raw = emitted[-1]
    cached = next(
        json.loads(p.read_text()) for p in (state / "reextraction-responses").glob("*.json")
        if "imported_from" not in json.loads(p.read_text())
        and json.loads(p.read_text())["response"] == raw
    )
    document = _document(authority, state)
    document["version"] = version
    if version == review.ANCESTRY_VERSION:
        document["donor_code_identity_sha256"] = canonical_sha256(authority["code_identity"])
    document["corrections"] = [_correction(
        raw, SimpleNamespace(**payload["source_identity"]), cached["request_hash"],
        "candidates[0].anchors[0]", payload["source_text"].strip(),
    )]
    return _write(path, document)


def test_documented_v2_review_schema():
    text = (Path(__file__).resolve().parents[2] / "docs/specs/SPEC-APPROVED-EXTRACTION-PROMPT.md").read_text()
    section = text.split("### Version 2: repeated, ancestry-preserving recovery", 1)[1]
    example = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert set(example) == review.ANCESTRY_FIELDS
    assert example["version"] == review.ANCESTRY_VERSION
    assert set(example["corrections"][0]) == review.CORRECTION_FIELDS


@pytest.fixture
def ancestry_case(prefix_case, tmp_path, legacy_quote_producer):
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
    return dict(
        source_path=source, window_run_path=windows, l1_state_root=l1, domain_path=domain,
        state_root=tmp_path / "original", foundry_config=_config(),
        max_calls=10, max_physical_calls=12, max_output_tokens=4096,
    )


def _bad_provider(emitted):
    def respond(request):
        raw = _records("not contiguous")
        raw["candidates"] = raw["candidates"][:1]
        emitted.append((json.loads(request["messages"][1]["content"]), raw))
        return raw
    sdk, calls = _sdk(respond)
    return lambda: FoundryClient(_config(), _sdk_client=sdk), calls


@pytest.mark.parametrize("unimported_ancestor_response", [False, True])
def test_repeated_rounds_and_unimported_paid_ancestry(ancestry_case, monkeypatch, unimported_ancestor_response):
    options = ancestry_case
    emitted = []
    factory, calls = _bad_provider(emitted)
    if unimported_ancestor_response:
        # Pay the last authorized root first. The first child will stop before importing it.
        original_run = schema2_stage.run_l2

        def out_of_order(**kwargs):
            inputs, _, materialized, _ = core.prepare_approved_sources(
                **{k: options[k] for k in ("source_path", "l1_state_root", "domain_path", "window_run_path")},
                discovery_file=None,
            )
            authority = json.loads((options["state_root"] / continuation.AUTHORITY).read_text())
            unit = continuation._roots(authority, inputs, materialized)[-1]
            prompt = continuation.render_extraction_prompt(
                continuation.compile_closed_vocabulary(inputs.domain_contract),
                source_unit_id=unit.source_unit_id, source_text_hash=unit.source_text_hash,
                source_text=unit.anchored_text, slice_start=unit.slice_start, slice_end=unit.slice_end,
            )
            kwargs["service"].complete(prompt=prompt, work_unit=unit)
            return original_run(**kwargs)

        def first_empty(request):
            payload = json.loads(request["messages"][1]["content"])
            raw = {"candidates": []} if not emitted else {"candidates": _records("not contiguous")["candidates"][:1]}
            emitted.append((payload, raw))
            return raw

        sdk, calls = _sdk(first_empty)
        factory = lambda: FoundryClient(_config(), _sdk_client=sdk)
        monkeypatch.setattr(schema2_stage, "run_l2", out_of_order)
    with pytest.raises(QuoteAnchorResolutionError):
        core.run_approved_reextraction(**options, anchor_mode=QUOTE_MODE, client_factory=factory)
    monkeypatch.undo()
    paid_calls = len(calls)
    assert paid_calls == (2 if unimported_ancestor_response else 1)
    originals = {options["state_root"]: _snapshot(options["state_root"])}
    review_path = _review_for(
        options["state_root"], emitted, options["state_root"].parent / "review-1.json",
        version=review.VERSION,
    )
    previous = options
    round_number = 1
    while True:
        child = _child(
            previous, state_root=options["state_root"].parent / f"round-{round_number}",
            approved_quote_review=review_path, max_calls=10, max_physical_calls=12,
        )
        plan = continuation.run_approved_continuation(**child, dry_run=True)
        assert plan["reused_responses"] == paid_calls
        assert plan["prior_spent"]["logical_calls"] == paid_calls
        assert plan["prior_spent"]["physical_calls"] == paid_calls
        assert plan["prior_spent"]["reserved_output_tokens"] == paid_calls * 4096
        assert plan["quote_review_chain"]["review_count"] == round_number
        if plan["remaining_work_units"]:
            with pytest.raises(ValueError, match="BUDGET_TOO_SMALL"):
                continuation.run_approved_continuation(
                    **{**child, "max_calls": 0, "max_physical_calls": 0}, dry_run=True,
                )
            assert not child["state_root"].exists()
            next_emitted = []
            factory, new_calls = _bad_provider(next_emitted)
            if unimported_ancestor_response:
                original_run = schema2_stage.run_l2

                def fail_before_unused_import(**kwargs):
                    inputs, _, materialized, _ = core.prepare_approved_sources(
                        **{k: options[k] for k in ("source_path", "l1_state_root", "domain_path", "window_run_path")},
                        discovery_file=None,
                    )
                    authority = json.loads((child["state_root"] / continuation.AUTHORITY).read_text())
                    roots = continuation._roots(authority, inputs, materialized)
                    unused_source = emitted[0][0]["source_identity"]["source_unit_id"]
                    reviewed_source = emitted[-1][0]["source_identity"]["source_unit_id"]
                    roots = sorted(roots, key=lambda unit: unit.source_unit_id != reviewed_source)
                    for unit in roots:
                        if unit.source_unit_id == unused_source:
                            continue
                        prompt = continuation.render_extraction_prompt(
                            continuation.compile_closed_vocabulary(inputs.domain_contract),
                            source_unit_id=unit.source_unit_id, source_text_hash=unit.source_text_hash,
                            source_text=unit.anchored_text, slice_start=unit.slice_start, slice_end=unit.slice_end,
                        )
                        kwargs["service"].complete(prompt=prompt, work_unit=unit)
                    return original_run(**kwargs)

                monkeypatch.setattr(schema2_stage, "run_l2", fail_before_unused_import)
            with pytest.raises(QuoteAnchorResolutionError):
                continuation.run_approved_continuation(**child, client_factory=factory)
            assert len(new_calls) == 1
            paid_calls += 1
            before_resume = _snapshot(child["state_root"])
            with pytest.raises(QuoteAnchorResolutionError):
                continuation.run_approved_continuation(
                    **child, resume=True, client_factory=lambda: pytest.fail("Resume repaid invalid response"),
                )
            assert _snapshot(child["state_root"]) == before_resume
            monkeypatch.undo()
            originals[child["state_root"]] = before_resume
            if unimported_ancestor_response:
                imported = [
                    json.loads(p.read_text()) for p in (child["state_root"] / "reextraction-responses").glob("*.json")
                    if "imported_from" in json.loads(p.read_text())
                ]
                assert len(imported) == 1  # original donor has TWO responses
            previous = child
            round_number += 1
            review_path = _review_for(
                child["state_root"], next_emitted,
                options["state_root"].parent / f"review-{round_number}.json",
            )
        else:
            result = continuation.run_approved_continuation(
                **child, client_factory=lambda: pytest.fail("All paid responses must be reused"),
            )
            assert result["lineage_spent"]["logical_calls"] == paid_calls == 3
            assert result["physical_calls"] == result["logical_calls"] == 0
            snapshot = _snapshot(child["state_root"])
            assert continuation.run_approved_continuation(**child, resume=True) == result
            assert _snapshot(child["state_root"]) == snapshot
            chain = json.loads((child["state_root"] / "approved-quote-review-ancestry.json").read_text())
            assert len(chain["reviews"]) == round_number
            for record in chain["reviews"]:
                data = base64.b64decode(record["review_file_utf8_base64"])
                assert _sha(data) == record["review_file_sha256"]
                assert json.loads(data) == record["review"]
            projections = [
                json.loads(p.read_text()) for p in (child["state_root"] / "reextraction-reviewed-responses").glob("*.json")
            ]
            assert len(projections) == round_number
            assert {p["review"]["review_file_sha256"] for p in projections} == {
                r["review_file_sha256"] for r in chain["reviews"]
            }
            for projection in projections:
                original = json.loads(base64.b64decode(projection["original_provider_output"]["raw_output_utf8_base64"]))
                assert core.response_hash(original, QUOTE_MODE) == projection["original_response_hash"]
                assert original["candidates"][0]["anchors"][0]["quote"] == "not contiguous"
                corrected = deepcopy(original)
                corrected["candidates"][0]["anchors"][0]["quote"] = projection["corrections"][0]["new_quote"]
                assert corrected == projection["reviewed_response"]
            review_path.write_text(review_path.read_text() + "\n")
            with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
                continuation.run_approved_continuation(**child, resume=True, dry_run=True)
            with pytest.raises(ValueError, match="REVIEWED_CHILD_DONOR_UNSUPPORTED"):
                continuation.run_approved_continuation(
                    **{**child, "approved_quote_review": None}, resume=True, dry_run=True,
                )
            break
    assert round_number == (2 if unimported_ancestor_response else 3)
    for state, snapshot in originals.items():
        assert _snapshot(state) == snapshot


def test_exact_old_helper_pair_only(legacy_quote_producer):
    helpers = {continuation.CODE_PATH: "a" * 64, review.CODE_PATH: "b" * 64}
    core_identity = core._code_identity()
    legacy = {**core_identity, **review.LEGACY_HELPERS}
    assert review.verify_helper_identity(
        legacy, current_helpers=helpers, review_version=review.VERSION, has_chain=False,
    ) == "legacy"
    assert review.verify_helper_identity(
        {**core_identity, **helpers}, current_helpers=helpers,
        review_version=review.ANCESTRY_VERSION, has_chain=True,
    ) == "current"
    for identity, version, has_chain in (
        ({**legacy, review.CODE_PATH: "c" * 64}, review.VERSION, False),
        ({**legacy, continuation.CODE_PATH: "c" * 64}, review.VERSION, False),
        ({**legacy, "unexpected.py": "c" * 64}, review.VERSION, False),
        (legacy, review.ANCESTRY_VERSION, False),
        (legacy, review.VERSION, True),
        ({**core_identity, **helpers}, review.ANCESTRY_VERSION, False),
    ):
        with pytest.raises(ValueError, match="HELPER_CODE_DRIFT"):
            review.verify_helper_identity(
                identity, current_helpers=helpers, review_version=version, has_chain=has_chain,
            )


@pytest.fixture
def sealed_review_child(failed_donor):
    options, path, first_document = failed_donor
    child = _child(options, approved_quote_review=path)
    continuation.run_approved_continuation(
        **child, client_factory=lambda: pytest.fail("No provider calls"),
    )
    authority = json.loads((child["state_root"] / continuation.AUTHORITY).read_text())
    document = _document(authority, child["state_root"])
    document["version"] = review.ANCESTRY_VERSION
    document["donor_code_identity_sha256"] = canonical_sha256(authority["code_identity"])
    document["corrections"] = deepcopy(first_document["corrections"])
    document["corrections"][0]["request_hash"] = "e" * 64
    path = _write(child["state_root"].parent / "next-review.json", document)
    next_child = _child(
        child, state_root=child["state_root"].parent / "next-child", approved_quote_review=path,
    )
    return options, child, next_child, document


@pytest.mark.parametrize(("target", "error"), [
    ("review-record", "REVIEW_RECORD_DRIFT"),
    ("review-bytes", "RETAINED_RECORD_INVALID"),
    ("missing-review", "REVIEW_ARTIFACT_INVALID"),
    ("chain", "REVIEW_CHAIN_DRIFT"),
    ("missing-chain", "REVIEW_ARTIFACT_INVALID"),
    ("projection", "REVIEW_PROJECTION_DRIFT"),
    ("missing-projection", "REVIEW_PROJECTION_DRIFT"),
    ("import", "IMPORT_PROOF_INVALID"),
    ("lineage-budget", "SEMANTICS_DRIFT"),
    ("helper", "HELPER_CODE_DRIFT"),
    ("ancestor", "DONOR_BINDING_DRIFT"),
])
def test_tampered_review_ancestry_fails_closed_even_if_inventory_resealed(sealed_review_child, target, error):
    original, child, next_child, document = sealed_review_child
    state = child["state_root"]
    if target in ("review-record", "review-bytes"):
        path = state / "approved-quote-correction-review.json"
        record = json.loads(path.read_text())
        record["actor" if target == "review-record" else "review_file_utf8_base64"] = "tampered"
        _write(path, record)
    elif target in ("missing-review", "missing-chain"):
        (state / ("approved-quote-correction-review.json" if target == "missing-review" else "approved-quote-review-ancestry.json")).unlink()
    elif target == "chain":
        path = state / "approved-quote-review-ancestry.json"
        record = json.loads(path.read_text())
        record["reviews"][0]["actor"] = "tampered"
        _write(path, record)
    elif target in ("projection", "missing-projection"):
        path = next((state / "reextraction-reviewed-responses").glob("*.json"))
        if target == "missing-projection":
            path.unlink()
        else:
            record = json.loads(path.read_text())
            record["reviewed_response"]["candidates"][0]["label"] = "changed non-anchor"
            _write(path, record)
    elif target == "import":
        path = next((state / "reextraction-responses").glob("*.json"))
        record = json.loads(path.read_text())
        record["imported_from"]["response_hash"] = "d" * 64
        _write(path, record)
    elif target in ("helper", "lineage-budget"):
        path = state / continuation.AUTHORITY
        authority = json.loads(path.read_text())
        if target == "helper":
            authority["code_identity"][continuation.CODE_PATH] = "d" * 64
        else:
            authority["continuation"]["prior_spent"]["physical_calls"] = 0
        authority["fingerprint"] = canonical_sha256({k: v for k, v in authority.items() if k != "fingerprint"})
        _write(path, authority)
    else:
        ancestor = original["state_root"]
        (ancestor / "unexpected-file.json").write_text("{}")
        core._write_state(ancestor / continuation.INTEGRITY, core._inventory(ancestor))
    core._write_state(state / continuation.INTEGRITY, core._inventory(state))
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    document.update(
        donor_fingerprint=authority["fingerprint"],
        donor_authority_sha256=_sha((state / continuation.AUTHORITY).read_bytes()),
        donor_integrity_sha256=_sha((state / continuation.INTEGRITY).read_bytes()),
        donor_code_identity_sha256=canonical_sha256(authority["code_identity"]),
    )
    _write(next_child["approved_quote_review"], document)
    before = _snapshot(state)
    with pytest.raises(ValueError, match=error):
        continuation.run_approved_continuation(
            **next_child, client_factory=lambda: pytest.fail("Tampering called provider"),
        )
    assert not next_child["state_root"].exists()
    assert _snapshot(state) == before


@pytest.mark.parametrize(("field", "value", "error"), [
    ("donor_code_identity_sha256", "d" * 64, "DONOR_CODE_BINDING_DRIFT"),
    ("donor_integrity_sha256", "d" * 64, "DONOR_BINDING_DRIFT"),
    ("version", review.VERSION, "SCHEMA_INVALID"),
])
def test_v2_explicit_donor_binding(sealed_review_child, field, value, error):
    _, child, next_child, document = sealed_review_child
    document[field] = value
    _write(next_child["approved_quote_review"], document)
    with pytest.raises(ValueError, match=error):
        continuation.run_approved_continuation(**next_child, dry_run=True)
    assert not next_child["state_root"].exists()


def test_cannot_replace_an_inherited_review(sealed_review_child):
    _, child, next_child, document = sealed_review_child
    previous = json.loads((child["state_root"] / "approved-quote-correction-review.json").read_text())
    document["corrections"] = previous["review"]["corrections"]
    _write(next_child["approved_quote_review"], document)
    with pytest.raises(ValueError, match="PREVIOUSLY_REVIEWED_REQUEST"):
        continuation.run_approved_continuation(**next_child, dry_run=True)
