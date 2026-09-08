from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, deterministic_contract_id
from fabric_kg_builder.domain.assessment import (
    AssessmentReport, CachedResponse, Decision, Window,
    assess_documents, review_assessment, verified_findings,
)
from fabric_kg_builder.domain.revision import BoundedRevisionClient, create_revision
from fabric_kg_builder.domain.stage import (
    SupplementalDesignLocation,
    finalize_l1_stage, load_prepared_l1_stage, preflight_l1_inputs, prepare_l1_stage,
)
from fabric_kg_builder.sources.inspector import DesignSamplingBudget
from tests.unit.test_l1_stage import _candidates, _intake


def _case(tmp_path: Path, *, file_count: int = 2):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(file_count):
        (source / f"record-{index:02}.html").write_text(
            f"<p>A governed record describes a governed subject. "
            f"Source {index:02} requires a special review before publication.</p>",
            encoding="utf-8",
        )
    limits = DesignSamplingBudget.default().model_dump(mode="json", exclude={"budget_snapshot_hash"})
    limits.update(max_source_files=1, max_samples_per_kind=1, max_excerpt_codepoints=24)
    budget = DesignSamplingBudget.model_validate_json(canonical_json({
        **limits, "budget_snapshot_hash": canonical_sha256(limits),
    }))
    candidates = _candidates("revision")
    candidates["semantic_type_candidates"][0]["proposed_type"]["declared_properties"] = [{
        "property_id": "property:revision.status", "display_name": "Status",
        "value_type": "string", "required": False,
    }]
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=_intake("revision"), project_id="project:revision",
        run_id="run:parent", model_version="fixture", model_hash=canonical_sha256("fixture"),
        budget=budget,
    )
    parent = prepare_l1_stage(preflight, candidates=candidates)
    parent_state = tmp_path / "parent"
    finalize_l1_stage(
        parent, decision=None, actor=None,
        state_root=parent_state, domain_path=parent_state / "domain.yaml",
    )
    plan = assess_documents(source, parent.proposal.draft_contract, dry_run=True)
    report = assess_documents(source, parent.proposal.draft_contract, max_calls=max(4, file_count), responses={
        window.window_id: {"findings": [{
            "category": "ontology_gap", "summary": "Preserve the special review condition.",
            "suggested_action": "refine_scope",
            "quote": "Source " + window.text.split("Source ")[1],
            "question_ids": ["cq:q1"], "semantic_ids": [],
        }]}
        for index, window in enumerate(plan.windows)
    })
    review, request = _review(report)
    return {
        "parent_state": parent_state, "source": source, "report": report,
        "review": review, "request": request, "state_root": tmp_path / "child",
        "domain_path": tmp_path / "child" / "domain.yaml",
    }, candidates, parent


def _review(report):
    return review_assessment(report, actor="reviewer", decisions=tuple(
        Decision(finding_id=finding.finding_id, disposition="accepted", rationale="Source verified.")
        for finding in report.findings
    ))


def _reseal_report(report, mutate):
    raw = report.model_dump(mode="json", exclude={"report_hash"})
    mutate(raw)
    for window in raw["windows"]:
        window["window_id"] = deterministic_contract_id("assessment-window", {
            key: value for key, value in window.items()
            if key not in {"window_id", "status", "reason", "response"}
        })
    raw["findings"] = [
        finding.model_dump(mode="json")
        for window in raw["windows"]
        for finding in verified_findings(Window.model_validate_json(canonical_json(window)), report.domain_contract_hash)
    ]
    raw["report_hash"] = canonical_sha256(raw)
    return AssessmentReport.model_validate_json(canonical_json(raw))


def _parent_bytes(path):
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


@pytest.mark.parametrize("field", [
    "extraction_ref", "source_file_id", "source_unit_id", "source_ref", "page", "range", "text", "inventory",
])
def test_rehashed_report_cannot_replace_any_trusted_window_field(tmp_path, field):
    args, candidates, _ = _case(tmp_path)

    def forge(raw):
        window = raw["windows"][0]
        if field == "inventory":
            raw["windows"] = raw["windows"][1:]
            raw["files"] = [
                item for item in raw["files"]
                if item["source_file_id"] != window["source_file_id"]
            ]
        elif field in {"source_file_id", "source_ref"}:
            original_file = window["source_file_id"]
            for item in raw["files"]:
                if item["source_file_id"] == original_file:
                    item[field] = "forged-reference"
            window[field] = "forged-reference"
        elif field == "extraction_ref":
            window[field] = "f" * 64
        elif field == "text":
            window["text"] = "A fabricated source claim."
            window["text_hash"] = canonical_sha256(window["text"])
            window["span_end"] = window["span_start"] + len(window["text"])
            window["response"]["findings"][0]["quote"] = window["text"]
        elif field == "range":
            window["span_start"] += 10
            window["span_end"] += 10
        elif field == "page":
            window["page"] = 99
        else:
            window[field] = "forged-reference"

    report = _reseal_report(args["report"], forge)
    review, request = _review(report)
    with pytest.raises(ValueError, match="trusted source"):
        create_revision(**{**args, "report": report, "review": review, "request": request}, candidates=candidates)
    assert not args["state_root"].exists()


@pytest.mark.parametrize("field", ["question_ids", "semantic_ids"])
def test_revision_revalidates_finding_references_even_after_rehash(tmp_path, field):
    args, candidates, _ = _case(tmp_path)

    def forge(raw):
        raw["windows"][0]["response"]["findings"][0][field] = ["unknown:parent"]

    report = _reseal_report(args["report"], forge)
    review, request = _review(report)
    with pytest.raises(ValueError, match="unknown parent definitions"):
        create_revision(**{**args, "report": report, "review": review, "request": request}, candidates=candidates)
    assert not args["state_root"].exists()


@pytest.mark.parametrize("change", ["omitted_parent", "identity", "property_type", "removed_property", "hierarchy"])
def test_revision_refuses_breaking_parent_schema_drift(tmp_path, change):
    args, candidates, _ = _case(tmp_path)
    before = _parent_bytes(args["parent_state"])
    if change == "omitted_parent":
        candidates = _candidates("replacement")
    elif change == "identity":
        candidates["semantic_type_candidates"][0]["proposed_type"]["identity_key_policy"]["namespace"] = "changed.namespace"
    elif change == "property_type":
        candidates["semantic_type_candidates"][0]["proposed_type"]["declared_properties"][0]["value_type"] = "integer"
    elif change == "removed_property":
        candidates["semantic_type_candidates"][0]["proposed_type"]["declared_properties"] = []
    else:
        root = candidates["semantic_type_candidates"][0]["proposed_type"]
        child = candidates["semantic_type_candidates"][1]["proposed_type"]
        child.update(
            classification="domain_specialization", parent_type_id=root["type_id"],
            identity_root_type_id=root["type_id"], identity_key_policy=None,
            generalization_basis={"governance_rationale": "Reclassified by model"},
        )
    with pytest.raises(ValueError, match="breaking-change authorization"):
        create_revision(**args, candidates=candidates)
    assert _parent_bytes(args["parent_state"]) == before
    assert not args["state_root"].exists()


def test_out_of_sample_findings_become_fresh_bound_design_evidence(tmp_path):
    args, candidates, parent = _case(tmp_path)
    before = _parent_bytes(args["parent_state"])
    assert len(parent.sample_manifest.entries) == 1
    candidates["semantic_type_candidates"][0]["proposed_type"]["description"] += " Includes reviewed applicability."
    result = create_revision(**args, candidates=candidates)
    child = load_prepared_l1_stage(state_root=args["state_root"])
    assert result["status"] == "blocked" and not result["approved"]
    assert child.proposal.draft_contract.approval.status == "draft"
    assert _parent_bytes(args["parent_state"]) == before
    finding_ids = {item.finding_id for item in args["request"].accepted_findings}
    span_ids = {item.evidence_span_id for item in child.evidence_spans}
    assert not finding_ids & span_ids
    by_unit = {item.source_unit_id: item for item in child.source_units}
    for finding in args["request"].accepted_findings:
        span = next(item for item in child.evidence_spans if item.quote == finding.quote)
        assert span.verifier_name.endswith("/domain_design")
        span.verify_against(by_unit[span.source_unit_id])
        assert span.evidence_span_id in child.design_context.evidence_span_ids
        assert span.source_unit_id in child.design_context.source_unit_ids
        assert any(span.evidence_span_id in entry.evidence_span_ids for entry in child.sample_manifest.entries)
    budget = child.preflight.budget
    assert budget.max_source_files == 2
    assert budget.max_samples_per_kind == 3
    assert budget.max_excerpt_codepoints >= max(len(item.quote) for item in child.evidence_spans)
    assert len({item.budget_snapshot_hash for item in (
        budget, child.sample_manifest, child.source_profile, child.design_context,
    )}) == 1
    changes = json.loads((args["state_root"] / "revision-changes.json").read_text("utf-8"))
    assert result["changes_hash"] == changes["changes_hash"]
    assert any(item["kind"] == "entity" and item["operation"] == "modified" for item in changes["changes"])


def test_revision_caps_supplemental_findings_and_entire_prompt(tmp_path):
    args, candidates, _ = _case(tmp_path, file_count=17)
    with pytest.raises(ValueError, match="capped at 16"):
        create_revision(**args, candidates=candidates)
    assert not args["state_root"].exists()


def test_revision_allows_compatible_optional_property_additions(tmp_path):
    args, candidates, _ = _case(tmp_path)
    candidates["semantic_type_candidates"][0]["proposed_type"]["declared_properties"].append({
        "property_id": "property:revision.review-required", "display_name": "Review required",
        "value_type": "boolean", "required": False,
    })
    result = create_revision(**args, candidates=candidates)
    assert result["status"] == "blocked"
    assert any(item["kind"] == "property" and item["operation"] == "added" for item in result["changes"])


def test_revision_rejects_new_required_property_on_existing_type(tmp_path):
    args, candidates, _ = _case(tmp_path)
    candidates["semantic_type_candidates"][0]["proposed_type"]["declared_properties"].append({
        "property_id": "property:revision.review-required", "display_name": "Review required",
        "value_type": "boolean", "required": True,
    })
    before = _parent_bytes(args["parent_state"])
    with pytest.raises(ValueError, match="adds required property.*breaking-change authorization"):
        create_revision(**args, candidates=candidates)
    assert not args["state_root"].exists()
    assert _parent_bytes(args["parent_state"]) == before


def _review_extra_source(args, parent, quote, *, ocr_cache=None, ocr_identity=None):
    options = {
        "window_chars": 256, "max_calls": 1000,
        "ocr_cache": ocr_cache, "ocr_identity": ocr_identity,
    }
    plan = assess_documents(args["source"], parent.proposal.draft_contract, dry_run=True, **options)
    target = next(window for window in plan.windows if quote in window.text)
    report = assess_documents(
        args["source"], parent.proposal.draft_contract, **options,
        responses={
            window.window_id: {"findings": [{
                "category": "ontology_gap", "summary": "Preserve reviewed exception.",
                "suggested_action": "refine_scope", "quote": quote,
                "question_ids": ["cq:q1"], "semantic_ids": [],
            }] if window.window_id == target.window_id else []}
            for window in plan.windows
        },
    )
    review, request = _review(report)
    return {**args, "report": report, "review": review, "request": request}, target


def _ocr_revision_case(tmp_path, extension):
    from io import BytesIO
    from PIL import Image
    from fabric_kg_builder.sources.docintel_cache import make_cached_layout, store_cached_layout

    args, candidates, parent = _case(tmp_path)
    source_file = args["source"] / f"scanned{extension}"
    image = BytesIO()
    Image.new("RGB", (24, 24), color="white").save(image, format="PNG")
    if extension == ".png":
        source_file.write_bytes(image.getvalue())
    else:
        import fitz
        with fitz.open() as document:
            page = document.new_page(width=100, height=100)
            page.insert_image(page.rect, stream=image.getvalue())
            document.save(source_file)
    quote = "OCR-only exception requires a special review."
    text = "# Scanned source\n" + "padding " * 40 + "\n| condition | action |\n" + quote
    identity = {
        "api_version": "2024-11-30", "model_id": "prebuilt-layout",
        "options": {"output_content_format": "markdown"},
    }
    cached = make_cached_layout(source_file.read_bytes(), {
        "content": text, "stringIndexType": "unicodeCodePoint",
        "pages": [{"pageNumber": 1, "spans": [{"offset": 0, "length": len(text)}]}],
    }, identity)
    cache_dir = tmp_path / "ocr-cache"
    cache_path = store_cached_layout(cache_dir, cached)
    args, target = _review_extra_source(
        args, parent, quote, ocr_cache=cache_dir, ocr_identity=identity,
    )
    return {**args, "ocr_cache": cache_dir}, candidates, target, cache_path


@pytest.mark.parametrize("extension", [".png", ".pdf"])
def test_cached_image_and_scanned_pdf_findings_mint_fresh_design_evidence(tmp_path, extension):
    args, candidates, target, cache_path = _ocr_revision_case(tmp_path, extension)
    before_cache = cache_path.read_bytes()
    before_parent = _parent_bytes(args["parent_state"])
    result = create_revision(**args, candidates=candidates)
    assert result["status"] == "blocked"
    child = load_prepared_l1_stage(state_root=args["state_root"])
    finding = args["request"].accepted_findings[0]
    span = next(item for item in child.evidence_spans if item.quote == finding.quote)
    unit = next(item for item in child.source_units if item.source_unit_id == span.source_unit_id)
    assert target.extraction_ref == cache_path.stem
    assert target.span_start >= 256
    assert span.span_start == finding.span_start >= target.span_start
    assert unit.text.startswith("# Scanned source")
    assert unit.locator.page == span.locator.page == 1
    assert span.verifier_name.endswith("/domain_design")
    span.verify_against(unit)
    assert span.evidence_span_id in child.design_context.evidence_span_ids
    assert any(span.evidence_span_id in item.evidence_span_ids for item in child.sample_manifest.entries)
    assert finding.finding_id != span.evidence_span_id
    assert cache_path.read_bytes() == before_cache
    assert _parent_bytes(args["parent_state"]) == before_parent


@pytest.mark.parametrize("tamper", ["extraction_ref", "cache_missing"])
def test_supplemental_ocr_verifier_reloads_exact_cache_after_report_validation(tmp_path, monkeypatch, tamper):
    from dataclasses import replace
    from fabric_kg_builder.domain import revision

    args, candidates, _, cache_path = _ocr_revision_case(tmp_path, ".png")
    original = revision.prepare_l1_stage

    def change_after_report_validation(preflight, **kwargs):
        if tamper == "cache_missing":
            cache_path.unlink()
        else:
            kwargs["supplemental_design_locations"] = tuple(
                replace(location, extraction_ref="f" * 64)
                for location in kwargs["supplemental_design_locations"]
            )
        return original(preflight, **kwargs)

    monkeypatch.setattr(revision, "prepare_l1_stage", change_after_report_validation)
    with pytest.raises(ValueError, match="cache does not match"):
        create_revision(**args, candidates=candidates)
    assert not args["state_root"].exists()


def test_native_finding_in_later_window_uses_full_unit_recorded_offsets(tmp_path):
    args, candidates, parent = _case(tmp_path)
    quote = "A later-window exception requires review."
    text = "prefix " * 100 + quote
    (args["source"] / "long.html").write_text(f"<p>{text}</p>", encoding="utf-8")
    args, target = _review_extra_source(args, parent, quote)
    assert target.span_start > 256 and len(target.text) < len(text)
    create_revision(**args, candidates=candidates)
    child = load_prepared_l1_stage(state_root=args["state_root"])
    span = next(item for item in child.evidence_spans if item.quote == quote)
    unit = next(item for item in child.source_units if item.source_unit_id == span.source_unit_id)
    assert span.span_start == text.index(quote)
    assert unit.text == text
    span.verify_against(unit)


@pytest.mark.parametrize("source_kind", ["native", ".png", ".pdf"])
def test_supplemented_revision_survives_cli_approval_and_explicit_l2_handoff(
    tmp_path, monkeypatch, source_kind,
):
    from click.testing import CliRunner
    from fabric_kg_builder.cli import cli
    from fabric_kg_builder.enrichment.schema2_sources import load_l2_inputs

    if source_kind == "native":
        args, candidates, _ = _case(tmp_path)
    else:
        args, candidates, _, _ = _ocr_revision_case(tmp_path, source_kind)
    before_parent = _parent_bytes(args["parent_state"])
    result = create_revision(**args, candidates=candidates)
    child = load_prepared_l1_stage(state_root=args["state_root"])
    budget_bytes = (args["state_root"] / "design-sampling-budget.json").read_bytes()
    expected_spans = {item.evidence_span_id for item in child.evidence_spans}
    assert all(
        any(span.quote == finding.quote for span in child.evidence_spans)
        for finding in args["request"].accepted_findings
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    approved = runner.invoke(cli, [
        "domain", "approve", "--file", str(args["domain_path"]),
        "--state-dir", str(args["state_root"]), "--approved-by", "revision-reviewer",
        "--project-id", result["project_id"], "--run-id", result["run_id"],
        "--proposal-hash", result["proposal_hash"],
    ])
    assert approved.exit_code == 0, approved.output
    reloaded = load_prepared_l1_stage(state_root=args["state_root"])
    inputs = load_l2_inputs(
        l1_state_root=args["state_root"], domain_path=args["domain_path"],
    )
    assert inputs.domain_contract.approval.status == "approved"
    assert inputs.l1_receipt.status == "succeeded"
    assert inputs.design_sample_manifest == reloaded.sample_manifest
    assert inputs.design_sample_manifest.sample_hash == child.sample_manifest.sample_hash
    assert inputs.design_sample_manifest.budget_snapshot_hash == reloaded.preflight.budget.budget_snapshot_hash
    assert expected_spans == {item.evidence_span_id for item in reloaded.evidence_spans}
    assert (args["state_root"] / "design-sampling-budget.json").read_bytes() == budget_bytes
    assert inputs.l1_receipt.accepted_contract_versions["l1.design_sample_manifest"] == "1.0.0"
    before_dry_run = _parent_bytes(tmp_path)
    l2_state = tmp_path / "explicit-run" / "l2"
    planned = runner.invoke(cli, [
        "enrich", "--input", str(args["source"]),
        "--domain-file", str(args["domain_path"]), "--l1-state", str(args["state_root"]),
        "--l2-state", str(l2_state), "--dry-run",
    ])
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["status"] == "planned"
    assert plan["remote_calls"] == plan["writes"] == 0
    assert not l2_state.exists()
    assert _parent_bytes(tmp_path) == before_dry_run
    assert _parent_bytes(args["parent_state"]) == before_parent


def test_revision_rejects_missing_or_tampered_custom_budget_snapshot(tmp_path):
    args, candidates, _ = _case(tmp_path)
    path = args["parent_state"] / "design-sampling-budget.json"
    original = path.read_bytes()
    payload = json.loads(original)
    payload["max_source_files"] += 1
    path.write_text(canonical_json(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="sampling budget snapshot"):
        create_revision(**args, candidates=candidates)
    path.write_bytes(original)
    path.unlink()
    with pytest.raises(ValueError, match="snapshot is unavailable"):
        create_revision(**args, candidates=candidates)
    assert not args["state_root"].exists()


def test_revision_offline_prompt_budget_fails_before_child_write(tmp_path):
    args, candidates, _ = _case(tmp_path)
    with pytest.raises(ValueError, match="prompt budget exhausted"):
        create_revision(**args, candidates=candidates, max_prompt_chars=256)
    assert not args["state_root"].exists()


@pytest.mark.parametrize("change", ["source_ref", "source_text", "quote"])
def test_supplemental_stage_hook_reverifies_raw_locations_locally(tmp_path, change):
    _, candidates, parent = _case(tmp_path)
    unit = parent.source_units[0]
    entry = next(
        item for item in parent.preflight.corpus.entries
        if item.source_file_id == unit.identity.source_file_id
    )
    values = {
        "source_file_id": entry.source_file_id, "source_ref": entry.relative_source_ref,
        "source_text": unit.text, "page": unit.locator.page,
        "span_start": 0, "span_end": len(unit.text), "quote": unit.text,
    }
    values[change] = "fabricated"
    with pytest.raises(ValueError):
        prepare_l1_stage(
            parent.preflight, candidates=candidates,
            supplemental_design_locations=(SupplementalDesignLocation(**values),),
        )


class _Client:
    def __init__(self, response, *, identity="fixture/model"):
        self.response = response
        self.identity = identity
        self.calls = []

    def execution_identity(self):
        return {"model": self.identity}

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.response)


def test_live_revision_input_contains_full_parent_and_completed_response_is_cached(tmp_path):
    args, candidates, parent = _case(tmp_path)
    provider = _Client(candidates)
    bounded = BoundedRevisionClient(provider, max_calls=1, response_cache=tmp_path / "cache")
    result = create_revision(**args, client=bounded)
    assert result["status"] == "blocked"
    user = json.loads(provider.calls[0]["user"].split("\n", 1)[1].rsplit("\n", 1)[0])
    correction = user["user_correction_instruction"]
    parent_input = json.loads(correction)["parent_domain"]
    assert parent_input == parent.proposal.draft_contract.model_dump(mode="json")
    cached = next((tmp_path / "cache").glob("*.json"))
    response = CachedResponse.model_validate_json(cached.read_text("utf-8"))
    assert response.response["raw_response"] == candidates
    replay_root = tmp_path / "replay-child"
    replay_client = BoundedRevisionClient(provider, max_calls=1, response_cache=tmp_path / "cache")
    replay = create_revision(
        **{**args, "state_root": replay_root, "domain_path": replay_root / "domain.yaml"},
        client=replay_client,
    )
    assert replay["model_calls"] == 0 and replay["replayed_calls"] == 1
    assert len(provider.calls) == 1
    metrics = json.loads((replay_root / "resource-metrics.json").read_text("utf-8"))
    assert metrics["foundry_calls"] == 0


@pytest.mark.parametrize("bad_response", [{"invalid": "proposal"}, ["invalid-root"]])
def test_completed_invalid_response_replays_after_failed_revision(tmp_path, bad_response):
    args, _, _ = _case(tmp_path)
    cache = tmp_path / "cache"
    provider = _Client(bad_response)
    for attempt in range(3):
        bounded = BoundedRevisionClient(provider, max_calls=1, response_cache=cache)
        with pytest.raises(ValueError):
            create_revision(**args, client=bounded)
        assert not args["state_root"].exists()
        if attempt >= 1:
            assert bounded.replayed_calls >= 1
        if attempt == 2:
            assert bounded.calls == 0
    assert len(provider.calls) <= 2
    payloads = [CachedResponse.model_validate_json(path.read_text("utf-8")) for path in cache.glob("*.json")]
    assert payloads
    assert all(item.response["raw_response"] == bad_response for item in payloads)


def test_revision_cache_binds_exact_request_and_model_and_detects_tampering(tmp_path):
    cache = tmp_path / "cache"
    provider = _Client({"answer": "raw"})
    bounded = BoundedRevisionClient(provider, max_calls=3, response_cache=cache)
    request = {"system": "system", "user": "first", "json_schema": {}, "max_completion_tokens": 256, "max_attempts": 9}
    bounded.complete_json(**request)
    bounded.complete_json(**request)
    assert len(provider.calls) == 1
    assert provider.calls[0]["max_attempts"] == 1
    bounded.complete_json(**{**request, "user": "changed"})
    provider.identity = "different/model"
    bounded.complete_json(**request)
    assert len(provider.calls) == 3
    provider.identity = "fixture/model"
    first_hash = canonical_sha256({
        "revision_client_version": "1.1.0",
        "model_identity": provider.execution_identity(),
        "request": {**request, "max_attempts": 1},
    })
    first = cache / f"{first_hash}.json"
    raw = json.loads(first.read_text("utf-8"))
    original = copy.deepcopy(raw)
    raw["response_hash"] = "0" * 64
    first.write_text(canonical_json(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="cached response hash"):
        bounded.complete_json(**request)
    original["request_hash"] = "0" * 64
    first.write_text(canonical_json(original), encoding="utf-8")
    with pytest.raises(ValueError, match="request fingerprint"):
        bounded.complete_json(**request)
    assert len(provider.calls) == 3


def test_revision_refuses_cache_inside_parent_before_inference(tmp_path):
    args, candidates, _ = _case(tmp_path)
    provider = _Client(candidates)
    bounded = BoundedRevisionClient(provider, max_calls=1, response_cache=args["parent_state"] / "cache")
    with pytest.raises(ValueError, match="cache must be separate"):
        create_revision(**args, client=bounded)
    assert not provider.calls


def test_revision_requires_cache_for_ocr_assessed_report(tmp_path):
    args, candidates, _ = _case(tmp_path)
    report = _reseal_report(args["report"], lambda raw: raw.update(
        ocr_identity={"model": "prebuilt-layout", "api_version": "2024-11-30"},
    ))
    review, request = _review(report)
    with pytest.raises(ValueError, match="requires its exact OCR response cache"):
        create_revision(
            **{**args, "report": report, "review": review, "request": request},
            candidates=candidates,
        )
    assert not args["state_root"].exists()


def test_revision_passes_exact_report_ocr_identity_to_window_revalidation(tmp_path, monkeypatch):
    from fabric_kg_builder.domain import revision

    args, candidates, parent = _case(tmp_path)
    identity = {"model": "prebuilt-layout", "api_version": "2024-11-30"}
    report = _reseal_report(args["report"], lambda raw: raw.update(ocr_identity=identity))
    review, request = _review(report)
    seen = []
    original = revision.assessment_windows

    def capture(source, **kwargs):
        seen.append(kwargs)
        return original(source, **kwargs)

    monkeypatch.setattr(revision, "assessment_windows", capture)
    cache = tmp_path / "ocr-cache"
    result = create_revision(
        **{**args, "report": report, "review": review, "request": request},
        candidates=candidates, ocr_cache=cache,
    )
    assert result["status"] == "blocked"
    assert seen == [{
        "window_chars": report.window_chars, "ocr_cache": cache, "ocr_identity": identity,
    }]
    assert not cache.exists()
