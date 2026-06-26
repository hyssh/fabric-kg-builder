"""Unit tests for fabric_def.build_ontology_parts and update_ontology_definition.

Verifies (per coordinator spec):
- build_ontology_parts returns 6 parts with correct paths
- KGEntity definition has entity_id/entity_type/display_name/canonical_key props
- DataBinding points to dbo.entities with LakehouseTable sourceType
- related_to RelationshipType definition has source/target pointing at KGEntity
- Contextualization points to dbo.relationships with source_entity_id/target_entity_id
- BigInt IDs are stable across two calls (deterministic)
- BigInt IDs are distinct (no collisions between entity_type_id, property ids, rel_type_id)
- update_ontology_definition mock=True returns parts count summary, no network
- update_ontology_definition live: asserts correct updateDefinition URL + InlineBase64 payload shape
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from fabric_kg_builder.ontology.fabric_def import (
    build_ontology_parts,
    get_stable_ids,
    _bigint_id,
    _guid_id,
)
from fabric_kg_builder.deploy.fabric_ontology import update_ontology_definition

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_WS = "11111111-1111-1111-1111-111111111111"
_LH = "c1a44e9d-0000-1111-2222-333344445555"
_SCHEMA = "dbo"
_NAME = "kg_ontology"


@pytest.fixture(scope="module")
def parts():
    return build_ontology_parts(
        workspace_id=_WS,
        lakehouse_item_id=_LH,
        schema=_SCHEMA,
        ontology_name=_NAME,
    )


@pytest.fixture(scope="module")
def ids():
    return get_stable_ids()


def _find_part(parts, path_fragment: str) -> dict:
    """Return the first part whose path contains *path_fragment*."""
    matches = [p for p in parts if path_fragment in p["path"]]
    assert matches, f"No part found with path containing '{path_fragment}'"
    return matches[0]


def _decode(part: dict) -> dict:
    """Decode payload_json directly (it's already a dict, not base64 in fabric_def)."""
    return part["payload_json"]


# ---------------------------------------------------------------------------
# Parts structure tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildOntologyPartsStructure:

    def test_returns_six_parts(self, parts):
        assert len(parts) == 6, f"Expected 6 parts, got {len(parts)}"

    def test_all_parts_have_path_and_payload_json(self, parts):
        for p in parts:
            assert "path" in p, f"Missing 'path' in part: {p}"
            assert "payload_json" in p, f"Missing 'payload_json' in part: {p}"
            assert isinstance(p["payload_json"], dict), f"payload_json must be dict: {p}"

    def test_path_definition_json_root(self, parts):
        paths = [p["path"] for p in parts]
        assert "definition.json" in paths

    def test_path_platform(self, parts):
        paths = [p["path"] for p in parts]
        assert ".platform" in paths

    def test_path_entity_type_definition(self, parts):
        paths = [p["path"] for p in parts]
        assert any("EntityTypes" in path and path.endswith("definition.json") for path in paths)

    def test_path_data_binding(self, parts):
        paths = [p["path"] for p in parts]
        assert any("DataBindings" in path for path in paths)

    def test_path_relationship_type_definition(self, parts):
        paths = [p["path"] for p in parts]
        assert any("RelationshipTypes" in path and path.endswith("definition.json") for path in paths)

    def test_path_contextualization(self, parts):
        paths = [p["path"] for p in parts]
        assert any("Contextualizations" in path for path in paths)

    def test_root_definition_json_is_empty_dict(self, parts):
        root = _find_part(parts, "definition.json")
        # Exclude EntityTypes/.../definition.json and RelationshipTypes/.../definition.json
        root_part = next(p for p in parts if p["path"] == "definition.json")
        assert root_part["payload_json"] == {}

    def test_platform_has_ontology_type(self, parts):
        platform = _find_part(parts, ".platform")
        payload = _decode(platform)
        assert payload["metadata"]["type"] == "Ontology"

    def test_platform_display_name(self, parts):
        platform = _find_part(parts, ".platform")
        payload = _decode(platform)
        assert payload["metadata"]["displayName"] == _NAME

    def test_platform_version(self, parts):
        platform = _find_part(parts, ".platform")
        payload = _decode(platform)
        assert payload["config"]["version"] == "2.0"

    def test_platform_logical_id_zeros(self, parts):
        platform = _find_part(parts, ".platform")
        payload = _decode(platform)
        assert payload["config"]["logicalId"] == "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# KGEntity EntityType definition tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKGEntityDefinition:

    def _entity_def(self, parts) -> dict:
        part = next(
            p for p in parts
            if "EntityTypes" in p["path"] and p["path"].endswith("definition.json")
        )
        return part["payload_json"]

    def test_entity_type_name_is_kgentity(self, parts):
        payload = self._entity_def(parts)
        assert payload["name"] == "KGEntity"

    def test_entity_type_namespace(self, parts):
        payload = self._entity_def(parts)
        assert payload["namespace"] == "usertypes"

    def test_entity_type_namespace_type(self, parts):
        payload = self._entity_def(parts)
        assert payload["namespaceType"] == "Imported"

    def test_entity_type_visibility(self, parts):
        payload = self._entity_def(parts)
        assert payload["visibility"] == "Visible"

    def test_entity_type_base_is_null(self, parts):
        payload = self._entity_def(parts)
        assert payload["baseEntityTypeId"] is None

    def test_entity_type_timeseries_empty(self, parts):
        payload = self._entity_def(parts)
        assert payload["timeseriesProperties"] == []

    def test_entity_type_untyped_empty(self, parts):
        payload = self._entity_def(parts)
        assert payload["untypedProperties"] == []

    def test_entity_type_has_four_properties(self, parts):
        payload = self._entity_def(parts)
        assert len(payload["properties"]) == 4

    def test_property_names(self, parts):
        payload = self._entity_def(parts)
        names = {p["name"] for p in payload["properties"]}
        assert names == {"entity_id", "entity_type", "display_name", "canonical_key"}

    def test_all_properties_string_type(self, parts):
        payload = self._entity_def(parts)
        for prop in payload["properties"]:
            assert prop["valueType"] == "String", f"Expected String, got {prop['valueType']} for {prop['name']}"

    def test_entity_id_parts_is_entity_id_prop(self, parts, ids):
        payload = self._entity_def(parts)
        assert payload["entityIdParts"] == [ids["prop_entity_id"]]

    def test_display_name_property_id(self, parts, ids):
        payload = self._entity_def(parts)
        assert payload["displayNamePropertyId"] == ids["prop_display_name"]

    def test_entity_type_id_in_path(self, parts, ids):
        et_path = next(
            p["path"] for p in parts
            if "EntityTypes" in p["path"] and p["path"].endswith("definition.json")
        )
        assert ids["entity_type_id"] in et_path

    def test_schema_url_present(self, parts):
        payload = self._entity_def(parts)
        assert "$schema" in payload
        assert "entityType" in payload["$schema"]

    def test_schema_url_uses_item_path(self, parts):
        """Verify schema URL uses /item/ontology/ (not /ontology/ alone)."""
        payload = self._entity_def(parts)
        assert "/item/ontology/" in payload["$schema"]


# ---------------------------------------------------------------------------
# DataBinding tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDataBinding:

    def _binding(self, parts) -> dict:
        part = next(p for p in parts if "DataBindings" in p["path"])
        return part["payload_json"]

    def test_binding_schema_url(self, parts):
        payload = self._binding(parts)
        assert "dataBinding" in payload["$schema"]

    def test_binding_type_non_timeseries(self, parts):
        payload = self._binding(parts)
        cfg = payload["dataBindingConfiguration"]
        assert cfg["dataBindingType"] == "NonTimeSeries"

    def test_binding_source_table_name(self, parts):
        payload = self._binding(parts)
        src = payload["dataBindingConfiguration"]["sourceTableProperties"]
        assert src["sourceTableName"] == "entities"

    def test_binding_source_schema(self, parts):
        payload = self._binding(parts)
        src = payload["dataBindingConfiguration"]["sourceTableProperties"]
        assert src["sourceSchema"] == _SCHEMA

    def test_binding_source_type(self, parts):
        payload = self._binding(parts)
        src = payload["dataBindingConfiguration"]["sourceTableProperties"]
        assert src["sourceType"] == "LakehouseTable"

    def test_binding_workspace_id(self, parts):
        payload = self._binding(parts)
        src = payload["dataBindingConfiguration"]["sourceTableProperties"]
        assert src["workspaceId"] == _WS

    def test_binding_item_id(self, parts):
        payload = self._binding(parts)
        src = payload["dataBindingConfiguration"]["sourceTableProperties"]
        assert src["itemId"] == _LH

    def test_binding_property_bindings_count(self, parts):
        payload = self._binding(parts)
        bindings = payload["dataBindingConfiguration"]["propertyBindings"]
        assert len(bindings) == 4

    def test_binding_entity_id_column(self, parts, ids):
        payload = self._binding(parts)
        bindings = payload["dataBindingConfiguration"]["propertyBindings"]
        entity_id_binding = next(b for b in bindings if b["sourceColumnName"] == "entity_id")
        assert entity_id_binding["targetPropertyId"] == ids["prop_entity_id"]

    def test_binding_display_name_column(self, parts, ids):
        payload = self._binding(parts)
        bindings = payload["dataBindingConfiguration"]["propertyBindings"]
        dn_binding = next(b for b in bindings if b["sourceColumnName"] == "display_name")
        assert dn_binding["targetPropertyId"] == ids["prop_display_name"]


# ---------------------------------------------------------------------------
# RelationshipType definition tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelationshipTypeDefinition:

    def _rel_def(self, parts) -> dict:
        part = next(
            p for p in parts
            if "RelationshipTypes" in p["path"] and p["path"].endswith("definition.json")
        )
        return part["payload_json"]

    def test_relationship_type_name(self, parts):
        payload = self._rel_def(parts)
        assert payload["name"] == "related_to"

    def test_relationship_type_namespace(self, parts):
        payload = self._rel_def(parts)
        assert payload["namespace"] == "usertypes"

    def test_relationship_type_namespace_type(self, parts):
        payload = self._rel_def(parts)
        assert payload["namespaceType"] == "Imported"

    def test_relationship_source_entity_type(self, parts, ids):
        payload = self._rel_def(parts)
        assert payload["source"]["entityTypeId"] == ids["entity_type_id"]

    def test_relationship_target_entity_type(self, parts, ids):
        payload = self._rel_def(parts)
        assert payload["target"]["entityTypeId"] == ids["entity_type_id"]

    def test_rel_type_id_in_path(self, parts, ids):
        rt_path = next(
            p["path"] for p in parts
            if "RelationshipTypes" in p["path"] and p["path"].endswith("definition.json")
        )
        assert ids["rel_type_id"] in rt_path

    def test_schema_url_present(self, parts):
        payload = self._rel_def(parts)
        assert "$schema" in payload
        assert "relationshipType" in payload["$schema"]

    def test_schema_url_uses_item_path(self, parts):
        payload = self._rel_def(parts)
        assert "/item/ontology/" in payload["$schema"]


# ---------------------------------------------------------------------------
# Contextualization tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContextualization:

    def _ctx(self, parts) -> dict:
        part = next(p for p in parts if "Contextualizations" in p["path"])
        return part["payload_json"]

    def test_ctx_schema_url(self, parts):
        payload = self._ctx(parts)
        assert "contextualization" in payload["$schema"]

    def test_ctx_table_name(self, parts):
        payload = self._ctx(parts)
        assert payload["dataBindingTable"]["sourceTableName"] == "relationships"

    def test_ctx_schema(self, parts):
        payload = self._ctx(parts)
        assert payload["dataBindingTable"]["sourceSchema"] == _SCHEMA

    def test_ctx_source_type(self, parts):
        payload = self._ctx(parts)
        assert payload["dataBindingTable"]["sourceType"] == "LakehouseTable"

    def test_ctx_workspace_id(self, parts):
        payload = self._ctx(parts)
        assert payload["dataBindingTable"]["workspaceId"] == _WS

    def test_ctx_item_id(self, parts):
        payload = self._ctx(parts)
        assert payload["dataBindingTable"]["itemId"] == _LH

    def test_ctx_source_key_ref_bindings(self, parts, ids):
        payload = self._ctx(parts)
        src_bindings = payload["sourceKeyRefBindings"]
        assert len(src_bindings) == 1
        assert src_bindings[0]["sourceColumnName"] == "source_entity_id"
        assert src_bindings[0]["targetPropertyId"] == ids["prop_entity_id"]

    def test_ctx_target_key_ref_bindings(self, parts, ids):
        payload = self._ctx(parts)
        tgt_bindings = payload["targetKeyRefBindings"]
        assert len(tgt_bindings) == 1
        assert tgt_bindings[0]["sourceColumnName"] == "target_entity_id"
        assert tgt_bindings[0]["targetPropertyId"] == ids["prop_entity_id"]


# ---------------------------------------------------------------------------
# BigInt ID stability + distinctness tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBigIntIds:

    def test_ids_stable_across_calls(self):
        """Deterministic: two calls with same seeds return same IDs."""
        ids1 = get_stable_ids()
        ids2 = get_stable_ids()
        assert ids1 == ids2

    def test_ids_are_positive_integers(self):
        ids = get_stable_ids()
        for key, val in ids.items():
            if key.startswith("binding") or key.startswith("ctx"):
                continue  # those are UUIDs
            assert val.isdigit(), f"ID '{key}' = '{val}' is not a positive integer string"
            assert int(val) > 0, f"ID '{key}' = '{val}' must be positive"

    def test_bigint_ids_are_distinct(self):
        """All BigInt IDs must be distinct within the ontology."""
        ids = get_stable_ids()
        bigint_ids = [v for k, v in ids.items() if not k.startswith("binding") and not k.startswith("ctx")]
        assert len(bigint_ids) == len(set(bigint_ids)), (
            f"Duplicate BigInt IDs detected: {bigint_ids}"
        )

    def test_parts_use_stable_ids_in_paths(self):
        """Part paths in two calls to build_ontology_parts must be identical."""
        p1 = build_ontology_parts(_WS, _LH, _SCHEMA, _NAME)
        p2 = build_ontology_parts(_WS, _LH, _SCHEMA, _NAME)
        paths1 = [p["path"] for p in p1]
        paths2 = [p["path"] for p in p2]
        assert paths1 == paths2

    def test_bigint_id_helper_deterministic(self):
        assert _bigint_id("test:seed:abc") == _bigint_id("test:seed:abc")

    def test_guid_id_helper_deterministic(self):
        assert _guid_id("test:guid:abc") == _guid_id("test:guid:abc")

    def test_different_seeds_different_ids(self):
        assert _bigint_id("KGEntity:entityType") != _bigint_id("KGEntity:prop:entity_id")
        assert _bigint_id("KGEntity:prop:entity_id") != _bigint_id("KGEntity:prop:entity_type")
        assert _bigint_id("KGEntity:prop:display_name") != _bigint_id("KGEntity:prop:canonical_key")


# ---------------------------------------------------------------------------
# update_ontology_definition tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpdateOntologyDefinitionMock:

    def test_mock_returns_dict(self):
        parts = build_ontology_parts(_WS, _LH)
        result = update_ontology_definition(
            workspace_id=_WS,
            ontology_item_id="test-item-id",
            parts=parts,
            mock=True,
        )
        assert isinstance(result, dict)

    def test_mock_returns_parts_count(self):
        parts = build_ontology_parts(_WS, _LH)
        result = update_ontology_definition(
            workspace_id=_WS,
            ontology_item_id="test-item-id",
            parts=parts,
            mock=True,
        )
        assert result["parts_count"] == len(parts)

    def test_mock_status_is_mock(self):
        parts = build_ontology_parts(_WS, _LH)
        result = update_ontology_definition(
            workspace_id=_WS,
            ontology_item_id="test-item-id",
            parts=parts,
            mock=True,
        )
        assert result["status"] == "mock"

    def test_mock_note_mentions_item_id(self):
        parts = build_ontology_parts(_WS, _LH)
        result = update_ontology_definition(
            workspace_id=_WS,
            ontology_item_id="my-item-9999",
            parts=parts,
            mock=True,
        )
        assert "my-item-9999" in result["note"]

    def test_mock_no_network_call(self, monkeypatch):
        import urllib.request
        def _fail(*a, **kw):
            raise AssertionError("HTTP call made during mock — forbidden!")
        monkeypatch.setattr(urllib.request, "urlopen", _fail)

        parts = build_ontology_parts(_WS, _LH)
        result = update_ontology_definition(
            workspace_id=_WS,
            ontology_item_id="mock-id",
            parts=parts,
            mock=True,
        )
        assert result["status"] == "mock"


@pytest.mark.unit
class TestUpdateOntologyDefinitionLive:

    def _make_resp(self, status_code: int, location: str = "") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"Location": location} if location else {}
        return resp

    def test_live_200_calls_correct_url(self):
        parts = build_ontology_parts(_WS, _LH)
        resp = self._make_resp(200)

        with patch("requests.post", return_value=resp) as mock_post:
            result = update_ontology_definition(
                workspace_id=_WS,
                ontology_item_id="live-item-abc",
                parts=parts,
                mock=False,
                token_provider=lambda: "test-token",
            )

        assert result["status"] == "ok-200"
        assert result["parts_count"] == len(parts)

        # Verify URL contains workspace + item + updateDefinition
        call_url = mock_post.call_args[0][0]
        assert _WS in call_url
        assert "live-item-abc" in call_url
        assert "updateDefinition" in call_url

    def test_live_200_payload_is_inline_base64(self):
        """Each part in the POST body must be InlineBase64 with valid base64 payload."""
        parts = build_ontology_parts(_WS, _LH)
        resp = self._make_resp(200)

        with patch("requests.post", return_value=resp) as mock_post:
            update_ontology_definition(
                workspace_id=_WS,
                ontology_item_id="live-item-abc",
                parts=parts,
                mock=False,
                token_provider=lambda: "test-token",
            )
            # Inspect posted body while still inside patch context
            call_kwargs = mock_post.call_args

        sent_body = call_kwargs[1]["json"]  # keyword arg json=...
        sent_parts = sent_body["definition"]["parts"]
        assert len(sent_parts) == len(parts)
        for sp in sent_parts:
            assert sp["payloadType"] == "InlineBase64"
            decoded = base64.b64decode(sp["payload"]).decode("utf-8")
            json.loads(decoded)  # must be valid JSON

    def test_live_202_returns_ok_202(self):
        parts = build_ontology_parts(_WS, _LH)
        resp = self._make_resp(202, location="https://api.fabric.microsoft.com/v1/operations/op-xyz")

        with patch("requests.post", return_value=resp):
            result = update_ontology_definition(
                workspace_id=_WS,
                ontology_item_id="live-item-lro",
                parts=parts,
                mock=False,
                token_provider=lambda: "test-token",
            )

        assert result["status"] == "ok-202"
        assert result["parts_count"] == len(parts)

    def test_live_payload_has_inline_base64_type(self):
        """Verify the REST body structure: definition.parts[*].payloadType == InlineBase64."""
        parts = build_ontology_parts(_WS, _LH)
        resp = self._make_resp(200)
        captured_body = {}

        def fake_post(url, headers, json, timeout):
            captured_body.update(json)
            return resp

        with patch("requests.post", side_effect=fake_post):
            update_ontology_definition(
                workspace_id=_WS,
                ontology_item_id="item-99",
                parts=parts,
                mock=False,
                token_provider=lambda: "test-token",
            )

        sent_parts = captured_body["definition"]["parts"]
        assert len(sent_parts) == len(parts)
        for sp in sent_parts:
            assert sp["payloadType"] == "InlineBase64", f"payloadType should be InlineBase64: {sp}"
            # Verify the payload is valid base64 that decodes to JSON
            decoded = base64.b64decode(sp["payload"]).decode("utf-8")
            parsed = json.loads(decoded)
            assert isinstance(parsed, dict)
