"""Tests for issues #9 and #10 — contract-driven source policy and text limits.

Covers:
- SourcePolicy construction and overlap guard
- validate_source_policy: closed-world enforcement (required/prohibited/extra)
- validate_published_source_policy: published snapshot closed-world enforcement
- Named text limit constants
- validate_data_agent_text: pass/fail per field, few-shot count and payload
- validate_instruction_deduplication: exact and near-identical detection
- validate_graph_few_shots: required when compiled competency contract exists
- Non-empty required fields
- deploy/data_agent.py fix: source-specific instructions, no duplication
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
        assert MAX_FEW_SHOT_COUNT == 5

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

