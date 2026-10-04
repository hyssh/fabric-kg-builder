"""Tests for release/ledger.py — ResourceLedger and ResourceRecord."""
from __future__ import annotations

import json

import pytest

from fabric_kg_builder.release.ledger import (
    AdoptionMode,
    ResourceKind,
    ResourceLedger,
    ResourceRecord,
    ResourceStatus,
)


def _make_record(
    resource_id: str = "res-001",
    kind: ResourceKind = ResourceKind.AZURE_STORAGE,
    adoption_mode: AdoptionMode = AdoptionMode.CREATE,
    status: ResourceStatus = ResourceStatus.UNKNOWN,
    **kwargs,
) -> ResourceRecord:
    return ResourceRecord(
        resource_id=resource_id,
        resource_kind=kind,
        environment="dev",
        adoption_mode=adoption_mode,
        owner="fabric-kg-builder",
        status=status,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ResourceRecord
# ---------------------------------------------------------------------------

class TestResourceRecord:
    def test_basic_creation(self):
        rec = _make_record()
        assert rec.resource_id == "res-001"
        assert rec.status == ResourceStatus.UNKNOWN
        assert rec.adoption_mode == AdoptionMode.CREATE

    def test_adopted_cannot_be_teardown_pending(self):
        with pytest.raises(ValueError, match="cannot be scheduled for teardown"):
            _make_record(
                adoption_mode=AdoptionMode.CONNECT,
                status=ResourceStatus.TEARDOWN_PENDING,
            )

    def test_adopted_cannot_be_torn_down(self):
        with pytest.raises(ValueError, match="cannot be scheduled for teardown"):
            _make_record(
                adoption_mode=AdoptionMode.CONNECT,
                status=ResourceStatus.TORN_DOWN,
            )

    def test_adopted_can_be_active(self):
        rec = _make_record(adoption_mode=AdoptionMode.CONNECT, status=ResourceStatus.ACTIVE)
        assert rec.status == ResourceStatus.ACTIVE

    def test_is_billable_active_managed_and_active(self):
        rec = _make_record(status=ResourceStatus.ACTIVE)
        assert rec.is_billable_active() is True

    def test_is_billable_active_managed_and_provisioning(self):
        rec = _make_record(status=ResourceStatus.PROVISIONING)
        assert rec.is_billable_active() is True

    def test_is_billable_active_managed_and_torn_down(self):
        rec = _make_record(status=ResourceStatus.TORN_DOWN)
        assert rec.is_billable_active() is False

    def test_is_billable_active_adopted(self):
        rec = _make_record(adoption_mode=AdoptionMode.CONNECT, status=ResourceStatus.ACTIVE)
        assert rec.is_billable_active() is False

    def test_mark_active_sets_status(self):
        rec = _make_record()
        rec.mark_active("arm-id-001")
        assert rec.status == ResourceStatus.ACTIVE
        assert rec.arm_or_fabric_id == "arm-id-001"
        assert rec.created_at is not None

    def test_mark_active_without_id(self):
        rec = _make_record()
        rec.mark_active()
        assert rec.status == ResourceStatus.ACTIVE
        assert rec.arm_or_fabric_id is None

    def test_schedule_teardown(self):
        rec = _make_record(status=ResourceStatus.ACTIVE)
        rec.schedule_teardown()
        assert rec.status == ResourceStatus.TEARDOWN_PENDING

    def test_schedule_teardown_adopted_raises(self):
        rec = _make_record(adoption_mode=AdoptionMode.CONNECT)
        with pytest.raises(ValueError, match="Cannot schedule teardown for adopted"):
            rec.schedule_teardown()

    def test_mark_torn_down(self):
        rec = _make_record(status=ResourceStatus.TEARDOWN_PENDING)
        rec.mark_torn_down()
        assert rec.status == ResourceStatus.TORN_DOWN
        assert rec.deleted_at is not None

    def test_mark_torn_down_adopted_raises(self):
        rec = _make_record(adoption_mode=AdoptionMode.CONNECT)
        with pytest.raises(ValueError, match="Cannot mark adopted resource"):
            rec.mark_torn_down()

    def test_mark_teardown_failed(self):
        rec = _make_record()
        rec.mark_teardown_failed("Timeout error")
        assert rec.status == ResourceStatus.TEARDOWN_FAILED
        assert rec.cleanup_error == "Timeout error"


# ---------------------------------------------------------------------------
# ResourceLedger
# ---------------------------------------------------------------------------

class TestResourceLedger:
    def test_register_single_resource(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record()
        ledger.register(rec)
        assert len(ledger.resources) == 1

    def test_register_duplicate_raises(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record()
        ledger.register(rec)
        with pytest.raises(ValueError, match="already registered"):
            ledger.register(_make_record())  # same ID

    def test_get_existing_resource(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record("res-001")
        ledger.register(rec)
        fetched = ledger.get("res-001")
        assert fetched.resource_id == "res-001"

    def test_get_missing_resource_raises(self):
        ledger = ResourceLedger(environment="dev")
        with pytest.raises(KeyError, match="not found"):
            ledger.get("nonexistent")

    def test_managed_resources(self):
        ledger = ResourceLedger(environment="dev")
        ledger.register(_make_record("res-m", adoption_mode=AdoptionMode.CREATE))
        ledger.register(_make_record("res-a", adoption_mode=AdoptionMode.CONNECT))
        managed = ledger.managed_resources()
        assert len(managed) == 1
        assert managed[0].resource_id == "res-m"

    def test_adopted_resources(self):
        ledger = ResourceLedger(environment="dev")
        ledger.register(_make_record("res-m", adoption_mode=AdoptionMode.CREATE))
        ledger.register(_make_record("res-a", adoption_mode=AdoptionMode.CONNECT))
        adopted = ledger.adopted_resources()
        assert len(adopted) == 1
        assert adopted[0].resource_id == "res-a"

    def test_active_billable_count(self):
        ledger = ResourceLedger(environment="dev")
        ledger.register(_make_record("r1", status=ResourceStatus.ACTIVE))
        ledger.register(_make_record("r2", status=ResourceStatus.UNKNOWN))
        ledger.register(_make_record("r3", adoption_mode=AdoptionMode.CONNECT, status=ResourceStatus.ACTIVE))
        count = ledger.active_billable_count()
        assert count == 1  # only r1 is managed + active

    def test_cleanup_incomplete(self):
        ledger = ResourceLedger(environment="dev")
        ledger.register(_make_record("r1", status=ResourceStatus.ACTIVE))
        rec2 = _make_record("r2", status=ResourceStatus.UNKNOWN)
        ledger.register(rec2)
        rec2.mark_active()
        rec2.mark_torn_down()
        incomplete = ledger.cleanup_incomplete()
        assert len(incomplete) == 1
        assert incomplete[0].resource_id == "r1"

    def test_teardown_failures(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record()
        rec.mark_teardown_failed("Error")
        ledger.register(rec)
        assert len(ledger.teardown_failures()) == 1

    def test_is_cleanup_complete_no_resources(self):
        ledger = ResourceLedger(environment="dev")
        assert ledger.is_cleanup_complete() is True

    def test_is_cleanup_complete_all_torn_down(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record()
        ledger.register(rec)
        rec.mark_active()
        rec.mark_torn_down()
        assert ledger.is_cleanup_complete() is True

    def test_to_safe_dict(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record()
        ledger.register(rec)
        d = ledger.to_safe_dict()
        assert d["environment"] == "dev"
        assert len(d["resources"]) == 1
        assert d["resources"][0]["resource_id"] == "res-001"

    def test_to_json(self):
        ledger = ResourceLedger(environment="dev")
        rec = _make_record()
        ledger.register(rec)
        js = ledger.to_json()
        data = json.loads(js)
        assert data["environment"] == "dev"
