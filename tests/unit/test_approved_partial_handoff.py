"""Offline partial extraction has its own approval, accounting and publication scope."""

import dataclasses
import json
import socket
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_partial_handoff as partial
from fabric_kg_builder.enrichment import approved_donor_continuation as continuation
from fabric_kg_builder.enrichment import approved_reextraction as core
from fabric_kg_builder.enrichment.schema2_validation_stage import load_l3_inputs, run_l3
from tests.unit.test_approved_donor_continuation import (
    donor, partial_donor, _snapshot,  # noqa: F401
)
from tests.unit.test_window_run_approved_cli import integrated_case  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401
from tests.unit.test_approved_quote_review import failed_donor  # noqa: F401
from tests.unit.test_approved_quote_ancestry import ancestry_case  # noqa: F401
from tests.unit.test_schema2_prototype_agent import WORKSPACE, published  # noqa: F401


def _options(donor):
    return {
        key: value for key, value in donor.items()
        if key not in {"state_root", "max_calls", "max_physical_calls", "max_output_tokens"}
    } | {
        "reuse_approved_run": donor["state_root"],
        "state_root": donor["state_root"].parent / "partial-l2",
    }


def _approve(options, plan):
    return partial.run_partial_handoff(
        **options, dry_run=False, approved_plan_hash=plan["plan_hash"],
        approval_actor="offline-test", approval_rationale="Use only completed existing roots.",
    )


def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(core, "_bounded_client", lambda *a, **k: pytest.fail("model client"))


def test_partial_zero_calls_exact_approval_donor_immutable_and_l3_l4(partial_donor, monkeypatch):
    from fabric_kg_builder.serving.lifecycle_projection import run_l4
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
    from fabric_kg_builder.serving.structured_publication import export_serving_question_context

    _deny_network(monkeypatch)
    options = _options(partial_donor)
    before = _snapshot(partial_donor["state_root"])
    plan = partial.run_partial_handoff(**options)
    assert plan["completed_root_count"] == plan["completed_leaf_count"] == 1
    assert plan["approved_contract_root_count"] == 3
    assert plan["excluded_root_count"] == 2
    assert plan["new_logical_calls"] == plan["new_physical_calls"] == plan["writes"] == 0
    assert not options["state_root"].exists()
    assert not (options["state_root"].parent / ".partial-l2-enrichment.lock").exists()
    with pytest.raises(ValueError, match="EXACT_PLAN_APPROVAL"):
        _approve(options, {**plan, "plan_hash": "0" * 64})
    assert not options["state_root"].exists()
    result = _approve(options, plan)
    assert result["status"] == "succeeded"
    assert _snapshot(partial_donor["state_root"]) == before
    inputs = load_l3_inputs(
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    assert inputs.l2_metrics.foundry_calls == 0
    assert inputs.l2_metrics.cache_hits == 1
    assert inputs.partial_extraction_scope["plan"]["plan_hash"] == plan["plan_hash"]
    l3 = run_l3(
        state_root=options["state_root"].parent / "partial-l3",
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    l4 = run_l4(l3, state_root=options["state_root"].parent / "partial-l4")
    source = SealedL4ServingSource.from_run(
        l4.run_root, input_manifest_search_roots=[l3.run_root],
    )
    context = export_serving_question_context(source)
    assert context["partial_extraction_scope"]["plan"]["completed_root_count"] == 1
    assert "not this extraction" in context["window_run_scope"]["scope_notice"]
    from fabric_kg_builder.deploy import schema2_prototype as publication
    from fabric_kg_builder.serving.structured_publication import L5aPublicationError
    # This small fixture lacks asserted hierarchy; partial approval must not waive that gate.
    with pytest.raises(L5aPublicationError, match="L5A_HIERARCHY_AUTHORITY_MISMATCH"):
        publication.publish_schema2_prototype(
            l4_run=source.root, l3_root=l3.run_root, workspace_id=WORKSPACE,
            name_prefix="partial", dry_run=True, approve_live=None,
            plan_path=options["state_root"].parent / "publication-plan.json",
            materialize_dir=options["state_root"].parent / "publication-materialized",
            journal_path=options["state_root"].parent / "publication-journal.json",
        )
    assert not (options["state_root"].parent / "publication-journal.json").exists()
    assert run_l4(l3, state_root=options["state_root"].parent / "partial-l4").reused
    with pytest.raises(ValueError, match="FRESH_STATE"):
        _approve(options, plan)
    assert _snapshot(partial_donor["state_root"]) == before


def _description_scope(inventory=1425, operator_exclusions=False):
    scope = {"plan": {
        "plan_hash": "a" * 64, "source_chunk_inventory_count": inventory,
        "source_unit_inventory_count": 4, "completed_root_count": 61,
        "approved_contract_root_count": 150, "excluded_root_count": 89,
        "completed_leaf_count": 63,
        "scope_notice": (
            "PARTIAL EXTRACTION: 61 of 150 approved roots complete; 89 excluded, not observed empty. "
            "The 1425-chunk corpus is not fully covered."
        ),
    }}
    if operator_exclusions:
        scope["plan"].update({
            "completed_root_count": 22, "selected_root_count": 22,
            "originally_completed_root_count": 61, "originally_completed_leaf_count": 64,
            "operator_excluded_completed_root_count": 39, "missing_root_count": 89,
            "excluded_root_count": 128, "completed_leaf_count": 22,
            "exclusion_rationale": "Withhold whole roots identified by the pinned quality report.",
            "scope_notice": (
                "PARTIAL EXISTING-DATA EXTRACTION: 22 selected complete roots, 39 completed-but-"
                "operator-excluded roots, 89 missing/incomplete roots of 150 approved roots. "
                "Source chunk inventory: 1425. Exclusion is not quality approval."
            ),
        })
    return scope


@pytest.mark.parametrize("inventory", [1425, None])
def test_partial_description_overrides_window_ceiling_without_truncating(inventory):
    from fabric_kg_builder.deploy import schema2_prototype as publication
    scope = _description_scope(inventory)
    window_scope = {
        "authority": "limited_committed_prefix_only", "selected_chunk_count": 150,
        "total_chunk_count": 1425, "omitted_chunk_count": 1275, "acceptance_hash": "b" * 64,
    } if inventory is not None else None
    description = publication._prototype_description("f" * 32, window_scope, scope)
    assert "approved roots=61/150 complete,89 excluded" in description
    assert "leaves=63" in description
    assert ("corpus=1425 chunks" if inventory is not None else "corpus=4 source units") in description
    assert "NOT full coverage" in description
    assert "scope=" + "a" * 64 in description
    assert "150/1425" not in description
    assert len(description) <= 256
    with pytest.raises(publication.PrototypePublicationError, match="no content was truncated"):
        publication._prototype_description("f" * 256, window_scope, scope)


def test_operator_exclusion_description_has_three_way_coverage():
    from fabric_kg_builder.deploy import schema2_prototype as publication
    scope = _description_scope(operator_exclusions=True)
    description = publication._prototype_description("f" * 32, None, scope)
    assert "roots=22/150 selected,39 completed-excluded,89 missing" in description
    assert "corpus=1425 chunks" in description and "NOT full coverage" in description
    assert "scope=" + "a" * 64 in description
    assert len(description) <= 256
    with pytest.raises(publication.PrototypePublicationError, match="no content was truncated"):
        publication._prototype_description("f" * 256, None, scope)


@pytest.mark.parametrize("operator_exclusions", [False, True])
def test_partial_agent_context_and_leading_warning_are_preserved(published, operator_exclusions):
    from fabric_kg_builder.deploy import schema2_prototype_agent as agent
    from fabric_kg_builder.knowledge.data_agent import decode_stage_snapshot
    handoff = agent._handoff(**{
        key: value for key, value in published.kwargs.items() if key != "out_state"
    })
    scope = _description_scope(operator_exclusions=operator_exclusions)
    context = {
        **handoff.context, "partial_extraction_scope": scope,
        "window_run_scope": {
            "kind": "partial-extraction-scope",
            "authority": "explicit_completed_response_roots_only",
            "scope_notice": scope["plan"]["scope_notice"],
        },
    }
    snapshot = decode_stage_snapshot(
        agent._definition(dataclasses.replace(handoff, context=context), "fixture", None), "draft",
    )
    assert snapshot.instruction.startswith(scope["plan"]["scope_notice"] + "\n\n")
    lakehouse = next(source for source in snapshot.sources if source["type"] == "lakehouse_tables")
    stored = json.loads(lakehouse["metadata"]["schema2_question_context"])
    assert stored["partial_extraction_scope"] == scope
    assert len(snapshot.instruction) <= 15_000
    assert published.backend.calls == []


@pytest.mark.parametrize("operator_exclusions", [False, True])
def test_partial_scope_binds_compilation_plan_and_item_requests(published, monkeypatch, tmp_path, operator_exclusions):
    from fabric_kg_builder.deploy import schema2_prototype as publication
    scope = _description_scope(operator_exclusions=operator_exclusions)
    export = publication.export_serving_question_context
    monkeypatch.setattr(publication, "export_serving_question_context", lambda source: {
        **export(source), "partial_extraction_scope": scope,
        "window_run_scope": {
            "authority": "explicit_completed_response_roots_only",
            "scope_notice": scope["plan"]["scope_notice"],
        },
    })
    plan = publication.publish_schema2_prototype(
        l4_run=published.source.root, l3_root=published.kwargs["l3_root"],
        workspace_id=WORKSPACE, name_prefix="partial", dry_run=True, approve_live=None,
        semantic_model=True,
        plan_path=tmp_path / "partial-plan.json", materialize_dir=tmp_path / "partial-materialized",
        journal_path=tmp_path / "partial-journal.json",
    )
    assert plan["provenance"]["partial_extraction_scope"] == scope
    assert plan["description"] == publication._prototype_description(plan["run_id"], None, scope)
    assert json.loads((tmp_path / "partial-plan.json").read_text())["description"] == plan["description"]
    assert not (tmp_path / "partial-journal.json").exists()
    assert published.backend.calls == []

    class CapturedRequest(Exception):
        pass

    def capture(method, url, **kwargs):
        assert method == "POST"
        assert kwargs["json"]["description"] == plan["description"]
        raise CapturedRequest

    for kind in ("lakehouse", "ontology", "graph", "semantic_model"):
        run = SimpleNamespace(plan=plan, data={"actions": {}}, items=lambda: [], save=lambda: None, request=capture)
        with pytest.raises(CapturedRequest):
            publication._Run.create(run, kind, None)


def test_donor_integrity_drift_fails_before_writes(donor):
    options = _options(donor)
    (donor["state_root"] / "untracked.json").write_text("{}")
    with pytest.raises(ValueError, match="CHECKPOINT_DRIFT"):
        partial.run_partial_handoff(**options)
    assert not options["state_root"].exists()


def test_snapshot_scope_drift_rejected(donor):
    options = _options(donor)
    plan = partial.run_partial_handoff(**options)
    _approve(options, plan)
    path = options["state_root"] / partial.SCOPE_FILE
    scope = json.loads(path.read_text())
    scope["plan"]["completed_root_count"] += 1
    path.write_text(json.dumps(scope))
    with pytest.raises(ValueError, match="SCOPE_INTEGRITY_DRIFT"):
        load_l3_inputs(
            l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
            domain_path=options["domain_path"],
        )


def test_incomplete_overflow_excludes_entire_root(monkeypatch):
    from fabric_kg_builder.enrichment.schema2_work_units import L2WorkUnit, split_work_unit
    root = L2WorkUnit("root", "root", "unit", "h" * 64, "one. two.", 0, 9, "test", "a" * 64)
    children = split_work_unit(root)
    monkeypatch.setattr(continuation, "_prompt", lambda unit, *args: str((unit.slice_start, unit.slice_end)))
    mode = core.OFFSET_MODE
    def key(unit):
        return core.response_hash(str((unit.slice_start, unit.slice_end)), mode)
    overflow = {"candidates": [{"candidate_kind": "relationship"}] * 2}
    responses = {
        key(root): continuation.PaidResponse(overflow, "donor", "parent", "hash"),
        key(children[0]): continuation.PaidResponse({"candidates": []}, "donor", "child", "hash2"),
    }
    included, excluded, leaves = partial._coverage(
        (root,), SimpleNamespace(responses=responses), None, None, 1, mode,
    )
    assert included == leaves == []
    assert len(excluded) == 1
    assert {n["status"] for n in excluded[0]["nodes"]} == {
        "overflow_parent", "response_backed_leaf", "missing_response",
    }
    responses[key(children[1])] = continuation.PaidResponse(
        {"candidates": []}, "donor", "child2", "hash3",
    )
    included, excluded, leaves = partial._coverage(
        (root,), SimpleNamespace(responses=responses), None, None, 1, mode,
    )
    assert len(included) == 1 and len(leaves) == 2 and not excluded


def test_canonical_parser_failure_is_not_silently_excluded(donor, monkeypatch):
    monkeypatch.setattr(partial, "_resolved", lambda *args: {"candidates": [{"bad": "candidate"}]})
    with pytest.raises(ValueError, match="CANONICAL_PARSE_FAILED"):
        partial.run_partial_handoff(**_options(donor))
    assert not _options(donor)["state_root"].exists()


def test_cli_exposes_explicit_approval_and_no_call_budget():
    result = CliRunner().invoke(cli, ["handoff-partial", "--help"])
    assert result.exit_code == 0
    assert "--approve-plan-hash" in result.output and "--approval-actor" in result.output
    assert "--exclude-completed-root" in result.output and "--exclusion-rationale" in result.output
    assert "--max-calls" not in result.output


def test_cli_passes_repeated_root_ids_and_exact_rationale(monkeypatch):
    from fabric_kg_builder.config import loader
    captured = {}
    monkeypatch.setattr(loader, "load_config", lambda **kwargs: SimpleNamespace(foundry="offline"))

    def capture(**kwargs):
        captured.update(kwargs)
        return {"status": "planned"}

    monkeypatch.setattr(partial, "run_partial_handoff", capture)
    result = CliRunner().invoke(cli, [
        "handoff-partial", "--input", "source", "--domain-file", "domain.yaml",
        "--l1-state", "l1", "--l2-state", "new-l2", "--reuse-approved-run", "donor",
        "--exclude-completed-root", "root-two", "--exclude-completed-root", "root-one",
        "--exclusion-rationale", " Exact reviewed reason. ", "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert captured["exclude_completed_roots"] == ("root-two", "root-one")
    assert captured["exclusion_rationale"] == " Exact reviewed reason. "
    assert captured["dry_run"] is True


@pytest.mark.parametrize(("global_flags", "local_flags", "expected"), [
    ([], [], True),
    ([], ["--materialize"], False),
    (["--dry-run"], ["--materialize"], True),
    (["--dry-run"], ["--dry-run"], True),
])
def test_cli_global_dry_run_cannot_be_overridden(
    monkeypatch, global_flags, local_flags, expected,
):
    from fabric_kg_builder.config import loader

    captured = {}
    monkeypatch.setattr(loader, "load_config", lambda **kwargs: SimpleNamespace(foundry="offline"))

    def capture(**kwargs):
        captured.update(kwargs)
        return {"status": "planned"}

    monkeypatch.setattr(partial, "run_partial_handoff", capture)
    result = CliRunner().invoke(cli, [
        *global_flags, "handoff-partial", "--input", "source", "--domain-file", "domain.yaml",
        "--l1-state", "l1", "--l2-state", "new-l2", "--reuse-approved-run", "donor",
        *local_flags,
    ])
    assert result.exit_code == 0, result.output
    assert captured["dry_run"] is expected


def test_whole_root_selection_preserves_split_node_audit_and_all_selected_leaves():
    roots, leaves = [], []
    for index in range(61):
        root = {
            "root_work_unit_id": f"root-{index}", "source_unit_id": f"unit-{index}",
            "source_text_hash": "a" * 64, "slice_start": 0, "slice_end": 40, "nodes": [],
        }
        count = 4 if index == 0 else 1
        if count > 1:
            root["nodes"].append({
                **{k: v for k, v in root.items() if k != "nodes"},
                "work_unit_id": "overflow-parent", "status": "overflow_parent",
            })
        for number in range(count):
            unit = SimpleNamespace(
                work_unit_id=f"leaf-{index}-{number}", source_unit_id=root["source_unit_id"],
                source_text_hash=root["source_text_hash"],
                slice_start=number * 40 // count, slice_end=(number + 1) * 40 // count,
            )
            root["nodes"].append({
                **partial._range(unit), "work_unit_id": unit.work_unit_id,
                "status": "response_backed_leaf", "response": {"retained": unit.work_unit_id},
            })
            leaves.append((unit, object()))
        roots.append(root)
    assert len(leaves) == 64
    ids = tuple(root["root_work_unit_id"] for root in roots[:39])
    selected, excluded, accepted = partial._select_completed_roots(roots, leaves, ids, "reviewed")
    assert len(selected) == len(accepted) == 22
    assert len(excluded) == 39
    assert selected == roots[39:]
    assert accepted == leaves[42:]
    assert all(
        excluded_root == {**root, "reason": "operator_excluded_completed_root", "exclusion_rationale": "reviewed"}
        for excluded_root, root in zip(excluded, roots[:39])
    )
    assert excluded[0]["nodes"][0]["status"] == "overflow_parent"
    assert len(excluded[0]["nodes"]) == 5
    reverse = partial._select_completed_roots(roots, leaves, tuple(reversed(ids)), "reviewed")
    assert reverse == (selected, excluded, accepted)
    selected, excluded, accepted = partial._select_completed_roots(
        roots, leaves, (roots[1]["root_work_unit_id"],), "reviewed",
    )
    assert len(accepted) == 63 and accepted[:4] == leaves[:4]


def test_reviewed_quote_retains_raw_and_projects_without_network(failed_donor, monkeypatch):
    options, review, _ = failed_donor
    before = _snapshot(options["state_root"])
    _deny_network(monkeypatch)
    partial_options = {**_options(options), "approved_quote_review": review}
    plan = partial.run_partial_handoff(**partial_options)
    assert len(plan["review_chain"]["reviews"]) == 1
    _approve(partial_options, plan)
    retained = json.loads(next(
        (partial_options["state_root"] / "retained-responses").glob("*.json")
    ).read_text())
    assert retained["response"]["candidates"][0]["anchors"][0]["quote"] == "not contiguous"
    assert retained["review_projection"]["resolved_response"]["candidates"]
    assert retained["provider_output"]["raw_output_utf8_base64"]
    inputs = load_l3_inputs(
        l2_state_root=partial_options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    assert len(inputs.candidate_batches) == 1
    assert _snapshot(options["state_root"]) == before


def test_destination_change_invalidates_approval(donor):
    options = _options(donor)
    plan = partial.run_partial_handoff(**options)
    different = {**options, "state_root": options["state_root"].with_name("different-l2")}
    with pytest.raises(ValueError, match="EXACT_PLAN_APPROVAL"):
        _approve(different, plan)
    assert not different["state_root"].exists()


@pytest.fixture
def reviewed_partial_donor(ancestry_case):
    from tests.unit.test_approved_quote_ancestry import _bad_provider, _review_for
    from tests.unit.test_approved_donor_continuation import _child
    from fabric_kg_builder.enrichment.approved_quote_anchors import QuoteAnchorResolutionError
    from fabric_kg_builder.enrichment import approved_quote_review as reviews

    emitted = []
    factory, _ = _bad_provider(emitted)
    with pytest.raises(QuoteAnchorResolutionError):
        core.run_approved_reextraction(
            **ancestry_case, anchor_mode=core.QUOTE_MODE, client_factory=factory,
        )
    original = ancestry_case["state_root"]
    review1 = _review_for(
        original, emitted, original.parent / "review1.json", version=reviews.VERSION,
    )
    child = _child(ancestry_case, approved_quote_review=review1, max_calls=10, max_physical_calls=12)
    emitted2 = []
    factory2, _ = _bad_provider(emitted2)
    with pytest.raises(QuoteAnchorResolutionError):
        continuation.run_approved_continuation(**child, client_factory=factory2)
    review2 = _review_for(child["state_root"], emitted2, original.parent / "review2.json")
    before = {p: _snapshot(p) for p in (original, child["state_root"])}
    options = {
        **_options(ancestry_case), "reuse_approved_run": child["state_root"],
        "approved_quote_review": review2,
    }
    return options, before


def test_reviewed_ancestry_partial_not_full_continuation(reviewed_partial_donor, monkeypatch):
    options, before = reviewed_partial_donor
    _deny_network(monkeypatch)
    plan = partial.run_partial_handoff(**options)
    assert plan["completed_root_count"] == 2
    assert plan["excluded_root_count"] == 1
    assert len(plan["review_chain"]["reviews"]) == 2
    _approve(options, plan)
    assert all(_snapshot(p) == snapshot for p, snapshot in before.items())


def test_operator_selection_approval_accounting_immutability_and_l3_l4(reviewed_partial_donor, monkeypatch):
    from fabric_kg_builder.serving.lifecycle_projection import run_l4
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
    from fabric_kg_builder.serving.structured_publication import export_serving_question_context

    options, before = reviewed_partial_donor
    _deny_network(monkeypatch)
    original = partial.run_partial_handoff(**options)
    _approve(options, original)
    before[options["state_root"]] = _snapshot(options["state_root"])
    excluded_root, selected_root = original["included_roots"]
    selected_options = {
        **options, "state_root": options["state_root"].with_name("partial-quality-l2"),
        "exclude_completed_roots": (excluded_root["root_work_unit_id"],),
        "exclusion_rationale": "Explicit whole-root quality exclusion; no waiver.",
    }
    plan = partial.run_partial_handoff(**selected_options)
    assert plan["writes"] == plan["new_logical_calls"] == plan["new_physical_calls"] == 0
    assert plan["originally_completed_root_count"] == 2
    assert plan["selected_root_count"] == plan["completed_root_count"] == 1
    assert plan["operator_excluded_completed_root_count"] == plan["missing_root_count"] == 1
    assert plan["excluded_root_count"] == 2
    assert plan["approved_contract_root_count"] == (
        plan["selected_root_count"] + plan["operator_excluded_completed_root_count"] + plan["missing_root_count"]
    )
    assert plan["originally_completed_root_count"] == (
        plan["selected_root_count"] + plan["operator_excluded_completed_root_count"]
    )
    assert plan["originally_completed_leaf_count"] == original["completed_leaf_count"]
    assert plan["included_roots"] == [selected_root]
    assert plan["excluded_roots"] == [*original["excluded_roots"], {
        **excluded_root, "reason": "operator_excluded_completed_root",
        "exclusion_rationale": selected_options["exclusion_rationale"],
    }]
    assert plan["outside_contract_ranges"] == original["outside_contract_ranges"]
    assert plan["review_chain"] == original["review_chain"]
    for changes in (
        {"exclude_completed_roots": (selected_root["root_work_unit_id"],)},
        {"exclusion_rationale": "A different rationale"},
        {"exclude_completed_roots": (), "exclusion_rationale": None},
    ):
        changed = {**selected_options, **changes}
        assert partial.run_partial_handoff(**changed)["plan_hash"] != plan["plan_hash"]
        with pytest.raises(ValueError, match="EXACT_PLAN_APPROVAL"):
            _approve(changed, plan)
        assert not selected_options["state_root"].exists()
    _approve(selected_options, plan)
    retained = [
        json.loads(path.read_text()) for path in
        (selected_options["state_root"] / "retained-responses").glob("*.json")
    ]
    selected_nodes = [n for n in selected_root["nodes"] if n["status"] == "response_backed_leaf"]
    assert len(retained) == len(selected_nodes) == plan["completed_leaf_count"]
    for saved, node in zip(sorted(retained, key=lambda r: r["range"]["slice_start"]), selected_nodes):
        assert saved["range"] == partial._range(SimpleNamespace(**node))
    l3 = run_l3(
        state_root=options["state_root"].with_name("selected-l3"),
        l2_state_root=selected_options["state_root"],
        l1_state_root=options["l1_state_root"], domain_path=options["domain_path"],
    )
    l4 = run_l4(l3, state_root=options["state_root"].with_name("selected-l4"))
    context = export_serving_question_context(SealedL4ServingSource.from_run(
        l4.run_root, input_manifest_search_roots=[l3.run_root],
    ))
    scope = context["partial_extraction_scope"]["plan"]
    for key in ("originally_completed_root_count", "selected_root_count", "missing_root_count",
                "operator_excluded_completed_root_count", "exclusion_rationale"):
        assert scope[key] == plan[key]
    assert "1 selected complete roots" in context["window_run_scope"]["scope_notice"]
    assert "1 completed-but-operator-excluded roots" in context["window_run_scope"]["scope_notice"]
    assert "1 missing/incomplete roots of 3 approved roots" in context["window_run_scope"]["scope_notice"]
    assert all(_snapshot(p) == snapshot for p, snapshot in before.items())


def test_invalid_operator_root_selection_fails_without_writes(partial_donor, monkeypatch):
    _deny_network(monkeypatch)
    options = _options(partial_donor)
    plan = partial.run_partial_handoff(**options)
    complete = plan["included_roots"][0]["root_work_unit_id"]
    missing = plan["excluded_roots"][0]["root_work_unit_id"]
    cases = [
        ((complete, complete), "reason", "DUPLICATE_EXCLUDED_ROOT"),
        (("unknown",), "reason", "EXCLUDED_ROOT_NOT_COMPLETE_OR_UNKNOWN"),
        ((missing,), "reason", "EXCLUDED_ROOT_NOT_COMPLETE_OR_UNKNOWN"),
        ((complete,), None, "EXCLUSION_RATIONALE_REQUIRED"),
        ((complete,), "  ", "EXCLUSION_RATIONALE_REQUIRED"),
        ((complete,), "reason", "ALL_COMPLETE_ROOTS_EXCLUDED"),
        ((), "reason", "EXCLUSION_RATIONALE_WITHOUT_ROOTS"),
    ]
    before = _snapshot(partial_donor["state_root"])
    for ids, rationale, error in cases:
        with pytest.raises(ValueError, match=error):
            partial.run_partial_handoff(
                **options, exclude_completed_roots=ids, exclusion_rationale=rationale,
            )
        assert not options["state_root"].exists()
    assert _snapshot(partial_donor["state_root"]) == before


def test_l3_rejects_evidence_in_excluded_range():
    from fabric_kg_builder.enrichment.schema2_validation_stage import _validate_prefix_evidence_spans
    inputs = SimpleNamespace(partial_extraction_scope={"plan": {"included_roots": [{
        "source_unit_id": "unit", "source_text_hash": "hash", "slice_start": 0, "slice_end": 4,
    }]}})
    span = SimpleNamespace(
        source_unit_id="unit", source_text_content_hash="hash", span_start=5, span_end=8,
        evidence_span_id="span",
    )
    with pytest.raises(ValueError, match="L3_EVIDENCE_OUTSIDE_PARTIAL_SCOPE"):
        _validate_prefix_evidence_spans(inputs, [span])
