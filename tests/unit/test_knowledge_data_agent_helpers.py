"""Tests for knowledge/data_agent.py — pure helpers, data models,
and capability-aware few-shot gating (issues #13).

Covers (original):
- Exception classes: LROTimeoutError, UnsupportedDataSourceType
- DataSourceElement: to_dict, optional fields, None exclusion
- FewShotExample: basic construction
- Hashing helpers: _canonical_hash, _text_hash — deterministic
- _encode_part / _decode_part_payload: round-trip, error cases
- Element normalization: _selected_children, _normalized_data_source_element, _selected_elements
- DataAgentUpsertResult, DataAgentPublishResult, DataAgentStageSnapshot
- build_definition_parts: returns list, correct keys, valid base64-JSON

Covers (new #13):
- graph_few_shots_from_competency_contract: observed row gating
  (after Verbal wires gate_competency_examples, only cases with observed rows pass)
- Property children in DataSourceElement: children preserved in to_dict()
- Deterministic property selection hash via _canonical_hash
"""
from __future__ import annotations

import base64
import json

import pytest

from fabric_kg_builder.knowledge.data_agent import (
    DataAgentDefinitionError,
    DataAgentPublishResult,
    DataAgentStageSnapshot,
    DataAgentTargetError,
    DataAgentUpsertResult,
    DataSourceElement,
    FewShotExample,
    LROTimeoutError,
    UnsupportedDataSourceType,
    _canonical_hash,
    _decode_part_payload,
    _encode_part,
    _normalized_data_source_element,
    _normalized_source_selection,
    _selected_children,
    _selected_elements,
    _text_hash,
    build_definition_parts,
    decode_stage_snapshot,
    graph_few_shots_from_competency_contract,
    stage_snapshot_from_spec,
)

# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class TestLROTimeoutError:
    def test_attributes(self):
        err = LROTimeoutError("https://api.example.com/lro", 30.5)
        assert err.operation_url == "https://api.example.com/lro"
        assert err.elapsed_seconds == 30.5
        assert "30.5s" in str(err)
        assert "api.example.com" in str(err)

    def test_is_exception(self):
        with pytest.raises(LROTimeoutError):
            raise LROTimeoutError("url", 5.0)


class TestUnsupportedDataSourceType:
    def test_attributes(self):
        err = UnsupportedDataSourceType("custom_type")
        assert err.source_type == "custom_type"
        assert "custom_type" in str(err)

    def test_is_exception(self):
        with pytest.raises(UnsupportedDataSourceType):
            raise UnsupportedDataSourceType("xyz")


# ---------------------------------------------------------------------------
# DataSourceElement
# ---------------------------------------------------------------------------


class TestDataSourceElement:
    def test_to_dict_basic(self):
        el = DataSourceElement(
            id="el-001",
            display_name="Person",
            type="graph.nodeType",
        )
        d = el.to_dict()
        assert d["id"] == "el-001"
        assert d["display_name"] == "Person"
        assert d["type"] == "graph.nodeType"
        assert d["is_selected"] is False
        assert "description" not in d

    def test_to_dict_with_optional_fields(self):
        el = DataSourceElement(
            id="el-002",
            display_name="Employs",
            type="graph.edgeType",
            is_selected=True,
            description="Employment relationship",
            data_type="edge",
            index_state="indexed",
            children=[{"id": "child-1"}],
        )
        d = el.to_dict()
        assert d["is_selected"] is True
        assert d["description"] == "Employment relationship"
        assert d["data_type"] == "edge"
        assert d["index_state"] == "indexed"
        assert d["children"] == [{"id": "child-1"}]

    def test_to_dict_none_fields_excluded(self):
        el = DataSourceElement(id="el-003", display_name="X", type="node", is_selected=False)
        d = el.to_dict()
        for optional_key in ("data_type", "description", "children", "index_state"):
            assert optional_key not in d


# ---------------------------------------------------------------------------
# FewShotExample
# ---------------------------------------------------------------------------


class TestFewShotExample:
    def test_basic(self):
        import uuid as _uuid
        fs = FewShotExample(id=str(_uuid.uuid4()), question="Who owns asset X?", query="MATCH (n) RETURN n")
        assert fs.question == "Who owns asset X?"
        assert fs.query == "MATCH (n) RETURN n"


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


class TestCanonicalHash:
    def test_returns_sha256_prefix(self):
        h = _canonical_hash({"a": 1})
        assert h.startswith("sha256:")

    def test_deterministic(self):
        h1 = _canonical_hash({"key": "value", "n": 42})
        h2 = _canonical_hash({"n": 42, "key": "value"})  # different key order
        assert h1 == h2  # sort_keys=True

    def test_different_values_different_hashes(self):
        h1 = _canonical_hash({"a": 1})
        h2 = _canonical_hash({"a": 2})
        assert h1 != h2

    def test_works_with_list(self):
        h = _canonical_hash([1, 2, 3])
        assert h.startswith("sha256:")


class TestTextHash:
    def test_returns_sha256_prefix(self):
        h = _text_hash("hello")
        assert h.startswith("sha256:")

    def test_deterministic(self):
        assert _text_hash("hello") == _text_hash("hello")

    def test_different_text_different_hash(self):
        assert _text_hash("hello") != _text_hash("world")


# ---------------------------------------------------------------------------
# _encode_part / _decode_part_payload
# ---------------------------------------------------------------------------


class TestEncodePart:
    def test_produces_inline_base64(self):
        part = _encode_part("Files/Config/data_agent.json", {"key": "value"})
        assert part["path"] == "Files/Config/data_agent.json"
        assert part["payloadType"] == "InlineBase64"
        # Decode and verify
        decoded = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
        assert decoded["key"] == "value"

    def test_round_trip(self):
        payload = {"instruction": "Answer user questions.", "sources": []}
        part = _encode_part("some/path.json", payload)
        recovered = _decode_part_payload(part)
        assert recovered["instruction"] == payload["instruction"]


class TestDecodePartPayload:
    def test_wrong_payload_type_raises(self):
        part = {"path": "some/path.json", "payloadType": "Raw", "payload": "data"}
        with pytest.raises(DataAgentDefinitionError, match="InlineBase64"):
            _decode_part_payload(part)

    def test_invalid_base64_raises(self):
        part = {"path": "some/path.json", "payloadType": "InlineBase64", "payload": "!!!"}
        with pytest.raises(DataAgentDefinitionError):
            _decode_part_payload(part)

    def test_non_json_raises(self):
        raw = base64.b64encode(b"not-json").decode("ascii")
        part = {"path": "p", "payloadType": "InlineBase64", "payload": raw}
        with pytest.raises(DataAgentDefinitionError):
            _decode_part_payload(part)


# ---------------------------------------------------------------------------
# Element normalization helpers
# ---------------------------------------------------------------------------


class TestSelectedChildren:
    def test_empty_children(self):
        assert _selected_children({}) == []
        assert _selected_children({"children": None}) == []

    def test_returns_only_selected(self):
        element = {
            "children": [
                {"id": "c1", "is_selected": True, "display_name": "C1", "type": "t"},
                {"id": "c2", "is_selected": False, "display_name": "C2", "type": "t"},
            ]
        }
        result = _selected_children(element)
        assert len(result) == 1
        assert result[0]["id"] == "c1"


class TestNormalizedDataSourceElement:
    def test_basic_normalization(self):
        element = {
            "id": "el-001",
            "display_name": "Person",
            "type": "graph.nodeType",
            "is_selected": True,
            "description": "A person entity",
        }
        result = _normalized_data_source_element(element)
        assert result["id"] == "el-001"
        assert result["is_selected"] is True
        assert result["description"] == "A person entity"
        assert "children" not in result

    def test_missing_fields_default_to_empty_string(self):
        result = _normalized_data_source_element({})
        assert result["id"] == ""
        assert result["display_name"] == ""
        assert result["type"] == ""


class TestSelectedElements:
    def test_empty_source(self):
        assert _selected_elements({}) == []

    def test_returns_only_selected(self):
        source = {
            "elements": [
                {"id": "e1", "is_selected": True, "display_name": "E1", "type": "t"},
                {"id": "e2", "is_selected": False, "display_name": "E2", "type": "t"},
            ]
        }
        result = _selected_elements(source)
        assert len(result) == 1
        assert result[0]["id"] == "e1"

    def test_sorted_by_id(self):
        source = {
            "elements": [
                {"id": "b", "is_selected": True, "display_name": "B", "type": "t"},
                {"id": "a", "is_selected": True, "display_name": "A", "type": "t"},
            ]
        }
        result = _selected_elements(source)
        assert result[0]["id"] == "a"


class TestNormalizedSourceSelection:
    def test_basic(self):
        source = {
            "type": "ontology",
            "workspaceId": "ws-001",
            "artifactId": "art-001",
            "displayName": "My Ontology",
            "metadata": {},
            "elements": [],
        }
        result = _normalized_source_selection(source)
        assert result["source_type"] == "ontology"
        assert result["workspace_id"] == "ws-001"
        assert result["artifact_id"] == "art-001"
        assert result["display_name"] == "My Ontology"
        assert result["elements"] == []

    def test_missing_fields_default_to_empty(self):
        result = _normalized_source_selection({})
        assert result["source_type"] == ""
        assert result["workspace_id"] == ""
        assert result["elements"] == []


# ---------------------------------------------------------------------------
# DataAgentUpsertResult
# ---------------------------------------------------------------------------


class TestDataAgentUpsertResult:
    def test_fields(self):
        r = DataAgentUpsertResult(
            item_id="item-001",
            created=True,
            status="created-201",
            display_name="My Agent",
            note="OK",
        )
        assert r.item_id == "item-001"
        assert r.created is True
        assert r.status == "created-201"
        assert r.display_name == "My Agent"

    def test_default_note(self):
        r = DataAgentUpsertResult(
            item_id="item-001",
            created=False,
            status="updated",
            display_name="Agent",
        )
        assert r.note == ""


# ---------------------------------------------------------------------------
# DataAgentPublishResult
# ---------------------------------------------------------------------------


class TestDataAgentPublishResult:
    def test_fields(self):
        r = DataAgentPublishResult(
            item_id="item-001",
            published_description="Published OK",
        )
        assert r.item_id == "item-001"
        assert r.status == "published"


# ---------------------------------------------------------------------------
# DataAgentStageSnapshot
# ---------------------------------------------------------------------------


def _make_inline_b64(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    return base64.b64encode(raw.encode()).decode("ascii")


class TestDataAgentStageSnapshot:
    def test_instruction_hash(self):
        snap = DataAgentStageSnapshot(
            stage="draft",
            instruction="Answer user questions.",
            sources=(),
        )
        h = snap.instruction_hash
        assert h.startswith("sha256:")

    def test_selection_hash_empty(self):
        snap = DataAgentStageSnapshot(stage="draft", instruction="x", sources=())
        h = snap.source_selection_hash
        assert h.startswith("sha256:")

    def test_selection_hash_with_sources(self):
        snap = DataAgentStageSnapshot(
            stage="draft",
            instruction="x",
            sources=({"type": "ontology", "workspaceId": "ws-1"},),
        )
        h = snap.source_selection_hash
        assert h.startswith("sha256:")


# ---------------------------------------------------------------------------
# build_definition_parts
# ---------------------------------------------------------------------------


class TestBuildDefinitionParts:
    def _make_spec(self, **kwargs):
        from fabric_kg_builder.knowledge.data_agent import DataAgentSpec
        return DataAgentSpec(
            display_name="Test Agent",
            instruction="Answer questions about the knowledge graph.",
            **kwargs,
        )

    def test_returns_list_of_parts(self):
        spec = self._make_spec()
        parts = build_definition_parts(spec)
        assert isinstance(parts, list)
        assert len(parts) >= 1

    def test_parts_have_required_keys(self):
        spec = self._make_spec()
        parts = build_definition_parts(spec)
        for part in parts:
            assert "path" in part
            assert "payload" in part
            assert "payloadType" in part

    def test_parts_are_valid_base64_json(self):
        spec = self._make_spec()
        parts = build_definition_parts(spec)
        for part in parts:
            raw = base64.b64decode(part["payload"])
            data = json.loads(raw)
            assert isinstance(data, dict)


# ===========================================================================
# Issue #13 — graph_few_shots_from_competency_contract: observed row gating
# (pre-implementation tests — fail RED until Verbal wires the gate)
# ===========================================================================


class TestGraphFewShotsObservedRowGating:
    """After Verbal wires gate_competency_examples into graph_few_shots_from_competency_contract:

    - Required cases with observed rows ≥ 1 → included
    - Required cases with observed rows == 0 → raises DATA_AGENT_REQUIRED_EXAMPLE_EMPTY
    - Optional cases with observed rows == 0 → silently omitted
    - Unrelated required cases with rows are unaffected by unavailable optional cases

    The gate function uses DataAvailability instances passed alongside the contract.
    """

    def _contract_with_required_case(self, rel_id: str, observed_rows: int) -> tuple[dict, dict]:
        """Build a contract + availability dict pair for a single required case.

        The contract includes both ``expected.relationship_types`` (consumed by
        ``gate_competency_examples``) and ``probes.direct_graph`` (consumed by
        the example-extraction pass in ``graph_few_shots_from_competency_contract``).
        Availability is a dict keyed by semantic_id, as gate_competency_examples requires.
        """
        contract = {
            "cases": [
                {
                    "id": "test-case",
                    "question": "Where is Chiller 1?",
                    "expected": {
                        "relationship_types": [rel_id],  # str → required by default
                    },
                    "probes": {
                        "direct_graph": {
                            "query": f"MATCH (a:`Asset`)-[:`{rel_id}`]->(b) RETURN a, b LIMIT 10",
                            "static_validation_passed": True,
                        }
                    },
                }
            ]
        }
        from fabric_kg_builder.semantic.schemas import DataAvailability
        if observed_rows > 0:
            avail = DataAvailability(
                semantic_id=rel_id,
                status="sufficient",
                observed_rows=observed_rows,
                required_rows=1,
            )
        else:
            avail = DataAvailability(semantic_id=rel_id, status="unavailable")
        return contract, {rel_id: avail}

    def _contract_with_optional_case(self, rel_id: str, observed_rows: int) -> tuple[dict, dict]:
        """Build a contract + availability dict pair for a single optional case.

        Production gate_competency_examples reads ``routes.direct_graph`` to
        determine case_required.  ``"optional"`` marks the case as optional so
        zero-row cases are silently omitted rather than raising.
        """
        contract = {
            "cases": [
                {
                    "id": "optional-case",
                    "question": "What warranty covers Asset X?",
                    "routes": {"direct_graph": "optional"},
                    "expected": {
                        "relationship_types": [rel_id],
                    },
                    "probes": {
                        "direct_graph": {
                            "query": f"MATCH (a:`Asset`)-[:`{rel_id}`]->(w) RETURN a, w LIMIT 10",
                            "static_validation_passed": True,
                        }
                    },
                }
            ]
        }
        from fabric_kg_builder.semantic.schemas import DataAvailability
        if observed_rows > 0:
            avail = DataAvailability(
                semantic_id=rel_id,
                status="sufficient",
                observed_rows=observed_rows,
                required_rows=0,
            )
        else:
            avail = DataAvailability(semantic_id=rel_id, status="unavailable")
        return contract, {rel_id: avail}

    def test_required_case_with_positive_rows_is_included(self):
        """Required case + observed_rows=5 → example included (published)."""
        contract, availability = self._contract_with_required_case(
            "relationship-type:located_at", observed_rows=5
        )
        # After Verbal's fix, graph_few_shots_from_competency_contract accepts availability
        examples = graph_few_shots_from_competency_contract(
            contract, availability=availability
        )
        assert len(examples) == 1
        assert examples[0].question == "Where is Chiller 1?"

    def test_required_case_with_zero_rows_raises_required_example_empty(self):
        """Required case + observed_rows=0 → raises DATA_AGENT_REQUIRED_EXAMPLE_EMPTY pre-flight."""
        from fabric_kg_builder.knowledge.validation import DataAgentRequiredExampleEmpty
        contract, availability = self._contract_with_required_case(
            "relationship-type:warranty", observed_rows=0
        )
        with pytest.raises(DataAgentRequiredExampleEmpty) as exc_info:
            graph_few_shots_from_competency_contract(contract, availability=availability)
        assert exc_info.value.code == "DATA_AGENT_REQUIRED_EXAMPLE_EMPTY"
        assert exc_info.value.observed_rows == 0

    def test_optional_case_with_zero_rows_is_silently_omitted(self):
        """Optional case + zero rows → omitted (empty examples list), no exception."""
        contract, availability = self._contract_with_optional_case(
            "relationship-type:warranty", observed_rows=0
        )
        examples = graph_few_shots_from_competency_contract(
            contract, availability=availability
        )
        assert len(examples) == 0, (
            "Optional-absent cases must be silently omitted from few-shot examples"
        )

    def test_unrelated_required_case_unaffected_by_unavailable_optional(self):
        """An unrelated required case with rows is unaffected by an unavailable optional case."""
        from fabric_kg_builder.semantic.schemas import DataAvailability
        contract = {
            "cases": [
                {
                    "id": "location-case",
                    "question": "Where is Asset X?",
                    "expected": {
                        "relationship_types": ["relationship-type:located_at"],
                    },
                    "probes": {
                        "direct_graph": {
                            "query": "MATCH (a:`Asset`)-[:`located_at`]->(l) RETURN a, l LIMIT 10",
                            "static_validation_passed": True,
                        }
                    },
                },
                {
                    "id": "warranty-case",
                    "question": "What warranty covers Asset X?",
                    "routes": {"direct_graph": "optional"},
                    "expected": {
                        "relationship_types": [
                            {"semantic_id": "relationship-type:warranty", "requirement": "optional"},
                        ],
                    },
                    "probes": {
                        "direct_graph": {
                            "query": "MATCH (a:`Asset`)-[:`covered_by`]->(w) RETURN a, w LIMIT 10",
                            "static_validation_passed": True,
                        }
                    },
                },
            ]
        }
        availability = {
            "relationship-type:located_at": DataAvailability(
                semantic_id="relationship-type:located_at",
                status="sufficient",
                observed_rows=7,
                required_rows=1,
            ),
            "relationship-type:warranty": DataAvailability(
                semantic_id="relationship-type:warranty", status="unavailable"
            ),
        }
        examples = graph_few_shots_from_competency_contract(
            contract, availability=availability
        )
        questions = [ex.question for ex in examples]
        assert "Where is Asset X?" in questions, (
            "Required case with positive rows must be included "
            "even when an unrelated optional case is unavailable"
        )
        assert "What warranty covers Asset X?" not in questions, (
            "Optional-absent case must not be included"
        )

    def test_no_availability_param_preserves_existing_behavior(self):
        """Without availability parameter: existing static_validation_passed behavior unchanged."""
        contract = {
            "cases": [
                {
                    "id": "location",
                    "question": "Where is Chiller 1?",
                    "probes": {
                        "direct_graph": {
                            "query": "MATCH (a:`Asset`)-[:`located_at`]->(l) RETURN a, l LIMIT 10",
                            "static_validation_passed": True,
                        }
                    },
                },
            ]
        }
        # No availability param — backward compat: existing validation still runs
        examples = graph_few_shots_from_competency_contract(contract)
        assert len(examples) == 1, (
            "Without availability parameter, existing static_validation_passed "
            "filtering must still work"
        )


# ---------------------------------------------------------------------------
# Property children in DataSourceElement (for #14 projection testing)
# ---------------------------------------------------------------------------


class TestDataSourceElementPropertyChildren:
    """DataSourceElement must preserve property children through to_dict()."""

    def test_to_dict_includes_children_when_set(self):
        """Children in DataSourceElement are included in to_dict()."""
        el = DataSourceElement(
            id="node-1",
            display_name="Asset",
            type="graph.nodeType",
            is_selected=True,
            children=[
                {
                    "id": "prop-sn",
                    "display_name": "serial_number",
                    "type": "graph.property",
                    "is_selected": True,
                    "data_type": "string",
                    "index_state": "indexed",
                }
            ],
        )
        d = el.to_dict()
        assert "children" in d
        assert len(d["children"]) == 1
        assert d["children"][0]["id"] == "prop-sn"

    def test_to_dict_excludes_children_when_none(self):
        """When children=None, to_dict() must not include a 'children' key."""
        el = DataSourceElement(id="node-1", display_name="Asset", type="graph.nodeType")
        d = el.to_dict()
        assert "children" not in d

    def test_property_child_count_from_stage_snapshot(self):
        """stage_snapshot_from_spec counts property children from DataSourceSpec.elements."""
        from fabric_kg_builder.knowledge.data_agent import (
            DataAgentSpec,
            DataSourceSpec,
            stage_snapshot_from_spec,
        )
        child_dict = {
            "id": "prop-sn",
            "display_name": "serial_number",
            "type": "graph.property",
            "is_selected": True,
            "data_type": "string",
            "index_state": "indexed",
        }
        node_with_children = DataSourceElement(
            id="node-1",
            display_name="Asset",
            type="graph.nodeType",
            is_selected=True,
            children=[child_dict],
        )
        spec = DataAgentSpec(
            display_name="Agent",
            instruction="Route.",
            sources=[
                DataSourceSpec(
                    source_type="graph",
                    name="g",
                    instructions="Use.",
                    description="G.",
                    elements=[node_with_children],
                    preview=True,
                )
            ],
        )
        snap = stage_snapshot_from_spec(spec)
        assert snap.property_child_count == 1, (
            f"Expected 1 selected property child, got {snap.property_child_count}"
        )
