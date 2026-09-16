"""Explicit under-budget work fails closed; separately approved handoff is honest."""

import base64
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.enrichment import approved_donor_continuation as continuation
from fabric_kg_builder.enrichment import approved_partial_handoff as partial
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.schema2_validation_stage import load_l3_inputs
from tests.unit.test_approved_donor_continuation import (
    _child, _cli_args, _seal, _snapshot, partial_donor,  # noqa: F401
)
from tests.unit.test_approved_partial_handoff import _approve, _deny_network
from tests.unit.test_approved_reextraction import _sdk
from tests.unit.test_approved_source_span_continuation import (
    _response, _factory, _provider, v2_partial, v2_donor,  # noqa: F401
)
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


@pytest.fixture
def bounded_child(v2_partial):
    child = _child(v2_partial, max_calls=1, max_physical_calls=1, allow_budget_limited_partial=True)
    sdk, requests = _sdk(_response)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        continuation.run_approved_continuation(**child, client_factory=_factory(child, sdk))
    assert len(requests) == 1
    return child


def _handoff_options(child):
    return {
        key: child[key] for key in (
            "source_path", "l1_state_root", "domain_path", "foundry_config", "window_run_path",
        )
    } | {"reuse_approved_run": child["state_root"],
         "state_root": child["state_root"].with_name("explicit-partial-handoff")}


def _resign_authority(state, authority):
    authority["fingerprint"] = canonical_sha256({
        key: value for key, value in authority.items() if key != "fingerprint"
    })
    (state / continuation.AUTHORITY).write_text(json.dumps(authority))
    _seal(state)


def test_strict_minimum_remains_default_and_optin_dryrun_discloses_deficit(v2_partial):
    child = _child(v2_partial, max_calls=1, max_physical_calls=1)
    before = _snapshot(v2_partial["state_root"])
    with pytest.raises(ValueError, match="BUDGET_TOO_SMALL.*2"):
        continuation.run_approved_continuation(**child, dry_run=True)
    assert not child["state_root"].exists()
    plan = continuation.run_approved_continuation(**child, allow_budget_limited_partial=True, dry_run=True)
    assert plan["status"] == "planned"
    assert plan["completion_expectation"] == "budget_limited_partial"
    assert plan["minimum_full_completion_calls"] == {"logical_calls": 2, "physical_calls": 2}
    assert plan["available_child_calls"] == {"logical_calls": 1, "physical_calls": 1}
    assert plan["full_completion_budget_sufficient"] is False
    assert plan["budget_limited_partial"] == continuation._partial_profile()
    assert plan["remote_calls"] == plan["writes"] == 0
    assert plan["lineage_budget_ceiling"]["physical_calls"] == 4
    assert not child["state_root"].exists()
    assert _snapshot(v2_partial["state_root"]) == before


def test_exhaustion_never_creates_successful_partial_receipt_or_automatic_handoff(bounded_child):
    state = bounded_child["state_root"]
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    assert authority[continuation.PARTIAL_PROFILE_KEY] == continuation._partial_profile()
    ledger = json.loads((state / continuation.BUDGET).read_text())
    assert len(ledger["logical_requests"]) == len(ledger["physical_attempts"]) == 1
    assert all(row["status"] == "succeeded" for rows in ledger.values() for row in rows)
    assert json.loads((state / continuation.INTEGRITY).read_text()) == core._inventory(state)
    assert not (state / "approved-reextraction-result.json").exists()
    receipt = state / "stage-receipt.json"
    assert not receipt.exists() or json.loads(receipt.read_text())["status"] != "succeeded"
    assert not (state / partial.SCOPE_FILE).exists()
    assert not state.with_name("explicit-partial-handoff").exists()
    own = [
        json.loads(path.read_text()) for path in (state / "reextraction-responses").glob("*.json")
        if "imported_from" not in json.loads(path.read_text())
    ]
    assert len(own) == 1
    _, provider = _provider(state, own[0]["request_hash"])
    assert json.loads(base64.b64decode(provider["raw_output_utf8_base64"])) == own[0]["response"]
    assert own[0]["response_hash"] == core.response_hash(own[0]["response"], core.SOURCE_SPANS_V2_MODE)
    assert list((state / "reextraction-resolved-responses").glob("*.json"))


@pytest.mark.parametrize(("logical", "physical", "charged_logical"), [(1, 2, 1), (2, 1, 2)])
def test_logical_and_physical_limits_remain_independent_and_failed_attempts_stay_charged(
    v2_partial, logical, physical, charged_logical,
):
    child = _child(
        v2_partial, max_calls=logical, max_physical_calls=physical,
        allow_budget_limited_partial=True,
    )
    sdk, requests = _sdk(_response)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        continuation.run_approved_continuation(**child, client_factory=_factory(child, sdk))
    ledger = json.loads((child["state_root"] / continuation.BUDGET).read_text())
    assert len(requests) == len(ledger["physical_attempts"]) == 1
    assert len(ledger["logical_requests"]) == charged_logical
    if physical == 1:
        assert ledger["logical_requests"][-1]["status"] == "failed"
    plan = continuation.run_approved_continuation(**child, resume=True, dry_run=True)
    assert plan["current_child_spent"]["logical_calls"] == charged_logical
    assert plan["current_child_spent"]["physical_calls"] == 1
    assert plan["lineage_spent"]["physical_calls"] == 4


def test_exact_resume_requires_same_profile_and_never_resets_spending(bounded_child):
    state = bounded_child["state_root"]
    before = _snapshot(state)
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        continuation.run_approved_continuation(
            **{**bounded_child, "allow_budget_limited_partial": False}, resume=True, dry_run=True,
        )
    assert _snapshot(state) == before
    plan = continuation.run_approved_continuation(**bounded_child, resume=True, dry_run=True)
    assert plan["current_child_spent"]["physical_calls"] == 1
    assert plan["remaining_child_budget"]["physical_calls"] == 0
    assert plan["available_child_calls"] == {"logical_calls": 0, "physical_calls": 0}
    assert plan["minimum_full_completion_calls"] == {"logical_calls": 1, "physical_calls": 1}
    assert plan["lineage_spent"]["physical_calls"] == 4
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        continuation.run_approved_continuation(
            **bounded_child, resume=True, client_factory=lambda: pytest.fail("exhausted resume created transport"),
        )
    assert _snapshot(state) == before


def test_separate_zero_call_handoff_seals_only_proven_complete_roots(bounded_child, monkeypatch):
    _deny_network(monkeypatch)
    options = _handoff_options(bounded_child)
    before = _snapshot(bounded_child["state_root"])
    ancestor_before = _snapshot(bounded_child["reuse_approved_run"])
    plan = partial.run_partial_handoff(**options)
    assert plan["completed_root_count"] == plan["selected_root_count"] == 2
    assert plan["approved_contract_root_count"] == 3
    assert plan["missing_root_count"] == plan["excluded_root_count"] == 1
    assert plan["operator_excluded_completed_root_count"] == 0
    assert plan["source_execution_profile"] == continuation._partial_profile()
    assert plan["prior_spent"]["physical_calls"] == 4
    assert plan["new_physical_calls"] == plan["remote_calls"] == plan["writes"] == 0
    assert "UNKNOWN, not observed-empty" in plan["scope_notice"]
    assert not options["state_root"].exists()
    with pytest.raises(ValueError, match="EXACT_PLAN_APPROVAL_REQUIRED"):
        _approve(options, {**plan, "plan_hash": "0" * 64})
    result = _approve(options, plan)
    assert result["remote_calls"] == 0 and result["completed_root_count"] == 2
    retained = [json.loads(path.read_text()) for path in (options["state_root"] / "retained-responses").glob("*.json")]
    assert len(retained) == 2
    for entry in retained:
        assert json.loads(base64.b64decode(entry["provider_output"]["raw_output_utf8_base64"])) == entry["response"]
        assert entry["resolved_response_hash"] == core.response_hash(entry["resolved_response"], core.SOURCE_SPANS_V2_MODE)
    inputs = load_l3_inputs(
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    assert inputs.partial_extraction_scope["plan"]["missing_root_count"] == 1
    assert inputs.l2_metrics.foundry_calls == 0
    assert _snapshot(bounded_child["state_root"]) == before
    assert _snapshot(bounded_child["reuse_approved_run"]) == ancestor_before


def test_future_strict_child_proves_profile_ancestry_without_inheriting_optin(bounded_child):
    next_child = _child(
        bounded_child, state_root=bounded_child["state_root"].with_name("strict-next"),
        max_calls=1, max_physical_calls=1, allow_budget_limited_partial=False,
    )
    plan = continuation.run_approved_continuation(**next_child, dry_run=True)
    assert plan["remaining_work_units"] == 1
    assert plan["prior_spent"]["physical_calls"] == 4
    assert "completion_expectation" not in plan
    sdk, requests = _sdk(_response)
    result = continuation.run_approved_continuation(**next_child, client_factory=_factory(next_child, sdk))
    assert result["status"] == "succeeded" and result["planned_chunks"] == 3
    assert result["physical_calls"] == len(requests) == 1
    assert result["lineage_spent"]["physical_calls"] == 5
    authority = json.loads((next_child["state_root"] / continuation.AUTHORITY).read_text())
    assert continuation.PARTIAL_PROFILE_KEY not in authority


@pytest.mark.parametrize("change", ["false", "integer", "version", "extra", "policy", "remove"])
def test_tampered_profile_never_authorizes_reuse_even_with_resealed_inventory(bounded_child, change):
    state = bounded_child["state_root"]
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    if change == "remove":
        del authority[continuation.PARTIAL_PROFILE_KEY]
    else:
        profile = authority[continuation.PARTIAL_PROFILE_KEY]
        if change in ("false", "integer"):
            profile["allow_budget_limited_partial"] = False if change == "false" else 1
        elif change == "version":
            profile["version"] = "unknown/99"
        elif change == "extra":
            profile["ignore_quality"] = True
        else:
            profile["exhaustion_policy"] = "succeed"
    _resign_authority(state, authority)
    before = _snapshot(state)
    with pytest.raises(ValueError, match="(PROFILE_INVALID|REQUEST_EQUIVALENCE_UNPROVEN)"):
        partial.run_partial_handoff(**_handoff_options(bounded_child))
    assert _snapshot(state) == before


@pytest.mark.parametrize("field", ["max_calls", "max_physical_calls"])
def test_forged_budget_cannot_expand_a_sealed_partial_child(bounded_child, field):
    state = bounded_child["state_root"]
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    authority[field] = 20
    _resign_authority(state, authority)
    before = _snapshot(state)
    with pytest.raises(ValueError, match="REQUEST_EQUIVALENCE_UNPROVEN"):
        continuation.run_approved_continuation(**bounded_child, resume=True, dry_run=True)
    assert _snapshot(state) == before


def test_partial_handoff_still_requires_ancestor_provider_bytes(bounded_child):
    ancestor = bounded_child["reuse_approved_run"]
    path, _ = _provider(ancestor)
    path.unlink()
    _seal(ancestor)
    with pytest.raises(ValueError, match="PROVIDER_RESPONSE"):
        partial.run_partial_handoff(**_handoff_options(bounded_child))


def test_profile_cannot_be_attached_to_a_fresh_producer(v2_donor):
    state = v2_donor["state_root"]
    authority = json.loads((state / continuation.AUTHORITY).read_text())
    authority[continuation.PARTIAL_PROFILE_KEY] = continuation._partial_profile()
    _resign_authority(state, authority)
    with pytest.raises(ValueError, match="PROFILE_INVALID"):
        continuation.run_approved_continuation(**_child(v2_donor), dry_run=True)


def test_cli_flag_is_continuation_only_and_default_stays_strict(v2_partial, monkeypatch):
    from fabric_kg_builder.config import loader

    child = _child(v2_partial, max_calls=1, max_physical_calls=1)
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=child["foundry_config"]))
    args = _cli_args(child)
    strict = CliRunner().invoke(cli, [*args, "--dry-run"])
    assert strict.exit_code != 0 and "BUDGET_TOO_SMALL" in strict.output
    opted = CliRunner().invoke(cli, [*args, "--allow-budget-limited-partial", "--dry-run"])
    assert opted.exit_code == 0, opted.output
    assert json.loads(opted.output)["completion_expectation"] == "budget_limited_partial"
    for prefix in ([], ["--reextract-approved"]):
        invalid = CliRunner().invoke(cli, [
            "enrich", "--input", str(child["source_path"]), *prefix, "--allow-budget-limited-partial",
        ])
        assert invalid.exit_code != 0
        assert "requires --reextract-approved and --reuse-approved-run" in invalid.output
    sdk, requests = _sdk(_response)
    bounded = core._bounded_client
    monkeypatch.setattr(core, "_bounded_client", lambda config, ledger, _unused: bounded(
        config, ledger, _factory(child, sdk),
    ))
    stopped = CliRunner().invoke(cli, [*args, "--allow-budget-limited-partial"])
    assert stopped.exit_code != 0 and "BUDGET_EXHAUSTED" in stopped.output
    assert len(requests) == 1
    resumed = CliRunner().invoke(cli, [*args, "--allow-budget-limited-partial", "--resume"])
    assert resumed.exit_code != 0 and "BUDGET_EXHAUSTED" in resumed.output
    assert len(requests) == 1
    wrong_resume = CliRunner().invoke(cli, [*args, "--resume", "--dry-run"])
    assert wrong_resume.exit_code != 0 and "FINGERPRINT_DRIFT" in wrong_resume.output


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_api_requires_explicit_boolean_flag(v2_partial, value):
    with pytest.raises(ValueError, match="PARTIAL_FLAG_INVALID"):
        continuation.run_approved_continuation(
            **_child(v2_partial), allow_budget_limited_partial=value, dry_run=True,
        )


def test_non_v2_mode_does_not_gain_partial_execution_profile(partial_donor):
    with pytest.raises(ValueError, match="PARTIAL_REQUIRES_SOURCE_SPANS_V2"):
        continuation.run_approved_continuation(
            **_child(partial_donor), allow_budget_limited_partial=True, dry_run=True,
        )
