"""Tests for issues #9, #10, #12, #13, #14 — contract-driven source policy, text limits,
capability-aware validation, example gating, and property assurance.

Covers (original #9/#10):
- SourcePolicy construction and overlap guard
- validate_source_policy: closed-world enforcement (required/prohibited/extra)
- validate_published_source_policy: published snapshot closed-world enforcement
- Named text limit constants
- validate_data_agent_text: pass/fail per field, few-shot count and payload
- validate_instruction_deduplication: exact and near-identical detection
- validate_graph_few_shots: required when compiled competency contract exists
- Non-empty required fields
- deploy/data_agent.py fix: source-specific instructions, no duplication

Covers (new #12/#13/#14):
- classify_relationship_availability: four-state mapping from DataAvailability + required flag
- New error codes: DATA_AGENT_PROPERTY_OMITTED, DATA_AGENT_REQUIRED_EXAMPLE_EMPTY,
  DATA_AGENT_UNAVAILABLE_RELATIONSHIP_CLAIMED (ValidationError subclasses with structured attrs)
- CompetencyExampleReceipt: model structure, round-trip, blocked vs published states
- gate_competency_examples: four states (executable_nonempty, required_absent, optional_absent,
  schema_supported_unobserved), backward compat (no contract), remediation presence,
  per-relationship observed_rows in receipt, unrelated examples unaffected
- QueryReadiness.observed_relationship_rows: new field, backward-compat default
- AgentPublicationReceipt: required/compiled/draft/published property count fields,
  property selection hashes, grounding text character counts
- Property omission cannot false-pass by comparing stripped snapshot to itself:
  published count must be compared to grounding.expected_property_child_count

Covers (McManus revision — formal blockers):
- selected_property_ids: content-based sorted property ID list on DataAgentStageSnapshot
- Property selection hash is content-based: equal-count different selections differ
- Compiled property omission blocks before draft/published checks (DATA_AGENT_PROPERTY_OMITTED)
- DataAgentRequiredExampleEmpty error type hierarchy and actionable string representation
  (blocker 1: type not caught by OSError/ValueError in CLI grounding try block)
- build_graph_source_instructions/description accept and use availability kwarg (blocker 2)
"""
from __future__ import annotations

import pytest

from fabric_kg_builder.knowledge.data_agent import (
    DataAgentSpec,
    DataAgentStageSnapshot,
    DataSourceSpec,
    FewShotExample,
)
from fabric_kg_builder.knowledge.validation import (
    MAX_FEW_SHOT_COUNT,
    MAX_FEW_SHOT_PAYLOAD_CHARS,
    MAX_GLOBAL_INSTRUCTION_CHARS,
    MAX_SOURCE_DESCRIPTION_CHARS,
    MAX_SOURCE_INSTRUCTION_CHARS,
    FewShotContractViolation,
    SourcePolicy,
    SourcePolicyViolation,
    TextLimitViolation,
    TextValidationResult,
    validate_data_agent_text,
    validate_graph_few_shots,
    validate_instruction_deduplication,
    validate_published_source_policy,
    validate_source_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    global_instruction: str = "Route to Ontology or Graph.",
    sources: list[DataSourceSpec] | None = None,
) -> DataAgentSpec:
    if sources is None:
        sources = [
            DataSourceSpec(
                source_type="ontology",
                name="my-ontology",
                instructions="Interpret approved entity types.",
                description="Semantic ontology for domain concepts.",
            ),
            DataSourceSpec(
                source_type="graph",
                name="my-graph",
                instructions="Traverse directed edges exactly.",
                description="Typed graph for relationship traversal.",
            ),
        ]
    return DataAgentSpec(
        display_name="Test Agent",
        instruction=global_instruction,
        sources=sources,
    )


def _make_snapshot(source_types: list[str]) -> DataAgentStageSnapshot:
    return DataAgentStageSnapshot(
        stage="published",
        instruction="x",
        sources=tuple({"type": t} for t in source_types),
    )


# ---------------------------------------------------------------------------
# Named constants — sanity
# ---------------------------------------------------------------------------


class TestNamedConstants:
    def test_global_instruction_limit(self):
        assert MAX_GLOBAL_INSTRUCTION_CHARS == 4_000

    def test_source_instruction_limit(self):
        assert MAX_SOURCE_INSTRUCTION_CHARS == 2_000

    def test_source_description_limit(self):
        assert MAX_SOURCE_DESCRIPTION_CHARS == 500

    def test_few_shot_count_limit(self):
        assert MAX_FEW_SHOT_COUNT == 7

    def test_few_shot_payload_limit(self):
        assert MAX_FEW_SHOT_PAYLOAD_CHARS == 10_000


# ---------------------------------------------------------------------------
# SourcePolicy construction
# ---------------------------------------------------------------------------


class TestSourcePolicyConstruction:
    def test_basic_construction(self):
        p = SourcePolicy(
            required=frozenset({"ontology", "graph"}),
            prohibited=frozenset({"lakehouse"}),
        )
        assert "ontology" in p.required
        assert "lakehouse" in p.prohibited

    def test_overlap_raises(self):
        with pytest.raises(ValueError, match="required and prohibited"):
            SourcePolicy(
                required=frozenset({"ontology"}),
                prohibited=frozenset({"ontology"}),
            )

    def test_empty_prohibited_default(self):
        p = SourcePolicy(required=frozenset({"graph"}))
        assert len(p.prohibited) == 0

    def test_allowed_extra_default_empty(self):
        p = SourcePolicy(required=frozenset({"ontology", "graph"}))
        assert len(p.allowed_extra) == 0

    def test_allowed_extra_prohibited_overlap_raises(self):
        with pytest.raises(ValueError, match="allowed_extra and prohibited"):
            SourcePolicy(
                required=frozenset({"ontology"}),
                prohibited=frozenset({"kusto"}),
                allowed_extra=frozenset({"kusto"}),
            )


# ---------------------------------------------------------------------------
# validate_source_policy — spec
# ---------------------------------------------------------------------------


class TestValidateSourcePolicy:
    def _policy(self) -> SourcePolicy:
        return SourcePolicy(
            required=frozenset({"ontology", "graph"}),
            prohibited=frozenset({"lakehouse"}),
        )

    def test_exact_configured_set_passes(self):
        spec = _make_spec()
        validate_source_policy(spec, self._policy())  # must not raise

    def test_missing_required_type_raises(self):
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name="ont",
                    instructions="...",
                    description="...",
                )
            ]
        )
        with pytest.raises(SourcePolicyViolation, match="SOURCE_POLICY_MISSING_REQUIRED"):
            validate_source_policy(spec, self._policy())

    def test_prohibited_type_present_raises(self):
        spec = _make_spec(
            sources=[
                DataSourceSpec(source_type="ontology", name="ont"),
                DataSourceSpec(source_type="graph", name="g"),
                DataSourceSpec(source_type="lakehouse", name="lh"),
            ]
        )
        with pytest.raises(SourcePolicyViolation, match="SOURCE_POLICY_PROHIBITED_PRESENT"):
            validate_source_policy(spec, self._policy())

    def test_extra_unlisted_type_raises_closed_world(self):
        """Closed-world: an extra type not in required or allowed_extra must raise."""
        spec = _make_spec(
            sources=[
                DataSourceSpec(source_type="ontology", name="ont"),
                DataSourceSpec(source_type="graph", name="g"),
                DataSourceSpec(source_type="kusto", name="k"),
            ]
        )
        with pytest.raises(SourcePolicyViolation, match="SOURCE_POLICY_EXTRA_TYPE"):
            validate_source_policy(spec, self._policy())

    def test_extra_type_in_allowed_extra_passes(self):
        """A type in allowed_extra is not rejected by closed-world enforcement."""
        policy = SourcePolicy(
            required=frozenset({"ontology", "graph"}),
            prohibited=frozenset({"lakehouse"}),
            allowed_extra=frozenset({"kusto"}),
        )
        spec = _make_spec(
            sources=[
                DataSourceSpec(source_type="ontology", name="ont"),
                DataSourceSpec(source_type="graph", name="g"),
                DataSourceSpec(source_type="kusto", name="k"),
            ]
        )
        validate_source_policy(spec, policy)  # must not raise

    def test_extra_type_error_code(self):
        """Closed-world violation has code SOURCE_POLICY_EXTRA_TYPE."""
        spec = _make_spec(
            sources=[
                DataSourceSpec(source_type="ontology", name="ont"),
                DataSourceSpec(source_type="graph", name="g"),
                DataSourceSpec(source_type="kusto", name="k"),
            ]
        )
        with pytest.raises(SourcePolicyViolation) as exc_info:
            validate_source_policy(spec, self._policy())
        assert exc_info.value.code == "SOURCE_POLICY_EXTRA_TYPE"
        assert exc_info.value.field == "kusto"

    def test_empty_sources_raises_for_required(self):
        spec = _make_spec(sources=[])
        with pytest.raises(SourcePolicyViolation):
            validate_source_policy(spec, self._policy())

    def test_violation_error_code(self):
        spec = _make_spec(sources=[DataSourceSpec(source_type="ontology", name="o")])
        with pytest.raises(SourcePolicyViolation) as exc_info:
            validate_source_policy(spec, self._policy())
        assert exc_info.value.code == "SOURCE_POLICY_MISSING_REQUIRED"
        assert exc_info.value.field == "graph"

    def test_prohibited_error_code(self):
        spec = _make_spec(
            sources=[
                DataSourceSpec(source_type="ontology", name="o"),
                DataSourceSpec(source_type="graph", name="g"),
                DataSourceSpec(source_type="lakehouse", name="lh"),
            ]
        )
        with pytest.raises(SourcePolicyViolation) as exc_info:
            validate_source_policy(spec, self._policy())
        assert exc_info.value.code == "SOURCE_POLICY_PROHIBITED_PRESENT"
        assert exc_info.value.field == "lakehouse"


# ---------------------------------------------------------------------------
# validate_published_source_policy — snapshot
# ---------------------------------------------------------------------------


class TestValidatePublishedSourcePolicy:
    def _policy(self) -> SourcePolicy:
        return SourcePolicy(
            required=frozenset({"ontology", "graph"}),
            prohibited=frozenset({"lakehouse"}),
        )

    def test_correct_published_passes(self):
        snap = _make_snapshot(["ontology", "graph"])
        validate_published_source_policy(snap, self._policy())  # must not raise

    def test_missing_required_in_published_raises(self):
        snap = _make_snapshot(["ontology"])
        with pytest.raises(SourcePolicyViolation, match="PUBLISHED_SOURCE_POLICY_MISSING_REQUIRED"):
            validate_published_source_policy(snap, self._policy())

    def test_prohibited_in_published_raises(self):
        snap = _make_snapshot(["ontology", "graph", "lakehouse"])
        with pytest.raises(SourcePolicyViolation, match="PUBLISHED_SOURCE_POLICY_PROHIBITED_PRESENT"):
            validate_published_source_policy(snap, self._policy())

    def test_error_code_missing(self):
        snap = _make_snapshot(["graph"])
        with pytest.raises(SourcePolicyViolation) as exc_info:
            validate_published_source_policy(snap, self._policy())
        assert exc_info.value.code == "PUBLISHED_SOURCE_POLICY_MISSING_REQUIRED"
        assert exc_info.value.field == "ontology"

    def test_error_code_prohibited(self):
        snap = _make_snapshot(["ontology", "graph", "lakehouse"])
        with pytest.raises(SourcePolicyViolation) as exc_info:
            validate_published_source_policy(snap, self._policy())
        assert exc_info.value.code == "PUBLISHED_SOURCE_POLICY_PROHIBITED_PRESENT"
        assert exc_info.value.field == "lakehouse"

    def test_extra_unlisted_published_type_raises_closed_world(self):
        """Closed-world: Fabric adding an unexpected source type must be rejected."""
        snap = _make_snapshot(["ontology", "graph", "kusto"])
        with pytest.raises(SourcePolicyViolation, match="PUBLISHED_SOURCE_POLICY_EXTRA_TYPE"):
            validate_published_source_policy(snap, self._policy())

    def test_extra_published_type_error_code(self):
        snap = _make_snapshot(["ontology", "graph", "kusto"])
        with pytest.raises(SourcePolicyViolation) as exc_info:
            validate_published_source_policy(snap, self._policy())
        assert exc_info.value.code == "PUBLISHED_SOURCE_POLICY_EXTRA_TYPE"
        assert exc_info.value.field == "kusto"

    def test_extra_published_type_in_allowed_extra_passes(self):
        """An extra published type in allowed_extra is not rejected."""
        policy = SourcePolicy(
            required=frozenset({"ontology", "graph"}),
            prohibited=frozenset({"lakehouse"}),
            allowed_extra=frozenset({"kusto"}),
        )
        snap = _make_snapshot(["ontology", "graph", "kusto"])
        validate_published_source_policy(snap, policy)  # must not raise


# ---------------------------------------------------------------------------
# validate_data_agent_text
# ---------------------------------------------------------------------------


class TestValidateDataAgentText:
    def test_compact_spec_all_pass(self):
        spec = _make_spec()
        results = validate_data_agent_text(spec)
        assert all(r.passed for r in results), [r for r in results if not r.passed]

    def test_returns_list_of_results(self):
        spec = _make_spec()
        results = validate_data_agent_text(spec)
        assert isinstance(results, list)
        assert all(isinstance(r, TextValidationResult) for r in results)

    def test_global_instruction_field_name(self):
        spec = _make_spec()
        results = validate_data_agent_text(spec)
        fields = [r.field for r in results]
        assert "global.instruction" in fields

    def test_source_instruction_fields_present(self):
        spec = _make_spec()
        results = validate_data_agent_text(spec)
        fields = [r.field for r in results]
        assert "ontology.dataSourceInstructions" in fields
        assert "graph.dataSourceInstructions" in fields

    def test_source_description_fields_present(self):
        spec = _make_spec()
        results = validate_data_agent_text(spec)
        fields = [r.field for r in results]
        assert "ontology.userDescription" in fields
        assert "graph.userDescription" in fields

    def test_global_instruction_too_long_fails(self):
        oversized = "x" * (MAX_GLOBAL_INSTRUCTION_CHARS + 1)
        spec = _make_spec(global_instruction=oversized)
        results = validate_data_agent_text(spec)
        global_result = next(r for r in results if r.field == "global.instruction")
        assert not global_result.passed
        assert global_result.actual == len(oversized)
        assert global_result.limit == MAX_GLOBAL_INSTRUCTION_CHARS

    def test_source_instruction_too_long_fails(self):
        oversized = "y" * (MAX_SOURCE_INSTRUCTION_CHARS + 1)
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions=oversized,
                    description="ok",
                )
            ]
        )
        results = validate_data_agent_text(spec)
        graph_instr = next(
            r for r in results if r.field == "graph.dataSourceInstructions"
        )
        assert not graph_instr.passed
        assert graph_instr.actual == len(oversized)

    def test_source_description_too_long_fails(self):
        oversized = "z" * (MAX_SOURCE_DESCRIPTION_CHARS + 1)
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name="o",
                    instructions="short",
                    description=oversized,
                )
            ]
        )
        results = validate_data_agent_text(spec)
        ont_desc = next(
            r for r in results if r.field == "ontology.userDescription"
        )
        assert not ont_desc.passed

    def test_few_shot_count_at_limit_passes(self):
        import uuid as _uuid
        few_shots = [
            FewShotExample(id=str(_uuid.uuid4()), question=f"Q{i}?", query=f"MATCH (n) RETURN n LIMIT {i}")
            for i in range(MAX_FEW_SHOT_COUNT)
        ]
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="ok",
                    description="ok",
                    few_shots=few_shots,
                )
            ]
        )
        results = validate_data_agent_text(spec)
        count_result = next(
            r for r in results if r.field == "graph.fewShots.count"
        )
        assert count_result.passed
        assert count_result.actual == MAX_FEW_SHOT_COUNT

    def test_few_shot_count_over_limit_fails(self):
        import uuid as _uuid
        few_shots = [
            FewShotExample(id=str(_uuid.uuid4()), question=f"Q{i}?", query=f"MATCH (n) RETURN n LIMIT {i}")
            for i in range(MAX_FEW_SHOT_COUNT + 2)
        ]
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="ok",
                    description="ok",
                    few_shots=few_shots,
                )
            ]
        )
        results = validate_data_agent_text(spec)
        count_result = next(
            r for r in results if r.field == "graph.fewShots.count"
        )
        assert not count_result.passed

    def test_few_shot_payload_too_large_fails(self):
        import uuid as _uuid
        # Create few-shots with very long queries to exceed payload limit
        few_shots = [
            FewShotExample(
                id=str(_uuid.uuid4()),
                question="Where is the asset?",
                query="MATCH (n) WHERE n.description CONTAINS '" + ("x" * 5_000) + "' RETURN n",
            )
            for _ in range(2)
        ]
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="ok",
                    description="ok",
                    few_shots=few_shots,
                )
            ]
        )
        results = validate_data_agent_text(spec)
        payload_result = next(
            (r for r in results if r.field == "graph.fewShots.payloadChars"),
            None,
        )
        assert payload_result is not None
        assert not payload_result.passed

    def test_result_has_remediation_when_failed(self):
        oversized = "x" * (MAX_GLOBAL_INSTRUCTION_CHARS + 1)
        spec = _make_spec(global_instruction=oversized)
        results = validate_data_agent_text(spec)
        global_result = next(r for r in results if r.field == "global.instruction")
        assert len(global_result.remediation) > 0

    def test_no_few_shots_no_payload_field(self):
        """When few_shots is None/empty, no payload size field is emitted."""
        spec = _make_spec()
        results = validate_data_agent_text(spec)
        fields = [r.field for r in results]
        payload_fields = [f for f in fields if "payloadChars" in f]
        assert payload_fields == []


# ---------------------------------------------------------------------------
# TextLimitViolation
# ---------------------------------------------------------------------------


class TestTextLimitViolation:
    def test_basic_attributes(self):
        err = TextLimitViolation(
            field="graph.dataSourceInstructions",
            actual=6840,
            limit=2000,
            remediation="Move schema detail into selected elements.",
        )
        assert err.code == "DATA_AGENT_TEXT_LIMIT"
        assert err.field == "graph.dataSourceInstructions"
        assert err.actual == 6840
        assert err.limit == 2000
        assert "6,840" in str(err)
        assert "2,000" in str(err)

    def test_is_exception(self):
        with pytest.raises(TextLimitViolation):
            raise TextLimitViolation("f", 100, 50)


# ---------------------------------------------------------------------------
# validate_instruction_deduplication
# ---------------------------------------------------------------------------


class TestValidateInstructionDeduplication:
    def test_distinct_instructions_no_duplicates(self):
        spec = _make_spec()
        result = validate_instruction_deduplication(spec)
        assert result == []

    def test_global_duplicated_in_source_raises_above_threshold(self):
        long_text = "Answer questions about the domain. " * 8  # > 200 chars
        spec = _make_spec(
            global_instruction=long_text,
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name="o",
                    instructions=long_text,
                    description="short",
                )
            ],
        )
        violations = validate_instruction_deduplication(spec)
        assert len(violations) == 1
        assert "global.instruction" in violations[0]
        assert "ontology.dataSourceInstructions" in violations[0]

    def test_two_sources_identical_raises_above_threshold(self):
        long_text = "Traverse the graph using directed edges and return evidence. " * 5
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name="o",
                    instructions=long_text,
                    description="short",
                ),
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions=long_text,
                    description="short",
                ),
            ]
        )
        violations = validate_instruction_deduplication(spec)
        assert any("ontology" in v and "graph" in v for v in violations)

    def test_short_shared_text_not_flagged(self):
        """Short shared terminology (< 200 chars) should not be flagged."""
        short_text = "Use approved concepts."
        spec = _make_spec(
            global_instruction=short_text,
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name="o",
                    instructions=short_text,
                    description="short",
                )
            ],
        )
        violations = validate_instruction_deduplication(spec)
        assert violations == []

    def test_whitespace_normalized_comparison(self):
        """Extra whitespace should not allow duplication to slip through."""
        base_text = "Route to Ontology or Graph for verified evidence. " * 5
        padded = "\n\n  " + base_text.replace(" ", "  ") + "\n\n"
        spec = _make_spec(
            global_instruction=base_text,
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions=padded,
                    description="short",
                )
            ],
        )
        violations = validate_instruction_deduplication(spec)
        assert len(violations) == 1

    def test_case_normalized_comparison(self):
        """Case differences should not allow duplication to slip through."""
        base_text = "Route to ontology or graph for verified evidence. " * 5
        upper = base_text.upper()
        spec = _make_spec(
            global_instruction=base_text,
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions=upper,
                    description="short",
                )
            ],
        )
        violations = validate_instruction_deduplication(spec)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# deploy/data_agent.py fix — source-specific instructions, no duplication
# ---------------------------------------------------------------------------


class TestBuildSemanticDataAgentSpecNoInstructionDuplication:
    """Verify that the legacy builder no longer copies global→source instructions."""

    def _make_plan(self):
        from unittest.mock import MagicMock
        plan = MagicMock()
        plan.entity_types = [
            MagicMock(type_name="Equipment", count=50),
            MagicMock(type_name="Facility", count=20),
        ]
        plan.relationship_pairs = []
        return plan

    def _sample_parts(self):
        import json
        graph_type = {
            "nodeTypes": [
                {"alias": "alias_eq", "labels": ["Equipment"]},
                {"alias": "alias_fa", "labels": ["Facility"]},
            ],
            "edgeTypes": [
                {
                    "alias": "e_located",
                    "labels": ["located_at"],
                    "sourceNodeType": {"alias": "alias_fa"},
                    "destinationNodeType": {"alias": "alias_eq"},
                }
            ],
        }
        return [{"path": "graphType.json", "payload_json": graph_type}]

    def test_ontology_source_instructions_differ_from_global(self):
        from fabric_kg_builder.deploy.data_agent import build_semantic_data_agent_spec
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-1",
            ontology_id="ont-1",
            ontology_name="Ontology",
            graph_model_id="gm-1",
            graph_model_name="Graph",
            ontology_plan=self._make_plan(),
            graph_parts=self._sample_parts(),
        )
        ontology_src = next(s for s in spec.sources if s.source_type == "ontology")
        assert ontology_src.instructions != spec.instruction, (
            "Ontology source instruction must be distinct from global instruction"
        )

    def test_graph_source_instructions_differ_from_global(self):
        from fabric_kg_builder.deploy.data_agent import build_semantic_data_agent_spec
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-1",
            ontology_id="ont-1",
            ontology_name="Ontology",
            graph_model_id="gm-1",
            graph_model_name="Graph",
            ontology_plan=self._make_plan(),
            graph_parts=self._sample_parts(),
        )
        graph_src = next(s for s in spec.sources if s.source_type == "graph")
        assert graph_src.instructions != spec.instruction, (
            "Graph source instruction must be distinct from global instruction"
        )

    def test_ontology_and_graph_source_instructions_differ(self):
        from fabric_kg_builder.deploy.data_agent import build_semantic_data_agent_spec
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-1",
            ontology_id="ont-1",
            ontology_name="Ontology",
            graph_model_id="gm-1",
            graph_model_name="Graph",
            ontology_plan=self._make_plan(),
            graph_parts=self._sample_parts(),
        )
        ont_src = next(s for s in spec.sources if s.source_type == "ontology")
        graph_src = next(s for s in spec.sources if s.source_type == "graph")
        assert ont_src.instructions != graph_src.instructions, (
            "Ontology and Graph source instructions must be distinct"
        )

    def test_deduplication_passes_for_built_spec(self):
        from fabric_kg_builder.deploy.data_agent import build_semantic_data_agent_spec
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-1",
            ontology_id="ont-1",
            ontology_name="Ontology",
            graph_model_id="gm-1",
            graph_model_name="Graph",
            ontology_plan=self._make_plan(),
            graph_parts=self._sample_parts(),
        )
        violations = validate_instruction_deduplication(spec)
        assert violations == [], f"Unexpected duplication: {violations}"

    def test_text_validation_passes_for_built_spec(self):
        from fabric_kg_builder.deploy.data_agent import build_semantic_data_agent_spec
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-1",
            ontology_id="ont-1",
            ontology_name="Ontology",
            graph_model_id="gm-1",
            graph_model_name="Graph",
            ontology_plan=self._make_plan(),
            graph_parts=self._sample_parts(),
        )
        results = validate_data_agent_text(spec)
        failures = [r for r in results if not r.passed]
        assert failures == [], f"Text validation failures: {failures}"

    def test_source_policy_passes_for_built_spec(self):
        from fabric_kg_builder.deploy.data_agent import build_semantic_data_agent_spec
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-1",
            ontology_id="ont-1",
            ontology_name="Ontology",
            graph_model_id="gm-1",
            graph_model_name="Graph",
            ontology_plan=self._make_plan(),
            graph_parts=self._sample_parts(),
        )
        policy = SourcePolicy(
            required=frozenset({"ontology", "graph"}),
            prohibited=frozenset(),
        )
        validate_source_policy(spec, policy)  # must not raise


# ---------------------------------------------------------------------------
# Graph few-shot required when competency contract exists (issue #10)
# ---------------------------------------------------------------------------


def _make_graph_only_spec(few_shots: list[FewShotExample] | None) -> DataAgentSpec:
    return _make_spec(
        sources=[
            DataSourceSpec(
                source_type="ontology",
                name="o",
                instructions="Interpret concepts.",
                description="Ontology.",
            ),
            DataSourceSpec(
                source_type="graph",
                name="g",
                instructions="Traverse edges.",
                description="Graph.",
                few_shots=few_shots,
            ),
        ]
    )


class TestValidateGraphFewShots:
    """Acceptance tests for validate_graph_few_shots (issue #10 blocker B2)."""

    def _one_few_shot(self) -> list[FewShotExample]:
        return [FewShotExample(id="fs-1", question="Who?", query="MATCH (n) RETURN n")]

    # --- contract_exists=True, 0 few-shots → must fail ---

    def test_contract_exists_zero_few_shots_raises(self):
        """Compiled contract + 0 surviving examples must hard-fail."""
        spec = _make_graph_only_spec(few_shots=None)
        with pytest.raises(FewShotContractViolation):
            validate_graph_few_shots(spec, contract_exists=True)

    def test_contract_exists_empty_list_raises(self):
        """Empty list (not None) is still zero; must hard-fail with contract."""
        spec = _make_graph_only_spec(few_shots=[])
        with pytest.raises(FewShotContractViolation):
            validate_graph_few_shots(spec, contract_exists=True)

    def test_contract_violation_error_code(self):
        spec = _make_graph_only_spec(few_shots=None)
        with pytest.raises(FewShotContractViolation) as exc_info:
            validate_graph_few_shots(spec, contract_exists=True)
        assert exc_info.value.code == "GRAPH_FEW_SHOTS_REQUIRED"

    def test_contract_violation_message_helpful(self):
        spec = _make_graph_only_spec(few_shots=None)
        with pytest.raises(FewShotContractViolation, match="competency contract"):
            validate_graph_few_shots(spec, contract_exists=True)

    # --- contract_exists=True, ≥1 few-shot → must pass ---

    def test_contract_exists_with_few_shots_passes(self):
        """Compiled contract + at least one surviving example passes."""
        spec = _make_graph_only_spec(few_shots=self._one_few_shot())
        validate_graph_few_shots(spec, contract_exists=True)  # must not raise

    def test_contract_exists_multiple_few_shots_passes(self):
        shots = [
            FewShotExample(id=f"fs-{i}", question=f"Q{i}?", query=f"MATCH (n{i}) RETURN n{i}")
            for i in range(3)
        ]
        spec = _make_graph_only_spec(few_shots=shots)
        validate_graph_few_shots(spec, contract_exists=True)  # must not raise

    # --- contract_exists=False, 0 few-shots → backward-compatible pass ---

    def test_no_contract_zero_few_shots_passes(self):
        """Without a compiled contract, zero few-shots is acceptable."""
        spec = _make_graph_only_spec(few_shots=None)
        validate_graph_few_shots(spec, contract_exists=False)  # must not raise

    def test_no_contract_empty_list_passes(self):
        spec = _make_graph_only_spec(few_shots=[])
        validate_graph_few_shots(spec, contract_exists=False)  # must not raise


class TestGraphFewShotsRequiredWithContract:
    """validate_data_agent_text reports few-shot counts correctly (issue #10).
    Enforcement of the contract gate is in TestValidateGraphFewShots above."""

    def test_zero_graph_few_shots_reported(self):
        spec = _make_spec(
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name="o",
                    instructions="Interpret concepts.",
                    description="Ontology.",
                ),
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="Traverse edges.",
                    description="Graph.",
                    few_shots=None,
                ),
            ]
        )
        results = validate_data_agent_text(spec)
        graph_count = next(
            r for r in results if r.field == "graph.fewShots.count"
        )
        assert graph_count.actual == 0
        assert graph_count.limit == MAX_FEW_SHOT_COUNT


# ===========================================================================
# Issues #12, #13, #14 — Capability-aware validation (pre-implementation tests)
#
# All imports in these test classes are lazy (inside test methods) so that
# missing symbols produce test ERRORs rather than collection failures.
# Tests will be RED until Verbal's implementation lands.
# ===========================================================================

# ---------------------------------------------------------------------------
# Helper: build a DataAvailability with correct constraints
# ---------------------------------------------------------------------------

def _avail(
    semantic_id: str,
    status: str,
    observed_rows: int | None = None,
    required_rows: int = 0,
):
    """Build a DataAvailability with correct field constraints for testing."""
    from fabric_kg_builder.semantic.schemas import DataAvailability
    return DataAvailability(
        semantic_id=semantic_id,
        status=status,
        observed_rows=observed_rows,
        required_rows=required_rows,
    )


# ---------------------------------------------------------------------------
# #13 D3 — classify_relationship_availability: four states
# ---------------------------------------------------------------------------


class TestClassifyRelationshipAvailability:
    """Tests for knowledge.validation.classify_relationship_availability.

    Requirement: map (DataAvailability, required) → one of four Literal states.
    These states are DERIVED from DataAvailabilityStatus — no new enum members.
    """

    def test_sufficient_status_is_executable_nonempty(self):
        """status='sufficient' (observed_rows > 0) → 'executable_nonempty' regardless of required."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1)
        assert classify_relationship_availability(avail, required=True) == "executable_nonempty"
        assert classify_relationship_availability(avail, required=False) == "executable_nonempty"

    def test_not_observed_is_schema_supported_unobserved(self):
        """status='not_observed' → 'schema_supported_unobserved' regardless of required flag."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:warranty", "not_observed")
        assert classify_relationship_availability(avail, required=True) == "schema_supported_unobserved"
        assert classify_relationship_availability(avail, required=False) == "schema_supported_unobserved"

    def test_unavailable_required_is_required_absent(self):
        """status='unavailable' + required=True → 'required_absent'."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:warranty", "unavailable")
        assert classify_relationship_availability(avail, required=True) == "required_absent"

    def test_insufficient_required_is_required_absent(self):
        """status='insufficient' + required=True → 'required_absent'."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:warranty", "insufficient", observed_rows=0, required_rows=1)
        assert classify_relationship_availability(avail, required=True) == "required_absent"

    def test_unavailable_optional_is_optional_absent(self):
        """status='unavailable' + required=False → 'optional_absent'."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:warranty", "unavailable")
        assert classify_relationship_availability(avail, required=False) == "optional_absent"

    def test_insufficient_optional_is_optional_absent(self):
        """status='insufficient' + required=False → 'optional_absent'."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:warranty", "insufficient", observed_rows=0, required_rows=1)
        assert classify_relationship_availability(avail, required=False) == "optional_absent"

    def test_required_flag_controls_absent_category(self):
        """Same DataAvailability with unavailable status: required flag determines which absent category."""
        from fabric_kg_builder.knowledge.validation import classify_relationship_availability
        avail = _avail("relationship-type:warranty", "unavailable")
        required_result = classify_relationship_availability(avail, required=True)
        optional_result = classify_relationship_availability(avail, required=False)
        assert required_result == "required_absent"
        assert optional_result == "optional_absent"
        assert required_result != optional_result


# ---------------------------------------------------------------------------
# #12/#13/#14 D4 — New error code classes (ValidationError subclasses)
# ---------------------------------------------------------------------------


class TestNewCapabilityValidationErrors:
    """New structured error codes introduced by scope E.

    Each error class must be a ValidationError subclass with a .code attribute.
    """

    def test_data_agent_property_omitted_is_validation_error(self):
        """DATA_AGENT_PROPERTY_OMITTED must be a ValidationError with correct code."""
        from fabric_kg_builder.knowledge.validation import (
            DataAgentPropertyOmitted,
            ValidationError,
        )
        err = DataAgentPropertyOmitted(
            property_id="prop-serial-number",
            stage="published",
        )
        assert isinstance(err, ValidationError)
        assert err.code == "DATA_AGENT_PROPERTY_OMITTED"
        assert "prop-serial-number" in str(err)

    def test_data_agent_required_example_empty_structured_attrs(self):
        """DATA_AGENT_REQUIRED_EXAMPLE_EMPTY must carry competency_id, relationship_id,
        observed_rows, expected_minimum, and a non-empty remediation string."""
        from fabric_kg_builder.knowledge.validation import (
            DataAgentRequiredExampleEmpty,
            ValidationError,
        )
        err = DataAgentRequiredExampleEmpty(
            competency_id="asset-warranty",
            relationship_id="relationship-type:warranty",
            observed_rows=0,
            expected_minimum=1,
            remediation="Load warranty relationship data and re-run deploy-data-agent.",
        )
        assert isinstance(err, ValidationError)
        assert err.code == "DATA_AGENT_REQUIRED_EXAMPLE_EMPTY"
        assert err.competency_id == "asset-warranty"
        assert err.relationship_id == "relationship-type:warranty"
        assert err.observed_rows == 0
        assert err.expected_minimum == 1
        assert len(err.remediation) > 0
        assert "asset-warranty" in str(err)

    def test_data_agent_unavailable_relationship_claimed_structured(self):
        """DATA_AGENT_UNAVAILABLE_RELATIONSHIP_CLAIMED must carry relationship_id and context."""
        from fabric_kg_builder.knowledge.validation import (
            DataAgentUnavailableRelationshipClaimed,
            ValidationError,
        )
        err = DataAgentUnavailableRelationshipClaimed(
            relationship_id="relationship-type:warranty",
            context="build_graph_source_description",
        )
        assert isinstance(err, ValidationError)
        assert err.code == "DATA_AGENT_UNAVAILABLE_RELATIONSHIP_CLAIMED"
        assert err.relationship_id == "relationship-type:warranty"
        assert "relationship-type:warranty" in str(err)


# ---------------------------------------------------------------------------
# #13 D3 — CompetencyExampleReceipt model
# ---------------------------------------------------------------------------


class TestCompetencyExampleReceipt:
    """Tests for semantic.schemas.CompetencyExampleReceipt (new _StrictPersistedModel)."""

    def test_fields_present_and_accessible(self):
        """Receipt must expose all seven planned fields."""
        from fabric_kg_builder.semantic.schemas import CompetencyExampleReceipt
        r = CompetencyExampleReceipt(
            competency_id="asset-details",
            required=True,
            required_relationship_ids=["relationship-type:located_at"],
            observed_rows={"relationship-type:located_at": 5},
            min_required_rows=1,
            status="published",
            remediation="",
            published=True,
        )
        assert r.competency_id == "asset-details"
        assert r.required is True
        assert r.published is True
        assert r.observed_rows["relationship-type:located_at"] == 5
        assert r.min_required_rows == 1

    def test_blocked_receipt_not_published(self):
        """A blocked receipt has published=False and a non-empty remediation."""
        from fabric_kg_builder.semantic.schemas import CompetencyExampleReceipt
        r = CompetencyExampleReceipt(
            competency_id="asset-warranty",
            required=True,
            required_relationship_ids=["relationship-type:warranty"],
            observed_rows={"relationship-type:warranty": 0},
            min_required_rows=1,
            status="blocked",
            remediation="Load warranty data: compile → deploy-ontology → deploy-data-agent.",
            published=False,
        )
        assert r.published is False
        assert r.status == "blocked"
        assert len(r.remediation) > 0

    def test_optional_absent_receipt_not_published(self):
        """Optional-absent case: published=False but status is not 'blocked' (no error raised)."""
        from fabric_kg_builder.semantic.schemas import CompetencyExampleReceipt
        r = CompetencyExampleReceipt(
            competency_id="warranty-chain",
            required=False,
            required_relationship_ids=["relationship-type:warranty"],
            observed_rows={"relationship-type:warranty": 0},
            min_required_rows=1,
            status="omitted",
            remediation="",
            published=False,
        )
        assert r.published is False
        assert r.status == "omitted"


# ---------------------------------------------------------------------------
# #13 D6 — gate_competency_examples: four-state gating, backward compat
# ---------------------------------------------------------------------------


def _make_required_case(case_id: str, rel_id: str, question: str | None = None) -> dict:
    """Build a minimal required case for gate_competency_examples tests.

    gate_competency_examples reads ``expected.relationship_types`` (not probes).
    A plain string in relationship_types → requirement="required" by default.
    """
    return {
        "id": case_id,
        "question": question or f"Q about {case_id}?",
        "expected": {
            "relationship_types": [rel_id],  # str → required by _required_relationships
        },
    }


def _make_optional_case(case_id: str, rel_id: str) -> dict:
    """Build a minimal optional case for gate_competency_examples tests.

    Production gate_competency_examples reads ``routes.direct_graph`` to
    determine case_required.  ``"optional"`` marks the case as optional.
    """
    return {
        "id": case_id,
        "question": f"Q about {case_id}?",
        "routes": {"direct_graph": "optional"},
        "expected": {
            "relationship_types": [rel_id],
        },
    }


class TestGateCompetencyExamples:
    """Tests for knowledge.validation.gate_competency_examples.

    Requirement: pure function, invoked pre-flight (before Fabric mutation).
    Four states based on classify_relationship_availability:
      executable_nonempty  → published=True
      required_absent      → raises DATA_AGENT_REQUIRED_EXAMPLE_EMPTY
      optional_absent      → published=False (silently omitted)
      schema_supported_unobserved → treated as optional_absent (no rows confirmed)
    """

    def test_required_case_with_positive_rows_is_published(self):
        """Required case + observed rows ≥ min_required_rows → published=True."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        contract = {"cases": [_make_required_case("asset-details", "relationship-type:located_at")]}
        availability = {
            "relationship-type:located_at": _avail(
                "relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1
            )
        }
        receipts = gate_competency_examples(contract, availability, min_required_rows=1)
        assert len(receipts) >= 1
        assert any(r.competency_id == "asset-details" and r.published for r in receipts)

    def test_required_case_with_zero_rows_raises(self):
        """Required case + observed rows == 0 → raises DATA_AGENT_REQUIRED_EXAMPLE_EMPTY."""
        from fabric_kg_builder.knowledge.validation import (
            gate_competency_examples,
            DataAgentRequiredExampleEmpty,
        )
        contract = {"cases": [_make_required_case("asset-warranty", "relationship-type:warranty")]}
        availability = {"relationship-type:warranty": _avail("relationship-type:warranty", "unavailable")}
        with pytest.raises(DataAgentRequiredExampleEmpty) as exc_info:
            gate_competency_examples(contract, availability, min_required_rows=1)
        assert exc_info.value.code == "DATA_AGENT_REQUIRED_EXAMPLE_EMPTY"
        assert exc_info.value.competency_id == "asset-warranty"
        assert exc_info.value.observed_rows == 0
        assert exc_info.value.expected_minimum == 1

    def test_required_empty_has_actionable_remediation(self):
        """Required-empty error must carry a non-empty remediation string."""
        from fabric_kg_builder.knowledge.validation import (
            gate_competency_examples,
            DataAgentRequiredExampleEmpty,
        )
        contract = {"cases": [_make_required_case("asset-warranty", "relationship-type:warranty")]}
        availability = {"relationship-type:warranty": _avail("relationship-type:warranty", "unavailable")}
        with pytest.raises(DataAgentRequiredExampleEmpty) as exc_info:
            gate_competency_examples(contract, availability, min_required_rows=1)
        assert len(exc_info.value.remediation) > 0, (
            "DATA_AGENT_REQUIRED_EXAMPLE_EMPTY must carry actionable remediation text"
        )

    def test_optional_case_with_zero_rows_is_silently_omitted(self):
        """Optional case + zero rows → omitted (published=False), no exception raised."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        contract = {"cases": [_make_optional_case("warranty-chain", "relationship-type:warranty")]}
        availability = {
            "relationship-type:warranty": _avail("relationship-type:warranty", "unavailable")
        }
        receipts = gate_competency_examples(contract, availability, min_required_rows=1)
        # Optional-absent: all receipts for this case must be not-published, no exception
        warranty_receipts = [r for r in receipts if r.competency_id == "warranty-chain"]
        assert all(not r.published for r in warranty_receipts), (
            "Optional-absent case must not be published"
        )

    def test_optional_case_with_rows_is_published(self):
        """Optional case + positive rows → published=True."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        contract = {"cases": [_make_optional_case("asset-location", "relationship-type:located_at")]}
        availability = {
            "relationship-type:located_at": _avail(
                "relationship-type:located_at", "sufficient", observed_rows=3, required_rows=0
            )
        }
        receipts = gate_competency_examples(contract, availability, min_required_rows=1)
        location_receipts = [r for r in receipts if r.competency_id == "asset-location"]
        assert any(r.published for r in location_receipts), (
            "Optional case with positive rows must be published"
        )

    def test_no_contract_is_noop_backward_compat(self):
        """No contract (None) → empty receipts, no exception (backward compat)."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        assert gate_competency_examples(None, {}, min_required_rows=1) == []

    def test_empty_contract_is_noop(self):
        """Contract with no cases → empty receipts."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        assert gate_competency_examples({}, {}, min_required_rows=1) == []
        assert gate_competency_examples({"cases": []}, {}, min_required_rows=1) == []

    def test_unrelated_required_case_unaffected_by_unavailable_optional(self):
        """A required case with rows must still publish even when another optional case is unavailable."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        contract = {
            "cases": [
                _make_required_case("asset-details", "relationship-type:located_at"),
                _make_optional_case("warranty-chain", "relationship-type:warranty"),
            ]
        }
        availability = {
            "relationship-type:located_at": _avail(
                "relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1
            ),
            "relationship-type:warranty": _avail("relationship-type:warranty", "unavailable"),
        }
        receipts = gate_competency_examples(contract, availability, min_required_rows=1)
        published_ids = {r.competency_id for r in receipts if r.published}
        assert "asset-details" in published_ids, (
            "Required case with positive rows must remain published "
            "when unrelated optional case is unavailable"
        )
        warranty_published = {r.competency_id for r in receipts if r.published and r.competency_id == "warranty-chain"}
        assert not warranty_published, "Optional-absent case must not be published"

    def test_per_relationship_observed_rows_in_receipt(self):
        """Each receipt must carry observed_rows keyed by relationship semantic_id."""
        from fabric_kg_builder.knowledge.validation import gate_competency_examples
        rel_id = "relationship-type:located_at"
        contract = {"cases": [_make_required_case("asset-details", rel_id)]}
        availability = {rel_id: _avail(rel_id, "sufficient", observed_rows=7, required_rows=1)}
        receipts = gate_competency_examples(contract, availability, min_required_rows=1)
        published = [r for r in receipts if r.published and r.competency_id == "asset-details"]
        assert len(published) == 1
        assert published[0].observed_rows.get(rel_id) == 7, (
            "Receipt must carry observed_rows per relationship ID for #13 dry-run reporting"
        )


# ---------------------------------------------------------------------------
# #13 D3 — QueryReadiness.observed_relationship_rows field extension
# ---------------------------------------------------------------------------


class TestObservedRelationshipRowsQueryReadiness:
    """QueryReadiness must expose observed_relationship_rows: dict[str, int]
    so per-relationship counts survive into the projection receipt for #13 gating.

    Backward compat: existing receipts without this field must not fail validation.
    """

    def test_observed_relationship_rows_field_exists_and_readable(self):
        """QueryReadiness must accept and expose observed_relationship_rows."""
        from fabric_kg_builder.semantic.schemas import QueryReadiness
        qr = QueryReadiness(
            count_query_passed=True,
            typed_path_query_passed=True,
            nonzero_required_competencies=True,
            gql_node_count=100,
            gql_edge_count=50,
            canvas_visibility="visible",
            observed_relationship_rows={
                "relationship-type:located_at": 42,
                "relationship-type:warranty": 0,
            },
        )
        assert qr.observed_relationship_rows["relationship-type:located_at"] == 42
        assert qr.observed_relationship_rows["relationship-type:warranty"] == 0

    def test_observed_relationship_rows_defaults_to_empty_dict(self):
        """Backward compat: QueryReadiness without observed_relationship_rows defaults to {}."""
        from fabric_kg_builder.semantic.schemas import QueryReadiness
        qr = QueryReadiness(
            count_query_passed=True,
            typed_path_query_passed=True,
            nonzero_required_competencies=True,
            gql_node_count=10,
            gql_edge_count=5,
            canvas_visibility="visible",
        )
        rows = getattr(qr, "observed_relationship_rows", {})
        assert isinstance(rows, dict), (
            "observed_relationship_rows must default to an empty dict "
            "when not provided (backward compat)"
        )


# ---------------------------------------------------------------------------
# #14 D3 — AgentPublicationReceipt property count and hash field extensions
# ---------------------------------------------------------------------------

_DUMMY_HASH = "sha256:" + "a" * 64


def _base_receipt_kwargs() -> dict:
    """Minimal valid kwargs for AgentPublicationReceipt including the new #14 fields."""
    h = _DUMMY_HASH
    return dict(
        semantic_model_manifest_hash=h,
        persisted_projection_receipt_hash=h,
        ontology_persisted_projection_hash=h,
        graph_persisted_projection_hash=h,
        workspace_name="ws-name",
        workspace_id="ws-001",
        data_agent_name="My Agent",
        data_agent_item_id="item-001",
        target_mode="create",
        actions=["create", "publish"],
        selected_sources=[
            {
                "source_type": "graph",
                "source_name": "g",
                "workspace_id": "ws-001",
                "artifact_id": "art-001",
                "selected_element_count": 5,
                "property_child_count": 3,
            }
        ],
        package_instruction_hash=h,
        compiled_instruction_hash=h,
        draft_instruction_hash=h,
        published_instruction_hash=h,
        compiled_source_selection_hash=h,
        draft_source_selection_hash=h,
        published_source_selection_hash=h,
        compiled_selected_element_hash=h,
        published_selected_element_hash=h,
        agent_schema_sidecar_hash=h,
        property_child_coverage=1.0,
        publication_status="published",
        validated_at_utc="2026-07-23T12:00:00Z",
        # New #14 fields:
        required_property_count=10,
        compiled_property_count=10,
        draft_property_count=10,
        published_property_count=10,
        compiled_property_selection_hash="sha256:" + "b" * 64,
        published_property_selection_hash="sha256:" + "b" * 64,
        # New #12 fields:
        global_instruction_chars=1420,
        instruction_chars={"graph": 800, "ontology": 500},
        description_chars={"graph": 165, "ontology": 180},
    )


class TestAgentPublicationReceiptPropertyFields:
    """AgentPublicationReceipt must carry required/compiled/draft/published property counts,
    property selection hashes, and grounding text character counts (issues #14, #12)."""

    def test_required_compiled_draft_published_property_counts(self):
        """Receipt exposes four property count fields for three-way comparison."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        receipt = AgentPublicationReceipt.model_validate(_base_receipt_kwargs())
        assert receipt.required_property_count == 10
        assert receipt.compiled_property_count == 10
        assert receipt.draft_property_count == 10
        assert receipt.published_property_count == 10

    def test_property_selection_hashes_are_valid_sha256(self):
        """compiled_ and published_property_selection_hash must be valid sha256 strings."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        receipt = AgentPublicationReceipt.model_validate(_base_receipt_kwargs())
        assert receipt.compiled_property_selection_hash.startswith("sha256:")
        assert len(receipt.compiled_property_selection_hash) == len("sha256:") + 64
        assert receipt.published_property_selection_hash.startswith("sha256:")

    def test_grounding_text_char_count_fields(self):
        """Receipt carries global_instruction_chars, instruction_chars, description_chars for #12 reporting."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        receipt = AgentPublicationReceipt.model_validate(_base_receipt_kwargs())
        assert receipt.global_instruction_chars == 1420
        assert isinstance(receipt.instruction_chars, dict)
        assert receipt.instruction_chars["graph"] == 800
        assert receipt.instruction_chars["ontology"] == 500
        assert isinstance(receipt.description_chars, dict)
        assert receipt.description_chars["graph"] == 165


# ---------------------------------------------------------------------------
# #14 — Property omission cannot false-pass by comparing stripped snapshot to itself
# ---------------------------------------------------------------------------


class TestPropertyOmissionNotSelfReferential:
    """Guard against the specific false-pass in #14:

    The old code compared:
        published.property_child_count != expected.property_child_count
    where expected = stage_snapshot_from_spec(spec) built from the already-stripped
    public definition. If children were stripped at projection time, both sides are 0
    and the check passes silently (false-pass).

    The correct check compares:
        published.property_child_count != grounding.expected_property_child_count
    (the original semantic requirement, always > 0 when agent-visible properties exist).
    """

    def _make_grounding_with_n_properties(self, n_props: int = 3):
        """Build a PersistedAgentGrounding with n_props agent-visible property children."""
        from fabric_kg_builder.knowledge.data_agent import (
            DataSourceElement,
            ELEMENT_TYPE_NODE,
            ELEMENT_TYPE_PROPERTY,
        )
        from fabric_kg_builder.knowledge.agent_validation import (
            PersistedAgentGrounding,
            _canonical_hash,
        )
        children = [
            {
                "id": f"prop-{i:03d}",
                "display_name": f"prop_{i}",
                "type": ELEMENT_TYPE_PROPERTY,
                "is_selected": True,
                "data_type": "string",
                "description": f"Agent-visible property {i}.",
                "index_state": "indexed",
            }
            for i in range(n_props)
        ]
        el = DataSourceElement(
            id="node-asset",
            display_name="Asset",
            type=ELEMENT_TYPE_NODE,
            is_selected=True,
            description="Asset node.",
            children=children,
            index_state="indexed",
        )
        sidecar = {"schema_version": "1.1", "semantic_model_manifest_hash": ""}
        sidecar_hash = _canonical_hash(sidecar)
        elem_hash = _canonical_hash({"elements": [el.to_dict()]})
        return PersistedAgentGrounding(
            elements=(el,),
            sidecar=sidecar,
            sidecar_hash=sidecar_hash,
            selected_element_hash=elem_hash,
            property_child_coverage=1.0,
            expected_property_child_count=n_props,
        )

    def test_public_projection_must_not_strip_property_children(self):
        """After fix: build_public_graph_source_projection must preserve children (not set to None).

        Current code: replace(element, children=None) → strips all children → bug #14.
        Fixed code: projects children into Fabric-accepted shape → children preserved.
        """
        from fabric_kg_builder.knowledge.agent_validation import build_public_graph_source_projection
        grounding = self._make_grounding_with_n_properties(3)
        elements, _metadata = build_public_graph_source_projection(grounding)
        node_elements = [e for e in elements if e.type == "graph.nodeType"]
        any_children = any(
            e.children is not None and len(e.children) > 0
            for e in node_elements
        )
        assert any_children, (
            "build_public_graph_source_projection must preserve property children "
            "(currently strips them via replace(element, children=None) — fix required for #14)"
        )

    def test_stage_snapshot_property_child_count_nonzero_after_projection(self):
        """After fix: stage_snapshot_from_spec on the projected elements sees nonzero property_child_count.

        This count is then compared against grounding.expected_property_child_count.
        If children are stripped, both would be 0 → false-pass.
        """
        from fabric_kg_builder.knowledge.agent_validation import build_public_graph_source_projection
        from fabric_kg_builder.knowledge.data_agent import (
            DataAgentSpec,
            DataSourceSpec,
            stage_snapshot_from_spec,
        )
        grounding = self._make_grounding_with_n_properties(3)
        elements, metadata = build_public_graph_source_projection(grounding)
        spec = DataAgentSpec(
            display_name="Test Agent",
            instruction="Route to Graph.",
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="my-graph",
                    instructions="Use the graph.",
                    description="My graph.",
                    elements=list(elements),
                    metadata=metadata,
                    preview=True,
                )
            ],
        )
        snap = stage_snapshot_from_spec(spec)
        assert snap.property_child_count == 3, (
            f"Expected property_child_count==3 after fix, got {snap.property_child_count}. "
            "If children are stripped in projection, this would be 0 (false-pass scenario)."
        )

    def test_grounding_expected_count_exceeds_stripped_snapshot_count(self):
        """Documents the false-pass: when children are stripped the snapshot has 0 children
        while grounding.expected_property_child_count is non-zero.

        The receipt's property-omission check MUST compare against
        grounding.expected_property_child_count, NOT against snapshot.property_child_count.
        """
        from dataclasses import replace as dc_replace
        from fabric_kg_builder.knowledge.data_agent import (
            DataAgentSpec,
            DataSourceSpec,
            stage_snapshot_from_spec,
        )
        grounding = self._make_grounding_with_n_properties(3)
        # Simulate what the BUGGY code does: strip children in projection
        stripped_elements = tuple(
            dc_replace(e, children=None) for e in grounding.elements
        )
        spec_stripped = DataAgentSpec(
            display_name="Test Agent",
            instruction="Route.",
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="Use graph.",
                    description="Graph.",
                    elements=list(stripped_elements),
                    preview=True,
                )
            ],
        )
        snap = stage_snapshot_from_spec(spec_stripped)
        # Stripped snapshot has 0 children
        assert snap.property_child_count == 0
        # Grounding's original requirement is 3
        assert grounding.expected_property_child_count == 3
        # The false-pass: comparing snap to itself (0 != 0 == False → no error)
        self_comparison_passes = (snap.property_child_count == snap.property_child_count)
        assert self_comparison_passes, "Self-comparison always passes — this is the false-pass"
        # The correct comparison catches the omission:
        correct_comparison_catches = (snap.property_child_count != grounding.expected_property_child_count)
        assert correct_comparison_catches, (
            "Comparing published_count to grounding.expected_property_child_count "
            "correctly detects the omission (0 != 3)"
        )


# ===========================================================================
# McManus revision — Formal Blocker Regression Tests
# ===========================================================================


def _make_snapshot_with_property_ids(prop_ids: list[str]) -> DataAgentStageSnapshot:
    """Build a DataAgentStageSnapshot whose selected elements have the given property IDs."""
    from fabric_kg_builder.knowledge.data_agent import ELEMENT_TYPE_NODE, ELEMENT_TYPE_PROPERTY
    children = [
        {
            "id": pid,
            "display_name": pid,
            "type": ELEMENT_TYPE_PROPERTY,
            "is_selected": True,
            "data_type": "string",
            "description": f"Property {pid}.",
            "index_state": "indexed",
        }
        for pid in prop_ids
    ]
    sources = (
        {
            "type": "graph",
            "workspaceId": "ws-001",
            "artifactId": "art-001",
            "displayName": "Graph",
            "elements": [
                {
                    "id": "node-asset",
                    "display_name": "Asset",
                    "type": ELEMENT_TYPE_NODE,
                    "is_selected": True,
                    "children": children,
                }
            ],
        },
    )
    return DataAgentStageSnapshot(
        stage="published",
        instruction="Route.",
        sources=sources,
    )


class TestPropertySelectionHashContentBased:
    """#14 (McManus): property selection hashes must be content-based (IDs not count).

    Equal-count different selections must produce different hashes so a swap of
    properties with the same cardinality is detected at audit time.
    """

    def test_selected_property_ids_returns_sorted_ids(self):
        snap = _make_snapshot_with_property_ids(["prop-z", "prop-a", "prop-m"])
        assert snap.selected_property_ids == ["prop-a", "prop-m", "prop-z"]

    def test_selected_property_ids_empty_when_no_children(self):
        snap = DataAgentStageSnapshot(
            stage="draft",
            instruction="x",
            sources=({"type": "graph", "elements": [{"id": "n", "is_selected": True}]},),
        )
        assert snap.selected_property_ids == []

    def test_equal_count_different_ids_produce_different_hashes(self):
        """Two selections with the same number but different property IDs must differ."""
        from fabric_kg_builder.knowledge.data_agent import _canonical_hash
        snap_a = _make_snapshot_with_property_ids(["prop-x", "prop-y"])
        snap_b = _make_snapshot_with_property_ids(["prop-a", "prop-b"])
        assert snap_a.property_child_count == snap_b.property_child_count == 2
        hash_a = _canonical_hash({"property_ids": snap_a.selected_property_ids})
        hash_b = _canonical_hash({"property_ids": snap_b.selected_property_ids})
        assert hash_a != hash_b, (
            "Selections with identical count but different IDs must produce different "
            "hashes. Count-only hashing is the bug being fixed."
        )

    def test_same_ids_produce_same_hash(self):
        """Same property IDs always hash the same regardless of construction order."""
        from fabric_kg_builder.knowledge.data_agent import _canonical_hash
        snap1 = _make_snapshot_with_property_ids(["prop-x", "prop-y"])
        snap2 = _make_snapshot_with_property_ids(["prop-y", "prop-x"])
        hash1 = _canonical_hash({"property_ids": snap1.selected_property_ids})
        hash2 = _canonical_hash({"property_ids": snap2.selected_property_ids})
        assert hash1 == hash2, (
            "Canonical ordering must make hash independent of insertion order."
        )

    def test_hash_result_is_valid_sha256(self):
        from fabric_kg_builder.knowledge.data_agent import _canonical_hash
        snap = _make_snapshot_with_property_ids(["prop-a", "prop-b"])
        h = _canonical_hash({"property_ids": snap.selected_property_ids})
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64


class TestCompiledPropertyOmissionBlocks:
    """#14 (McManus): compiled property count must be validated against the original
    semantic requirement *before* draft/published checks.  A compiled omission should
    raise AgentPublicationError(DATA_AGENT_PROPERTY_OMITTED) at the earliest boundary.
    """

    def _make_grounding_n(self, n: int):
        """Build a PersistedAgentGrounding requiring exactly n properties."""
        from fabric_kg_builder.knowledge.data_agent import (
            DataSourceElement,
            ELEMENT_TYPE_NODE,
            ELEMENT_TYPE_PROPERTY,
        )
        from fabric_kg_builder.knowledge.agent_validation import (
            PersistedAgentGrounding,
            _canonical_hash,
        )
        children = [
            {
                "id": f"p{i}",
                "display_name": f"p{i}",
                "type": ELEMENT_TYPE_PROPERTY,
                "is_selected": True,
                "data_type": "string",
                "description": "",
                "index_state": "indexed",
            }
            for i in range(n)
        ]
        el = DataSourceElement(
            id="node-a",
            display_name="A",
            type=ELEMENT_TYPE_NODE,
            is_selected=True,
            children=children,
        )
        sidecar = {"schema_version": "1.1", "semantic_model_manifest_hash": "sha256:" + "0" * 64}
        sidecar_hash = _canonical_hash(sidecar)
        return PersistedAgentGrounding(
            elements=(el,),
            sidecar=sidecar,
            sidecar_hash=sidecar_hash,
            selected_element_hash=_canonical_hash({"elements": [el.to_dict()]}),
            property_child_coverage=1.0,
            expected_property_child_count=n,
        )

    def _make_spec_with_n_properties(self, n: int, grounding):
        """Build a DataAgentSpec with exactly n property children."""
        from fabric_kg_builder.knowledge.data_agent import (
            DataAgentSpec,
            DataSourceSpec,
            DataSourceElement,
            ELEMENT_TYPE_NODE,
            ELEMENT_TYPE_PROPERTY,
        )
        children = [
            {
                "id": f"p{i}",
                "display_name": f"p{i}",
                "type": ELEMENT_TYPE_PROPERTY,
                "is_selected": True,
                "data_type": "string",
                "description": "",
                "index_state": "indexed",
            }
            for i in range(n)
        ]
        el = DataSourceElement(
            id="node-a",
            display_name="A",
            type=ELEMENT_TYPE_NODE,
            is_selected=True,
            children=children,
        )
        return DataAgentSpec(
            display_name="Agent",
            instruction="Route.",
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="Use graph.",
                    description="Graph.",
                    elements=[el],
                    metadata={"fabricKgAgentSchema": grounding.sidecar},
                    preview=True,
                )
            ],
        )

    def test_compiled_omission_raises_property_omitted(self):
        """compiled_prop_count < required_prop_count must raise AgentPublicationError
        with code DATA_AGENT_PROPERTY_OMITTED before any draft/published check."""
        from fabric_kg_builder.knowledge.agent_validation import (
            AgentPublicationError,
            build_agent_publication_receipt,
        )
        from fabric_kg_builder.knowledge.data_agent import (
            DataAgentStageSnapshot,
            stage_snapshot_from_spec,
        )
        from unittest.mock import MagicMock

        grounding = self._make_grounding_n(3)
        spec_short = self._make_spec_with_n_properties(2, grounding)
        expected = stage_snapshot_from_spec(spec_short)
        assert expected.property_child_count == 2
        assert grounding.expected_property_child_count == 3

        draft = DataAgentStageSnapshot(
            stage="draft",
            instruction="Route.",
            sources=expected.sources,
        )
        published = DataAgentStageSnapshot(
            stage="published",
            instruction="Route.",
            sources=expected.sources,
        )

        projection_receipt = MagicMock()
        projection_receipt.semantic_model_manifest_hash = grounding.sidecar.get(
            "semantic_model_manifest_hash", ""
        )
        projection_receipt.graph_model_id = "gm-001"

        with pytest.raises(AgentPublicationError) as exc_info:
            build_agent_publication_receipt(
                target_mode="create",
                configured_target_item_id=None,
                workspace_name="ws",
                workspace_id="ws-001",
                data_agent_name="Agent",
                data_agent_item_id="item-001",
                package_instruction_hash=expected.instruction_hash,
                expected=expected,
                draft=draft,
                published=published,
                grounding=grounding,
                projection_receipt=projection_receipt,
                projection_receipt_hash="sha256:" + "e" * 64,
                publication_status="published",
            )
        assert exc_info.value.code == "DATA_AGENT_PROPERTY_OMITTED"
        assert "2" in str(exc_info.value)  # compiled count in message
        assert "3" in str(exc_info.value)  # required count in message

    def test_compiled_count_equals_required_passes_this_check(self):
        """When compiled_prop_count == required_prop_count the compiled check passes."""
        from fabric_kg_builder.knowledge.data_agent import stage_snapshot_from_spec
        grounding = self._make_grounding_n(2)
        spec = self._make_spec_with_n_properties(2, grounding)
        snap = stage_snapshot_from_spec(spec)
        assert snap.property_child_count == 2
        assert grounding.expected_property_child_count == 2
        # The compiled check: compiled == required → no DATA_AGENT_PROPERTY_OMITTED at this step.
        assert snap.property_child_count == grounding.expected_property_child_count


class TestRequiredExampleEmptyBoundaryClassification:
    """Blocker 1 (McManus): DataAgentRequiredExampleEmpty must NOT be a subclass of
    OSError or ValueError — the grounding try block's catch-all would swallow it,
    hiding it from the operator.  It must be caught by its own specific clause so
    the ClickException / BuildDeployError surfaces the structured message.
    """

    def test_error_importable_from_validation_module(self):
        from fabric_kg_builder.knowledge.validation import DataAgentRequiredExampleEmpty
        assert issubclass(DataAgentRequiredExampleEmpty, Exception)

    def test_not_subclass_of_os_error(self):
        """Must not be caught accidentally by except (OSError, ...) in grounding block."""
        from fabric_kg_builder.knowledge.validation import DataAgentRequiredExampleEmpty
        assert not issubclass(DataAgentRequiredExampleEmpty, OSError)

    def test_not_subclass_of_value_error(self):
        """Must not be caught accidentally by except (ValueError, ...) in grounding block."""
        from fabric_kg_builder.knowledge.validation import DataAgentRequiredExampleEmpty
        assert not issubclass(DataAgentRequiredExampleEmpty, ValueError)

    def test_str_representation_is_actionable(self):
        """str(exc) produces an operator-readable message (no raw traceback lines)."""
        from fabric_kg_builder.knowledge.validation import DataAgentRequiredExampleEmpty
        exc = DataAgentRequiredExampleEmpty(
            competency_id="asset-warranty",
            relationship_id="warranty:covered_by",
            observed_rows=0,
            expected_minimum=1,
        )
        msg = str(exc)
        assert "DATA_AGENT_REQUIRED_EXAMPLE_EMPTY" in msg
        assert "asset-warranty" in msg
        assert "warranty:covered_by" in msg
        assert "≥1" in msg or "1" in msg

    def test_attributes_carry_structured_data(self):
        """Error must carry structured attributes for programmatic use."""
        from fabric_kg_builder.knowledge.validation import DataAgentRequiredExampleEmpty
        exc = DataAgentRequiredExampleEmpty(
            competency_id="c1",
            relationship_id="r1",
            observed_rows=0,
            expected_minimum=1,
        )
        assert exc.competency_id == "c1"
        assert exc.relationship_id == "r1"
        assert exc.observed_rows == 0
        assert exc.expected_minimum == 1


class TestGraphSourceAvailabilityWiring:
    """Blocker 2 (McManus): build_graph_source_instructions and build_graph_source_description
    must accept and use the availability kwarg so deployed text is capability-aware and
    never over-claims an unobserved relationship.
    """

    def _ctx(self) -> dict:
        return {
            "contract_name": "MyContract",
            "contract_hash": "sha256:" + "a" * 64,
            "contract_description": "Test domain.",
        }

    def _make_avail(self, status: str):
        """Build a minimal DataAvailability-like object with the given status."""
        from fabric_kg_builder.semantic.schemas import DataAvailability
        if status == "sufficient":
            return DataAvailability(
                semantic_id="warranty:covered_by",
                status="sufficient",
                observed_rows=10,
                required_rows=1,
            )
        elif status == "insufficient":
            return DataAvailability(
                semantic_id="warranty:covered_by",
                status="insufficient",
                observed_rows=0,
                required_rows=1,
            )
        else:  # unavailable or not_observed
            return DataAvailability(
                semantic_id="warranty:covered_by",
                status=status,
                required_rows=1,
            )

    def test_instructions_accept_availability_kwarg(self):
        from fabric_kg_builder.semantic.instructions import build_graph_source_instructions
        result = build_graph_source_instructions(self._ctx(), availability=None)
        assert isinstance(result, str)

    def test_instructions_with_unavailable_rel_names_it(self):
        from fabric_kg_builder.semantic.instructions import build_graph_source_instructions
        avail = {"warranty:covered_by": self._make_avail("unavailable")}
        result = build_graph_source_instructions(self._ctx(), availability=avail)
        assert "warranty:covered_by" in result
        assert "not claimed" in result or "not be claimed" in result or "no currently observed" in result

    def test_description_accepts_availability_kwarg(self):
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        result = build_graph_source_description(self._ctx(), availability=None)
        assert isinstance(result, str)

    def test_description_without_availability_is_generic(self):
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        result = build_graph_source_description(self._ctx(), availability=None)
        assert "directed Graph" in result

    def test_description_with_available_rel_names_it(self):
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        avail = {"warranty:covered_by": self._make_avail("sufficient")}
        result = build_graph_source_description(self._ctx(), availability=avail)
        assert "covered_by" in result

    def test_description_with_only_unavailable_rels_notes_no_data(self):
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        avail = {"warranty:covered_by": self._make_avail("unavailable")}
        result = build_graph_source_description(self._ctx(), availability=avail)
        assert "no verified published data" in result or "no relationship paths" in result.lower() or "unavailable" in result.lower()
