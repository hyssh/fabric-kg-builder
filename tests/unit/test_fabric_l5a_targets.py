"""Tests for the live Fabric L5a target adapter.

The read paths must be real against a Fabric-shaped API, and every mutating
path must fail closed with a named capability code rather than attempting an
unfenced item create or delete.
"""

from __future__ import annotations

import base64
import json

import pytest

from fabric_kg_builder.deploy.fabric_l5a_targets import (
    FABRIC_ITEM_CAPABILITIES,
    L5A_STATE_PART,
    FabricCapabilityUnavailable,
    FabricL5aTargetClient,
)

WORKSPACE = "00000000-0000-0000-0000-0000000000ff"


class _Response:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class _Transport:
    """Minimal Fabric-shaped transport.

    ``items`` maps collection to the item list a ``GET`` answers with, and
    ``definitions`` maps item id to the ``getDefinition`` body.
    """

    def __init__(self, items=None, definitions=None, definition_status=200):
        self.items = items or {}
        self.definitions = definitions or {}
        self.definition_status = definition_status
        self.gets: list[str] = []
        self.posts: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        collection = url.rsplit("/", 1)[-1]
        return _Response(200, {"value": self.items.get(collection, [])})

    def post(self, url, headers=None, timeout=None):
        self.posts.append(url)
        if self.definition_status != 200:
            return _Response(self.definition_status, {})
        item_id = url.rsplit("/", 2)[-2]
        return _Response(200, {"definition": self.definitions.get(item_id, {})})


def _state_part(envelope: dict) -> dict:
    return {
        "parts": [
            {
                "path": L5A_STATE_PART,
                "payload": base64.b64encode(
                    json.dumps(envelope).encode("utf-8")
                ).decode("ascii"),
                "payloadType": "InlineBase64",
            }
        ]
    }


def _envelope() -> dict:
    return {
        "target_kind": "ontology",
        "target_version": "1.0.0",
        "definition": {"target_kind": "ontology"},
        "table_snapshots": [
            {
                "table_id": "l4_semantic_asserted_entities",
                "schema_hash": "a" * 64,
                "row_count": 3,
                "canonical_id_set_hash": "b" * 64,
                "row_fingerprint": "c" * 64,
            }
        ],
        "access_policy_id": "access-policy:release",
        "access_policy_hash": "d" * 64,
        "publication_token": "publication-token:release",
        "required_member_manifest_rows": [],
        "required_member_rows": [],
    }


def _client(transport) -> FabricL5aTargetClient:
    return FabricL5aTargetClient(
        workspace_id=WORKSPACE,
        token="token",
        transport=transport,
    )


@pytest.mark.unit
def test_inspect_reports_no_state_for_an_item_that_does_not_exist():
    """An absent release-owned item is a clean absence, not a failure.

    ``run_l5a`` distinguishes create from update on exactly this answer, so
    it must not be conflated with a transport fault.
    """

    client = _client(_Transport())
    operation = client.inspect("ontology", "target:release-ontology")
    assert operation.state is None
    assert operation.accounting.operation_refs


@pytest.mark.unit
def test_inspect_reports_no_state_for_an_item_this_release_never_published():
    """An item that exists but carries no L5a part has no prior publication.

    Returning a synthesized state here would let a rollback claim to restore
    something that was never published.
    """

    transport = _Transport(
        items={"ontologies": [{"displayName": "release-ontology", "id": "i1"}]},
        definitions={"i1": {"parts": [{"path": "other.json", "payload": ""}]}},
    )
    operation = _client(transport).inspect("ontology", "target:release-ontology")
    assert operation.state is None
    assert transport.posts


@pytest.mark.unit
def test_read_back_reconstructs_the_canonical_state_from_the_definition():
    """Read-back must reproduce the exact published state.

    ``_validate_state`` compares this against the compiled expectation field
    by field, so a lossy reconstruction fails the publication.
    """

    transport = _Transport(
        items={"ontologies": [{"displayName": "release-ontology", "id": "i1"}]},
        definitions={"i1": _state_part(_envelope())},
    )
    state = _client(transport).read_back(
        "ontology",
        "target:release-ontology",
    ).state
    assert state is not None
    assert state.target_id == "target:release-ontology"
    assert state.publication_token == "publication-token:release"
    assert state.table_snapshots[0].row_count == 3
    assert state.access_policy_hash == "d" * 64


@pytest.mark.unit
def test_publish_refuses_an_unfenced_create():
    """Creating an item cannot be fenced, so publication must refuse.

    Fabric returns an empty ETag on item GET and ignores ``If-Match`` on
    DELETE, so there is no compare-and-swap authority to create under.
    """

    client = _client(_Transport())
    with pytest.raises(FabricCapabilityUnavailable) as excinfo:
        client.publish(
            "ontology",
            "target:release-ontology",
            definition_path=None,
            table_paths={},
            access_policy=None,
            expected_state=None,
            publication_token="publication-token:release",
        )
    assert excinfo.value.code == "L5A_FABRIC_CREATE_UNFENCED"


@pytest.mark.unit
def test_cleanup_refuses_an_unfenced_delete():
    """Rollback-by-delete would remove an item another writer may own now."""

    client = _client(_Transport())
    with pytest.raises(FabricCapabilityUnavailable) as excinfo:
        client.cleanup(
            "ontology",
            "target:release-ontology",
            publication_token="publication-token:release",
        )
    assert excinfo.value.code == "L5A_FABRIC_DELETE_UNFENCED"


@pytest.mark.unit
def test_a_forbidden_definition_is_a_named_capability_gap():
    """A protected sensitivity label must not read as an empty definition.

    Treating 403 as "no state" would let a publication believe the target
    was unpublished and route a failure into an unfenced create.
    """

    transport = _Transport(
        items={"ontologies": [{"displayName": "release-ontology", "id": "i1"}]},
        definition_status=403,
    )
    with pytest.raises(FabricCapabilityUnavailable) as excinfo:
        _client(transport).inspect("ontology", "target:release-ontology")
    assert excinfo.value.code == "L5A_FABRIC_DEFINITION_FORBIDDEN"


@pytest.mark.unit
def test_an_async_definition_is_a_named_capability_gap():
    """A 202 long-running getDefinition is unsupported, not empty."""

    transport = _Transport(
        items={"graphModels": [{"displayName": "release-graph", "id": "i1"}]},
        definition_status=202,
    )
    with pytest.raises(FabricCapabilityUnavailable) as excinfo:
        _client(transport).inspect("graph", "target:release-graph")
    assert excinfo.value.code == "L5A_FABRIC_DEFINITION_ASYNC_UNSUPPORTED"


@pytest.mark.unit
def test_capability_report_marks_create_and_delete_no_go_for_every_target():
    """The release plan records a capability NO-GO, not a bare failure."""

    report = _client(_Transport()).capability_report()
    for kind in ("parquet", "semantic_model", "ontology", "graph"):
        assert report[f"fabric.{kind}.create"] is False
        assert report[f"fabric.{kind}.delete"] is False
        assert report[f"fabric.{kind}.read"] is True
        assert report[f"fabric.{kind}.update_definition"] is True
    assert "empty ETag" in str(report["fabric.capability_reason"])
    assert FABRIC_ITEM_CAPABILITIES.create is False
