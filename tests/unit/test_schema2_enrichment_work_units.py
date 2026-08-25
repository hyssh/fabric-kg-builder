from __future__ import annotations

import json
import time
from pathlib import Path

from fabric_kg_builder.enrichment.orchestrator import (
    EnrichmentWorkItem,
    _sanitize_checkpoint_manifest,
    _split_schema2_work_item,
    enrich_batch,
)

from .test_schema2_enrichment_validation import _context


_SOURCE_FILE_ID = "src:split-test"
_SOURCE_TEXT = "LEFT marker.\n\nSHARED marker.\n\nRIGHT marker."


class _BudgetClient:
    def __init__(self, *, fail_if_called: bool = False) -> None:
        self.calls = 0
        self.fail_if_called = fail_if_called

    def execution_identity(self) -> dict:
        return {"provider": "test", "model": "budget-client-v1"}

    def complete_json(self, *, user: str, **_kwargs) -> dict:
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("resume must not call the model")
        has_left = "LEFT marker." in user
        has_right = "RIGHT marker." in user
        if (has_left and has_right) or (not has_left and not has_right):
            indexes = range(26)
        elif has_left:
            indexes = range(13)
        else:
            indexes = range(12, 26)
        entities = []
        relationships = []
        for index in indexes:
            entities.extend(
                [
                    {
                        "id_hint": f"event-{index}",
                        "type": "ReplacementEvent",
                        "label": f"Replacement event {index}",
                        "confidence": 0.9,
                    },
                    {
                        "id_hint": f"tool-{index}",
                        "type": "Tool",
                        "label": f"Tool {index}",
                        "confidence": 0.9,
                    },
                ]
            )
            relationships.append(
                {
                    "source_id_hint": f"event-{index}",
                    "relation": "requires_tool",
                    "target_id_hint": f"tool-{index}",
                    "direction": "forward",
                    "confidence": 0.9,
                }
            )
        return {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": entities,
            "relationships": relationships,
        }


class _SingleClient(_BudgetClient):
    def complete_json(self, **_kwargs) -> dict:
        self.calls += 1
        return {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [
                {
                    "id_hint": "event-1",
                    "type": "ReplacementEvent",
                    "label": "Replacement event",
                    "confidence": 0.9,
                },
                {
                    "id_hint": "tool-1",
                    "type": "Tool",
                    "label": "Tool",
                    "confidence": 0.9,
                },
            ],
            "relationships": [
                {
                    "source_id_hint": "event-1",
                    "relation": "requires_tool",
                    "target_id_hint": "tool-1",
                    "confidence": 0.9,
                }
            ],
        }


class _MixedMalformedRelationshipClient(_BudgetClient):
    def complete_json(self, **_kwargs) -> dict:
        self.calls += 1
        return {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p2",
            "entities": [
                {
                    "id_hint": "event-1",
                    "type": "ReplacementEvent",
                    "label": "Replacement event",
                    "confidence": 0.9,
                },
                {
                    "id_hint": "tool-1",
                    "type": "Tool",
                    "label": "Tool",
                    "confidence": 0.9,
                },
            ],
            "relationships": [
                {
                    "source_id_hint": "event-1",
                    "relation": "requires_tool",
                    "target_id_hint": "tool-1",
                    "confidence": 0.9,
                },
                {
                    "id_hint": "malformed-candidate",
                    "source_id_hint": "event-1",
                    "relation": "requires_tool",
                    "confidence": 0.9,
                },
            ],
        }


class _FailureThenSuccessClient(_SingleClient):
    def complete_json(self, **kwargs) -> dict:
        if self.calls == 0:
            self.calls += 1
            raise ValueError(
                f"Invalid source payload: {_SOURCE_TEXT}; "
                f"partial=LEFT marker; name=Jane Customer; "
                f"email=jane.customer@example.test; ssn=123-45-6789; "
                f"unlabeled=CredentialValue0123456789ABCDEF; "
                f"api-key={'A' * 32}; "
                f"url=https://service.example/path?sig=remote-secret; "
                f"body=private response\x00fragment"
            )
        return super().complete_json(**kwargs)


def _root_item() -> EnrichmentWorkItem:
    context = _context()
    return EnrichmentWorkItem(
        work_unit_key=f"{_SOURCE_FILE_ID}:pass:p2",
        group_key=_SOURCE_FILE_ID,
        ordinal=1,
        source_file_id=_SOURCE_FILE_ID,
        source_content=_SOURCE_TEXT,
        pass_name="p2",
        input_hash="input:test",
        execution_identity_hash="execution:test",
        semantic_contract_hash=context.contract_hash,
        domain_brief=None,
        default_source_type="document_span",
        lineage=None,
        semantic_context=None,
        schema2_context=context,
        queued_at=time.perf_counter(),
    )


def test_split_child_ids_and_order_are_stable() -> None:
    first = _split_schema2_work_item(_root_item())
    second = _split_schema2_work_item(_root_item())
    assert first is not None
    assert second is not None
    assert [item.work_unit_key for item in first] == [
        item.work_unit_key for item in second
    ]
    assert first[0].source_content.endswith("SHARED marker.\n\n")
    assert first[1].source_content.startswith("SHARED marker.")
    assert first[0].source_start < first[1].source_start


def test_over_budget_response_splits_without_candidate_loss(
    tmp_path: Path,
) -> None:
    client = _BudgetClient()
    records = enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        client,
        None,
        tmp_path,
        schema2_context=_context(),
    )

    assert client.calls == 3
    assert len(records.relationships) == 26
    assert records.failed_work_units == []
    checkpoint = json.loads(
        (tmp_path / ".checkpoint.json").read_text(encoding="utf-8")
    )
    parent = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]
    assert parent["status"] == "split"
    assert len(parent["child_work_unit_keys"]) == 2
    assert all(
        checkpoint["work_units"][key]["status"] == "succeeded"
        for key in parent["child_work_unit_keys"]
    )


def test_split_leaf_receipts_resume_without_model_calls(
    tmp_path: Path,
) -> None:
    enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        _BudgetClient(),
        None,
        tmp_path,
        schema2_context=_context(),
    )
    resume_client = _BudgetClient(fail_if_called=True)
    resumed = enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        resume_client,
        None,
        tmp_path,
        schema2_context=_context(),
        resume=True,
    )

    assert resume_client.calls == 0
    assert len(resumed.relationships) in {0, 26}


def test_resume_reuses_successful_leaf_and_reruns_only_failed_child(
    tmp_path: Path,
) -> None:
    enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        _BudgetClient(),
        None,
        tmp_path,
        schema2_context=_context(),
    )
    checkpoint_path = tmp_path / ".checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    parent_key = f"{_SOURCE_FILE_ID}:pass:p2"
    child_key = checkpoint["work_units"][parent_key]["child_work_unit_keys"][0]
    receipt = checkpoint["work_units"][child_key]["receipt"]
    (tmp_path / receipt).unlink()
    checkpoint["work_units"][child_key] = {
        **checkpoint["work_units"][child_key],
        "status": "failed",
    }
    checkpoint["groups"][_SOURCE_FILE_ID]["status"] = "failed"
    checkpoint["completed"] = [
        key for key in checkpoint.get("completed", []) if key != _SOURCE_FILE_ID
    ]
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    resume_client = _BudgetClient()
    resumed = enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        resume_client,
        None,
        tmp_path,
        schema2_context=_context(),
        resume=True,
    )

    assert resume_client.calls == 1
    assert len(resumed.relationships) == 26


def test_indivisible_over_budget_source_fails_explicitly(
    tmp_path: Path,
) -> None:
    records = enrich_batch(
        "single",
        _SOURCE_FILE_ID,
        _BudgetClient(),
        None,
        tmp_path,
        schema2_context=_context(),
    )
    checkpoint = json.loads(
        (tmp_path / ".checkpoint.json").read_text(encoding="utf-8")
    )
    state = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]

    assert records.relationships == []
    assert records.failed_work_units == [f"{_SOURCE_FILE_ID}:pass:p2"]
    assert state["status"] == "failed"
    assert state["error_type"] == "RelationBudgetOverflowError"


def test_split_depth_limit_fails_before_fanout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "fabric_kg_builder.enrichment.orchestrator._MAX_SCHEMA2_SPLIT_DEPTH",
        0,
    )
    records = enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        _BudgetClient(),
        None,
        tmp_path,
        schema2_context=_context(),
    )
    checkpoint = json.loads(
        (tmp_path / ".checkpoint.json").read_text(encoding="utf-8")
    )
    state = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]

    assert records.failed_work_units == [f"{_SOURCE_FILE_ID}:pass:p2"]
    assert state["status"] == "failed"
    assert state["error_type"] == "RelationBudgetSplitDepthError"


def test_asserted_without_evidence_writes_no_success_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def _invalid_asserted(output, *_args, **_kwargs):
        output.relationships[0] = output.relationships[0].model_copy(
            update={"assertion_status": "asserted"}
        )
        return output

    monkeypatch.setattr(
        "fabric_kg_builder.enrichment.orchestrator.apply_schema2_contract",
        _invalid_asserted,
    )
    records = enrich_batch(
        "single",
        _SOURCE_FILE_ID,
        _SingleClient(),
        None,
        tmp_path,
        schema2_context=_context(),
    )
    checkpoint = json.loads(
        (tmp_path / ".checkpoint.json").read_text(encoding="utf-8")
    )
    state = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]

    assert records.failed_work_units == [f"{_SOURCE_FILE_ID}:pass:p2"]
    assert state["status"] == "failed"
    assert state["error_type"] == "Schema2WorkUnitInvariantError"
    assert not list(tmp_path.glob("r_*.json"))


def test_malformed_schema2_relationship_fails_without_silent_loss(
    tmp_path: Path,
) -> None:
    records = enrich_batch(
        "single",
        _SOURCE_FILE_ID,
        _MixedMalformedRelationshipClient(),
        None,
        tmp_path,
        schema2_context=_context(),
    )
    checkpoint = json.loads(
        (tmp_path / ".checkpoint.json").read_text(encoding="utf-8")
    )
    state = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]

    assert records.relationships == []
    assert records.failed_work_units == [f"{_SOURCE_FILE_ID}:pass:p2"]
    assert state["status"] == "failed"
    assert state["error_type"] == "Schema2MalformedRelationshipError"
    assert not list(tmp_path.glob("r_*.json"))


def test_schema1_tolerant_relationship_behavior_remains_compatible(
    tmp_path: Path,
) -> None:
    records = enrich_batch(
        "single",
        _SOURCE_FILE_ID,
        _MixedMalformedRelationshipClient(),
        None,
        tmp_path,
    )
    checkpoint = json.loads(
        (tmp_path / ".checkpoint.json").read_text(encoding="utf-8")
    )
    state = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]

    assert len(records.relationships) == 1
    assert records.failed_work_units == []
    assert state["status"] == "succeeded"


def test_failure_diagnostics_are_actionable_redacted_and_retained_on_resume(
    tmp_path: Path,
    caplog,
) -> None:
    client = _FailureThenSuccessClient()
    first = enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        client,
        None,
        tmp_path,
        schema2_context=_context(),
    )
    assert first.failed_work_units == [f"{_SOURCE_FILE_ID}:pass:p2"]

    resumed = enrich_batch(
        _SOURCE_TEXT,
        _SOURCE_FILE_ID,
        client,
        None,
        tmp_path,
        schema2_context=_context(),
        resume=True,
    )
    assert resumed.failed_work_units == []

    checkpoint_text = (tmp_path / ".checkpoint.json").read_text(
        encoding="utf-8"
    )
    checkpoint = json.loads(checkpoint_text)
    state = checkpoint["work_units"][f"{_SOURCE_FILE_ID}:pass:p2"]

    assert state["status"] == "succeeded"
    assert state["source_file_id"] == _SOURCE_FILE_ID
    assert state["work_unit_key"] == f"{_SOURCE_FILE_ID}:pass:p2"
    assert state["attempt_count"] == 2
    assert [attempt["status"] for attempt in state["attempts"]] == [
        "failed",
        "succeeded",
    ]
    failure = state["attempts"][0]
    assert failure["exception_category"] == "validation"
    assert failure["exception_type"] == "ValueError"
    assert failure["retry_eligible"] is True
    assert failure["exception_message"] == (
        "The enrichment result failed validation; review the contract and "
        "retry metadata."
    )
    assert _SOURCE_TEXT not in checkpoint_text
    assert "A" * 32 not in checkpoint_text
    for canary in (
        "LEFT marker",
        "Jane Customer",
        "jane.customer@example.test",
        "123-45-6789",
        "CredentialValue0123456789ABCDEF",
        "sig=remote-secret",
        "private response",
    ):
        assert canary not in checkpoint_text
        assert canary not in caplog.text
    assert _SOURCE_TEXT not in caplog.text
    assert "A" * 32 not in caplog.text
    assert f"source={_SOURCE_FILE_ID}" in caplog.text
    assert "retry_eligible=True" in caplog.text


def test_legacy_checkpoint_diagnostics_are_normalized_before_resave() -> None:
    canary = "Jane jane@example.test 123-45-6789 secret response body"
    manifest = {
        "work_units": {
            "src:test:pass:p2": {
                "status": "failed",
                "input_hash": "input:test",
                "error_type": "CustomerSpecificFailure",
                "error_message": canary,
                "validation_payload": {"body": canary},
                "attempts": [
                    {
                        "attempt": 1,
                        "status": "failed",
                        "exception_type": "CustomerSpecificFailure",
                        "exception_message": canary,
                        "raw_response": canary,
                    }
                ],
            }
        },
        "groups": {},
        "documents": {},
    }

    _sanitize_checkpoint_manifest(manifest)

    serialized = json.dumps(manifest)
    assert canary not in serialized
    state = manifest["work_units"]["src:test:pass:p2"]
    assert state["error_type"] == "EnrichmentInternalError"
    assert state["attempts"][0]["exception_message"] == (
        "The enrichment worker failed internally; inspect safe category and "
        "retry metadata."
    )
    assert "validation_payload" not in state
    assert "raw_response" not in state["attempts"][0]
