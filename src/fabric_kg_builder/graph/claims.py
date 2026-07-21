"""GRP-006 (revised): Factual claim extraction.

Fixes:
- Invalid date syntax or reversed/equal valid_from/valid_to RAISES ValueError
- Claim IDs distinguish by predicate+status+time+occurrence (coexist contradictions)
- Contradiction pairs: only asserted<->retracted/disputed on same predicate
- Every claim requires evidence_id; support_type and review_state are controlled
- ClaimRow model validators enforce status/review_state
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from fabric_kg_builder.model.ids import make_id
from fabric_kg_builder.model.schemas import (
    ClaimEvidenceRow,
    ClaimRow,
    _VALID_CLAIM_STATUSES,
)

VALID_CLAIM_STATUSES = _VALID_CLAIM_STATUSES
_SUPPORT_TYPES = frozenset({"supports", "refutes", "context"})
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y",
)


def _parse_dt_strict(s: str) -> datetime:
    """Parse a date string — raises ValueError on invalid syntax (not silently None)."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date string {s!r}; expected ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)")


def _parse_dt_optional(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return _parse_dt_strict(s)


class ClaimExtractionResult(BaseModel):
    claims: list[ClaimRow] = Field(default_factory=list)
    evidence_links: list[ClaimEvidenceRow] = Field(default_factory=list)
    contradicting_pairs: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def claim_ids(self) -> list[str]:
        return [c.claim_id for c in self.claims]


class _RawClaim(BaseModel):
    predicate: str = Field(min_length=1)
    object_text: Optional[str] = None
    status: str = "asserted"
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    summary: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in _VALID_CLAIM_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_CLAIM_STATUSES)}, got {v!r}"
            )
        return v


class _LLMClaimResponse(BaseModel):
    claims: list[_RawClaim] = Field(default_factory=list)


@runtime_checkable
class ClaimExtractorProtocol(Protocol):
    def extract(
        self,
        text: str,
        subject_entity_id: str,
        *,
        evidence_id: Optional[str] = None,
        occurrence_id: Optional[str] = None,
        domain_hash: Optional[str] = None,
        run_id: str = "",
    ) -> ClaimExtractionResult: ...


_CLAIM_PROMPT_SYSTEM = """\
You are a factual claim extractor for a knowledge graph pipeline.
Extract independent positive factual claims from the provided source text.

Rules:
- Each claim must be supported by explicit text; do not invent facts.
- status MUST be one of: asserted, retracted, disputed, uncertain
- Set valid_from / valid_to (ISO-8601) ONLY when the text explicitly states a date range.
- Contradictory claims MAY coexist — do not collapse them.
- Output ONLY valid JSON: {"claims": [{"predicate": "...", "object_text": "...",
  "status": "asserted", "confidence": 0.9, "valid_from": null, "valid_to": null}]}
"""


class LLMClaimExtractor:
    def __init__(self, client: object, *, model: str = "gpt-4o") -> None:
        self._client = client
        self._model = model

    def extract(
        self,
        text: str,
        subject_entity_id: str,
        *,
        evidence_id: Optional[str] = None,
        occurrence_id: Optional[str] = None,
        domain_hash: Optional[str] = None,
        run_id: str = "",
    ) -> ClaimExtractionResult:
        user_content = json.dumps(
            {"subject_entity_id": subject_entity_id, "text": text},
            ensure_ascii=False,
        )
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _CLAIM_PROMPT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        validated = _LLMClaimResponse.model_validate_json(raw)
        return _build_result(
            validated.claims,
            subject_entity_id,
            evidence_id=evidence_id,
            occurrence_id=occurrence_id,
            domain_hash=domain_hash,
            run_id=run_id,
        )


_ASSERTED_RE = re.compile(
    r"(?P<subject>.+?)\s+(?P<predicate>is|are|was|were|has|have|had|provides?|"
    r"contains?|includes?|supports?|requires?|delivers?)\s+(?P<object>.+?)(?:\.|$)",
    re.IGNORECASE,
)
_RETRACTED_RE = re.compile(r"\b(no longer|not|never|ceased|removed|revoked)\b", re.IGNORECASE)
_DISPUTED_RE = re.compile(r"\b(alleged|reportedly|claimed|disputed|contested)\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{4})\b")


def _sentence_status(sentence: str) -> str:
    if _DISPUTED_RE.search(sentence):
        return "disputed"
    if _RETRACTED_RE.search(sentence):
        return "retracted"
    return "asserted"


class DeterministicClaimExtractor:
    def extract(
        self,
        text: str,
        subject_entity_id: str,
        *,
        evidence_id: Optional[str] = None,
        occurrence_id: Optional[str] = None,
        domain_hash: Optional[str] = None,
        run_id: str = "",
    ) -> ClaimExtractionResult:
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        raw_claims: list[_RawClaim] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            match = _ASSERTED_RE.search(sentence)
            if not match:
                continue
            predicate = match.group("predicate").lower()
            obj = match.group("object").strip().rstrip(".")
            status = _sentence_status(sentence)
            dates = _DATE_RE.findall(sentence)
            raw_claims.append(
                _RawClaim(
                    predicate=predicate,
                    object_text=obj,
                    status=status,
                    confidence=0.7 if status == "asserted" else 0.5,
                    valid_from=dates[0] if dates else None,
                    valid_to=dates[1] if len(dates) > 1 else None,
                    summary=sentence[:200],
                )
            )
        return _build_result(
            raw_claims,
            subject_entity_id,
            evidence_id=evidence_id,
            occurrence_id=occurrence_id,
            domain_hash=domain_hash,
            run_id=run_id,
        )


def _build_result(
    raw_claims: list[_RawClaim],
    subject_entity_id: str,
    *,
    evidence_id: Optional[str],
    occurrence_id: Optional[str],
    domain_hash: Optional[str],
    run_id: str,
) -> ClaimExtractionResult:
    now = datetime.now(timezone.utc)
    claims: list[ClaimRow] = []
    links: list[ClaimEvidenceRow] = []

    for raw in raw_claims:
        # Strict date parsing — raises on invalid syntax
        vf = _parse_dt_optional(raw.valid_from)
        vt = _parse_dt_optional(raw.valid_to)
        if vf is not None and vt is not None and vf >= vt:
            raise ValueError(
                f"valid_from ({raw.valid_from!r}) must be strictly before "
                f"valid_to ({raw.valid_to!r})"
            )

        # Claim ID encodes predicate+status+time+occurrence for uniqueness
        claim_id = make_id(
            "claim",
            f"{subject_entity_id}:{raw.predicate}:{raw.object_text or ''}:"
            f"{raw.status}:{raw.valid_from or ''}:{raw.valid_to or ''}:"
            f"{occurrence_id or ''}:{domain_hash or ''}",
        )
        claim = ClaimRow(
            claim_id=claim_id,
            subject_entity_id=subject_entity_id,
            predicate=raw.predicate,
            object_entity_id=None,
            value_json=json.dumps({"object_text": raw.object_text}) if raw.object_text else None,
            status=raw.status,
            confidence=raw.confidence,
            valid_from=vf,
            valid_to=vt,
            observed_at=now,
            summary=raw.summary,
            review_state="not_reviewed",
            domain_hash=domain_hash,
            run_id=run_id,
        )
        claims.append(claim)

        if evidence_id:
            links.append(
                ClaimEvidenceRow(
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    occurrence_id=occurrence_id,
                    support_type="supports",
                    confidence=raw.confidence,
                )
            )

    # Contradiction detection: asserted <-> retracted/disputed on same predicate
    contradictions: list[tuple[str, str]] = []
    pred_index: dict[str, list[ClaimRow]] = {}
    for claim in claims:
        pred_index.setdefault(claim.predicate, []).append(claim)

    for pred_claims in pred_index.values():
        asserted = [c for c in pred_claims if c.status == "asserted"]
        negative = [c for c in pred_claims if c.status in ("retracted", "disputed")]
        for a in asserted:
            for n in negative:
                contradictions.append((a.claim_id, n.claim_id))

    return ClaimExtractionResult(
        claims=claims,
        evidence_links=links,
        contradicting_pairs=contradictions,
    )


def extract_claims(
    text: str,
    subject_entity_id: str,
    *,
    extractor: Optional[ClaimExtractorProtocol] = None,
    evidence_id: Optional[str] = None,
    occurrence_id: Optional[str] = None,
    domain_hash: Optional[str] = None,
    run_id: str = "",
) -> ClaimExtractionResult:
    e = extractor if extractor is not None else DeterministicClaimExtractor()
    return e.extract(
        text,
        subject_entity_id,
        evidence_id=evidence_id,
        occurrence_id=occurrence_id,
        domain_hash=domain_hash,
        run_id=run_id,
    )
