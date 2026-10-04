"""New GPT-5.4 allowances never change sealed extraction/replay budgets."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config import loader
from fabric_kg_builder.enrichment import approved_donor_continuation as continuation
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient, FoundryJSONResponseError
from tests.unit.test_approved_donor_continuation import _child, _snapshot
from tests.unit.test_approved_reextraction import _options, _sdk
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401


def _args(options):
    return [
        "enrich", "--input", str(options["source_path"]),
        "--domain-file", str(options["domain_path"]),
        "--l1-state", str(options["l1_state_root"]),
        "--l2-state", str(options["state_root"]),
        "--window-run", str(options["window_run_path"]),
        "--reextract-approved", "--max-calls", str(options["max_calls"]),
    ]


def _configure(monkeypatch, options, *, deployment="custom", model="gpt-5.4"):
    config = options["foundry_config"].model_copy(update={
        "chat_deployment": deployment, "chat_model": model,
    })
    options["foundry_config"] = config
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=config))
    return config


@pytest.mark.parametrize(("deployment", "model", "expected"), [
    ("gpt-5.4", None, 32768),
    ("gpt-5.4-2026-03-05", None, 32768),
    ("custom", "gpt-5.4", 32768),
    ("custom", "gpt-5.4-2026-03-05", 32768),
    ("gpt-5.4", "gpt-4.1", 8000),
    ("gpt-4.1", None, 8000),
    ("gpt-5", None, 8000),
    ("custom", None, 8000),
])
def test_cli_fresh_budget_uses_actual_configured_model(
    integrated_case, monkeypatch, deployment, model, expected,
):
    options = _options(integrated_case)
    _configure(monkeypatch, options, deployment=deployment, model=model)
    result = CliRunner().invoke(cli, [*_args(options), "--dry-run"])
    assert result.exit_code == 0, (result.output, result.exception)
    plan = json.loads(result.output)
    assert plan["max_output_tokens"] == expected
    assert plan["input_budget"]["output_reserve_tokens"] == expected
    assert plan["input_budget"]["max_context_tokens"] == 96000
    assert plan["input_budget"]["framing_reserve_tokens"] == 1024
    assert plan["remote_calls"] == plan["writes"] == 0
    assert not options["state_root"].exists()


def test_cli_preserves_explicit_maximum_and_fails_instead_of_shrinking(integrated_case, monkeypatch):
    options = _options(integrated_case)
    _configure(monkeypatch, options)
    args = [*_args(options), "--max-output-tokens", "128000", "--dry-run"]
    rejected = CliRunner().invoke(cli, args)
    assert rejected.exit_code != 0
    assert "INVALID_CONTEXT_BUDGET" in rejected.output
    assert not options["state_root"].exists()
    accepted = CliRunner().invoke(cli, [*args, "--max-context-tokens", "200000"])
    assert accepted.exit_code == 0, (accepted.output, accepted.exception)
    assert json.loads(accepted.output)["max_output_tokens"] == 128000


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
def test_cli_new_default_reaches_physical_request(integrated_case, monkeypatch, route):
    options = _options(integrated_case)
    options["foundry_config"] = options["foundry_config"].model_copy(update={"inference_api": route})
    config = _configure(monkeypatch, options)
    sdk, requests = _sdk()

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(output_text='{"candidates": []}', status="completed")

    sdk.responses = SimpleNamespace(create=create)
    bounded = core._bounded_client
    monkeypatch.setattr(core, "_bounded_client", lambda config, ledger, _factory: bounded(
        config, ledger, lambda: FoundryClient(config, _sdk_client=sdk),
    ))
    result = CliRunner().invoke(cli, _args(options))
    assert result.exit_code == 0, (result.output, result.exception)
    token_field = "max_output_tokens" if route == "project_responses" else "max_completion_tokens"
    assert len(requests) == 1 and requests[0][token_field] == 32768
    assert json.loads(result.output)["reserved_output_tokens"] == 32768
    authority = json.loads(
        (options["state_root"] / "approved-reextraction-authority.json").read_text()
    )
    assert authority["max_output_tokens"] == authority["input_budget"]["output_reserve_tokens"] == 32768


@pytest.mark.parametrize("explicit", [
    [], ["--max-output-tokens", "8192"], ["--max-context-tokens", "88000"],
])
def test_cli_resume_inherits_each_omitted_sealed_limit(integrated_case, monkeypatch, explicit):
    options = _options(integrated_case)
    config = _configure(monkeypatch, options)
    options.update(max_output_tokens=8192, max_context_tokens=88000)
    sdk, requests = _sdk()
    original = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    before = _snapshot(options["state_root"])
    result = CliRunner().invoke(cli, [*_args(options), "--resume", *explicit])
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.output) == original
    assert len(requests) == 1
    assert _snapshot(options["state_root"]) == before
    rejected = CliRunner().invoke(cli, [
        *_args(options), "--resume", "--max-output-tokens", "32768",
    ])
    assert rejected.exit_code != 0 and "FINGERPRINT_DRIFT" in rejected.output
    assert _snapshot(options["state_root"]) == before


def test_core_fresh_default_stays_legacy_and_resume_still_checks_code(integrated_case, monkeypatch):
    options = _options(integrated_case)
    config = _configure(monkeypatch, options)
    options.pop("max_output_tokens")
    sdk, _ = _sdk()
    result = core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    assert result["max_output_tokens"] == 8000
    before = _snapshot(options["state_root"])
    identity = core._code_identity()
    monkeypatch.setattr(core, "_code_identity", lambda: {**identity, "changed-code": "different"})
    rejected = CliRunner().invoke(cli, [*_args(options), "--resume"])
    assert rejected.exit_code != 0 and "FINGERPRINT_DRIFT" in rejected.output
    assert _snapshot(options["state_root"]) == before


def test_cli_donor_child_and_resume_inherit_budgets_without_mutation(integrated_case, monkeypatch):
    options = _options(integrated_case)
    config = _configure(monkeypatch, options)
    options.update(max_output_tokens=8192, max_context_tokens=88000)
    sdk, requests = _sdk()
    core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(config, _sdk_client=sdk),
    )
    donor_before = _snapshot(options["state_root"])
    child = _child(options)
    args = [
        *_args(child), "--reuse-approved-run", str(options["state_root"]),
    ]
    for suffix in (["--dry-run"], [], ["--resume"]):
        result = CliRunner().invoke(cli, [*args, *suffix])
        assert result.exit_code == 0, (result.output, result.exception)
        summary = json.loads(result.output)
        assert summary["max_output_tokens"] == 8192
        assert summary["input_budget"]["max_context_tokens"] == 88000
    assert len(requests) == 1
    assert _snapshot(options["state_root"]) == donor_before
    rejected = CliRunner().invoke(cli, [
        *args, "--dry-run", "--max-output-tokens", "32768",
    ])
    assert rejected.exit_code != 0 and "DONOR_SEMANTICS_DRIFT" in rejected.output
    assert _snapshot(options["state_root"]) == donor_before


@pytest.mark.parametrize("route", ["chat_completions", "project_responses"])
@pytest.mark.parametrize("use_donor", [False, True])
@pytest.mark.parametrize("raw", [
    '{"candidates": [{"private_source": "unfinished',
    '{"candidates": []}',
])
def test_truncated_output_is_retained_privately_never_accepted(
    integrated_case, monkeypatch, route, use_donor, raw,
):
    options = _options(integrated_case)
    config = _configure(monkeypatch, options).model_copy(update={"inference_api": route})
    options.update(foundry_config=config, max_calls=2, max_output_tokens=32768)
    sdk, requests = _sdk()

    def create(**kwargs):
        requests.append(kwargs)
        if route == "project_responses":
            return SimpleNamespace(
                output_text=raw, status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            )
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=raw), finish_reason="length",
        )])

    sdk.chat.completions.create = create
    sdk.responses = SimpleNamespace(create=create)
    factory = lambda: FoundryClient(config, _sdk_client=sdk)
    with pytest.raises(FoundryJSONResponseError):
        core.run_approved_reextraction(**options, client_factory=factory)
    if use_donor:
        donor_before = _snapshot(options["state_root"])
        options = _child(options, max_calls=1, max_physical_calls=1)
        runner = continuation.run_approved_continuation
    else:
        donor_before = None
        options["resume"] = True
        runner = core.run_approved_reextraction
    with pytest.raises(FoundryJSONResponseError) as error:
        runner(**options, client_factory=factory)
    assert raw not in str(error.value)
    failures = list((options["state_root"] / "reextraction-failures").glob("*.json"))
    assert len(failures) == (1 if use_donor else 2)
    for failure in failures:
        diagnostics = json.loads(failure.read_text())["diagnostics"]
        assert diagnostics["raw_output"] == raw
        assert diagnostics["max_completion_tokens"] == 32768
        assert diagnostics["parse_error"] == "provider_incomplete"
    assert not list((options["state_root"] / "reextraction-responses").glob("*.json"))
    assert not (options["state_root"] / "approved-reextraction-result.json").exists()
    assert json.loads((options["state_root"] / "reextraction-integrity.json").read_text()) == (
        core._inventory(options["state_root"])
    )
    if use_donor:
        assert _snapshot(options["reuse_approved_run"]) == donor_before


@pytest.mark.parametrize("payload", [
    {},
    {"max_output_tokens": 4096},
    {"max_output_tokens": 4096, "input_budget": {"max_context_tokens": 96000}},
    {"max_output_tokens": 4096, "input_budget": core.budget_policy(96000, 8000)},
])
def test_incomplete_sealed_budgets_never_fall_back_to_new_defaults(tmp_path, payload):
    state = tmp_path / "sealed"
    state.mkdir()
    authority = state / "approved-reextraction-authority.json"
    authority.write_text(json.dumps(payload))
    before = _snapshot(state)
    with pytest.raises(ValueError, match="CHECKPOINT_INCOMPLETE"):
        core.resolve_token_budgets(sealed_state=state)
    assert _snapshot(state) == before
