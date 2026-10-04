"""GRP-005 (revised): Typed description consolidation with verifier (VAL-036).

SummaryConsolidationResult carries: summary, distinct_facts, supporting
occurrence_ids, supporting_evidence_ids. SummaryVerifier checks that all
cited occurrences exist and every distinct fact is represented. Backward-
compatible consolidate_description helper returns a plain string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class SummarizerProtocol(Protocol):
    def summarize(self, texts: list[str], *, max_length: int) -> str: ...


@dataclass
class SummaryConsolidationResult:
    """Typed consolidation result with grounding metadata."""
    summary: str
    distinct_facts: list[str] = field(default_factory=list)
    supporting_occurrence_ids: list[str] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)

    def as_string(self) -> str:
        return self.summary


@dataclass
class VerificationResult:
    passed: bool
    missing_occurrence_ids: list[str] = field(default_factory=list)
    unrepresented_facts: list[str] = field(default_factory=list)


class SummaryVerifier:
    """VAL-036: verify all cited occurrences exist and every fact is represented."""

    def verify(
        self,
        result: SummaryConsolidationResult,
        occurrence_text_map: dict[str, str],
    ) -> VerificationResult:
        missing = [
            oid for oid in result.supporting_occurrence_ids
            if oid not in occurrence_text_map
        ]
        unrepresented: list[str] = []
        for fact in result.distinct_facts:
            fact_tokens = set(fact.lower().split())
            represented = any(
                len(fact_tokens & set(text.lower().split())) >= max(1, len(fact_tokens) // 2)
                for text in occurrence_text_map.values()
            )
            if not represented:
                unrepresented.append(fact)
        passed = not missing and not unrepresented
        return VerificationResult(
            passed=passed,
            missing_occurrence_ids=missing,
            unrepresented_facts=unrepresented,
        )


# ---------------------------------------------------------------------------
# LLM-backed summarizer (validated)
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field, field_validator  # noqa: E402


class _LLMSummaryResponse(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    distinct_facts: list[str] = Field(default_factory=list)
    grounded: bool = True

    @field_validator("summary")
    @classmethod
    def _no_hallucination(cls, v: str) -> str:
        forbidden = ["as an AI", "I cannot", "I don'\''t know"]
        for phrase in forbidden:
            if phrase.lower() in v.lower():
                raise ValueError(f"LLM response contains disallowed phrase: {phrase!r}")
        return v.strip()


_SYSTEM_PROMPT = """\
You are a knowledge graph curator.
Given a list of occurrence descriptions about the same entity, produce:
1. ONE concise factual description (summary, max {max_length} chars)
2. A list of distinct facts extracted from the texts

Rules:
- Only use information present in the provided texts.
- Output ONLY valid JSON: {{"summary": "...", "distinct_facts": ["...", ...], "grounded": true}}
"""


class LLMSummarizer:
    def __init__(self, client: object, *, model: str = "gpt-4o") -> None:
        self._client = client
        self._model = model

    def summarize(self, texts: list[str], *, max_length: int = 300) -> str:
        return self.consolidate(texts, max_length=max_length).summary

    def consolidate(
        self,
        texts: list[str],
        occurrence_ids: Optional[list[str]] = None,
        evidence_ids: Optional[list[str]] = None,
        *,
        max_length: int = 300,
    ) -> SummaryConsolidationResult:
        if not texts:
            return SummaryConsolidationResult(summary="")
        user_content = json.dumps({"occurrence_texts": texts}, ensure_ascii=False)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT.format(max_length=max_length)},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        parsed = _LLMSummaryResponse.model_validate_json(raw)
        return SummaryConsolidationResult(
            summary=parsed.summary[:max_length],
            distinct_facts=parsed.distinct_facts,
            supporting_occurrence_ids=occurrence_ids or [],
            supporting_evidence_ids=evidence_ids or [],
        )


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


class DeterministicSummarizer:
    """Offline deterministic — only uses provided occurrence text."""

    def summarize(self, texts: list[str], *, max_length: int = 300) -> str:
        return self.consolidate(texts, max_length=max_length).summary

    def consolidate(
        self,
        texts: list[str],
        occurrence_ids: Optional[list[str]] = None,
        evidence_ids: Optional[list[str]] = None,
        *,
        max_length: int = 300,
    ) -> SummaryConsolidationResult:
        if not texts:
            return SummaryConsolidationResult(summary="")
        seen: set[str] = set()
        unique: list[str] = []
        for t in texts:
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(t.strip())
        if not unique:
            return SummaryConsolidationResult(summary="")

        by_len = sorted(unique, key=len, reverse=True)
        base = by_len[0]
        base_tokens = set(base.lower().split())

        distinct_facts: list[str] = [base]
        additions: list[str] = []
        for candidate in by_len[1:]:
            candidate_tokens = set(candidate.lower().split())
            new_tokens = candidate_tokens - base_tokens
            if len(new_tokens) >= 2:
                additions.append(candidate)
                base_tokens |= candidate_tokens
                distinct_facts.append(candidate)

        result = base
        for addition in additions:
            trial = result + " " + addition
            if len(trial) <= max_length:
                result = trial
            else:
                break

        return SummaryConsolidationResult(
            summary=result[:max_length],
            distinct_facts=distinct_facts,
            supporting_occurrence_ids=occurrence_ids or [],
            supporting_evidence_ids=evidence_ids or [],
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

_DEFAULT_SUMMARIZER = DeterministicSummarizer()


def consolidate_description(
    occurrence_texts: list[str],
    *,
    summarizer: Optional[SummarizerProtocol] = None,
    occurrence_ids: Optional[list[str]] = None,
    evidence_ids: Optional[list[str]] = None,
    max_length: int = 300,
) -> str:
    """Backward-compatible string helper."""
    s = summarizer if summarizer is not None else _DEFAULT_SUMMARIZER
    if hasattr(s, "consolidate"):
        return s.consolidate(
            occurrence_texts,
            occurrence_ids,
            evidence_ids,
            max_length=max_length,
        ).summary
    return s.summarize(occurrence_texts, max_length=max_length)


def consolidate_description_typed(
    occurrence_texts: list[str],
    *,
    summarizer: Optional[SummarizerProtocol] = None,
    occurrence_ids: Optional[list[str]] = None,
    evidence_ids: Optional[list[str]] = None,
    max_length: int = 300,
) -> SummaryConsolidationResult:
    """Typed consolidation result."""
    s = summarizer if summarizer is not None else _DEFAULT_SUMMARIZER
    if hasattr(s, "consolidate"):
        return s.consolidate(
            occurrence_texts,
            occurrence_ids,
            evidence_ids,
            max_length=max_length,
        )
    summary = s.summarize(occurrence_texts, max_length=max_length)
    return SummaryConsolidationResult(
        summary=summary,
        distinct_facts=[summary] if summary else [],
        supporting_occurrence_ids=occurrence_ids or [],
        supporting_evidence_ids=evidence_ids or [],
    )
