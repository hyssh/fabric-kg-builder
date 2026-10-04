"""0.2.7 upstream findings (b) heading context, (c) page-split headings, (d) local scope.

Each finding has a HEAD reproduction (asserting the fixed behaviour, so it fails
on the pre-fix code), a direct fix test, and a downstream L3 test.
"""

from __future__ import annotations

from fabric_kg_builder.contracts.base import deterministic_contract_id
from fabric_kg_builder.enrichment import schema2_validation_stage as stage
from fabric_kg_builder.enrichment.schema2_evidence import (
    REJECTION_REASONS, verify_and_mint_extraction_span,
)
from tests.unit import test_schema2_evidence as evidence
from tests.unit import test_schema2_validation_stage as fixtures
from tests.unit.test_schema2_work_unit_references import (
    _hierarchy, _inputs, _record,
)


# ---------------------------------------------------------------------------
# (d) leaf-local endpoint scope and duplicate local IDs
# ---------------------------------------------------------------------------


def _relationship(unit, source, target):
    rel = source.model_copy(update={
        "candidate_kind": "relationship",
        "approved_semantic_id": "relationship-type:test.link",
        "proposed_source_entity_id": source.semantic_id,
        "proposed_target_entity_id": target,
        "proposed_anchor": stage.ProposedAnchorView(
            span_start=0, span_end=len(unit.text), quote=unit.text,
        ),
    })
    span = verify_and_mint_extraction_span(
        source_unit=unit, anchor=rel.proposed_anchor.to_anchor(),
        verified_at_utc=evidence._NOW,
    ).span
    return rel, span


def _rel_reasons(unit, hierarchy, shared, rel, span):
    return stage._relationship_reasons(
        record=rel, hierarchy=hierarchy, shared=shared, source_unit=unit,
        anchor=rel.proposed_anchor.to_anchor(), evidence_span=span,
    )[0]


def test_d_reason_codes_are_registered_rejections():
    assert {"LOCAL_ENDPOINT_OUT_OF_SCOPE", "LOCAL_ID_DUPLICATE"} <= REJECTION_REASONS


def test_d_cross_leaf_canonical_endpoint_is_out_of_scope():
    unit = evidence._unit("Alpha links Beta.")
    hierarchy = _hierarchy()
    owner = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local="e1", quote="Alpha")
    foreign = _record(
        unit, entity_id="entity:two", type_id="semantic-type:test.other",
        local="e2", work="work:two", quote="Beta",
    )
    shared = stage._build_shared_context(_inputs(unit, hierarchy, [owner, foreign]))
    rel, span = _relationship(unit, owner, foreign.semantic_id)
    reasons = _rel_reasons(unit, hierarchy, shared, rel, span)
    assert "LOCAL_ENDPOINT_OUT_OF_SCOPE" in reasons
    assert "ENDPOINT_UNRESOLVED" in reasons


def test_d_placeholder_naming_another_leafs_local_id_is_out_of_scope():
    unit = evidence._unit("Alpha links Beta.")
    hierarchy = _hierarchy()
    owner = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local="e1", quote="Alpha")
    foreign = _record(
        unit, entity_id="entity:two", type_id="semantic-type:test.other",
        local="e2", work="work:two", quote="Beta",
    )
    shared = stage._build_shared_context(_inputs(unit, hierarchy, [owner, foreign]))
    placeholder = deterministic_contract_id(
        "unresolved-entity", {"source_unit_id": unit.source_unit_id, "local_id": "e2"},
    )
    rel, span = _relationship(unit, owner, placeholder)
    assert "LOCAL_ENDPOINT_OUT_OF_SCOPE" in _rel_reasons(unit, hierarchy, shared, rel, span)


def test_d_truly_dangling_placeholder_keeps_historical_unresolved():
    unit = evidence._unit("Alpha links Beta.")
    hierarchy = _hierarchy()
    owner = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local="e1", quote="Alpha")
    shared = stage._build_shared_context(_inputs(unit, hierarchy, [owner]))
    placeholder = deterministic_contract_id(
        "unresolved-entity", {"source_unit_id": unit.source_unit_id, "local_id": "nowhere"},
    )
    rel, span = _relationship(unit, owner, placeholder)
    reasons = _rel_reasons(unit, hierarchy, shared, rel, span)
    assert "ENDPOINT_UNRESOLVED" in reasons
    assert "LOCAL_ENDPOINT_OUT_OF_SCOPE" not in reasons
    assert "LOCAL_ID_DUPLICATE" not in reasons


def test_d_duplicate_local_id_rejects_both_declarations_and_endpoint():
    unit = evidence._unit("Alpha Beta Gamma")
    hierarchy = _hierarchy()
    first = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local="e1", quote="Alpha")
    second = _record(unit, entity_id="entity:two", type_id="semantic-type:test.other", local="E1", quote="Beta")
    shared = stage._build_shared_context(_inputs(unit, hierarchy, [first, second]))
    assert stage._local_declaration_reasons(first, shared) == ("LOCAL_ID_DUPLICATE",)
    assert stage._local_declaration_reasons(second, shared) == ("LOCAL_ID_DUPLICATE",)
    assert stage._local_endpoint_scope_reasons(
        entity_id=second.semantic_id, source_unit_id=unit.source_unit_id,
        work_unit_id="work:one", shared=shared,
    ) == ("LOCAL_ID_DUPLICATE",)


def test_d_same_entity_classification_siblings_are_not_duplicates():
    unit = evidence._unit("Alpha Alpha")
    first = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local="e1", quote="Alpha")
    shared = stage._build_shared_context(_inputs(unit, _hierarchy(), [first, first]))
    assert stage._local_declaration_reasons(first, shared) == ()


def _states(result):
    return {
        (item.candidate_kind, item.current_state, tuple(sorted(item.reason_codes)))
        for item in result.candidate_results
    }


def test_d_downstream_duplicate_local_id_is_rejected_at_l3(tmp_path):
    """HEAD repro: both entities were ASSERTED with no duplicate reason."""

    def mutate(candidates, work_unit):
        candidates = [dict(item) for item in candidates]
        candidates[1]["local_id"] = "record-1"
        return candidates

    l1, domain, _ = fixtures._pipeline(tmp_path, "records", mutate=mutate)
    result = fixtures._l3(tmp_path, l1, domain)
    entities = [i for i in result.candidate_results if i.candidate_kind == "entity"]
    assert entities
    assert all(i.current_state == "rejected" for i in entities)
    assert all("LOCAL_ID_DUPLICATE" in i.reason_codes for i in entities)
    assert not any(i.current_state == "asserted" for i in result.candidate_results)


def test_d_downstream_valid_pipeline_is_unchanged(tmp_path):
    l1, domain, _ = fixtures._pipeline(tmp_path, "records")
    result = fixtures._l3(tmp_path, l1, domain)
    assert result.candidate_results
    assert {i.current_state for i in result.candidate_results} == {"asserted"}
    for item in result.candidate_results:
        assert "LOCAL_ENDPOINT_OUT_OF_SCOPE" not in item.reason_codes
        assert "LOCAL_ID_DUPLICATE" not in item.reason_codes


# ---------------------------------------------------------------------------
# (b) ancestor heading context in the default Schema-2 prompt
# (c) governing heading carried across cached-layout page splits
# ---------------------------------------------------------------------------

import json

import pytest

from fabric_kg_builder.contracts.evidence import SourceUnit
from fabric_kg_builder.contracts.identity import ImmutableSourceLocator
from fabric_kg_builder.enrichment import schema2_work_units as work_units
from fabric_kg_builder.enrichment.schema2_extraction import (
    L2_PROMPT_VERSION, compile_closed_vocabulary, render_extraction_prompt,
)
from tests.unit import test_schema2_extraction as extraction_fixtures
from tests.unit import test_schema2_work_units as work_unit_fixtures


def _unit(text, *, page=None, section_path=None, cached=False, ordinal=0):
    base = work_unit_fixtures._source_unit(text)
    locator_args = {
        "blob_uri": "https://storage.example/source", "blob_version_id": "v1",
    }
    if page is not None:
        locator_args["page"] = page
    if section_path is not None:
        locator_args["section_path"] = list(section_path)
    if cached:
        locator_args["native_layer_id"] = work_units.CACHED_LAYOUT_LAYER_ID
        locator_args["native_object_id"] = f"cache-key/pages/{page}"
    return SourceUnit.mint(
        identity=base.identity, unit_kind="paragraph", text=text,
        ordinal=ordinal,
        locator=ImmutableSourceLocator.from_authority(**locator_args),
    )


def _plan(units, *, heading_context=True):
    planned = work_units.plan_work_units(
        tuple(units), pass_name="schema2", authority_fingerprint="f" * 64,
        heading_context=heading_context,
    )
    return {item.source_unit_id: item for item in planned}


def _prompt(work_unit):
    vocabulary = compile_closed_vocabulary(extraction_fixtures._domain())
    return render_extraction_prompt(
        vocabulary, source_unit_id=work_unit.source_unit_id,
        source_text_hash="a" * 64, source_text=work_unit.text,
        slice_start=work_unit.slice_start, slice_end=work_unit.slice_end,
        heading_path=work_unit.heading_path,
    )


def test_b_prompt_version_is_bumped_for_heading_context():
    assert L2_PROMPT_VERSION == "l2-schema-constrained/1.4.0"
    assert work_units.HEADING_CONTEXT_POLICY_VERSION == "heading-context/1.0.0"


def test_b_repro_section_path_reaches_the_prompt():
    """HEAD repro: no heading_path field/kwarg; ancestors never reach the prompt."""
    unit = _unit("Open valve V-1.", section_path=("Maintenance", "Valve procedure"))
    planned = _plan([unit])[unit.source_unit_id]
    assert planned.heading_path == ("Maintenance", "Valve procedure")
    payload = json.loads(_prompt(planned))
    assert payload["heading_context"] == {
        "policy": "heading-context/1.0.0",
        "ancestor_headings": ["Maintenance", "Valve procedure"],
        "authority": "interpretation_only_not_primary_evidence",
        "rule": payload["heading_context"]["rule"],
    }
    assert payload["source_text"] == "Open valve V-1."


def test_b_no_heading_prompt_and_work_unit_id_are_byte_identical():
    unit = _unit("Open valve V-1.")
    with_context = _plan([unit])[unit.source_unit_id]
    without = _plan([unit], heading_context=False)[unit.source_unit_id]
    assert with_context.heading_path == ()
    assert with_context.work_unit_id == without.work_unit_id
    vocabulary = compile_closed_vocabulary(extraction_fixtures._domain())
    legacy = render_extraction_prompt(
        vocabulary, source_unit_id=without.source_unit_id,
        source_text_hash="a" * 64, source_text=without.text,
        slice_start=without.slice_start, slice_end=without.slice_end,
    )
    assert _prompt(with_context) == legacy
    assert "heading_context" not in legacy


def test_b_heading_context_is_opt_in_and_changes_work_unit_identity():
    unit = _unit("Open valve V-1.", section_path=("Maintenance",))
    off = _plan([unit], heading_context=False)[unit.source_unit_id]
    on = _plan([unit])[unit.source_unit_id]
    assert off.heading_path == ()
    assert on.heading_path == ("Maintenance",)
    assert off.work_unit_id != on.work_unit_id


def test_b_heading_path_is_bounded_and_normalized():
    long = "x" * 500
    path = ("  A \n  b ", "", "C", "D", "E", "F", "G", long)
    bounded = work_units._bounded_heading_path(path)
    assert len(bounded) == work_units.HEADING_CONTEXT_MAX_DEPTH
    assert bounded[0] == "C"
    assert bounded[-1] == "x" * work_units.HEADING_CONTEXT_MAX_CHARS
    assert work_units._bounded_heading_path(("  A \n  b ",)) == ("A b",)


def test_b_default_run_l2_enables_heading_context_only_for_current_prompt():
    import inspect

    from fabric_kg_builder.enrichment import schema2_stage

    source = inspect.getsource(schema2_stage.run_l2)
    assert "heading_context=prompt_version == L2_PROMPT_VERSION" in source
    assert "heading_path=work_unit.heading_path" in source


def test_b_downstream_heading_reaches_prompt_and_l3_is_unchanged(tmp_path, monkeypatch):
    """HEAD repro: the leaf under <h1> never saw its heading in the prompt."""
    monkeypatch.setattr(
        fixtures, "_SENTENCE",
        "</p><h1>Records Procedure</h1><p>A governed record describes a governed subject.",
    )
    prompts = []
    original = fixtures._Service.complete

    def capture(self, *, prompt, work_unit):
        prompts.append((work_unit, prompt))
        return original(self, prompt=prompt, work_unit=work_unit)

    monkeypatch.setattr(fixtures._Service, "complete", capture)
    l1, domain, _ = fixtures._pipeline(tmp_path, "records")
    leaf = [p for w, p in prompts if "governed record" in w.text]
    assert leaf
    payload = json.loads(leaf[0])
    assert payload["heading_context"]["ancestor_headings"] == ["Records Procedure"]
    result = fixtures._l3(tmp_path, l1, domain)
    assert result.candidate_results
    assert {i.current_state for i in result.candidate_results} == {"asserted"}


def test_c_repro_cached_page_heading_governs_next_page():
    """HEAD repro: page N+1 steps lost the page-N procedure heading."""
    page1 = _unit(
        "# Maintenance\n\n## Procedure X\n\nPreparation note.",
        page=1, cached=True, ordinal=0,
    )
    page2 = _unit("1. Close valve.\n2. Drain line.", page=2, cached=True, ordinal=1)
    planned = _plan([page2, page1])
    assert planned[page2.source_unit_id].heading_path == ("Maintenance", "Procedure X")
    # A page's own headings are in its own text, never duplicated as ancestors.
    assert planned[page1.source_unit_id].heading_path == ()
    payload = json.loads(_prompt(planned[page2.source_unit_id]))
    assert payload["heading_context"]["ancestor_headings"] == ["Maintenance", "Procedure X"]
    # Identity and locator authority are untouched.
    assert page2.locator.section_path is None
    assert planned[page2.source_unit_id].source_unit_id == page2.source_unit_id


def test_c_cached_heading_stack_replaces_same_level_siblings():
    page1 = _unit("# Manual\n\n## Procedure A\n\nstep", page=1, cached=True, ordinal=0)
    page2 = _unit("## Procedure B\n\nstep", page=2, cached=True, ordinal=1)
    page3 = _unit("continued step", page=3, cached=True, ordinal=2)
    planned = _plan([page1, page2, page3])
    assert planned[page2.source_unit_id].heading_path == ("Manual", "Procedure A")
    assert planned[page3.source_unit_id].heading_path == ("Manual", "Procedure B")


def test_c_fresh_layout_section_path_crosses_pages_unchanged():
    page1 = _unit("Procedure X intro", page=1, section_path=("Procedure X",))
    page2 = _unit("1. Close valve.", page=2, section_path=("Procedure X",))
    planned = _plan([page1, page2])
    assert planned[page1.source_unit_id].heading_path == ("Procedure X",)
    assert planned[page2.source_unit_id].heading_path == ("Procedure X",)


def test_c_split_children_inherit_heading_path():
    text = " ".join(f"Step {index} closes valve {index}." for index in range(60))
    unit = _unit(text, page=2, section_path=("Procedure X",))
    root = _plan([unit])[unit.source_unit_id]
    children = work_units.split_work_unit(root)
    assert children is not None
    assert all(child.heading_path == ("Procedure X",) for child in children)


def test_c_downstream_carried_heading_never_changes_l3_outcome(tmp_path, monkeypatch):
    """Heading context is interpretation only: identical proposals give identical L3 states."""
    monkeypatch.setattr(
        fixtures, "_SENTENCE",
        "</p><h1>Records Procedure</h1><p>A governed record describes a governed subject.",
    )
    from fabric_kg_builder.enrichment import window_prefix

    prompts = []
    original = fixtures._Service.complete

    def capture(self, *, prompt, work_unit):
        prompts.append(prompt)
        return original(self, prompt=prompt, work_unit=work_unit)

    monkeypatch.setattr(fixtures._Service, "complete", capture)
    runs = {}
    for name, derive in (("with", work_units.derive_heading_paths), ("without", lambda units: {})):
        monkeypatch.setattr(work_units, "derive_heading_paths", derive)
        monkeypatch.setattr(window_prefix, "derive_heading_paths", derive)
        root = tmp_path / name
        root.mkdir()
        l1, domain, _ = fixtures._pipeline(root, "records")
        runs[name] = _states(fixtures._l3(root, l1, domain))
        runs[name + "_headed"] = any("heading_context" in p for p in prompts)
        prompts.clear()
    assert runs["with_headed"] and not runs["without_headed"]
    assert runs["with"] == runs["without"]
    assert runs["with"]
