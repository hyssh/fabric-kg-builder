"""Append-only candidate lifecycle and mutually exclusive accounting."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    sorted_unique,
    utc_timestamp,
)
from .identity import CanonicalIdentityEnvelope

NonNegativeInt = Annotated[int, Field(ge=0)]


class AssertionState(str, Enum):
    PROPOSED = "proposed"
    DISCOVERY = "discovery"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    ASSERTED = "asserted"


_ALLOWED_TRANSITIONS: frozenset[tuple[AssertionState | None, AssertionState]] = frozenset(
    {
        (None, AssertionState.PROPOSED),
        (AssertionState.PROPOSED, AssertionState.DISCOVERY),
        (AssertionState.PROPOSED, AssertionState.UNRESOLVED),
        (AssertionState.PROPOSED, AssertionState.REJECTED),
        (AssertionState.PROPOSED, AssertionState.UNSUPPORTED),
        (AssertionState.PROPOSED, AssertionState.ASSERTED),
        (AssertionState.DISCOVERY, AssertionState.UNRESOLVED),
        (AssertionState.DISCOVERY, AssertionState.REJECTED),
        (AssertionState.DISCOVERY, AssertionState.UNSUPPORTED),
        (AssertionState.DISCOVERY, AssertionState.ASSERTED),
        (AssertionState.UNRESOLVED, AssertionState.REJECTED),
        (AssertionState.UNRESOLVED, AssertionState.UNSUPPORTED),
        (AssertionState.UNRESOLVED, AssertionState.ASSERTED),
    }
)

_STATE_ALIASES = {
    "proposed": AssertionState.PROPOSED,
    "discovery": AssertionState.DISCOVERY,
    "unresolved": AssertionState.UNRESOLVED,
    "unverified": AssertionState.UNRESOLVED,
    "rejected": AssertionState.REJECTED,
    "unsupported": AssertionState.UNSUPPORTED,
    "asserted": AssertionState.ASSERTED,
}


def assertion_state_from_authority(value: str) -> AssertionState:
    """Map existing status aliases through one C0 enum."""
    try:
        return _STATE_ALIASES[value.strip().casefold()]
    except KeyError as exc:
        raise ValueError(f"unknown canonical assertion state: {value!r}") from exc


class CandidateLifecycleRecord(ContractModel):
    identity: CanonicalIdentityEnvelope
    lifecycle_record_id: RequiredText
    candidate_id: RequiredText
    candidate_version_id: RequiredText
    candidate_kind: Literal["entity", "relationship", "property"]
    sequence: NonNegativeInt
    prior_lifecycle_record_id: RequiredText | None
    from_state: AssertionState | None
    to_state: AssertionState
    reason_codes: tuple[str, ...] = ()
    evidence_span_ids: tuple[str, ...] = ()
    governance_justification_id: RequiredText | None
    resolved_source_entity_id: RequiredText | None
    resolved_target_entity_id: RequiredText | None
    source_inheritance_path: tuple[str, ...] = ()
    target_inheritance_path: tuple[str, ...] = ()
    validator_name: RequiredText
    validator_version: RequiredText
    transition_hash: Sha256
    occurred_at_utc: datetime

    _utc = field_validator("occurred_at_utc")(utc_timestamp)

    @field_validator(
        "reason_codes",
        "evidence_span_ids",
        "source_inheritance_path",
        "target_inheritance_path",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def _transition(self) -> "CandidateLifecycleRecord":
        if self.identity.contract_kind != "c0.candidate_lifecycle_record":
            raise ValueError("invalid lifecycle identity contract_kind")
        if (self.from_state, self.to_state) not in _ALLOWED_TRANSITIONS:
            raise ValueError(
                f"forbidden lifecycle transition: {self.from_state!r} -> {self.to_state.value}"
            )
        initial = self.from_state is None
        if initial != (self.sequence == 0 and self.prior_lifecycle_record_id is None):
            raise ValueError("only the initial proposed event has sequence 0 and no prior record")
        if not initial and (self.sequence == 0 or self.prior_lifecycle_record_id is None):
            raise ValueError("non-initial lifecycle events require sequence and prior record")
        expected = canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"transition_hash", "occurred_at_utc"},
            )
        )
        if self.transition_hash != expected:
            raise ValueError("transition_hash does not match lifecycle semantic content")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "CandidateLifecycleRecord":
        for field_name in (
            "reason_codes",
            "evidence_span_ids",
            "source_inheritance_path",
            "target_inheritance_path",
        ):
            raw = values.get(field_name, ())
            values[field_name] = sorted_unique(raw, field_name=field_name)
        values["transition_hash"] = canonical_sha256(
            {
                key: value
                for key, value in values.items()
                if key not in {"transition_hash", "occurred_at_utc"}
            }
        )
        return cls.model_validate(values)


class CandidateAccountingDisposition(ContractModel):
    identity: CanonicalIdentityEnvelope
    input_candidate_id: RequiredText
    disposition: Literal["retained", "deduplicated"]
    retained_candidate_id: RequiredText | None
    deduplicated_into_candidate_id: RequiredText | None
    current_state: AssertionState | None
    reason_codes: tuple[str, ...] = ()

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _reason_codes(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="reason_codes")
        return value

    @model_validator(mode="after")
    def _exclusive(self) -> "CandidateAccountingDisposition":
        if self.identity.contract_kind != "c0.candidate_accounting_disposition":
            raise ValueError("invalid accounting disposition identity contract_kind")
        if self.disposition == "retained":
            if (
                self.retained_candidate_id is None
                or self.deduplicated_into_candidate_id is not None
                or self.current_state is None
            ):
                raise ValueError(
                    "retained disposition requires one retained ID/current state and no dedup target"
                )
        elif (
            self.retained_candidate_id is not None
            or self.deduplicated_into_candidate_id is None
            or self.current_state is not None
        ):
            raise ValueError(
                "deduplicated disposition requires one dedup target and no lifecycle state"
            )
        return self


def allowed_lifecycle_transitions() -> frozenset[
    tuple[AssertionState | None, AssertionState]
]:
    return _ALLOWED_TRANSITIONS
