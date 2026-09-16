"""Offline continuation proves original requests, rather than trusting renamed response files."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_donor_continuation as continuation
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from tests.unit.test_approved_reextraction import _config, _no_source_reads, _options, _sdk
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


def _snapshot(state):
    return {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}


def _seal(state):
    core._write_state(state / continuation.INTEGRITY, core._inventory(state))


def _child(options, **updates):
    return {
        **options, "reuse_approved_run": options["state_root"],
        "state_root": options["state_root"].parent / "child-l2",
        "max_calls": 0, "max_physical_calls": 0, **updates,
    }


def _cli_args(options):
    return [
        "enrich", "--input", str(options["source_path"]),
        "--domain-file", str(options["domain_path"]),
        "--l1-state", str(options["l1_state_root"]),
        "--l2-state", str(options["state_root"]),
        "--window-run", str(options["window_run_path"]),
        "--reextract-approved", "--reuse-approved-run", str(options["reuse_approved_run"]),
        "--max-calls", str(options["max_calls"]),
        "--max-physical-calls", str(options["max_physical_calls"]),
        "--max-output-tokens", str(options["max_output_tokens"]),
    ]


@pytest.fixture
def donor(integrated_case):
    options = _options(integrated_case)
    sdk, _ = _sdk()
    core.run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    return options


@pytest.fixture
def partial_donor(prefix_case, tmp_path):
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
        max_calls=3, max_physical_calls=3, max_output_tokens=4096,
    )
    sdk, requests = _fallback_sdk()
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        core.run_approved_reextraction(
            **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    assert len(requests) == 3
    assert len(list((options["state_root"] / "reextraction-responses").glob("*.json"))) == 1
    return options


def _fallback_sdk():
    class UnsupportedSchema(Exception):
        status_code = 400

    def response(request):
        if request["response_format"]["type"] == "json_schema":
            raise UnsupportedSchema("offline strict schema rejection")
        return {"candidates": []}

    return _sdk(response)


def test_zero_call_dry_run_and_complete_reuse_are_immutable(donor, monkeypatch):
    _no_source_reads(monkeypatch)
    before = _snapshot(donor["state_root"])
    options = _child(donor)
    plan = continuation.run_approved_continuation(
        **options, dry_run=True, client_factory=lambda: pytest.fail("dry-run client"),
    )
    assert plan["remaining_work_units"] == 0
    assert plan["reused_responses"] == 1
    assert plan["data_lineage"] == {
        "enabled_after": "L1_approval",
        "domain_contract_hash": plan["domain_contract_hash"],
        "storage": "L2_candidate_lifecycle_and_source_manifests",
    }
    assert plan["remote_calls"] == plan["writes"] == 0
    assert not options["state_root"].exists()
    assert not (options["state_root"].parent / ".child-l2-enrichment.lock").exists()
    result = continuation.run_approved_continuation(
        **options, client_factory=lambda: pytest.fail("called despite paid response"),
    )
    assert result["logical_calls"] == result["physical_calls"] == 0
    assert result["data_lineage"] == plan["data_lineage"]
    donor_authority = json.loads(
        (donor["state_root"] / "approved-reextraction-authority.json").read_text()
    )
    assert result["data_lineage"]["domain_contract_hash"] == (
        donor_authority["domain_authorities"]["domain_contract_hash"]
    )
    assert result["lineage_spent"] == plan["prior_spent"] == {
        "logical_calls": 1, "physical_calls": 1, "reserved_output_tokens": 4096,
    }
    assert result["fingerprint"] != result["donor"]["fingerprint"]
    copied = next((options["state_root"] / "reextraction-responses").glob("*.json"))
    cached = json.loads(copied.read_text())
    assert cached["imported_from"]["donor_fingerprint"] == result["donor"]["fingerprint"]
    assert cached["request_hash"] != cached["imported_from"]["request_hash"]
    resumed = continuation.run_approved_continuation(
        **options, resume=True, client_factory=lambda: pytest.fail("resume called model"),
    )
    assert resumed == result
    assert _snapshot(donor["state_root"]) == before
    with pytest.raises(ValueError, match="FRESH_STATE_REQUIRED"):
        continuation.run_approved_continuation(**options, dry_run=True)


def test_partial_donor_only_extracts_remaining_units_and_retains_failed_spend(partial_donor, monkeypatch):
    _no_source_reads(monkeypatch)
    options = _child(partial_donor, max_calls=2, max_physical_calls=4)
    before = _snapshot(partial_donor["state_root"])
    plan = continuation.run_approved_continuation(**options, dry_run=True)
    assert plan["planned_chunks"] == 3
    assert plan["remaining_work_units"] == 2
    assert plan["prior_spent"] == {
        "logical_calls": 2, "physical_calls": 3, "reserved_output_tokens": 3 * 4096,
    }
    assert plan["lineage_budget_ceiling"] == {
        "logical_calls": 4, "physical_calls": 7, "reserved_output_tokens": 7 * 4096,
    }
    sdk, requests = _fallback_sdk()
    result = continuation.run_approved_continuation(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert result["logical_calls"] == 2
    assert result["physical_calls"] == len(requests) == 4
    assert result["lineage_spent"] == plan["lineage_budget_ceiling"]
    assert all("UNPROCESSED_PREFIX_SENTINEL" not in r["messages"][1]["content"] for r in requests)
    authority = json.loads((options["state_root"] / continuation.AUTHORITY).read_text())
    assert all(r["messages"][0]["content"].startswith(authority["system_prompt"]) for r in requests)
    assert all("{{a few shot}}" not in r["messages"][0]["content"] for r in requests)
    child_responses = [
        json.loads(p.read_text()) for p in (options["state_root"] / "reextraction-responses").glob("*.json")
    ]
    assert len(child_responses) == 3
    assert sum("imported_from" in response for response in child_responses) == 1
    assert continuation.run_approved_continuation(
        **options, resume=True, client_factory=lambda: pytest.fail("resumed model"),
    ) == result
    assert _snapshot(partial_donor["state_root"]) == before
    # A third generation cannot reset either donor's failed attempts or the child's paid calls.
    grandchild = {**options, "reuse_approved_run": options["state_root"],
                  "state_root": options["state_root"].parent / "grandchild-l2",
                  "max_calls": 0, "max_physical_calls": 0}
    final = continuation.run_approved_continuation(
        **grandchild, client_factory=lambda: pytest.fail("grandchild called model"),
    )
    assert final["lineage_spent"] == result["lineage_spent"]
    assert final["reused_responses"] == 3


def test_insufficient_additional_budget_is_no_call_no_state(partial_donor):
    options = _child(partial_donor, max_calls=1, max_physical_calls=1)
    with pytest.raises(ValueError, match="BUDGET_TOO_SMALL.*2"):
        continuation.run_approved_continuation(**options, dry_run=True)
    assert not options["state_root"].exists()


def test_replacement_examples_reconstruct_donor_and_reach_child_sdk(partial_donor):
    from fabric_kg_builder.domain.service import load_domain_contract
    from fabric_kg_builder.enrichment.approved_few_shot import render_system_prompt
    from tests.unit.test_approved_few_shot import _replacement

    contract = load_domain_contract(partial_donor["domain_path"])
    replacement = _replacement(contract)
    donor_options = {
        **partial_donor, "state_root": partial_donor["state_root"].parent / "replacement-donor",
        "few_shot": replacement,
    }
    sdk, _ = _fallback_sdk()
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        core.run_approved_reextraction(
            **donor_options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    options = _child(donor_options, max_calls=2, max_physical_calls=2)
    sdk, requests = _sdk()
    result = continuation.run_approved_continuation(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    rendered, _ = render_system_prompt(core.SYSTEM_PROMPT, contract, few_shot=replacement)
    assert len(requests) == 2
    assert all(request["messages"][0]["content"].startswith(rendered) for request in requests)
    assert continuation.run_approved_continuation(
        **options, resume=True, client_factory=lambda: pytest.fail("exact resume called SDK"),
    ) == result
    with pytest.raises(ValueError, match="DONOR_SEMANTICS_DRIFT"):
        continuation.run_approved_continuation(**{**options, "few_shot": None}, resume=True, dry_run=True)


def test_active_donor_is_rejected_even_with_a_preexisting_integrity_seal(donor):
    import fcntl

    state = donor["state_root"]
    with (state.parent / f".{state.name}-enrichment.lock").open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="DONOR_ACTIVE"):
            continuation.run_approved_continuation(**_child(donor), dry_run=True)
    assert not _child(donor)["state_root"].exists()


@pytest.mark.parametrize("change", ["missing-integrity", "logical-started", "physical-started", "tampered"])
def test_incomplete_uncertain_or_tampered_donor_fails_closed(donor, change):
    state = donor["state_root"]
    if change == "missing-integrity":
        (state / continuation.INTEGRITY).unlink()
    elif change == "tampered":
        next((state / "reextraction-responses").glob("*.json")).write_text("{}")
    else:
        ledger = json.loads((state / continuation.BUDGET).read_text())
        key = "logical_requests" if change == "logical-started" else "physical_attempts"
        ledger[key][0]["status"] = "started"
        core._write_state(state / continuation.BUDGET, ledger)
        _seal(state)
    with pytest.raises(ValueError, match="DONOR_(CHECKPOINT_INCOMPLETE|UNCERTAIN_CALL|CHECKPOINT_DRIFT)"):
        continuation.run_approved_continuation(**_child(donor), dry_run=True)


@pytest.mark.parametrize("change", ["model", "prompt", "examples", "schema", "code", "output", "context", "scope", "contract"])
def test_semantic_drift_cannot_be_excused_as_budget_change(donor, monkeypatch, change):
    options = _child(donor)
    if change == "model":
        options["foundry_config"] = _config().model_copy(update={"chat_deployment": "other"})
    elif change == "prompt":
        monkeypatch.setattr(core, "SYSTEM_PROMPT", core.SYSTEM_PROMPT + " changed")
    elif change == "examples":
        from fabric_kg_builder.domain.service import load_domain_contract
        from tests.unit.test_approved_few_shot import _replacement

        options["few_shot"] = _replacement(load_domain_contract(options["domain_path"]))
    elif change == "schema":
        original = core.raw_candidate_response_schema
        monkeypatch.setattr(core, "raw_candidate_response_schema", lambda: {**original(), "description": "changed"})
    elif change == "code":
        original = core._code_identity
        monkeypatch.setattr(core, "_code_identity", lambda: {**original(), "enrichment/schema2_stage.py": "changed"})
    elif change == "output":
        options["max_output_tokens"] = 8192
    elif change == "context":
        options["max_context_tokens"] = 95_000
    else:
        original = continuation._base_authority

        def changed(*args):
            result = original(*args)
            if change == "scope":
                result["scope"][0]["source_text_hash"] = "changed"
            else:
                result["domain_authorities"] = {**result["domain_authorities"], "domain_contract_hash": "changed"}
            return result

        monkeypatch.setattr(continuation, "_base_authority", changed)
    with pytest.raises(ValueError, match="DONOR_SEMANTICS_DRIFT"):
        continuation.run_approved_continuation(**options, dry_run=True)
    assert not options["state_root"].exists()


def test_continuation_uses_same_full_request_accounting_without_donor_mutation(donor):
    options = _child(donor)
    before = _snapshot(donor["state_root"])
    plan = continuation.run_approved_continuation(**options, dry_run=True)
    prior = json.loads((donor["state_root"] / "approved-reextraction-result.json").read_text())
    assert plan["input_budget"] == prior["input_budget"]
    assert plan["input_budget"]["maxima"]["appended_schema_utf8_bytes"] > 0
    assert plan["input_budget"]["output_reserve_tokens"] == options["max_output_tokens"]
    assert _snapshot(donor["state_root"]) == before


def test_continuation_rechecks_every_request_before_any_reuse_or_call(donor, monkeypatch):
    from fabric_kg_builder.contracts.base import canonical_json

    options = _child(donor)
    before = _snapshot(donor["state_root"])
    original = continuation._prompt
    # Simulate an unforeseen child rendering increase after donor proof; neither
    # importing a paid response nor allowing zero additional calls bypasses the cap.
    verify = continuation._verify_donor

    def verified(*args, **kwargs):
        result = verify(*args, **kwargs)
        def oversized(*args):
            payload = json.loads(original(*args))
            payload["interpretation_context"]["inherited_heading"] = "大" * 100000
            return canonical_json(payload)
        monkeypatch.setattr(continuation, "_prompt", oversized)
        return result

    monkeypatch.setattr(continuation, "_verify_donor", verified)
    with pytest.raises(ValueError, match="INPUT_BUDGET_EXCEEDED"):
        continuation.run_approved_continuation(
            **options, client_factory=lambda: pytest.fail("constructed SDK"),
        )
    assert not options["state_root"].exists()
    assert _snapshot(donor["state_root"]) == before


@pytest.mark.parametrize("change", ["bad-response-hash", "renamed-request", "no-success", "unpaid"])
def test_resealed_bad_response_or_spending_still_fails_proof(donor, change):
    state = donor["state_root"]
    path = next((state / "reextraction-responses").glob("*.json"))
    cached = json.loads(path.read_text())
    ledger = json.loads((state / continuation.BUDGET).read_text())
    if change == "bad-response-hash":
        cached["response_hash"] = "wrong"
    elif change == "renamed-request":
        cached["request_hash"] = "a" * 64
        path.unlink()
        path = path.with_name(cached["request_hash"] + ".json")
        for rows in ledger.values():
            rows[0]["request_hash"] = cached["request_hash"]
    elif change == "no-success":
        ledger["logical_requests"][0]["status"] = "failed"
    else:
        ledger["physical_attempts"][0]["status"] = "failed"
    core._write_state(path, cached)
    core._write_state(state / continuation.BUDGET, ledger)
    _seal(state)
    with pytest.raises(ValueError, match="DONOR_(RESPONSE_DRIFT|REQUEST_EQUIVALENCE_UNPROVEN|SUCCESS_PROOF_MISSING)"):
        continuation.run_approved_continuation(**_child(donor), dry_run=True)


def test_raw_responses_reenter_candidate_validation_not_proposed_partition_replay(integrated_case):
    options = _options(integrated_case)
    sdk, calls = _sdk({"candidates": [{"candidate_kind": "entity", "label": "not a full candidate"}]})
    with pytest.raises(ValueError, match="L2_CANDIDATE_SCHEMA_INVALID"):
        core.run_approved_reextraction(
            **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    assert len(calls) == 1
    before = _snapshot(options["state_root"])
    with pytest.raises(ValueError, match="L2_CANDIDATE_SCHEMA_INVALID"):
        continuation.run_approved_continuation(
            **_child(options), client_factory=lambda: pytest.fail("recalled invalid paid response"),
        )
    assert _snapshot(options["state_root"]) == before
    child = _child(options)["state_root"]
    assert json.loads((child / continuation.BUDGET).read_text()) == {
        "logical_requests": [], "physical_attempts": [],
    }


def test_child_resume_rejects_budget_donor_and_response_drift(donor):
    options = _child(donor)
    continuation.run_approved_continuation(**options)
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        continuation.run_approved_continuation(**{**options, "max_calls": 1}, resume=True, dry_run=True)
    path = next((options["state_root"] / "reextraction-responses").glob("*.json"))
    path.write_text("{}")
    with pytest.raises(ValueError, match="CHECKPOINT_DRIFT"):
        continuation.run_approved_continuation(**options, resume=True, dry_run=True)


@pytest.mark.parametrize("dry_run", [False, True])
def test_child_cannot_overwrite_donor_or_its_ancestors(donor, dry_run):
    middle = _child(donor)
    continuation.run_approved_continuation(**middle)
    for producer in (donor, middle):
        for protected in (donor["state_root"], producer["state_root"]):
            for state in (protected, protected / "nested", protected / "deep" / "nested", protected.parent):
                parent = donor["state_root"].parent
                before = _snapshot(parent)
                paths_before = set(parent.rglob("*"))
                with pytest.raises(ValueError, match="FRESH_STATE_REQUIRED"):
                    continuation.run_approved_continuation(
                        **_child(producer, state_root=state), dry_run=dry_run,
                        client_factory=lambda: pytest.fail("invalid target constructed client"),
                    )
                assert _snapshot(parent) == before
                assert set(parent.rglob("*")) == paths_before
                assert json.loads((protected / continuation.INTEGRITY).read_text()) == core._inventory(protected)
                plan = continuation.run_approved_continuation(
                    **_child(producer, state_root=parent / "verified-child"), dry_run=True,
                )
                assert plan["remaining_work_units"] == 0


def test_child_lock_uses_validated_resolved_path_without_creating_donor_directories(donor):
    before = _snapshot(donor["state_root"])
    paths_before = set(donor["state_root"].rglob("*"))
    state = donor["state_root"] / "must-not-create" / ".." / ".." / "safe-child"
    result = continuation.run_approved_continuation(
        **_child(donor, state_root=state),
        client_factory=lambda: pytest.fail("recalled fully cached response"),
    )
    assert result["state_root"] == str(state.resolve())
    assert _snapshot(donor["state_root"]) == before
    assert set(donor["state_root"].rglob("*")) == paths_before
    assert continuation.run_approved_continuation(**_child(donor), dry_run=True)["reused_responses"] == 1


def test_symlink_donor_rejected_without_reading_target(donor):
    alias = donor["state_root"].parent / "alias-l2"
    alias.symlink_to(donor["state_root"], target_is_directory=True)
    with pytest.raises(ValueError, match="DONOR_UNSAFE_STATE"):
        continuation.run_approved_continuation(**{**_child(donor), "reuse_approved_run": alias}, dry_run=True)


def test_imported_responses_run_local_candidate_processor_again(donor, monkeypatch):
    from fabric_kg_builder.enrichment import schema2_stage

    original = schema2_stage.build_candidate_batch
    processed = []

    def processor(*args, **kwargs):
        processed.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(schema2_stage, "build_candidate_batch", processor)
    continuation.run_approved_continuation(
        **_child(donor), client_factory=lambda: pytest.fail("recalled cached source"),
    )
    assert processed == [[]]


def test_paid_response_survives_local_child_failure_and_exact_resume(partial_donor, monkeypatch):
    from fabric_kg_builder.enrichment import schema2_stage

    options = _child(partial_donor, max_calls=2, max_physical_calls=2)
    sdk, requests = _sdk()
    client_factory = lambda: FoundryClient(_config(), _sdk_client=sdk)
    original = schema2_stage.build_candidate_batch
    def fail_first_paid_response(*args, **kwargs):
        # Child authority changes work-unit ordering; imported responses need not be first.
        if requests:
            raise ValueError("offline local validation failure")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(schema2_stage, "build_candidate_batch", fail_first_paid_response)
        with pytest.raises(ValueError, match="offline local validation failure"):
            continuation.run_approved_continuation(**options, client_factory=client_factory)
    assert len(requests) == 1
    acquired = {
        path: path.read_bytes()
        for path in (options["state_root"] / "reextraction-responses").glob("*.json")
        if "imported_from" not in json.loads(path.read_text())
    }
    assert len(acquired) == 1
    plan = continuation.run_approved_continuation(**options, dry_run=True, resume=True)
    assert plan["current_child_spent"]["logical_calls"] == 1
    assert plan["remaining_child_budget"]["logical_calls"] == 1
    result = continuation.run_approved_continuation(**options, resume=True, client_factory=client_factory)
    assert result["logical_calls"] == result["physical_calls"] == len(requests) == 2
    assert {path: path.read_bytes() for path in acquired} == acquired
    assert requests[0]["messages"][1]["content"] != requests[1]["messages"][1]["content"]
    assert continuation.run_approved_continuation(
        **options, resume=True, client_factory=lambda: pytest.fail("completed resume recalled model"),
    ) == result


def test_recursive_overflow_reconstructs_exact_donor_parent_request(integrated_case):
    from fabric_kg_builder.enrichment.schema2_sources import load_l2_inputs

    options = _options(integrated_case)
    inputs = load_l2_inputs(l1_state_root=options["l1_state_root"], domain_path=options["domain_path"])
    overflow = {"candidates": [{"candidate_kind": "relationship"}] * (
        inputs.domain_contract.reasoning_policy.max_relations_per_work_unit + 1
    )}
    sdk, calls = _sdk(overflow)
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        core.run_approved_reextraction(
            **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
        )
    assert len(calls) == 1
    child = _child(options, max_calls=2, max_physical_calls=2)
    plan = continuation.run_approved_continuation(**child, dry_run=True)
    assert plan["remaining_work_units"] == 2
    sdk, requests = _sdk()
    result = continuation.run_approved_continuation(
        **child, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk),
    )
    assert result["logical_calls"] == result["physical_calls"] == len(requests) == 2
    assert result["reused_responses"] == 1


def test_resealed_import_provenance_is_not_trusted(donor):
    options = _child(donor)
    continuation.run_approved_continuation(**options)
    path = next((options["state_root"] / "reextraction-responses").glob("*.json"))
    cached = json.loads(path.read_text())
    cached["imported_from"]["request_hash"] = "a" * 64
    core._write_state(path, cached)
    _seal(options["state_root"])
    grandchild = {
        **options, "reuse_approved_run": options["state_root"],
        "state_root": options["state_root"].parent / "grandchild-l2",
    }
    with pytest.raises(ValueError, match="DONOR_IMPORT_PROOF_INVALID"):
        continuation.run_approved_continuation(**grandchild, dry_run=True)


def test_child_seals_entire_donor_inventory_not_just_reused_responses(donor):
    options = _child(donor)
    continuation.run_approved_continuation(**options)
    (donor["state_root"] / "extra-file.txt").write_text("donor changed after import")
    _seal(donor["state_root"])
    with pytest.raises(ValueError, match="FINGERPRINT_DRIFT"):
        continuation.run_approved_continuation(**options, resume=True, dry_run=True)


@pytest.mark.parametrize("global_dry_run", [False, True])
def test_public_continuation_cli_no_call_dry_run_and_zero_budget_resume(donor, monkeypatch, global_dry_run):
    from fabric_kg_builder.config import loader

    _no_source_reads(monkeypatch)
    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=_config()))
    monkeypatch.setattr(core, "_bounded_client", lambda *_a, **_k: pytest.fail("CLI constructed live client"))
    before = _snapshot(donor["state_root"])
    options = _child(donor)
    args = _cli_args(options)
    dry_args = ["--dry-run", *args] if global_dry_run else [*args, "--dry-run"]
    planned = CliRunner().invoke(cli, dry_args)
    assert planned.exit_code == 0, (planned.output, planned.exception)
    plan = json.loads(planned.output)
    assert plan["writes"] == plan["remote_calls"] == plan["remaining_work_units"] == 0
    assert plan["reused_responses"] == 1
    assert not options["state_root"].exists()
    assert not (options["state_root"].parent / ".child-l2-enrichment.lock").exists()
    completed = CliRunner().invoke(cli, args)
    assert completed.exit_code == 0, (completed.output, completed.exception)
    resumed = CliRunner().invoke(cli, [*args, "--resume"])
    assert resumed.exit_code == 0, (resumed.output, resumed.exception)
    assert json.loads(resumed.output) == json.loads(completed.output)
    assert _snapshot(donor["state_root"]) == before


def test_public_donor_flag_requires_explicit_approved_mode(donor):
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(donor["source_path"]),
        "--reuse-approved-run", str(donor["state_root"]),
    ])
    assert result.exit_code != 0
    assert "require --reextract-approved" in result.output


def test_public_cli_insufficient_additional_budget_is_no_call(partial_donor, monkeypatch):
    from fabric_kg_builder.config import loader

    monkeypatch.setattr(loader, "load_config", lambda **_: SimpleNamespace(foundry=_config()))
    monkeypatch.setattr(core, "_bounded_client", lambda *_a, **_k: pytest.fail("CLI constructed live client"))
    options = _child(partial_donor, max_calls=1, max_physical_calls=2)
    result = CliRunner().invoke(cli, [*_cli_args(options), "--dry-run"])
    assert result.exit_code != 0
    assert "BUDGET_TOO_SMALL" in result.output
    assert not options["state_root"].exists()
