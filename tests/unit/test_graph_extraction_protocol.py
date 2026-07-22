"""Tests for graph/extraction.py — SubgraphExtractionRequest and LLM response models."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from fabric_kg_builder.graph.extraction import (
    ExtractionProtocol,
    LLMExtractionClient,
    SubgraphExtractionRequest,
    _LLMEntityOccurrence,
    _LLMExtractionResponse,
    _LLMRelationshipOccurrence,
    _validate_spans_in_text,
)
from fabric_kg_builder.graph.occurrence import SubgraphOccurrence


# ---------------------------------------------------------------------------
# SubgraphExtractionRequest
# ---------------------------------------------------------------------------

class TestSubgraphExtractionRequest:
    def test_minimal_creation(self):
        req = SubgraphExtractionRequest(
            text_unit_id="unit-001",
            source_text="Apple is a technology company.",
            domain_summary="Technology domain",
        )
        assert req.text_unit_id == "unit-001"
        assert req.domain_hash is None
        assert req.competency_questions == []
        assert req.allowed_entity_types == []

    def test_with_all_fields(self):
        req = SubgraphExtractionRequest(
            text_unit_id="unit-002",
            source_text="text",
            domain_summary="domain",
            domain_hash="dhash-123",
            competency_questions=["Who is Apple?"],
            allowed_entity_types=["Organization"],
            allowed_relationship_types=["owns"],
            observed_types=["Organization"],
            source_locator_json='{"path": "file.pdf"}',
        )
        assert req.domain_hash == "dhash-123"
        assert req.competency_questions == ["Who is Apple?"]
        assert req.source_locator_json == '{"path": "file.pdf"}'


# ---------------------------------------------------------------------------
# _LLMEntityOccurrence
# ---------------------------------------------------------------------------

class TestLLMEntityOccurrence:
    def test_valid_minimal(self):
        e = _LLMEntityOccurrence(
            local_id="e1",
            entity_type="Organization",
            display_name="Apple",
            confidence=0.9,
        )
        assert e.local_id == "e1"
        assert e.span_start is None

    def test_valid_with_spans(self):
        e = _LLMEntityOccurrence(
            local_id="e1",
            entity_type="Organization",
            display_name="Apple",
            confidence=0.9,
            span_start=0,
            span_end=5,
        )
        assert e.span_start == 0
        assert e.span_end == 5

    def test_invalid_span_end_before_start(self):
        with pytest.raises(ValidationError):
            _LLMEntityOccurrence(
                local_id="e1",
                entity_type="Organization",
                display_name="Apple",
                confidence=0.9,
                span_start=10,
                span_end=5,
            )

    def test_one_span_without_other_raises(self):
        with pytest.raises(ValidationError):
            _LLMEntityOccurrence(
                local_id="e1",
                entity_type="Org",
                display_name="A",
                confidence=0.9,
                span_start=0,
                span_end=None,
            )

    def test_confidence_out_of_range(self):
        with pytest.raises(ValidationError):
            _LLMEntityOccurrence(
                local_id="e1",
                entity_type="Org",
                display_name="A",
                confidence=1.5,
            )

    def test_empty_local_id_raises(self):
        with pytest.raises(ValidationError):
            _LLMEntityOccurrence(
                local_id="",
                entity_type="Org",
                display_name="A",
                confidence=0.5,
            )


# ---------------------------------------------------------------------------
# _LLMRelationshipOccurrence
# ---------------------------------------------------------------------------

class TestLLMRelationshipOccurrence:
    def test_valid(self):
        r = _LLMRelationshipOccurrence(
            local_id="r1",
            relationship_type="owns",
            source_local_id="e1",
            target_local_id="e2",
            confidence=0.8,
        )
        assert r.local_id == "r1"

    def test_invalid_span(self):
        with pytest.raises(ValidationError):
            _LLMRelationshipOccurrence(
                local_id="r1",
                relationship_type="owns",
                source_local_id="e1",
                target_local_id="e2",
                confidence=0.8,
                span_start=10,
                span_end=5,
            )


# ---------------------------------------------------------------------------
# _LLMExtractionResponse
# ---------------------------------------------------------------------------

class TestLLMExtractionResponse:
    def _make_entity(self, local_id: str) -> _LLMEntityOccurrence:
        return _LLMEntityOccurrence(
            local_id=local_id,
            entity_type="Organization",
            display_name=local_id.upper(),
            confidence=0.9,
        )

    def _make_rel(self, local_id: str, src: str, tgt: str) -> _LLMRelationshipOccurrence:
        return _LLMRelationshipOccurrence(
            local_id=local_id,
            relationship_type="owns",
            source_local_id=src,
            target_local_id=tgt,
            confidence=0.8,
        )

    def test_empty_response(self):
        resp = _LLMExtractionResponse()
        assert resp.entity_occurrences == []

    def test_valid_with_entities_and_rels(self):
        resp = _LLMExtractionResponse(
            entity_occurrences=[self._make_entity("e1"), self._make_entity("e2")],
            relationship_occurrences=[self._make_rel("r1", "e1", "e2")],
        )
        assert len(resp.entity_occurrences) == 2

    def test_duplicate_entity_ids_raise(self):
        e1a = self._make_entity("e1")
        e1b = self._make_entity("e1")
        with pytest.raises(ValidationError, match="Duplicate entity"):
            _LLMExtractionResponse(entity_occurrences=[e1a, e1b])

    def test_relationship_refs_nonexistent_source(self):
        e1 = self._make_entity("e1")
        r = self._make_rel("r1", "nonexistent", "e1")
        with pytest.raises(ValidationError, match="not in entity_occurrences"):
            _LLMExtractionResponse(entity_occurrences=[e1], relationship_occurrences=[r])

    def test_relationship_refs_nonexistent_target(self):
        e1 = self._make_entity("e1")
        r = self._make_rel("r1", "e1", "nonexistent")
        with pytest.raises(ValidationError, match="not in entity_occurrences"):
            _LLMExtractionResponse(entity_occurrences=[e1], relationship_occurrences=[r])

    def test_duplicate_relationship_ids_raise(self):
        e1 = self._make_entity("e1")
        e2 = self._make_entity("e2")
        r1a = self._make_rel("r1", "e1", "e2")
        r1b = self._make_rel("r1", "e2", "e1")
        with pytest.raises(ValidationError, match="Duplicate relationship"):
            _LLMExtractionResponse(
                entity_occurrences=[e1, e2],
                relationship_occurrences=[r1a, r1b],
            )


# ---------------------------------------------------------------------------
# _validate_spans_in_text
# ---------------------------------------------------------------------------

class TestValidateSpansInText:
    def test_passes_within_bounds(self):
        e = _LLMEntityOccurrence(
            local_id="e1", entity_type="Org", display_name="Apple",
            confidence=0.9, span_start=0, span_end=5
        )
        resp = _LLMExtractionResponse(entity_occurrences=[e])
        source_text = "Apple"  # length 5
        # Should not raise
        _validate_spans_in_text(resp, source_text)

    def test_fails_entity_span_exceeds_text(self):
        e = _LLMEntityOccurrence(
            local_id="e1", entity_type="Org", display_name="Apple",
            confidence=0.9, span_start=0, span_end=100
        )
        resp = _LLMExtractionResponse(entity_occurrences=[e])
        with pytest.raises(ValueError, match="exceeds source text length"):
            _validate_spans_in_text(resp, "short")

    def test_fails_rel_span_exceeds_text(self):
        e1 = _LLMEntityOccurrence(
            local_id="e1", entity_type="Org", display_name="A", confidence=0.9
        )
        e2 = _LLMEntityOccurrence(
            local_id="e2", entity_type="Org", display_name="B", confidence=0.9
        )
        r = _LLMRelationshipOccurrence(
            local_id="r1", relationship_type="owns",
            source_local_id="e1", target_local_id="e2",
            confidence=0.8, span_start=0, span_end=200
        )
        resp = _LLMExtractionResponse(entity_occurrences=[e1, e2], relationship_occurrences=[r])
        with pytest.raises(ValueError, match="exceeds source text length"):
            _validate_spans_in_text(resp, "short text")


# ---------------------------------------------------------------------------
# LLMExtractionClient
# ---------------------------------------------------------------------------

class TestLLMExtractionClient:
    def _make_response_json(self) -> str:
        return json.dumps({
            "entity_occurrences": [
                {
                    "local_id": "e1",
                    "entity_type": "Organization",
                    "display_name": "Apple",
                    "description": "Tech company",
                    "aliases": [],
                    "confidence": 0.9,
                }
            ],
            "relationship_occurrences": [],
        })

    def test_satisfies_protocol(self):
        mock_client = MagicMock()
        client = LLMExtractionClient(mock_client)
        assert isinstance(client, ExtractionProtocol)

    def test_calls_client_and_returns_subgraph(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = (
            self._make_response_json()
        )
        extractor = LLMExtractionClient(mock_client)
        request = SubgraphExtractionRequest(
            text_unit_id="unit-001",
            source_text="Apple is a technology company.",
            domain_summary="Tech domain",
        )
        result = extractor.extract(request)
        assert isinstance(result, SubgraphOccurrence)
        assert len(result.entity_occurrences) == 1
        assert result.entity_occurrences[0].display_name == "Apple"
