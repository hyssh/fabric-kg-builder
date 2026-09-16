"""Approved reextraction is an explicit, budgeted new L2, never legacy replay."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


def _config():
    return FoundryConfig(endpoint="https://offline.invalid", chat_deployment="offline-configured",
                         openai_endpoint="https://offline.invalid")


def _options(case, state=None):
    root, source, _, windows, _, l1, domain, _ = case
    return dict(
        source_path=source, l1_state_root=l1, domain_path=domain,
        state_root=state or root / "fresh-l2", window_run_path=windows,
        foundry_config=_config(), max_calls=1, max_output_tokens=4096,
    )


def _sdk(response=None):
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        raw = response(kwargs) if callable(response) else response or {"candidates": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)), finish_reason="stop")],
        )

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    sdk.with_options = Mock(return_value=sdk)
    return sdk, requests


def _no_source_reads(monkeypatch):
    from fabric_kg_builder.enrichment.schema2_sources import IndexedSourceCorpusReader
    from fabric_kg_builder.sources import corpus

    monkeypatch.setattr(IndexedSourceCorpusReader, "read", lambda *_: pytest.fail("Parsed physical source"))
    monkeypatch.setattr(corpus, "extract_verified_source_snapshot", lambda *_: pytest.fail("Parsed/OCRed physical source"))
    monkeypatch.setattr(corpus, "validate_corpus_manifest_against_source", lambda *_a, **_k: pytest.fail("Read original bytes"))


def test_documented_prompt_matches_versioned_runtime():
    document = (
        Path(__file__).resolve().parents[2]
        / "docs/specs/SPEC-APPROVED-EXTRACTION-PROMPT.md"
    ).read_text(encoding="utf-8")
    current = document.split("## Current base system prompt: 1.3.0\n", 1)[1]
    assert current.split("```text\n", 1)[1].split("\n```", 1)[0] == core.SYSTEM_PROMPT
    assert core.VERSION == "approved-source-reextraction/1.3.0"
    assert core.SYSTEM_PROMPT.count("{{a few shot}}") == 1
    assert 'candidate_kind is a RECORD KIND, never an ontology type.' in core.SYSTEM_PROMPT
    assert 'source_text[span_start - slice_start:span_end - slice_start] must equal quote' in core.SYSTEM_PROMPT


def test_dry_run_no_client_no_writes_no_original_source_access(integrated_case, monkeypatch):
    options = _options(integrated_case)
    _no_source_reads(monkeypatch)
    result = core.run_approved_reextraction(
        **options, dry_run=True, client_factory=lambda: pytest.fail("Dry run constructed client"),
    )
    assert result["remote_calls"] == result["writes"] == result["original_source_reads"] == 0
    assert result["planned_chunks"] == 1
    assert result["data_lineage"] == {
        "enabled_after": "L1_approval",
        "domain_contract_hash": result["domain_contract_hash"],
        "storage": "L2_candidate_lifecycle_and_source_manifests",
    }
    assert not options["state_root"].exists()
    assert not (options["state_root"].parent / f".{options['state_root'].name}-enrichment.lock").exists()


def test_fresh_response_and_exact_resume_preserve_approved_cache(integrated_case, monkeypatch):
    options = _options(integrated_case)
    _no_source_reads(monkeypatch)
    before = {
        path: path.read_bytes()
        for root in (options["window_run_path"], options["l1_state_root"])
        for path in root.rglob("*") if path.is_file()
    }
    domain_before = options["domain_path"].read_bytes()
    sdk, requests = _sdk()
    result = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert result["physical_calls"] == result["logical_calls"] == len(requests) == 1
    assert sdk.with_options.call_args.kwargs == {"max_retries": 0}
    prompt = json.loads(requests[0]["messages"][1]["content"])
    assert prompt["entity_types"] and prompt["entity_types"][0]["effective_properties"]
    assert prompt["interpretation_context"]["authority"] == "interpretation_only_not_primary_evidence"
    assert "old candidates" in requests[0]["messages"][0]["content"]
    system = requests[0]["messages"][0]["content"]
    schema_instruction = (
        "\nReturn an object that validates exactly against this JSON "
        "Schema. Do not add fields that the schema does not permit.\n"
    )
    authority = json.loads((options["state_root"] / "approved-reextraction-authority.json").read_text())
    assert "{{a few shot}}" not in system
    assert authority["system_prompt_template"] == core.SYSTEM_PROMPT
    assert result["data_lineage"] == {
        "enabled_after": "L1_approval",
        "domain_contract_hash": authority["domain_authorities"]["domain_contract_hash"],
        "storage": "L2_candidate_lifecycle_and_source_manifests",
    }
    assert system == authority["system_prompt"] + schema_instruction + json.dumps(
        core.raw_candidate_response_schema(), sort_keys=True,
    )
    checkpoint = json.loads((options["state_root"] / "checkpoint.json").read_text())
    for item in checkpoint["work_units"].values():
        leaf = json.loads((options["state_root"] / "checkpoint-leaves" / item["artifact"]).read_text())
        assert leaf["raw_candidate_count"] == 0  # Discovery had candidates; no old values used.
    resumed = core.run_approved_reextraction(
        **options, resume=True, client_factory=lambda: pytest.fail("Exact resume called model"),
    )
    assert resumed == result
    assert options["domain_path"].read_bytes() == domain_before
    assert all(path.read_bytes() == data for path, data in before.items())
    with pytest.raises(ValueError, match="FRESH_STATE_REQUIRED"):
        core.run_approved_reextraction(**options, dry_run=True)


def test_new_declared_values_are_extracted_from_text_not_old_candidates(integrated_case):
    options = _options(integrated_case)

    def response(request):
        payload = json.loads(request["messages"][1]["content"])
        entity = next(item for item in payload["entity_types"]
                      if not item["abstract"] and any(prop["value_type"] == "number" for prop in item["effective_properties"]))
        prop = next(item for item in entity["effective_properties"] if item["value_type"] == "number")
        name_prop = next(item for item in entity["effective_properties"] if item["value_type"] == "string")
        text = payload["source_text"]
        start = text.index("80.5") + payload["source_identity"]["slice_start"]
        anchor = {"span_start": start, "span_end": start + 4, "quote": "80.5", "model_authored_evidence_id": None}
        label = "governed subject"
        label_start = text.index(label) + payload["source_identity"]["slice_start"]
        label_anchor = {
            "span_start": label_start, "span_end": label_start + len(label),
            "quote": label, "model_authored_evidence_id": None,
        }
        identity = entity["identity_key_policy"]
        return {"candidates": [
            {
                "candidate_kind": "entity", "local_id": "new-instance", "observed_type": entity["type_id"],
                "label": label, "anchors": [label_anchor], "stable_source_identity": None,
                "identity_key": {field: label for field in identity["business_key_fields"]},
            },
            {
                "candidate_kind": "property", "owner_local_id": "new-instance",
                "observed_property": prop["property_id"], "value": 80.5, "normalized_value": 80.5,
                "temporal_key": None, "anchor": anchor,
            },
            {
                "candidate_kind": "property", "owner_local_id": "new-instance",
                "observed_property": name_prop["property_id"], "value": label, "normalized_value": label,
                "temporal_key": None, "anchor": label_anchor,
            },
        ]}

    sdk, requests = _sdk(response)
    result = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert result["logical_calls"] == 1
    leaves = [json.loads(path.read_text()) for path in (options["state_root"] / "checkpoint-leaves").glob("*.json")]
    properties = [candidate for leaf in leaves for candidate in leaf["proposed_candidates"]
                  if candidate["candidate_kind"] == "property"]
    assert {json.loads(candidate["value_json"]) for candidate in properties} == {80.5, "governed subject"}


def test_fingerprint_and_response_drift_fail_before_model(integrated_case, monkeypatch):
    options = _options(integrated_case)
    sdk, _ = _sdk()
    core.run_approved_reextraction(**options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk))
    for delta in (
        {"max_calls": 2}, {"max_output_tokens": 2048}, {"max_context_tokens": 95_000},
        {"foundry_config": _config().model_copy(update={"chat_deployment": "different"})},
    ):
        with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
            core.run_approved_reextraction(**{**options, **delta}, resume=True, dry_run=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core, "_code_identity", lambda: {"changed": "code"})
        with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
            core.run_approved_reextraction(**options, resume=True, dry_run=True)
    response = next((options["state_root"] / "reextraction-responses").glob("*.json"))
    response.write_text("{}")
    with pytest.raises(ValueError, match="CHECKPOINT_DRIFT"):
        core.run_approved_reextraction(**options, resume=True, dry_run=True)


def test_physical_retries_cannot_exceed_reserved_budget(tmp_path, monkeypatch):
    from fabric_kg_builder.enrichment import foundry_client

    class RateLimitError(Exception):
        status_code = 429

    calls = []

    def failing(**kwargs):
        calls.append(kwargs)
        raise RateLimitError("offline")

    sdk, _ = _sdk()
    sdk.chat.completions.create = failing
    ledger = core._BudgetLedger(tmp_path / "budget", max_calls=1, max_physical_calls=2, max_output_tokens=4096)
    ledger.request_hash = "request"
    monkeypatch.setattr(foundry_client, "_transport_retry_sleep", lambda _: None)
    client = core._bounded_client(_config(), ledger, lambda: FoundryClient(_config(), _sdk_client=sdk))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        client.complete_json(system="offline", user="offline", json_schema={},
                             max_completion_tokens=4096, max_attempts=1)
    assert len(calls) == 2
    assert ledger.metrics() == {"logical_calls": 0, "physical_calls": 2, "reserved_output_tokens": 8192}
    assert all(row["status"] == "failed" for row in ledger.data["physical_attempts"])


def test_public_cli_dry_run_and_explicit_flag_contract(integrated_case, monkeypatch):
    from fabric_kg_builder.config import loader

    options = _options(integrated_case)
    _no_source_reads(monkeypatch)
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=_config()))
    args = [
        "enrich", "--input", str(options["source_path"]), "--domain-file", str(options["domain_path"]),
        "--l1-state", str(options["l1_state_root"]), "--l2-state", str(options["state_root"]),
        "--window-run", str(options["window_run_path"]), "--reextract-approved", "--max-calls", "1",
    ]
    result = CliRunner().invoke(cli, [*args, "--dry-run"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.output)["writes"] == 0
    result = CliRunner().invoke(cli, [*args, "--replay-only"])
    assert result.exit_code != 0 and "conflicts" in result.output
    result = CliRunner().invoke(cli, args[:-2])
    assert result.exit_code != 0 and "explicit --max-calls" in result.output
    result = CliRunner().invoke(cli, ["enrich", "--input", str(options["source_path"]), "--max-calls", "1"])
    assert result.exit_code != 0 and "require --reextract-approved" in result.output


def test_exact_approved_prefix_excludes_other_chunks_and_preflights_budget(prefix_case, tmp_path, monkeypatch):
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
    _no_source_reads(monkeypatch)
    options = dict(source_path=source, window_run_path=windows, l1_state_root=l1,
                   domain_path=domain, state_root=tmp_path / "l2",
                   foundry_config=_config(), max_calls=2)
    with pytest.raises(ValueError, match="BUDGET_TOO_SMALL.*3"):
        core.run_approved_reextraction(**options, dry_run=True)
    assert not options["state_root"].exists()
    options["max_calls"] = 3
    plan = core.run_approved_reextraction(**options, dry_run=True)
    assert plan["planned_chunks"] == 3
    sdk, requests = _sdk()
    result = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert result["physical_calls"] == result["logical_calls"] == 3
    assert all("UNPROCESSED_PREFIX_SENTINEL" not in request["messages"][1]["content"] for request in requests)
    sealed = json.loads((options["state_root"] / "approved-reextraction-authority.json").read_text())
    assert len(sealed["scope"]) == 3
    for request in requests:
        identity = json.loads(request["messages"][1]["content"])["source_identity"]
        assert identity in sealed["scope"]


def test_logical_budget_and_uncertain_attempt_survive_resume(tmp_path):
    options = dict(max_calls=1, max_physical_calls=2, max_output_tokens=4096)
    ledger = core._BudgetLedger(tmp_path, **options)
    row = ledger.reserve("logical_requests", "request")
    with pytest.raises(ValueError, match="UNCERTAIN_CALL"):
        core._BudgetLedger(tmp_path, **options)
    ledger.finish(row, "failed")
    resumed = core._BudgetLedger(tmp_path, **options)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        resumed.reserve("logical_requests", "retry")


def test_code_identity_seals_grounded_evidence_rules():
    assert len(core._code_identity()["enrichment/schema2_evidence.py"]) == 64
    assert len(core._code_identity()["enrichment/approved_few_shot.py"]) == 64


def test_model_metadata_override_is_not_a_deployment(integrated_case):
    with pytest.raises(ValueError, match="configured Foundry deployment"):
        core.run_approved_reextraction(**_options(integrated_case), model_override="fake-model", dry_run=True)


def test_distinct_configured_deployments_share_source_not_checkpoints(integrated_case):
    options = _options(integrated_case)
    first = core.run_approved_reextraction(**options, dry_run=True)
    different = _config().model_copy(update={"chat_deployment": "offline-comparison"})
    options.update(foundry_config=different, state_root=options["state_root"].with_name("comparison-l2"))
    sdk, requests = _sdk()
    second = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(different, _sdk_client=sdk),
    )
    assert first["source_unit_manifest_hash"] == second["source_unit_manifest_hash"]
    assert first["domain_contract_hash"] == second["domain_contract_hash"]
    assert first["fingerprint"] != second["fingerprint"]
    assert requests[0]["model"] == second["model_version"] == "offline-comparison"


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
def test_preflight_matches_actual_sdk_envelope_and_counts_full_schema(integrated_case, route):
    from fabric_kg_builder.enrichment.approved_input_budget import account_request, request_envelope

    options = _options(integrated_case)
    if route == "project_responses":
        options["foundry_config"] = _config().model_copy(update={"inference_api": route})
    config = options["foundry_config"]
    sdk, calls = _sdk()
    if route == "project_responses":
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text='{"candidates":[]}', status="completed")
        sdk.responses = SimpleNamespace(create=create)
    plan = core.run_approved_reextraction(**options, dry_run=True)
    core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    authority = json.loads((options["state_root"] / "approved-reextraction-authority.json").read_text())
    request = calls[0]
    user = request["input"].split("\n", 1)[1] if route == "project_responses" else request["messages"][1]["content"]
    assert request_envelope(
        config, system=authority["system_prompt"], user=user,
        schema=core.raw_candidate_response_schema(), output=options["max_output_tokens"],
    ) == request
    accounting = account_request(request, authority["input_budget"])
    assert accounting["estimated_total_tokens_upper_bound"] == plan["input_budget"]["maxima"]["estimated_total_tokens_upper_bound"]
    assert accounting["estimated_input_tokens_upper_bound"] > (
        len(authority["system_prompt"].encode("utf-8")) + len(user.encode("utf-8"))
        + len(json.dumps(authority["response_schema"], sort_keys=True).encode("utf-8"))
    )
    assert "Synthetic example" not in json.dumps(plan)
    assert "source_text" not in json.dumps(plan)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("fixed_overflow", [False, True])
def test_complete_request_cap_rejects_before_any_reservation(integrated_case, dry_run, fixed_overflow):
    options = _options(integrated_case)
    plan = core.run_approved_reextraction(**options, dry_run=True)
    maxima = plan["input_budget"]["maxima"]
    options["max_context_tokens"] = (
        maxima["fixed_total_tokens_upper_bound"] - 1 if fixed_overflow
        else maxima["estimated_total_tokens_upper_bound"] - 1
    )
    reason = "fixed_overhead_exceeds_cap" if fixed_overflow else "source_slice_requires_rechunk"
    with pytest.raises(ValueError, match=f"INPUT_BUDGET_EXCEEDED.*{reason}") as error:
        core.run_approved_reextraction(
            **options, dry_run=dry_run, client_factory=lambda: pytest.fail("preflight constructed SDK"),
        )
    assert "available_serialized_source_bytes=" in str(error.value)
    assert "governed subject" not in str(error.value)
    assert not options["state_root"].exists()
    options["max_context_tokens"] = maxima["estimated_total_tokens_upper_bound"]
    allowed = core.run_approved_reextraction(**options, dry_run=True)
    assert allowed["input_budget"]["minimum_remaining_context_tokens"] == 0


def test_unicode_bytes_output_reserve_and_physical_guard(tmp_path):
    from fabric_kg_builder.enrichment.approved_input_budget import account_request, budget_policy, enforce_request

    policy = budget_policy(10_000, 4096)
    ascii_request = {"messages": [{"content": "a"}], "max_completion_tokens": 4096}
    unicode_request = {"messages": [{"content": "中"}], "max_completion_tokens": 4096}
    emoji_request = {"messages": [{"content": "😀"}], "max_completion_tokens": 4096}
    ascii_total = account_request(ascii_request, policy)["estimated_total_tokens_upper_bound"]
    assert account_request(unicode_request, policy)["estimated_total_tokens_upper_bound"] == ascii_total + 2
    assert account_request(emoji_request, policy)["estimated_total_tokens_upper_bound"] == ascii_total + 3
    policy = budget_policy(ascii_total, 4096)
    enforce_request(ascii_request, policy)
    with pytest.raises(ValueError, match="INPUT_BUDGET_EXCEEDED"):
        enforce_request(unicode_request, policy)
    with pytest.raises(ValueError, match="INVALID_CONTEXT_BUDGET"):
        budget_policy(4096 + 1024, 4096)
    ledger = core._BudgetLedger(
        tmp_path / "unused", max_calls=1, max_physical_calls=1,
        max_output_tokens=4096, max_context_tokens=ascii_total,
    )
    with pytest.raises(ValueError, match="INPUT_BUDGET_EXCEEDED"):
        ledger.physical(lambda **_: pytest.fail("oversize SDK request"), **unicode_request)
    assert ledger.metrics()["logical_calls"] == ledger.metrics()["physical_calls"] == 0
    assert not ledger.path.exists()


def test_cli_context_cap_is_explicit_and_approved_only(integrated_case, monkeypatch):
    from fabric_kg_builder.config import loader

    options = _options(integrated_case)
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=_config()))
    args = [
        "enrich", "--input", str(options["source_path"]), "--domain-file", str(options["domain_path"]),
        "--l1-state", str(options["l1_state_root"]), "--l2-state", str(options["state_root"]),
        "--window-run", str(options["window_run_path"]), "--reextract-approved", "--max-calls", "1",
        "--max-context-tokens", "90000", "--dry-run",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.output)["input_budget"]["max_context_tokens"] == 90_000
    result = CliRunner().invoke(cli, ["enrich", "--input", str(options["source_path"]), "--max-context-tokens", "90000"])
    assert result.exit_code != 0 and "require --reextract-approved" in result.output
    result = CliRunner().invoke(cli, ["enrich", "--help"])
    assert "--max-context-tokens" in result.output and "UTF-8" in result.output


def test_recursive_request_preflight_rejects_new_heading_before_reservation(integrated_case, monkeypatch):
    from dataclasses import replace
    from fabric_kg_builder.enrichment import schema2_stage
    from fabric_kg_builder.enrichment.schema2_extraction import compile_closed_vocabulary, render_extraction_prompt

    options = _options(integrated_case)
    inputs, _, materialized, _ = core.prepare_approved_sources(
        **{key: options[key] for key in ("source_path", "l1_state_root", "domain_path", "window_run_path")},
    )
    root = core.plan_approved_work_units(
        materialized.source_units, contract=inputs.domain_contract,
        pass_name="schema-constrained-extraction", authority_fingerprint="0" * 64,
    )[0]
    root = replace(root, anchor_text="huge heading " * 10000)
    vocabulary = compile_closed_vocabulary(inputs.domain_contract)
    prompt = render_extraction_prompt(
        vocabulary, source_unit_id=root.source_unit_id, source_text_hash=root.source_text_hash,
        source_text=root.anchored_text, slice_start=root.slice_start, slice_end=root.slice_end,
    )
    monkeypatch.setattr(schema2_stage, "run_l2", lambda **kw: kw["service"].complete(prompt=prompt, work_unit=root))
    with pytest.raises(ValueError, match="INPUT_BUDGET_EXCEEDED"):
        core.run_approved_reextraction(**options, client_factory=lambda: pytest.fail("constructed SDK"))
    assert not (options["state_root"] / "reextraction-budget.json").exists()


def test_large_valid_examples_are_budgeted_before_client(integrated_case):
    from fabric_kg_builder.domain.service import load_domain_contract
    from fabric_kg_builder.enrichment.approved_few_shot import generate_examples, validate_examples

    options = _options(integrated_case)
    contract = load_domain_contract(options["domain_path"])
    examples = generate_examples(contract)
    examples[0]["source_text"] += "synthetic context " * 6000
    examples[0]["slice_end"] = examples[0]["slice_start"] + len(examples[0]["source_text"])
    validate_examples(contract, examples)
    with pytest.raises(ValueError, match="INPUT_BUDGET_EXCEEDED.*fixed_overhead_exceeds_cap"):
        core.run_approved_reextraction(
            **options, few_shot=examples, client_factory=lambda: pytest.fail("constructed SDK"),
        )
    assert not options["state_root"].exists()
