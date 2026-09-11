"""Distinct >=99% processing waivers and explicit limited committed-prefix scopes."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from fabric_kg_builder.contracts.base import ContractModel, RequiredText, Sha256, canonical_sha256


class WindowRunAcceptanceError(ValueError):
    """Processing coverage or the reviewed run binding cannot authorize a waiver."""


class WindowRunCoverageGap(ContractModel):
    chunk_id: str
    source_file_id: str
    source_unit_id: str
    slice_start: int
    slice_end: int
    status: Literal["failed", "unprocessed"]
    reason: str | None


class WindowRunFileCoverage(ContractModel):
    source_file_id: str
    prepared_status: Literal["processed", "no_candidates", "failed", "unsupported", "deferred"]
    prepared_reason: str | None
    processed_chunks: int = Field(ge=0)
    total_chunks: int = Field(ge=0)
    missing_chunk_ids: list[str]


class WindowRunCoverage(ContractModel):
    processed_chunks: int = Field(ge=0)
    total_chunks: int = Field(gt=0)
    gaps: list[WindowRunCoverageGap]
    per_file_coverage: list[WindowRunFileCoverage]
    source_preparation_errors: list[WindowRunFileCoverage]
    verified_candidates: int = Field(ge=0)
    quarantined_candidates: int = Field(ge=0)
    grounding_issue_counts: dict[str, int]
    pending_mapping_count: int = Field(ge=0)
    quarantined_mapping_count: int = Field(ge=0)
    run_state: Literal["partial"] = "partial"
    run_reason: str | None
    semantic_recall: Literal["not_claimed"] = "not_claimed"
    evidence_approved: Literal[False] = False
    answers_verified: Literal[False] = False

    @model_validator(mode="after")
    def _counts(self):
        gap_ids = [gap.chunk_id for gap in self.gaps]
        if (
            self.processed_chunks + len(gap_ids) != self.total_chunks
            or len(set(gap_ids)) != len(gap_ids)
            or sum(row.processed_chunks for row in self.per_file_coverage) != self.processed_chunks
            or sum(row.total_chunks for row in self.per_file_coverage) != self.total_chunks
            or sorted(gap_ids) != sorted(key for row in self.per_file_coverage for key in row.missing_chunk_ids)
            or self.source_preparation_errors != [
                row for row in self.per_file_coverage if row.prepared_status not in {"processed", "no_candidates"}
            ]
        ):
            raise WindowRunAcceptanceError("WINDOW_RUN_COVERAGE_ACCOUNTING_MISMATCH")
        return self


class _CoverageReview(ContractModel):
    window_run_hash: Sha256
    prepared_corpus_hash: Sha256
    corpus_hash: Sha256
    context_hash: Sha256
    snapshot_hash: Sha256
    final_mapping_hash: Sha256
    coverage: WindowRunCoverage
    actor: RequiredText
    rationale: RequiredText
    min_chunk_coverage: Decimal = Field(ge=Decimal("0.99"), le=Decimal("1"), strict=False)
    authority: Literal["prototype_chunk_coverage_only"] = "prototype_chunk_coverage_only"
    ontology_approved: Literal[False] = False
    evidence_approved: Literal[False] = False

    @model_validator(mode="after")
    def _threshold(self):
        coverage = self.coverage
        if coverage.source_preparation_errors:
            raise WindowRunAcceptanceError("WINDOW_RUN_PREPARATION_INCOMPLETE: unknown chunk denominators cannot be waived")
        if Fraction(coverage.processed_chunks, coverage.total_chunks) < Fraction(str(self.min_chunk_coverage)):
            raise WindowRunAcceptanceError(
                f"WINDOW_RUN_COVERAGE_BELOW_THRESHOLD: {coverage.processed_chunks}/{coverage.total_chunks} "
                f"is below {self.min_chunk_coverage}"
            )
        return self


class WindowRunCoveragePreview(_CoverageReview):
    artifact_kind: Literal["domain.window_run_coverage_preview"] = "domain.window_run_coverage_preview"
    accepted: Literal[False] = False


class WindowRunCoverageAcceptance(_CoverageReview):
    artifact_kind: Literal["domain.window_run_coverage_acceptance"] = "domain.window_run_coverage_acceptance"
    artifact_version: Literal["1.0.0"] = "1.0.0"
    accepted: Literal[True] = True
    acceptance_hash: Sha256

    @model_validator(mode="after")
    def _digest(self):
        if self.acceptance_hash != canonical_sha256(self.model_dump(mode="json", exclude={"acceptance_hash"})):
            raise WindowRunAcceptanceError("WINDOW_RUN_ACCEPTANCE_HASH_MISMATCH")
        return self


class WindowRunPrefixChunk(ContractModel):
    chunk_id: str
    source_file_id: str
    source_unit_id: str
    source_text_hash: Sha256
    slice_start: int = Field(ge=0)
    slice_end: int = Field(gt=0)


class _PrefixReview(ContractModel):
    window_run_hash: Sha256
    prepared_corpus_hash: Sha256
    corpus_hash: Sha256
    context_hash: Sha256
    snapshot_hash: Sha256
    final_mapping_hash: Sha256
    coverage: WindowRunCoverage
    cursor: int = Field(gt=0)
    selected_committed_chunk_ids: list[str]
    selected_chunks: list[WindowRunPrefixChunk]
    omitted_chunk_ids: list[str]
    actor: RequiredText
    rationale: RequiredText
    scope_notice: RequiredText
    authority: Literal["limited_committed_prefix_only"] = "limited_committed_prefix_only"
    ontology_approved: Literal[False] = False
    evidence_approved: Literal[False] = False
    full_corpus_coverage: Literal[False] = False

    @model_validator(mode="after")
    def _scope(self):
        selected = self.selected_committed_chunk_ids
        if (
            self.coverage.source_preparation_errors
            or self.cursor != len(selected) or self.cursor != self.coverage.processed_chunks
            or len(set(selected)) != len(selected)
            or [chunk.chunk_id for chunk in self.selected_chunks] != selected
            or self.omitted_chunk_ids != [gap.chunk_id for gap in self.coverage.gaps]
            or set(selected) & set(self.omitted_chunk_ids)
            or self.cursor + len(self.omitted_chunk_ids) != self.coverage.total_chunks
            or not self.omitted_chunk_ids
            or any(chunk.slice_end <= chunk.slice_start for chunk in self.selected_chunks)
            or self.scope_notice != prefix_scope_notice(self.cursor, self.coverage.total_chunks)
        ):
            raise WindowRunAcceptanceError("WINDOW_RUN_PREFIX_SCOPE_MISMATCH")
        return self


class WindowRunPrefixPreview(_PrefixReview):
    artifact_kind: Literal["domain.window_run_prefix_preview"] = "domain.window_run_prefix_preview"
    accepted: Literal[False] = False


class WindowRunPrefixAcceptance(_PrefixReview):
    artifact_kind: Literal["domain.window_run_prefix_acceptance"] = "domain.window_run_prefix_acceptance"
    artifact_version: Literal["1.0.0"] = "1.0.0"
    accepted: Literal[True] = True
    acceptance_hash: Sha256

    @model_validator(mode="after")
    def _digest(self):
        if self.acceptance_hash != canonical_sha256(self.model_dump(mode="json", exclude={"acceptance_hash"})):
            raise WindowRunAcceptanceError("WINDOW_RUN_PREFIX_ACCEPTANCE_HASH_MISMATCH")
        return self


WindowRunAcceptance = Annotated[
    WindowRunCoverageAcceptance | WindowRunPrefixAcceptance, Field(discriminator="artifact_kind"),
]


def prefix_scope_notice(selected, total):
    return (
        f"LIMITED COMMITTED-PREFIX PROTOTYPE: only {selected} of {total} full-corpus planned chunks "
        f"are in the approved extraction scope; {total - selected} chunks are excluded, not observed empty. "
        "The original run remains partial. Cached in-flight responses outside the committed cursor are not included. "
        "This is not a >=99% coverage waiver, full-corpus ontology coverage, evidence approval or verified answers. "
        "Answer only from cited validated evidence in this prefix; identify scope gaps and abstain from full-corpus claims."
    )


def _run_values(run, *, actor, rationale, _validation=None):
    from .window_run import WindowedRun, _verify_chunks, _verify_prepared

    if _validation is None:
        run = WindowedRun.model_validate(run.model_dump(mode="python"))
        _verify_chunks(run.prepared, run.chunk_plan)
        _verify_prepared(run.prepared)
    else:
        run = _validation.run(run)
    if run.state != "partial":
        raise WindowRunAcceptanceError("WINDOW_RUN_NOT_PARTIAL: complete runs need no coverage waiver")
    if not run.final_snapshot.concepts:
        raise WindowRunAcceptanceError("WINDOW_RUN_BOOTSTRAP_INCOMPLETE")
    if not run.chunk_plan:
        raise WindowRunAcceptanceError("WINDOW_RUN_EMPTY_CHUNK_DENOMINATOR")
    observations = {item.chunk.chunk_id: item for item in run.chunks}
    good = {
        key for key, item in observations.items()
        if item.status in {"processed", "no_candidates"} and item.response is not None
    }
    gaps = [WindowRunCoverageGap(
        chunk_id=chunk.chunk_id, source_file_id=chunk.source_file_id,
        source_unit_id=chunk.source_unit_id, slice_start=chunk.slice_start, slice_end=chunk.slice_end,
        status="failed" if chunk.chunk_id in observations and observations[chunk.chunk_id].status == "failed" else "unprocessed",
        reason=observations[chunk.chunk_id].reason if chunk.chunk_id in observations else run.reason,
    ) for chunk in run.chunk_plan if chunk.chunk_id not in good]
    files = []
    for source in run.prepared.sources:
        chunks = [chunk for chunk in run.chunk_plan if chunk.source_file_id == source.source_file_id]
        files.append(WindowRunFileCoverage(
            source_file_id=source.source_file_id, prepared_status=source.status, prepared_reason=source.reason,
            processed_chunks=sum(chunk.chunk_id in good for chunk in chunks), total_chunks=len(chunks),
            missing_chunk_ids=[chunk.chunk_id for chunk in chunks if chunk.chunk_id not in good],
        ))
    quarantines = [entry for item in run.chunks for entry in item.candidate_grounding if entry.disposition == "quarantined"]
    coverage = WindowRunCoverage(
        processed_chunks=len(good), total_chunks=len(run.chunk_plan), gaps=gaps,
        per_file_coverage=files,
        source_preparation_errors=[row for row in files if row.prepared_status not in {"processed", "no_candidates"}],
        verified_candidates=sum(len(item.response.candidates) for item in run.chunks if item.response is not None),
        quarantined_candidates=len(quarantines),
        grounding_issue_counts=dict(Counter(code for entry in quarantines for code in entry.issue_codes)),
        pending_mapping_count=sum(item.status == "pending" for item in run.final_mapping.records),
        quarantined_mapping_count=sum(item.status == "quarantined" for item in run.final_mapping.records),
        run_reason=run.reason,
    )
    return dict(
        window_run_hash=run.artifact_hash, prepared_corpus_hash=run.prepared.artifact_hash,
        corpus_hash=run.prepared.corpus.corpus_hash, context_hash=run.context.artifact_hash,
        snapshot_hash=run.final_snapshot.artifact_hash, final_mapping_hash=run.final_mapping.artifact_hash,
        coverage=coverage, actor=actor, rationale=rationale,
    )


def _values(run, *, actor, rationale, minimum, _validation=None):
    return {**_run_values(run, actor=actor, rationale=rationale, _validation=_validation), "min_chunk_coverage": Decimal(str(minimum))}


def preview_window_run_partial(run, *, actor, rationale, min_chunk_coverage=Decimal("0.99"), _validation=None):
    return WindowRunCoveragePreview(**_values(run, actor=actor, rationale=rationale, minimum=min_chunk_coverage, _validation=_validation))


def accept_window_run_partial(run, *, actor, rationale, min_chunk_coverage=Decimal("0.99"), _validation=None):
    values = _values(run, actor=actor, rationale=rationale, minimum=min_chunk_coverage, _validation=_validation)
    preview = WindowRunCoveragePreview(**values)
    values = preview.model_dump(mode="python", exclude={"artifact_kind", "accepted"})
    values["coverage"] = preview.coverage
    draft = WindowRunCoverageAcceptance.model_construct(acceptance_hash="0" * 64, **values)
    digest = canonical_sha256(draft.model_dump(mode="json", exclude={"acceptance_hash"}))
    return WindowRunCoverageAcceptance(**values, acceptance_hash=digest)


def validate_window_run_acceptance(acceptance, run, *, _validation=None):
    from .window_validation import WindowValidationOperation

    operation = _validation or WindowValidationOperation()
    if operation.acceptance_validated(acceptance, run):
        return acceptance
    if isinstance(acceptance, WindowRunPrefixAcceptance):
        acceptance = WindowRunPrefixAcceptance.model_validate(acceptance.model_dump(mode="python"))
        expected = accept_window_run_prefix(run, actor=acceptance.actor, rationale=acceptance.rationale, _validation=operation)
        if acceptance != expected:
            raise WindowRunAcceptanceError("WINDOW_RUN_PREFIX_ACCEPTANCE_BINDING_DRIFT")
        operation.remember_acceptance(acceptance, run)
        return acceptance
    acceptance = WindowRunCoverageAcceptance.model_validate(acceptance.model_dump(mode="python"))
    expected = accept_window_run_partial(
        run, actor=acceptance.actor, rationale=acceptance.rationale,
        min_chunk_coverage=acceptance.min_chunk_coverage,
        _validation=operation,
    )
    if acceptance != expected:
        raise WindowRunAcceptanceError("WINDOW_RUN_ACCEPTANCE_BINDING_DRIFT: run, source, context, snapshot or coverage differs")
    operation.remember_acceptance(acceptance, run)
    return acceptance


def load_window_run_acceptance(path):
    return TypeAdapter(WindowRunAcceptance).validate_json(Path(path).read_text(encoding="utf-8"))


def _prefix_values(run, *, actor, rationale, _validation=None):
    values = _run_values(run, actor=actor, rationale=rationale, _validation=_validation)
    selected = run.chunk_plan[:run.cursor]
    if [item.chunk for item in run.chunks] != selected:
        raise WindowRunAcceptanceError("WINDOW_RUN_PREFIX_NOT_COMMITTED")
    return {
        **values, "cursor": run.cursor,
        "selected_committed_chunk_ids": [chunk.chunk_id for chunk in selected],
        "selected_chunks": [WindowRunPrefixChunk(**{
            key: getattr(chunk, key) for key in WindowRunPrefixChunk.model_fields
        }) for chunk in selected],
        "omitted_chunk_ids": [chunk.chunk_id for chunk in run.chunk_plan[run.cursor:]],
        "scope_notice": prefix_scope_notice(run.cursor, len(run.chunk_plan)),
    }


def preview_window_run_prefix(run, *, actor, rationale, _validation=None):
    return WindowRunPrefixPreview(**_prefix_values(run, actor=actor, rationale=rationale, _validation=_validation))


def accept_window_run_prefix(run, *, actor, rationale, _validation=None):
    preview = preview_window_run_prefix(run, actor=actor, rationale=rationale, _validation=_validation)
    values = preview.model_dump(mode="python", exclude={"artifact_kind", "accepted"})
    values.update(coverage=preview.coverage, selected_chunks=preview.selected_chunks)
    draft = WindowRunPrefixAcceptance.model_construct(acceptance_hash="0" * 64, **values)
    digest = canonical_sha256(draft.model_dump(mode="json", exclude={"acceptance_hash"}))
    return WindowRunPrefixAcceptance(**values, acceptance_hash=digest)
