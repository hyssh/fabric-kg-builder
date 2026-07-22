"""Tests for deploy/fabric_ontology.py — pure helper functions."""
from __future__ import annotations

import pytest

from fabric_kg_builder.deploy.fabric_ontology import (
    _created_item_id,
    _operation_location,
)


class TestOperationLocation:
    def test_from_location_header(self):
        headers = {"Location": "https://api.fabric.microsoft.com/operations/op-123"}
        result = _operation_location(headers)
        assert result == "https://api.fabric.microsoft.com/operations/op-123"

    def test_from_lowercase_location(self):
        headers = {"location": "https://api.fabric.microsoft.com/operations/op-456"}
        result = _operation_location(headers)
        assert result == "https://api.fabric.microsoft.com/operations/op-456"

    def test_from_operation_id_header(self):
        headers = {"x-ms-operation-id": "op-789"}
        result = _operation_location(headers)
        assert "op-789" in result

    def test_from_absolute_operation_id(self):
        headers = {"x-ms-operation-id": "https://api.fabric.microsoft.com/operations/op-absolute"}
        result = _operation_location(headers)
        assert result == "https://api.fabric.microsoft.com/operations/op-absolute"

    def test_empty_headers(self):
        result = _operation_location({})
        assert result == ""

    def test_none_values_return_empty(self):
        headers = {"Location": None, "x-ms-operation-id": None}
        result = _operation_location(headers)
        assert result == ""


class TestCreatedItemId:
    def test_from_id_field(self):
        operation = {"id": "item-001", "status": "Succeeded"}
        result = _created_item_id(operation)
        assert result == "item-001"

    def test_from_item_id_field(self):
        operation = {"itemId": "item-002", "status": "Succeeded"}
        result = _created_item_id(operation)
        assert result == "item-002"

    def test_from_result_id(self):
        operation = {"status": "Succeeded", "result": {"id": "result-001"}}
        result = _created_item_id(operation)
        assert result == "result-001"

    def test_from_result_item_id(self):
        operation = {"status": "Succeeded", "result": {"itemId": "result-002"}}
        result = _created_item_id(operation)
        assert result == "result-002"

    def test_prefers_id_over_result(self):
        operation = {"id": "direct-001", "result": {"id": "result-001"}}
        result = _created_item_id(operation)
        assert result == "direct-001"

    def test_empty_operation_returns_empty(self):
        result = _created_item_id({})
        assert result == ""

    def test_none_values_skipped(self):
        operation = {"id": None, "itemId": "item-003"}
        result = _created_item_id(operation)
        assert result == "item-003"
