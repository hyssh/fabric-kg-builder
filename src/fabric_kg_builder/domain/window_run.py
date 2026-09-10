"""Raw-source working windows: frozen extraction, grounded proposals, durable barriers.

This ledger is deliberately not a DiscoveryRun or an approved domain. Received
model bytes are retained separately from the candidate-only grounding adapter.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from fabric_kg_builder.contracts.base import Sha256, canonical_json, canonical_sha256
from fabric_kg_builder.enrichment.schema2_extraction import RawCandidateResponse
from fabric_kg_builder.sources.corpus import validate_corpus_manifest_against_source

from .discovery import (
    DISCOVERY_SYSTEM, ChunkObservation, DiscoveryBudget, DiscoveryChunk,
    DiscoveryProviderDiagnostic, PreparedCorpus, _check_observation, _grounded_observation, _prepared_reader,
    _reader_binding, _save_provider_diagnostic, _seal, _write, load_discovery, plan_discovery_chunks,
)
from .question_routing import question_routing_context
from .window_schema import (
    ChangeDecision, ObservationMapping, PendingProposal, SchemaChange,
    WindowProposal, WorkingConcept, WorkingSchemaSnapshot, _WindowArtifact, _WindowModel,
    _coordinator_lock, _evaluate, map_chunk, seed_snapshot,
)

RUN_PROMPT_VERSION = "raw-working-window/1.1.0"
PENDING_CONTEXT_V1 = "working-vocabulary-pending/1.0.0"
PENDING_CONTEXT_VERSION = "working-vocabulary-pending/1.1.0"
RUN_SYSTEM = DISCOVERY_SYSTEM + """
Return BOTH candidates and schema_proposals in the same response. The supplied
working schema is frozen for every worker in this window and is NOT approved.
At version zero it is empty. Observe unknown concepts rather than forcing them
into existing names. Propose additive definitions and explicit aliases using
candidate_indices (zero-based indexes into YOUR candidates array) as support.
Each schema proposal has action, reason, candidate_indices, and concept for
add_concept, or concept_id and alias for add_alias. Entities must have
identity_policy {"mode":"unresolved"}. Relationships require nonempty source and
target entity concept IDs and identity_policy with a nonempty context_policy.
Properties require owner entity concept IDs and a scalar value_type. New concepts
must use their observed name; propose aliases separately. Do not invent identity
rules, merge instances, reverse relations, or treat working acceptance as approval.
Proposals in this response may depend on entities proposed in this same window.
Retain common/domain annotations, optional retrieval details and ambiguity in
working_context and pending, without inventing graph properties.
For common/domain annotations, layer belongs on the schema proposal (alongside
action, reason and concept), or in working_context, NOT inside concept. The
concept object permits only its declared schema fields. Preserve the annotation
in its supported location rather than discarding it.
All intake questions, including unrouted questions, remain in the immutable
context. They do not prohibit common concepts. Model-echoed IDs do not establish
processing coverage. Empty schema_proposals is allowed for already-known terms.
Repair requests contain only failed proposals and diagnostics, plus the SAME
full context and frozen schema and required primary source. Return corrected
schema_proposals referring to the supplied original candidate indexes. Do not
replace the original candidates or claim new source facts during a repair.
Every corrected schema proposal MUST include replaces_proposal_index, the exact
original schema_proposals array index from failed_proposals. Each original index
may be replaced at most once. A correction supersedes that failed proposal, never
appends a competing definition. Do not replace accepted proposals. Preserve the
candidate_indices that support the correction from original_candidates.
"""


class RunConfig(_WindowModel):
    window_size: int = Field(default=8, ge=1, le=1024)
    max_concurrency: int = Field(default=4, ge=1, le=16)
    max_request_chars: int = Field(default=96_000, ge=1024)
    max_completion_tokens: int = Field(default=8192, ge=128, le=32_768)
    max_chunk_chars: int = Field(default=8000, ge=128, le=64_000)
    prompt_version: Literal["raw-working-window/1.1.0"] = RUN_PROMPT_VERSION


class RunBudget(_WindowModel):
    max_calls: int = Field(default=0, ge=0)
    max_repair_calls: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0, description="Optional aggregate token reservation cap; zero disables this cap.")
    max_windows: int | None = Field(default=None, ge=0)
    stop_after_document: int | None = Field(default=None, ge=1)
    retry_uncertain: bool = False
    retry_invalid_response: bool = False
    request_char_budget: int | None = Field(
        default=None, ge=1024, description="Explicit invocation admission ceiling; None uses the immutable config ceiling.")


class RunContext(_WindowArtifact):
    context_version: Literal["1.0.0"] = "1.0.0"
    intake_raw: dict[str, Any]
    intake_text: str
    routing: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _original(self):
        if json.loads(self.intake_text) != self.intake_raw:
            raise ValueError("intake snapshot does not match original text")
        if question_routing_context(self.intake_raw) != self.routing:
            raise ValueError("intake routing differs from immutable context")
        return self


class WindowInputs(_WindowArtifact):
    prepared: PreparedCorpus
    context: RunContext
    chunk_plan: list[DiscoveryChunk] = Field(default_factory=list)
    source_cache_hash: Sha256
    source_cache_kind: Literal["prepared", "discovery"] = "prepared"


class WorkingResponse(_WindowModel):
    """Output schema is typed; parsing isolates malformed individual proposals."""

    candidates: list[dict[str, Any]]
    schema_proposals: list[dict[str, Any]]
    pending: list[dict[str, Any]] = Field(default_factory=list)
    working_context: dict[str, Any] = Field(default_factory=dict)


class _ProposedChange(_WindowModel):
    action: Literal["add_concept", "add_alias", "merge", "split"]
    candidate_indices: list[int] = Field(min_length=1)
    reason: str
    concept: WorkingConcept | None = None
    concept_id: str | None = None
    alias: str | None = None
    layer: Literal["common", "domain"] | None = None
    replaces_proposal_index: int | None = Field(default=None, ge=0)


class ProposalDiagnostic(_WindowModel):
    chunk_id: str
    proposal_index: int
    proposal: Any
    status: Literal["rejected", "pending"]
    reason: str
    response_hash: Sha256
    channel: Literal["schema_proposals", "pending", "repair", "working_context"] = "schema_proposals"


class _Ledger(_WindowArtifact):
    kind: str
    payload: dict[str, Any]


class WindowedExchange(_WindowModel):
    request: _Ledger
    response: _Ledger


class RepairSupersession(_WindowModel):
    chunk_id: str
    original_response_hash: Sha256
    original_proposal_index: int
    repair_request_hash: Sha256
    repair_response_hash: Sha256
    repair_proposal_index: int


class WindowedMapping(_WindowArtifact):
    authority: Literal["review_required"] = "review_required"
    snapshot_hash: Sha256
    schema_version: int
    records: list[ObservationMapping]


class WindowedLog(_WindowArtifact):
    window_index: int
    chunk_ids: list[str]
    before_hash: Sha256
    after_hash: Sha256
    previous_log_hash: Sha256 | None
    input_schema_version: int
    context_hash: Sha256
    request_hashes: list[Sha256]
    response_hashes: list[Sha256]
    repair_request_hashes: list[Sha256] = Field(default_factory=list)
    repair_response_hashes: list[Sha256] = Field(default_factory=list)
    exchanges: list[WindowedExchange]
    original_diagnostics: list[ProposalDiagnostic] = Field(default_factory=list)
    supersessions: list[RepairSupersession] = Field(default_factory=list)
    records: list[ObservationMapping]
    decisions: list[ChangeDecision]
    diagnostics: list[ProposalDiagnostic]
    pending: list[PendingProposal]
    working_context: list[dict[str, Any]]
    remapped_previous_count: int = 0
    newly_mapped_previous_count: int = 0
    no_op: bool


class _Manifest(_WindowArtifact):
    inputs: WindowInputs
    config: RunConfig
    chunk_plan: list[DiscoveryChunk]
    windows: list[list[str]]
    seed: WorkingSchemaSnapshot
    model_version: str
    model_hash: Sha256
    prompt_hash: Sha256


class _Commit(_WindowArtifact):
    manifest_hash: Sha256
    snapshot: WorkingSchemaSnapshot
    log: WindowedLog
    chunks: list[ChunkObservation]


class WindowedRun(_WindowArtifact):
    artifact_kind: Literal["domain.windowed_run"] = "domain.windowed_run"
    artifact_version: Literal["1.1.0"] = "1.1.0"
    authority: Literal["working_only"] = "working_only"
    prepared: PreparedCorpus
    context: RunContext
    config: RunConfig
    chunk_plan: list[DiscoveryChunk]
    source_cache_hash: Sha256
    manifest_hash: Sha256
    model_version: str
    model_hash: Sha256
    prompt_version: Literal["raw-working-window/1.1.0"] = RUN_PROMPT_VERSION
    prompt_hash: Sha256
    chunks: list[ChunkObservation]
    final_snapshot: WorkingSchemaSnapshot
    final_mapping: WindowedMapping
    state: Literal["complete", "partial"]
    reason: str | None
    cursor: int
    last_chunk_id: str | None
    logs: list[WindowedLog]
    model_call_count: int
    repair_call_count: int
    reserved_tokens: int
    working_context: list[dict[str, Any]]
    semantic_recall: Literal["not_claimed"] = "not_claimed"

    @property
    def run_hash(self):
        return self.artifact_hash

    @property
    def full_corpus_design_ready(self):
        return self.state == "complete"

    @model_validator(mode="after")
    def _accounting(self):
        ids = [c.chunk_id for c in self.chunk_plan]
        if len(set(ids)) != len(ids) or [c.chunk.chunk_id for c in self.chunks] != ids[:self.cursor]:
            raise ValueError("windowed chunk accounting mismatch")
        if self.cursor != len(self.chunks):
            raise ValueError("windowed cursor differs from committed chunks")
        if [cid for log in self.logs for cid in log.chunk_ids] != ids[:self.cursor]:
            raise ValueError("windowed logs omit or reorder chunks")
        prior = seed_snapshot()
        last = None
        offset = 0
        for index, log in enumerate(self.logs):
            if (log.window_index != index or log.previous_log_hash != last
                    or log.before_hash != prior.artifact_hash
                    or log.input_schema_version != index
                    or log.context_hash != self.context.artifact_hash):
                raise ValueError("windowed log chain mismatch")
            items = self.chunks[offset:offset + len(log.chunk_ids)]
            _validate_exchanges(
                log, items, prior, prepared=self.prepared, context=self.context,
                manifest_hash=self.manifest_hash, model_hash=self.model_hash, config=self.config,
                history=self.logs[:index])
            evaluation = _effective_window(prior, items, log.exchanges, self.config)
            _validate_evaluation(log, evaluation, prior)
            prior = evaluation.snapshot
            offset += len(items)
            last = log.artifact_hash
        if self.final_snapshot != prior:
            raise ValueError("windowed final snapshot differs from history")
        if self.final_snapshot.version != len(self.logs):
            raise ValueError("windowed schema version differs from barriers")
        units = {u.source_unit_id: u for u in self.prepared.source_units}
        for item in self.chunks:
            if item.chunk != self.chunk_plan[ids.index(item.chunk.chunk_id)]:
                raise ValueError("windowed observation source binding mismatch")
            if item.request_prompt_version is not None:
                raise ValueError("integrated observations cannot claim a legacy prompt")
            _check_observation(item, units[item.chunk.source_unit_id])
        records = [r for c in self.chunks for r in map_chunk(c, self.final_snapshot)]
        if (self.final_mapping.records != records
                or self.final_mapping.snapshot_hash != self.final_snapshot.artifact_hash
                or self.final_mapping.schema_version != self.final_snapshot.version):
            raise ValueError("windowed final mapping is stale or changed")
        complete = (self.cursor == len(ids) and bool(self.final_snapshot.concepts)
                    and all(s.status in {"processed", "no_candidates"} for s in self.prepared.sources))
        if (self.state == "complete") != complete:
            raise ValueError("windowed completeness differs from source/schema accounting")
        if self.last_chunk_id != (ids[self.cursor - 1] if self.cursor else None):
            raise ValueError("windowed last chunk mismatch")
        return self


@dataclass(frozen=True)
class WindowedExecutionResult:
    run: WindowedRun
    model_call_count: int
    repair_call_count: int
    reserved_tokens: int
    reused_response_count: int
    reused_window_count: int
    execution_report_hash: str

    def __getattr__(self, name):
        return getattr(self.run, name)

    def model_dump(self, **kwargs):
        return self.run.model_dump(**kwargs)

    @property
    def invocation(self):
        return {key: value for key, value in self.__dict__.items() if key != "run"}


def _verify_prepared(prepared):
    PreparedCorpus.model_validate(prepared.model_dump(mode="python"))
    source = Path(prepared.source_path)
    validate_corpus_manifest_against_source(prepared.corpus, source, identity=prepared.base_identity)
    if _reader_binding(_prepared_reader(prepared, source), prepared.corpus) != prepared.reader_binding:
        raise ValueError("prepared reader/source cache identity drift")


def _verify_chunks(prepared, chunks):
    units = {u.source_unit_id: u for u in prepared.source_units}
    ranges = {}
    seen = set()
    for chunk in chunks:
        unit = units.get(chunk.source_unit_id)
        if (chunk.chunk_id in seen or unit is None or chunk.source_file_id != unit.source_file_id
                or chunk.source_text_hash != unit.text_content_hash
                or not 0 <= chunk.slice_start < chunk.slice_end <= len(unit.text)):
            raise ValueError("invalid cached chunk source identity")
        seen.add(chunk.chunk_id)
        if any(uid not in units or units[uid].source_file_id != chunk.source_file_id
               for uid in chunk.context_unit_ids):
            raise ValueError("context neighbor outside primary document")
        if len(set(chunk.context_unit_ids)) != len(chunk.context_unit_ids):
            raise ValueError("duplicate context anchors")
        ranges.setdefault(chunk.source_unit_id, []).append((chunk.slice_start, chunk.slice_end))
    for unit in units.values():
        cursor = 0
        for start, end in sorted(ranges.get(unit.source_unit_id, [])):
            if start != cursor:
                raise ValueError("cached chunks overlap or omit primary source text")
            cursor = end
        if cursor != len(unit.text):
            raise ValueError("cached chunks do not cover every source unit")


def load_window_inputs(*, prepared_path=None, discovery_path=None, intake_path, source_file_ids=None):
    """Load verified raw sources only. Existing model observations are not input."""
    if (prepared_path is None) == (discovery_path is None):
        raise ValueError("provide exactly one prepared_path or discovery_path")
    if source_file_ids is not None:
        raise ValueError("source_file_ids scope filtering is not supported; use stop_after_document")
    text = Path(intake_path).read_text(encoding="utf-8")
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("intake must be a JSON object")
    context = _seal(RunContext, intake_raw=raw, intake_text=text, routing=question_routing_context(raw))
    if discovery_path is not None:
        cache = load_discovery(Path(discovery_path))
        prepared = cache.prepared
        chunks = [c.chunk for c in cache.chunks]
        _verify_chunks(prepared, chunks)
        source_order = {s.source_file_id: i for i, s in enumerate(prepared.sources)}
        unit_order = {u.source_unit_id: (u.ordinal, u.source_unit_id) for u in prepared.source_units}
        chunks.sort(key=lambda c: (source_order[c.source_file_id], unit_order[c.source_unit_id], c.slice_start))
        kind = "discovery"
    else:
        cache = PreparedCorpus.model_validate_json(Path(prepared_path).read_text(encoding="utf-8"))
        prepared, chunks, kind = cache, [], "prepared"
    _verify_prepared(prepared)
    return _seal(WindowInputs, prepared=prepared, context=context, chunk_plan=chunks,
                 source_cache_hash=cache.artifact_hash, source_cache_kind=kind)


def _plan(inputs, config):
    chunks = list(inputs.chunk_plan) if inputs.source_cache_kind == "discovery" else plan_discovery_chunks(
        inputs.prepared, DiscoveryBudget(max_chunk_chars=config.max_chunk_chars))
    _verify_chunks(inputs.prepared, chunks)
    windows = []
    for chunk in chunks:
        if (not windows or len(windows[-1]) >= config.window_size
                or windows[-1][0].source_file_id != chunk.source_file_id):
            windows.append([])
        windows[-1].append(chunk)
    return chunks, [[c.chunk_id for c in window] for window in windows]


def plan_window_run(inputs, config=RunConfig()):
    chunks, windows = _plan(inputs, config)
    return {
        "prepared_hash": inputs.prepared.artifact_hash, "context_hash": inputs.context.artifact_hash,
        "config": config.model_dump(mode="json"), "document_count": len(inputs.prepared.sources),
        "source_unit_count": len(inputs.prepared.source_units), "chunk_count": len(chunks),
        "window_count": len(windows), "windows": windows, "minimum_extraction_calls": len(chunks),
        "primary_codepoint_count": sum(c.slice_end - c.slice_start for c in chunks),
        "scope": "full_prepared_corpus", "initial_schema_version": 0,
        "source_statuses": [s.model_dump(mode="json") for s in inputs.prepared.sources],
    }


def _source_payload(prepared, chunk):
    units = {u.source_unit_id: u for u in prepared.source_units}
    unit = units[chunk.source_unit_id]
    return {
        "chunk": chunk.model_dump(mode="json"), "text": unit.text[chunk.slice_start:chunk.slice_end],
        "offset_base": chunk.slice_start, "locator": unit.locator.model_dump(mode="json"),
        "primary_text_boundary": {"source_unit_id": chunk.source_unit_id,
                                  "start": chunk.slice_start, "end": chunk.slice_end,
                                  "only_evidence_field": "input.text"},
        "adjacency_context": [
            {"source_unit_id": uid, "source_file_id": units[uid].source_file_id,
             "text_hash": units[uid].text_content_hash, "text": units[uid].text,
             "locator": units[uid].locator.model_dump(mode="json"),
             "scope": "context_only_not_primary_candidate_evidence"}
            for uid in chunk.context_unit_ids
        ],
    }


def _request(manifest, snapshot, chunk, pending, *, repair=None):
    payload = {
        **_source_payload(manifest.inputs.prepared, chunk),
        "context": manifest.inputs.context.model_dump(mode="json"),
        "context_hash": manifest.inputs.context.artifact_hash,
        "schema": snapshot.model_dump(mode="json"), "schema_hash": snapshot.artifact_hash,
        "schema_version": snapshot.version, "pending": pending,
    }
    if repair is not None:
        payload["repair"] = repair
    schema = WorkingResponse.model_json_schema()
    # The candidate channel has the existing strict candidate union. Schema
    # proposals are parsed independently so one malformed concept cannot erase
    # valid siblings.
    candidate_schema = RawCandidateResponse.model_json_schema()
    schema.setdefault("$defs", {}).update(candidate_schema.get("$defs", {}))
    schema["properties"]["candidates"] = candidate_schema["properties"]["candidates"]
    proposal_schema = _ProposedChange.model_json_schema()
    schema["$defs"].update(proposal_schema.pop("$defs", {}))
    schema["properties"]["schema_proposals"] = {"type": "array", "items": proposal_schema}
    request = {
        "system": RUN_SYSTEM, "user": canonical_json({"input": payload}),
        "json_schema": schema, "max_completion_tokens": manifest.config.max_completion_tokens,
        "max_attempts": 1,
    }
    return _seal(_Ledger, kind="request", payload={
        "request": request, "manifest_hash": manifest.artifact_hash,
        "schema_hash": snapshot.artifact_hash, "chunk_id": chunk.chunk_id,
        "context_hash": manifest.inputs.context.artifact_hash,
        "prompt_version": manifest.config.prompt_version, "model_hash": manifest.model_hash,
        "repair": repair is not None,
    })


def _read(path, model, kind=None):
    value = model.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if kind is not None and value.kind != kind:
        raise ValueError("window ledger kind mismatch")
    return value


def _decode(response, config):
    raw = response.payload["response"]
    size = len(raw) if isinstance(raw, str) else len(canonical_json(raw))
    if size > config.max_completion_tokens * 16:
        raise ValueError("response exceeds explicit output character bound")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
        raise ValueError("unparseable candidate envelope")
    if not isinstance(raw.get("schema_proposals"), list):
        raise ValueError("missing schema_proposals channel")
    return raw


def _error_text(exc):
    if isinstance(exc, ValidationError):
        return canonical_json([{"type": e["type"], "loc": list(e["loc"]), "message": e["msg"]}
                               for e in exc.errors(include_input=False, include_url=False)])
    return str(exc)


def _proposals(raw, observation, response_hash, *, repair=False):
    changes, pending, errors = [], [], []
    raw_pending = raw.get("pending", [])
    if raw.get("pending") == {}:
        errors.append(ProposalDiagnostic(
            chunk_id=observation.chunk.chunk_id, proposal_index=-1,
            proposal={}, status="rejected", response_hash=response_hash,
            reason="empty_pending_object: no entries; original shape retained in raw response",
            channel="pending",
        ))
    elif not isinstance(raw_pending, list):
        errors.append(ProposalDiagnostic(
            chunk_id=observation.chunk.chunk_id, proposal_index=-1,
            proposal=raw_pending, status="rejected", response_hash=response_hash,
            reason="invalid_pending_metadata: expected an array; original value retained, not interpreted as pending entries",
            channel="pending",
        ))
    if not isinstance(raw.get("working_context", {}), dict):
        errors.append(ProposalDiagnostic(
            chunk_id=observation.chunk.chunk_id, proposal_index=-1,
            proposal=raw["working_context"], status="rejected", response_hash=response_hash,
            reason="invalid_working_context_metadata: expected an object; original value retained, not used as annotations",
            channel="working_context",
        ))
    eligible = {g.candidate_index: g.observation_id for g in observation.candidate_grounding
                if g.disposition == "verified"}
    known_ids = set(eligible.values())
    for index, proposal in enumerate(raw["schema_proposals"]):
        try:
            if not isinstance(proposal, dict):
                raise ValueError("schema proposal must be an object")
            item = dict(proposal)
            replacement = item.pop("replaces_proposal_index", None)
            if replacement is not None and not repair:
                raise ValueError("original extraction cannot replace another proposal")
            indices = item.pop("candidate_indices", None)
            item.pop("layer", None)  # Raw annotation stays in response/working context.
            if indices is not None:
                if not isinstance(indices, list) or any(type(i) is not int or i not in eligible for i in indices):
                    raise ValueError("proposal support references missing/quarantined primary candidates")
                item["observation_ids"] = [eligible[i] for i in indices]
            change = SchemaChange.model_validate(item)
            if not set(change.observation_ids) <= known_ids:
                raise ValueError("proposal support is outside this primary response")
            changes.append((change, index, proposal, response_hash, observation.chunk.chunk_id))
        except (TypeError, ValueError) as exc:
            errors.append(ProposalDiagnostic(chunk_id=observation.chunk.chunk_id, proposal_index=index,
                                            proposal=proposal, status="rejected", reason=_error_text(exc),
                                            response_hash=response_hash))
    for index, item in enumerate(raw_pending if isinstance(raw_pending, list) else []):
        try:
            value = dict(item)
            indices = value.pop("candidate_indices", None)
            if indices is not None:
                if any(type(i) is not int or i not in eligible for i in indices):
                    raise ValueError("pending references missing/quarantined evidence")
                value["observation_ids"] = [eligible[i] for i in indices]
            entry = PendingProposal.model_validate(value)
            if not set(entry.observation_ids) <= known_ids:
                raise ValueError("pending references missing/quarantined evidence")
            pending.append(entry)
        except (TypeError, ValueError) as exc:
            errors.append(ProposalDiagnostic(chunk_id=observation.chunk.chunk_id, proposal_index=index,
                                            proposal=item, status="rejected", reason=_error_text(exc),
                                            response_hash=response_hash, channel="pending"))
    return changes, pending, errors


def _barrier(snapshot, observations, changes, pending, diagnostics):
    records = [r for item in observations for r in map_chunk(item, snapshot)]
    # Identical proposals from parallel chunks share support; different scoped
    # proposals are never merged, and unknown dependencies remain rejected.
    merged = {}
    origins = {}
    for change, index, raw, response_hash, chunk_id in changes:
        shape = change.model_dump(mode="json", exclude={"observation_ids", "reason"})
        key = canonical_sha256(shape)
        if key in merged:
            old = merged[key]
            merged[key] = old.model_copy(update={"observation_ids": sorted(set(old.observation_ids) | set(change.observation_ids))})
        else:
            merged[key] = change
        origins.setdefault(key, []).append((index, raw, response_hash, chunk_id))
    next_snapshot, decisions = _evaluate(
        snapshot, records, WindowProposal(changes=list(merged.values()), pending=pending), [],
        items=observations, partition_scoped_evidence=True)
    errors = list(diagnostics)
    for decision in decisions:
        if decision.status == "accepted_working":
            continue
        key = canonical_sha256(decision.change.model_dump(mode="json", exclude={"observation_ids", "reason"}))
        for index, raw, response_hash, chunk_id in origins.get(key, []):
            errors.append(ProposalDiagnostic(chunk_id=chunk_id, proposal_index=index, proposal=raw,
                                            status="pending" if decision.status == "pending" else "rejected",
                                            reason=decision.reason, response_hash=response_hash))
    return next_snapshot, decisions, errors, records


def _change_shape(change):
    return canonical_sha256(change.model_dump(mode="json", exclude={"observation_ids", "reason"}))


def _repairable(item, changes, decisions, diagnostics):
    accepted = {_change_shape(d.change) for d in decisions if d.status == "accepted_working"}
    accepted_origins = {(chunk_id, response_hash, index) for change, index, _, response_hash, chunk_id in changes
                        if _change_shape(change) in accepted}
    return [d for d in diagnostics
            if d.chunk_id == item.chunk.chunk_id and d.status == "rejected"
            and d.channel == "schema_proposals"
            and (d.chunk_id, d.response_hash, d.proposal_index) not in accepted_origins]


def _repair_payload(item, original, failed, original_response_hash):
    return {
        "failed_proposals": [d.model_dump(mode="json") for d in failed],
        "original_candidates": original["candidates"],
        "original_request_hash": item.request_hash,
        "original_response_hash": original_response_hash,
        "replacement_policy": "explicit_original_proposal_index",
    }


@dataclass
class _WindowEvaluation:
    snapshot: WorkingSchemaSnapshot
    decisions: list[ChangeDecision]
    diagnostics: list[ProposalDiagnostic]
    records: list[ObservationMapping]
    pending: list[PendingProposal]
    contexts: list[dict[str, Any]]
    original_diagnostics: list[ProposalDiagnostic]
    supersessions: list[RepairSupersession]
    repairable: dict[str, list[ProposalDiagnostic]]


def _effective_window(snapshot, items, exchanges, config):
    """Derive authority exclusively from immutable original and repair envelopes."""
    if len(exchanges) < len(items):
        raise ValueError("window is missing original response envelopes")
    changes, pending, errors, contexts = [], [], [], []
    originals, original_hashes = {}, {}
    for item, exchange in zip(items, exchanges[:len(items)]):
        if exchange.request.payload["repair"] or exchange.request.artifact_hash != item.request_hash:
            raise ValueError("original response order/request identity differs")
        raw = _decode(exchange.response, config)
        chunk_id = item.chunk.chunk_id
        originals[chunk_id] = raw
        original_hashes[chunk_id] = exchange.response.artifact_hash
        batch, waiting, invalid = _proposals(raw, item, exchange.response.artifact_hash)
        changes.extend(batch)
        pending.extend(waiting)
        errors.extend(invalid)
        annotations = raw.get("working_context", {})
        contexts.append({"chunk_id": chunk_id, "response_hash": exchange.response.artifact_hash,
                         "annotations": annotations if isinstance(annotations, dict) else {},
                         "schema_annotations": raw["schema_proposals"]})
    _, initial_decisions, initial_diagnostics, _ = _barrier(snapshot, items, changes, pending, errors)
    repairable = {item.chunk.chunk_id: _repairable(item, changes, initial_decisions, initial_diagnostics) for item in items}
    if len(exchanges) > len(items):
        # Repairing an entity must not resurrect an earlier rejected dependent
        # definition (including a duplicate from another worker). Failed
        # originals stay quarantined unless explicitly replaced by a correction.
        inactive = {(d.chunk_id, d.response_hash, d.proposal_index)
                    for failed in repairable.values() for d in failed}
        changes = [entry for entry in changes if (entry[4], entry[3], entry[1]) not in inactive]
        diagnostic_keys = {canonical_sha256(d) for d in errors}
        for failed in repairable.values():
            for diagnostic in failed:
                key = canonical_sha256(diagnostic)
                if key not in diagnostic_keys:
                    errors.append(diagnostic)
                    diagnostic_keys.add(key)
    items_by_id = {item.chunk.chunk_id: item for item in items}
    repaired_chunks, supersessions = set(), []
    for exchange in exchanges[len(items):]:
        request, response = exchange.request, exchange.response
        chunk_id = request.payload["chunk_id"]
        if not request.payload["repair"] or chunk_id not in items_by_id or chunk_id in repaired_chunks:
            raise ValueError("repair must uniquely reference an original chunk")
        repaired_chunks.add(chunk_id)
        item = items_by_id[chunk_id]
        failed = repairable[chunk_id]
        expected_repair = _repair_payload(item, originals[chunk_id], failed, original_hashes[chunk_id])
        actual_repair = json.loads(request.payload["request"]["user"])["input"].get("repair")
        if not failed or actual_repair != expected_repair:
            raise ValueError("repair original proposal/index linkage differs from evaluated failures")
        try:
            repaired = _decode(response, config)
            if repaired["candidates"] not in ([], originals[chunk_id]["candidates"]):
                raise ValueError("repair must not replace original observations")
        except (TypeError, ValueError) as exc:
            errors.append(ProposalDiagnostic(chunk_id=chunk_id, proposal_index=0,
                                            proposal=response.payload["response"], status="rejected",
                                            reason=_error_text(exc), response_hash=response.artifact_hash,
                                            channel="repair"))
            continue
        failed_indexes = {d.proposal_index for d in failed}
        links = [p.get("replaces_proposal_index") if isinstance(p, dict) else None
                 for p in repaired["schema_proposals"]]
        counts = Counter(i for i in links if type(i) is int)
        valid_repair_indexes = set()
        for index, link in enumerate(links):
            if type(link) is not int or link not in failed_indexes or counts[link] != 1:
                errors.append(ProposalDiagnostic(
                    chunk_id=chunk_id, proposal_index=index, proposal=repaired["schema_proposals"][index],
                    status="rejected", reason="repair requires one unique failed original replaces_proposal_index",
                    response_hash=response.artifact_hash, channel="repair"))
                continue
            valid_repair_indexes.add(index)
            supersessions.append(RepairSupersession(
                chunk_id=chunk_id, original_response_hash=original_hashes[chunk_id],
                original_proposal_index=link, repair_request_hash=request.artifact_hash,
                repair_response_hash=response.artifact_hash, repair_proposal_index=index,
            ))
        replaced = {links[index] for index in valid_repair_indexes}
        changes = [entry for entry in changes
                   if not (entry[4] == chunk_id and entry[3] == original_hashes[chunk_id] and entry[1] in replaced)]
        errors = [d for d in errors if not (d.chunk_id == chunk_id and d.response_hash == original_hashes[chunk_id]
                                          and d.channel == "schema_proposals" and d.proposal_index in replaced)]
        batch, waiting, invalid = _proposals(repaired, item, response.artifact_hash, repair=True)
        changes.extend(entry for entry in batch if entry[1] in valid_repair_indexes)
        pending.extend(waiting)
        errors.extend(d for d in invalid if d.channel != "schema_proposals" or d.proposal_index in valid_repair_indexes)
    next_snapshot, decisions, diagnostics, records = _barrier(snapshot, items, changes, pending, errors)
    return _WindowEvaluation(next_snapshot, decisions, diagnostics, records, pending, contexts,
                             initial_diagnostics, supersessions, repairable)


def _pending_ledger(logs):
    return [p.model_dump(mode="json") for log in logs for p in log.pending] + [
        d.model_dump(mode="json") for log in logs for d in log.diagnostics]


def _pending_display(value):
    """Explicitly omit oversized summary fields; their complete values stay local."""
    encoded = canonical_json(value)
    if len(encoded) <= 256:
        return value
    return {"omitted_from_summary": True, "value_hash": canonical_sha256(value),
            "serialized_codepoints": len(encoded)}


def _pending_context_v1(logs):
    """Frozen v1.0 renderer: existing request bytes remain reproducible."""
    ledger = _pending_ledger(logs)
    by_observation = {record.observation_id: record for log in logs for record in log.records}
    by_candidate = {(record.chunk_id, record.candidate_index): record for log in logs for record in log.records}
    concept_kinds = {decision.change.concept.concept_id: decision.change.concept.kind
                     for log in logs for decision in log.decisions if decision.change.concept is not None}
    groups = {}
    for item in ledger:
        proposal = item.get("proposal", {})
        proposal = proposal if isinstance(proposal, dict) else {}
        concept = proposal.get("concept", {})
        concept = concept if isinstance(concept, dict) else {}
        candidate_indexes = proposal.get("candidate_indices", [])
        candidate_indexes = candidate_indexes if isinstance(candidate_indexes, list) else []
        observation_ids = item.get("observation_ids", proposal.get("observation_ids", []))
        observation_ids = observation_ids if isinstance(observation_ids, list) else []
        evidence = [by_observation[oid] for oid in observation_ids if isinstance(oid, str) and oid in by_observation]
        evidence.extend(by_candidate[(item.get("chunk_id"), index)] for index in candidate_indexes
                        if type(index) is int and (item.get("chunk_id"), index) in by_candidate)
        observed_kinds = sorted({record.kind for record in evidence if isinstance(record.kind, str)})
        observed_terms = sorted({record.observed_term for record in evidence if isinstance(record.observed_term, str)})
        concept_id = proposal.get("concept_id", concept.get("concept_id"))
        known_kind = concept_kinds.get(concept_id) if isinstance(concept_id, str) else None
        scope = {
            key: concept[key] for key in (
                "owner_type_ids", "source_type_ids", "target_type_ids", "direction",
                "parent_type_id", "endpoint_policy", "value_type",
            ) if key in concept
        }
        scope["concept_id"] = concept_id
        scope["layer"] = proposal.get("layer", concept.get("layer"))
        if "identity_policy" in concept:
            scope["identity_policy_hash"] = canonical_sha256(concept["identity_policy"])
        scope = {key: value for key, value in scope.items() if value is not None}
        signature = {
            "kind": concept.get("kind", known_kind or (observed_kinds[0] if len(observed_kinds) == 1 else None)),
            "term": proposal.get("alias", concept.get("name", observed_terms[0] if len(observed_terms) == 1 else None)),
            "scope": scope, "action": proposal.get("action", "pending"),
            "status": item.get("status", "pending"), "channel": item.get("channel", "pending"),
            "conflict_reason": item["reason"],
        }
        key = canonical_sha256(signature)
        entry = groups.setdefault(key, {
            "signature_hash": key, "signature": signature, "count": 0, "examples": [],
        })
        entry["count"] += 1
        if "response_hash" in item:
            example = (item["response_hash"], item.get("channel", "schema_proposals"), item["proposal_index"])
        else:
            example = (None, "pending", canonical_sha256(item))
        if example not in entry["examples"] and len(entry["examples"]) < 3:
            entry["examples"].append(example)
    reasons, scopes, responses, entries = [], [], [], []
    reason_ids, scope_ids, response_ids = {}, {}, {}

    def intern(value, catalog, ids):
        key = canonical_sha256(value)
        if key not in ids:
            ids[key] = len(catalog)
            catalog.append(value)
        return ids[key]

    for key in sorted(groups):
        group = groups[key]
        signature = group["signature"]
        scope = {field: _pending_display(value) for field, value in signature["scope"].items()}
        reason = {name: _pending_display(signature[name]) for name in ("status", "channel", "conflict_reason")}
        examples = []
        for response_hash, channel, index in group["examples"]:
            if response_hash is None:
                examples.append(f"pending:{index}")
            else:
                response_index = intern(response_hash, responses, response_ids)
                examples.append(f"response:{response_index}:{channel}:{index}")
        entries.append({
            "signature_hash": key,
            **{name: _pending_display(signature[name]) for name in ("kind", "term", "action") if signature[name] is not None},
            "scope_id": intern(scope, scopes, scope_ids), "reason_id": intern(reason, reasons, reason_ids),
            "count": group["count"], "example_ids": examples,
        })
    return {
        "format_version": PENDING_CONTEXT_V1,
        "authority": "working_vocabulary_context_only",
        "coverage": "aggregated_pending_vocabulary_not_occurrence_adjudication",
        "full_ledger_hash": canonical_sha256(ledger), "ledger_entry_count": len(ledger),
        "unique_signature_count": len(groups), "max_example_ids_per_signature": 3,
        "full_diagnostics_and_source_proposals": "retained_in_local_window_ledgers",
        "example_id_scope": "response:<response_catalog_index>:<channel>:<proposal_index>; pending:<entry_hash>",
        "scope_catalog": scopes, "reason_catalog": reasons, "response_catalog": responses, "entries": entries,
    }


def _pending_context(logs):
    """Columnar type-conflict context, not an instance/identity merge operation."""
    ledger = _pending_ledger(logs)
    observations = {r.observation_id: r for log in logs for r in log.records}
    candidates = {(r.chunk_id, r.candidate_index): r for log in logs for r in log.records}
    concept_kinds = {d.change.concept.concept_id: d.change.concept.kind
                     for log in logs for d in log.decisions if d.change.concept is not None}
    groups = {}

    def type_set(value):
        return sorted(set(value)) if isinstance(value, list) and all(isinstance(v, str) for v in value) else value

    for ledger_index, item in enumerate(ledger):
        proposal = item.get("proposal", {})
        proposal = proposal if isinstance(proposal, dict) else {}
        concept = proposal.get("concept", {})
        concept = concept if isinstance(concept, dict) else {}
        indexes = proposal.get("candidate_indices", [])
        indexes = indexes if isinstance(indexes, list) else []
        observation_ids = item.get("observation_ids", proposal.get("observation_ids", []))
        observation_ids = observation_ids if isinstance(observation_ids, list) else []
        evidence = [observations[oid] for oid in observation_ids if isinstance(oid, str) and oid in observations]
        evidence.extend(candidates[(item.get("chunk_id"), i)] for i in indexes
                        if type(i) is int and (item.get("chunk_id"), i) in candidates)
        kinds = sorted({r.kind for r in evidence if isinstance(r.kind, str)})
        terms = sorted({r.observed_term for r in evidence if isinstance(r.observed_term, str)})
        proposed_id = proposal.get("concept_id", concept.get("concept_id"))
        known_kind = concept_kinds.get(proposed_id) if isinstance(proposed_id, str) else None
        kind = concept.get("kind", known_kind or (kinds[0] if len(kinds) == 1 else None))
        action = proposal.get("action", "pending")
        scope = {
            "parent": concept.get("parent_type_id"),
            "owners": type_set(concept.get("owner_type_ids", sorted({r.owner_type_id for r in evidence if r.owner_type_id}))),
            "sources": type_set(concept.get("source_type_ids", sorted({r.source_type_id for r in evidence if r.source_type_id}))),
            "targets": type_set(concept.get("target_type_ids", sorted({r.target_type_id for r in evidence if r.target_type_id}))),
            "direction": concept.get("direction", "source_to_target" if kind == "relationship" else None),
            "endpoint_policy": concept.get("endpoint_policy", "exact"),
            "layer": proposal.get("layer", concept.get("layer")),
            "value_type": concept.get("value_type"),
            "identity_policy": canonical_sha256(concept.get("identity_policy", {})),
            # A proposed ID is membership metadata, not an additive type's scope.
            # Existing targets remain essential for aliases and breaking changes.
            "target_concept": proposed_id if action != "add_concept" else None,
        }
        signature = {
            "kind": kind, "term": proposal.get("alias", concept.get("name", terms[0] if len(terms) == 1 else None)),
            "action": action, "scope": scope,
            "reason": [item.get("status", "pending"), item.get("channel", "pending"), item["reason"]],
        }
        key = canonical_sha256(signature)
        group = groups.setdefault(key, {
            "signature": signature, "members": [], "proposed_ids": set(), "sources": set(), "examples": [],
        })
        group["members"].append(canonical_sha256(item))
        if proposed_id is not None:
            group["proposed_ids"].add(canonical_sha256(proposed_id))
        if "response_hash" in item:
            group["sources"].add(canonical_sha256({
                "response_hash": item["response_hash"], "channel": item.get("channel", "schema_proposals"),
                "proposal_index": item["proposal_index"], "chunk_id": item["chunk_id"],
            }))
        else:
            group["sources"].update(canonical_sha256({"observation_id": r.observation_id}) for r in evidence)
        if len(group["examples"]) < 3:
            group["examples"].append(ledger_index)

    catalogs = {
        name: [] for name in (
            "kinds", "terms", "actions", "identifiers", "directions", "endpoint_policies",
            "layers", "value_types", "identity_policy_hashes", "invalid_scope_values",
            "statuses", "channels", "conflict_reasons", "scopes", "reasons",
        )
    }
    catalog_indexes = {name: {} for name in catalogs}

    def intern(name, value, *, display=True):
        if value is None:
            return None
        key = canonical_sha256(value)
        if key not in catalog_indexes[name]:
            catalog_indexes[name][key] = len(catalogs[name])
            catalogs[name].append(_pending_display(value) if display else value)
        return catalog_indexes[name][key]

    def identifier(value):
        if value is None or isinstance(value, str):
            return intern("identifiers", value)
        return {"invalid": intern("invalid_scope_values", value)}

    def identifiers(value):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [identifier(v) for v in value]
        return {"invalid": intern("invalid_scope_values", value)}

    scope_columns = ["parent", "owners", "sources", "targets", "direction", "endpoint_policy",
                     "layer", "value_type", "identity_policy", "target_concept"]
    columns = ["kind", "term", "action", "scope", "reason", "count", "proposed_id_count",
               "source_member_count", "example_ids"]
    rows, membership = [], []
    for key in sorted(groups):
        group = groups[key]
        signature = group["signature"]
        scope = signature["scope"]
        scope_row = [
            identifier(scope["parent"]), identifiers(scope["owners"]),
            identifiers(scope["sources"]), identifiers(scope["targets"]),
            intern("directions", scope["direction"]), intern("endpoint_policies", scope["endpoint_policy"]),
            intern("layers", scope["layer"]), intern("value_types", scope["value_type"]),
            intern("identity_policy_hashes", scope["identity_policy"]), identifier(scope["target_concept"]),
        ]
        reason_row = [intern(name, value) for name, value in zip(
            ("statuses", "channels", "conflict_reasons"), signature["reason"])]
        rows.append([
            intern("kinds", signature["kind"]), intern("terms", signature["term"]),
            intern("actions", signature["action"]), intern("scopes", scope_row, display=False),
            intern("reasons", reason_row, display=False), len(group["members"]),
            len(group["proposed_ids"]), len(group["sources"]), group["examples"],
        ])
        membership.append({
            "signature": key, "members": group["members"],
            "proposed_ids": sorted(group["proposed_ids"]), "sources": sorted(group["sources"]),
        })
    return {
        "format_version": PENDING_CONTEXT_VERSION, "authority": "working_vocabulary_context_only",
        "coverage": "aggregated_pending_vocabulary_not_occurrence_adjudication",
        "identity_effect": "no_instance_or_concept_identity_merge",
        "type_signature_policy": "kind/name/parent/owner/endpoints/layer/value_type/identity_policy/conflict;"
                                 "add_concept_ID_is_membership_only;alias_target_ID_is_scope;endpoint_ID_sets_are_literal",
        "full_ledger_hash": canonical_sha256(ledger), "ledger_entry_count": len(ledger),
        "unique_signature_count": len(groups), "membership_index_hash": canonical_sha256(membership),
        "catalog_hash": canonical_sha256(catalogs), "max_example_ids_per_signature": 3,
        "source_member_count_unit": "distinct response/proposal references or observation IDs",
        "example_id_scope": "zero-based entries in the exact local ledger bound by full_ledger_hash",
        "full_diagnostics_and_source_proposals": "retained_in_local_window_ledgers",
        "columns": columns, "scope_columns": scope_columns,
        "reason_columns": ["status", "channel", "conflict_reason"],
        "column_catalogs": {
            "kind": "kinds", "term": "terms", "action": "actions", "scope": "scopes", "reason": "reasons",
        },
        "scope_column_catalogs": {
            "parent": "identifiers", "owners": "identifiers", "sources": "identifiers", "targets": "identifiers",
            "direction": "directions", "endpoint_policy": "endpoint_policies", "layer": "layers",
            "value_type": "value_types", "identity_policy": "identity_policy_hashes", "target_concept": "identifiers",
        },
        "reason_column_catalogs": {"status": "statuses", "channel": "channels", "conflict_reason": "conflict_reasons"},
        "catalogs": catalogs, "rows": rows,
    }


def _expected_pending(stored, history):
    if isinstance(stored, list):
        return _pending_ledger(history)
    if isinstance(stored, dict) and stored.get("format_version") == PENDING_CONTEXT_V1:
        return _pending_context_v1(history)
    if isinstance(stored, dict) and stored.get("format_version") == PENDING_CONTEXT_VERSION:
        return _pending_context(history)
    raise ValueError("unsupported pending context representation")


def _resumable_request(root, manifest, snapshot, chunk, pending, legacy_pending, *, previous_pending=None, repair=None):
    preferred = _request(manifest, snapshot, chunk, pending, repair=repair)
    previous = _request(manifest, snapshot, chunk, previous_pending, repair=repair) if previous_pending is not None else None
    legacy = _request(manifest, snapshot, chunk, legacy_pending, repair=repair)
    for candidate in (preferred, previous, legacy):
        if candidate is None:
            continue
        key = candidate.artifact_hash
        if ((root / "responses" / f"{key}.json").exists()
                or any((root / "dispatches").glob(f"{key}-*.json"))):
            stored = _read(root / "requests" / f"{key}.json", _Ledger, "request")
            if stored != candidate:
                raise ValueError("cached request differs from exact supported pending representation")
            # A durable legacy response is reusable. An uncertain legacy
            # dispatch must also retain its identity, never become a fresh call.
            return stored
    return preferred


def _validate_exchanges(log, items, snapshot, *, prepared, context, manifest_hash, model_hash, config, history):
    expected_requests = [e.request.artifact_hash for e in log.exchanges]
    expected_responses = [e.response.artifact_hash for e in log.exchanges]
    if (log.request_hashes + log.repair_request_hashes != expected_requests
            or log.response_hashes + log.repair_response_hashes != expected_responses
            or len(log.request_hashes) != len(items)
            or len(log.response_hashes) != len(items)):
        raise ValueError("window embedded request/response accounting mismatch")
    by_id = {item.chunk.chunk_id: item for item in items}
    units = {u.source_unit_id: u for u in prepared.source_units}
    for position, exchange in enumerate(log.exchanges):
        request, response = exchange.request, exchange.response
        chunk_id = request.payload.get("chunk_id")
        if (request.kind != "request" or response.kind != "response" or chunk_id not in by_id
                or response.payload.get("request_hash") != request.artifact_hash
                or request.payload.get("manifest_hash") != manifest_hash
                or request.payload.get("context_hash") != context.artifact_hash
                or request.payload.get("schema_hash") != snapshot.artifact_hash
                or request.payload.get("model_hash") != model_hash
                or request.payload.get("prompt_version") != config.prompt_version
                or request.payload.get("repair") != (position >= len(items))):
            raise ValueError("window embedded request/response identity mismatch")
        item = by_id[chunk_id]
        if position < len(items) and item != items[position]:
            raise ValueError("window original request order differs")
        payload = json.loads(request.payload["request"]["user"])["input"]
        expected_payload = {
            **_source_payload(prepared, item.chunk), "context": context.model_dump(mode="json"),
            "context_hash": context.artifact_hash, "schema": snapshot.model_dump(mode="json"),
            "schema_hash": snapshot.artifact_hash, "schema_version": snapshot.version,
            "pending": _expected_pending(payload.get("pending"), history),
        }
        if position >= len(items):
            expected_payload["repair"] = payload.get("repair")
        if payload != expected_payload:
            raise ValueError("window request source/context/schema drift")
        if position < len(items):
            raw = _decode(response, config)
            expected_item = _grounded_observation(item.chunk, request.artifact_hash, {"candidates": raw["candidates"]},
                                                  units[item.chunk.source_unit_id], request_prompt_version=None)
            if item != expected_item:
                raise ValueError("window observation differs from durable raw response")


def _validate_evaluation(log, evaluation, snapshot):
    if (evaluation.snapshot.artifact_hash != log.after_hash
            or evaluation.decisions != log.decisions
            or evaluation.diagnostics != log.diagnostics
            or evaluation.records != log.records
            or evaluation.pending != log.pending
            or evaluation.contexts != log.working_context
            or evaluation.original_diagnostics != log.original_diagnostics
            or evaluation.supersessions != log.supersessions
            or log.no_op != (snapshot.concepts == evaluation.snapshot.concepts)):
        raise ValueError("window evaluation differs from persisted raw proposal envelopes")


def _load_commits(root, manifest):
    snapshot, logs, observations = manifest.seed, [], []
    paths = sorted((root / "windows").glob("*.json"))
    for index, path in enumerate(paths):
        if path.name != f"{index:06d}.json" or index >= len(manifest.windows):
            raise ValueError("missing/reordered window commit")
        commit = _read(path, _Commit)
        log = commit.log
        if (commit.manifest_hash != manifest.artifact_hash or log.window_index != index
                or log.chunk_ids != manifest.windows[index] or log.before_hash != snapshot.artifact_hash
                or log.after_hash != commit.snapshot.artifact_hash
                or commit.snapshot.before_hash != snapshot.artifact_hash
                or commit.snapshot.version != index + 1
                or log.previous_log_hash != (logs[-1].artifact_hash if logs else None)):
            raise ValueError("window commit binding/chain mismatch")
        if (len(log.request_hashes) != len(log.chunk_ids)
                or len(log.response_hashes) != len(log.request_hashes)
                or [item.chunk.chunk_id for item in commit.chunks] != log.chunk_ids):
            raise ValueError("window commit request/chunk accounting mismatch")
        for position, (key, digest) in enumerate(zip(log.request_hashes, log.response_hashes)):
            request = _read(root / "requests" / f"{key}.json", _Ledger, "request")
            response = _read(root / "responses" / f"{key}.json", _Ledger, "response")
            if (request.artifact_hash != key or response.artifact_hash != digest
                    or response.payload["request_hash"] != key
                    or request.payload["manifest_hash"] != manifest.artifact_hash
                    or request.payload["schema_hash"] != snapshot.artifact_hash):
                raise ValueError("window request/response ledger mismatch")
            chunk = commit.chunks[position].chunk
            payload = json.loads(request.payload["request"]["user"])["input"]
            if (request.payload["chunk_id"] != chunk.chunk_id
                    or payload["context"] != manifest.inputs.context.model_dump(mode="json")
                    or payload["schema"] != snapshot.model_dump(mode="json")
                    or any(payload.get(k) != value for k, value in _source_payload(manifest.inputs.prepared, chunk).items())):
                raise ValueError("window request source/context/schema drift")
            raw = _decode(response, manifest.config)
            unit = next(u for u in manifest.inputs.prepared.source_units if u.source_unit_id == chunk.source_unit_id)
            expected = _grounded_observation(chunk, key, {"candidates": raw["candidates"]}, unit,
                                             request_prompt_version=None)
            if expected != commit.chunks[position]:
                raise ValueError("window observation differs from durable raw response")
        if len(log.repair_request_hashes) != len(log.repair_response_hashes):
            raise ValueError("window repair response accounting mismatch")
        for key, digest in zip(log.repair_request_hashes, log.repair_response_hashes):
            request = _read(root / "requests" / f"{key}.json", _Ledger, "request")
            response = _read(root / "responses" / f"{key}.json", _Ledger, "response")
            if (request.artifact_hash != key or response.artifact_hash != digest
                    or response.payload["request_hash"] != key
                    or request.payload["manifest_hash"] != manifest.artifact_hash
                    or request.payload["schema_hash"] != snapshot.artifact_hash
                    or not request.payload["repair"]):
                raise ValueError("window repair ledger mismatch")
        _validate_exchanges(
            log, commit.chunks, snapshot, prepared=manifest.inputs.prepared, context=manifest.inputs.context,
            manifest_hash=manifest.artifact_hash, model_hash=manifest.model_hash, config=manifest.config,
            history=logs)
        for exchange in log.exchanges:
            key = exchange.request.artifact_hash
            if (_read(root / "requests" / f"{key}.json", _Ledger, "request") != exchange.request
                    or _read(root / "responses" / f"{key}.json", _Ledger, "response") != exchange.response):
                raise ValueError("embedded window envelopes differ from persisted ledgers")
        evaluation = _effective_window(snapshot, commit.chunks, log.exchanges, manifest.config)
        _validate_evaluation(log, evaluation, snapshot)
        if commit.snapshot != evaluation.snapshot:
            raise ValueError("committed schema differs from raw proposal evaluation")
        observations.extend(commit.chunks)
        logs.append(log)
        snapshot = commit.snapshot
    return snapshot, logs, observations


def _publish(root, manifest, snapshot, logs, observations, reason, counters):
    records = [r for c in observations for r in map_chunk(c, snapshot)]
    mapping = _seal(WindowedMapping, snapshot_hash=snapshot.artifact_hash,
                    schema_version=snapshot.version, records=records)
    complete = (len(observations) == len(manifest.chunk_plan) and bool(snapshot.concepts)
                and all(s.status in {"processed", "no_candidates"} for s in manifest.inputs.prepared.sources))
    dispatches = [_read(p, _Ledger, "dispatch") for p in (root / "dispatches").glob("*.json")]
    run = _seal(
        WindowedRun, prepared=manifest.inputs.prepared, context=manifest.inputs.context,
        config=manifest.config, chunk_plan=manifest.chunk_plan,
        source_cache_hash=manifest.inputs.source_cache_hash, manifest_hash=manifest.artifact_hash,
        model_version=manifest.model_version, model_hash=manifest.model_hash, prompt_hash=manifest.prompt_hash,
        chunks=observations, final_snapshot=snapshot, final_mapping=mapping,
        state="complete" if complete else "partial",
        reason=None if complete else (
            "bootstrap_blocked" if reason == "source_preparation_incomplete" and not snapshot.concepts and observations else reason),
        cursor=len(observations), last_chunk_id=observations[-1].chunk.chunk_id if observations else None,
        logs=logs, model_call_count=len(dispatches),
        repair_call_count=sum(d.payload["repair"] for d in dispatches),
        reserved_tokens=sum(d.payload["reserved_tokens"] for d in dispatches),
        working_context=[context for log in logs for context in log.working_context],
    )
    _write(root / "reports" / f"{run.artifact_hash}.json", run)
    if complete:
        _write(root / "run.json", run)
        _write(root / "schema.json", snapshot)
        _write(root / "mapping.json", mapping)
    pointer = _seal(_Ledger, kind="latest", payload={"run_hash": run.artifact_hash})
    staging = root / f".latest-{uuid.uuid4().hex}.json"
    _write(staging, pointer)
    os.replace(staging, root / "latest.json")
    report = _seal(_Ledger, kind="invocation", payload={"run_hash": run.artifact_hash, **counters})
    _write(root / "invocations" / f"{uuid.uuid4().hex}.json", report)
    return WindowedExecutionResult(run=run, execution_report_hash=report.artifact_hash, **counters)


@_coordinator_lock
def run_windowed(*, inputs, output_dir, config=RunConfig(), budget=RunBudget(),
                 client=None, model_version="none", model_hash=None):
    """Resume exact immutable requests; uncertain remote dispatch requires opt-in."""
    root = Path(output_dir)
    inputs = WindowInputs.model_validate(inputs.model_dump(mode="python"))
    _verify_prepared(inputs.prepared)
    chunks, windows = _plan(inputs, config)
    manifest = _seal(_Manifest, inputs=inputs, config=config, chunk_plan=chunks, windows=windows,
                     seed=seed_snapshot(), model_version=model_version,
                     model_hash=model_hash or canonical_sha256({"model_version": model_version}),
                     prompt_hash=canonical_sha256({"system": RUN_SYSTEM, "response": WorkingResponse.model_json_schema(),
                                                  "proposals": _ProposedChange.model_json_schema(),
                                                  "candidates": RawCandidateResponse.model_json_schema()}))
    _write(root / "manifest.json", manifest)
    snapshot, logs, observations = _load_commits(root, manifest)
    counters = dict(model_call_count=0, repair_call_count=0, reserved_tokens=0,
                    reused_response_count=0, reused_window_count=len(logs))
    if (root / "run.json").exists():
        run = load_windowed_run(root)
        if run.manifest_hash != manifest.artifact_hash:
            raise ValueError("completed window authority differs from invocation")
        return _publish(root, manifest, snapshot, logs, observations, None, counters)
    by_id = {c.chunk_id: c for c in chunks}
    units = {u.source_unit_id: u for u in inputs.prepared.source_units}
    document_ids = [s.source_file_id for s in inputs.prepared.sources]
    initial_windows = len(logs)
    reason = "source_preparation_incomplete"
    repair_total = sum(_read(p, _Ledger, "dispatch").payload["repair"]
                       for p in (root / "dispatches").glob("*.json"))

    def reserve(request):
        nonlocal repair_total
        key = request.artifact_hash
        _write(root / "requests" / f"{key}.json", request)
        response_path = root / "responses" / f"{key}.json"
        if response_path.exists():
            response = _read(response_path, _Ledger, "response")
            if response.payload["request_hash"] != key:
                raise ValueError("cached response request mismatch")
            counters["reused_response_count"] += 1
            return response, None
        is_repair = request.payload["repair"]
        request_chars = len(canonical_json(request.payload["request"]))
        request_char_budget = config.max_request_chars if budget.request_char_budget is None else budget.request_char_budget
        if request_chars > request_char_budget:
            return None, "context_limit"
        prior_dispatches = sorted((root / "dispatches").glob(f"{key}-*.json"))
        if prior_dispatches:
            last_dispatch = _read(prior_dispatches[-1], _Ledger, "dispatch")
            invalid_path = root / "received-invalid" / f"{key}-{last_dispatch.payload['attempt']:04d}.json"
            if invalid_path.exists():
                invalid = _read(invalid_path, _Ledger, "received_invalid_response")
                diagnostic_hash = invalid.payload["provider_diagnostic_hash"]
                diagnostic = _read(root / "diagnostics" / f"{key}-{diagnostic_hash}.json", DiscoveryProviderDiagnostic)
                if (invalid.payload["request_hash"] != key
                        or invalid.payload["dispatch_hash"] != last_dispatch.artifact_hash
                        or diagnostic.request_hash != key or diagnostic.artifact_hash != diagnostic_hash):
                    raise ValueError("received-invalid provider diagnostic binding mismatch")
                if not budget.retry_invalid_response:
                    return None, "received_invalid_response_requires_explicit_retry"
            elif not budget.retry_uncertain:
                return None, "uncertain_dispatch_requires_explicit_retry"
        reserve_tokens = len(canonical_json(request.payload["request"]).encode("utf-8")) + config.max_completion_tokens
        if (counters["model_call_count"] >= budget.max_calls
                or (budget.max_tokens and counters["reserved_tokens"] + reserve_tokens > budget.max_tokens)):
            return None, "model_budget_exhausted"
        if is_repair and repair_total >= budget.max_repair_calls:
            return None, "repair_budget_exhausted"
        if client is None:
            return None, "model_client_required"
        dispatch = _seal(_Ledger, kind="dispatch", payload={
            "request_hash": key, "repair": is_repair, "reserved_tokens": reserve_tokens,
            "attempt": len(prior_dispatches), "explicit_retry": bool(prior_dispatches),
            "request_chars": request_chars, "effective_request_char_budget": request_char_budget,
        })
        _write(root / "dispatches" / f"{key}-{len(prior_dispatches):04d}.json", dispatch)
        counters["model_call_count"] += 1
        counters["reserved_tokens"] += reserve_tokens
        counters["repair_call_count"] += int(is_repair)
        repair_total += int(is_repair)
        return None, None

    def dispatch(request):
        key = request.artifact_hash
        dispatched = _read(sorted((root / "dispatches").glob(f"{key}-*.json"))[-1], _Ledger, "dispatch")
        try:
            raw = client.complete_json(**request.payload["request"])
            response = _seal(_Ledger, kind="response", payload={
                "request_hash": request.artifact_hash, "response": raw,
            })
            _write(root / "responses" / f"{request.artifact_hash}.json", response)
            return response, None
        except Exception as exc:
            provider_hash = _save_provider_diagnostic(root, key, exc)
            if provider_hash is not None:
                diagnostic = _seal(_Ledger, kind="received_invalid_response", payload={
                    "request_hash": key, "dispatch_hash": dispatched.artifact_hash,
                    "attempt": dispatched.payload["attempt"], "provider_diagnostic_hash": provider_hash,
                    "error": str(exc), "error_type": type(exc).__name__,
                })
                _write(root / "received-invalid" / f"{key}-{dispatched.payload['attempt']:04d}.json", diagnostic)
                return None, "received_invalid_response_requires_explicit_retry"
            diagnostic = _seal(_Ledger, kind="transport_error", payload={
                "request_hash": key, "dispatch_hash": dispatched.artifact_hash,
                "attempt": dispatched.payload["attempt"], "error": str(exc), "error_type": type(exc).__name__,
            })
            _write(root / "errors" / f"{diagnostic.artifact_hash}.json", diagnostic)
            return None, "uncertain_dispatch_requires_explicit_retry"

    for index in range(len(logs), len(windows)):
        if budget.max_windows is not None and len(logs) - initial_windows >= budget.max_windows:
            reason = "window_budget_exhausted"
            break
        window_chunks = [by_id[cid] for cid in windows[index]]
        if (budget.stop_after_document is not None
                and document_ids.index(window_chunks[0].source_file_id) >= budget.stop_after_document):
            reason = "document_stop"
            break
        pending_context = _pending_context(logs)
        previous_pending = _pending_context_v1(logs)
        legacy_pending = _pending_ledger(logs)
        requests = [_resumable_request(root, manifest, snapshot, chunk, pending_context, legacy_pending,
                                       previous_pending=previous_pending)
                    for chunk in window_chunks]
        responses, needs_dispatch = {}, []
        blocked = None
        for request in requests:
            response, error = reserve(request)
            if error:
                blocked = error
                break
            if response is not None:
                responses[request.artifact_hash] = response
            else:
                needs_dispatch.append(request)
        # Reserved jobs must always be dispatched, even when a later job hits a
        # budget. Their durable responses are reused before this barrier commits.
        with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
            received = list(pool.map(dispatch, needs_dispatch))
        for request, (response, error) in zip(needs_dispatch, received):
            if response is not None:
                responses[request.artifact_hash] = response
            else:
                blocked = error
        if blocked:
            reason = blocked
            break
        items, exchanges = [], []
        decoded = {}
        try:
            for chunk, request in zip(window_chunks, requests):
                response = responses[request.artifact_hash]
                raw = _decode(response, config)
                item = _grounded_observation(
                    chunk, request.artifact_hash, {"candidates": raw["candidates"]},
                    units[chunk.source_unit_id], request_prompt_version=None)
                items.append(item)
                decoded[chunk.chunk_id] = raw
                exchanges.append(WindowedExchange(request=request, response=response))
        except (TypeError, ValueError) as exc:
            reason = f"response_validation_failed: {exc}"
            break
        original_evaluation = _effective_window(snapshot, items, exchanges, config)
        repair_requests = []
        repair_responses = []
        for item in items:
            failed = original_evaluation.repairable[item.chunk.chunk_id]
            if not failed:
                continue
            repair = _resumable_request(root, manifest, snapshot, item.chunk, pending_context, legacy_pending,
                                       previous_pending=previous_pending, repair=_repair_payload(
                item, decoded[item.chunk.chunk_id], failed, responses[item.request_hash].artifact_hash))
            # Do not write an unattempted repair request unless a cached response
            # exists or the caller has explicitly supplied a repair budget.
            if (repair_total >= budget.max_repair_calls
                    and not (root / "responses" / f"{repair.artifact_hash}.json").exists()
                    and not list((root / "dispatches").glob(f"{repair.artifact_hash}-*.json"))):
                continue
            response, error = reserve(repair)
            if error:
                if (error in {"uncertain_dispatch_requires_explicit_retry", "received_invalid_response_requires_explicit_retry"}
                        or list((root / "dispatches").glob(f"{repair.artifact_hash}-*.json"))):
                    blocked = error
                    break
                continue
            if response is None:
                response, error = dispatch(repair)
            if error:
                blocked = error
                break
            repair_requests.append(repair.artifact_hash)
            repair_responses.append(response.artifact_hash)
            exchanges.append(WindowedExchange(request=repair, response=response))
        if blocked:
            reason = blocked
            break
        effective = _effective_window(snapshot, items, exchanges, config)
        next_snapshot, decisions, diagnostics, records = (
            effective.snapshot, effective.decisions, effective.diagnostics, effective.records)
        evaluation = _seal(_Ledger, kind="evaluation", payload={
            "manifest_hash": manifest.artifact_hash, "window_index": index,
            "schema_hash": snapshot.artifact_hash, "next_schema_hash": next_snapshot.artifact_hash,
            "request_hashes": [r.artifact_hash for r in requests],
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "diagnostics": [d.model_dump(mode="json") for d in diagnostics],
        })
        _write(root / "evaluations" / f"{evaluation.artifact_hash}.json", evaluation)
        before = {r.observation_id: r for old in observations for r in map_chunk(old, snapshot)}
        after = [r for old in observations for r in map_chunk(old, next_snapshot)]
        log = _seal(
            WindowedLog, window_index=index, chunk_ids=windows[index],
            before_hash=snapshot.artifact_hash, after_hash=next_snapshot.artifact_hash,
            previous_log_hash=logs[-1].artifact_hash if logs else None,
            input_schema_version=snapshot.version, context_hash=inputs.context.artifact_hash,
            request_hashes=[r.artifact_hash for r in requests],
            response_hashes=[responses[r.artifact_hash].artifact_hash for r in requests],
            repair_request_hashes=repair_requests, repair_response_hashes=repair_responses,
            exchanges=exchanges, original_diagnostics=effective.original_diagnostics,
            supersessions=effective.supersessions,
            records=records, decisions=decisions,
            diagnostics=diagnostics, pending=effective.pending, working_context=effective.contexts,
            remapped_previous_count=sum((r.status, r.concept_id) !=
                                        (before[r.observation_id].status, before[r.observation_id].concept_id) for r in after),
            newly_mapped_previous_count=sum(r.status == "mapped" and before[r.observation_id].status != "mapped" for r in after),
            no_op=snapshot.concepts == next_snapshot.concepts,
        )
        commit = _seal(_Commit, manifest_hash=manifest.artifact_hash, snapshot=next_snapshot, log=log, chunks=items)
        _write(root / "windows" / f"{index:06d}.json", commit)
        observations.extend(items)
        logs.append(log)
        snapshot = next_snapshot
        if not snapshot.concepts:
            reason = "bootstrap_blocked"
            break
    return _publish(root, manifest, snapshot, logs, observations, reason, counters)


def windowed_model_binding(path):
    """Read resume identity without constructing a client or requiring a report.

    The manifest exists before dispatch, including an interruption before the
    first response or publication. run_windowed independently checks all drift.
    """
    path = Path(path)
    manifest = _read(path / "manifest.json" if path.is_dir() else path, _Manifest)
    return {
        "model_version": manifest.model_version, "model_hash": manifest.model_hash,
        "prompt_version": manifest.config.prompt_version, "prompt_hash": manifest.prompt_hash,
        "manifest_hash": manifest.artifact_hash, "inputs_hash": manifest.inputs.artifact_hash,
        "config": manifest.config.model_dump(mode="json"),
    }


def load_windowed_run(path):
    path = Path(path)
    root = path if path.is_dir() else None
    if root is not None:
        if (root / "run.json").exists():
            path = root / "run.json"
        else:
            pointer = _read(root / "latest.json", _Ledger, "latest")
            path = root / "reports" / f"{pointer.payload['run_hash']}.json"
    run = _read(path, WindowedRun)
    _verify_chunks(run.prepared, run.chunk_plan)
    _verify_prepared(run.prepared)
    if root is not None:
        manifest = _read(root / "manifest.json", _Manifest)
        snapshot, logs, chunks = _load_commits(root, manifest)
        if (run.manifest_hash != manifest.artifact_hash or run.final_snapshot != snapshot
                or run.logs != logs or run.chunks != chunks):
            raise ValueError("window run differs from immutable commit authority")
    return run


@dataclass(frozen=True)
class _CommittedProgress:
    prepared: PreparedCorpus
    chunk_plan: list[DiscoveryChunk]
    snapshot: WorkingSchemaSnapshot
    logs: list[WindowedLog]
    chunks: list[ChunkObservation]
    manifest_hash: str
    report: WindowedRun | None
    model_call_count: int
    repair_call_count: int
    reserved_tokens: int

    @property
    def publication_state(self):
        if self.report is not None and self.report.cursor == len(self.chunks):
            return "published"
        return "unpublished_progress" if self.chunks else "report_pending"


def _committed_progress(path):
    """Inspect an immutable prefix without locking, publishing, or claiming liveness."""
    path = Path(path)
    if not path.is_dir():
        run = load_windowed_run(path)
        return _CommittedProgress(
            run.prepared, run.chunk_plan, run.final_snapshot, run.logs, run.chunks,
            run.manifest_hash, run, run.model_call_count, run.repair_call_count, run.reserved_tokens)
    manifest = _read(path / "manifest.json", _Manifest)
    _verify_prepared(manifest.inputs.prepared)
    _verify_chunks(manifest.inputs.prepared, manifest.chunk_plan)
    snapshot, logs, chunks = _load_commits(path, manifest)
    report = None
    report_path = path / "run.json"
    has_report = report_path.exists()
    if not has_report and (path / "latest.json").exists():
        pointer = _read(path / "latest.json", _Ledger, "latest")
        report_path = path / "reports" / f"{pointer.payload['run_hash']}.json"
        has_report = True
    if has_report:
        report = _read(report_path, WindowedRun)
        if report.cursor > len(chunks):
            # A concurrent publisher may have advanced after our directory
            # listing. Commits only append; refresh once to include its prefix.
            snapshot, logs, chunks = _load_commits(path, manifest)
        if (report.manifest_hash != manifest.artifact_hash
                or report.prepared != manifest.inputs.prepared or report.context != manifest.inputs.context
                or report.chunk_plan != manifest.chunk_plan or report.config != manifest.config
                or report.logs != logs[:len(report.logs)] or report.chunks != chunks[:report.cursor]
                or (report.cursor == len(chunks) and report.final_snapshot != snapshot)):
            raise ValueError("published report differs from verified committed prefix")
    dispatches = [_read(p, _Ledger, "dispatch") for p in sorted((path / "dispatches").glob("*.json"))]
    for dispatch in dispatches:
        key = dispatch.payload["request_hash"]
        request = _read(path / "requests" / f"{key}.json", _Ledger, "request")
        if request.artifact_hash != key or request.payload["manifest_hash"] != manifest.artifact_hash:
            raise ValueError("dispatch differs from inspected manifest")
    return _CommittedProgress(
        manifest.inputs.prepared, manifest.chunk_plan, snapshot, logs, chunks, manifest.artifact_hash,
        report, len(dispatches), sum(d.payload["repair"] for d in dispatches),
        sum(d.payload["reserved_tokens"] for d in dispatches))


def windowed_status(path):
    progress = _committed_progress(path)
    prepared, plan, chunks, logs = progress.prepared, progress.chunk_plan, progress.chunks, progress.logs
    snapshot = progress.snapshot
    published = progress.publication_state == "published"
    report = progress.report
    completed_ids = {item.chunk.chunk_id for item in chunks}
    completed_documents = [
        source.source_file_id for source in prepared.sources
        if source.status in {"processed", "no_candidates"} and
        all(chunk.chunk_id in completed_ids for chunk in plan if chunk.source_file_id == source.source_file_id)
    ]
    completed_units = [
        unit.source_unit_id for unit in prepared.source_units
        if all(chunk.chunk_id in completed_ids for chunk in plan if chunk.source_unit_id == unit.source_unit_id)
    ]
    records = [r for item in chunks for r in map_chunk(item, snapshot)]
    return {
        "artifact_kind": "domain.windowed_run" if published else "domain.windowed_progress",
        "state": report.state if published else progress.publication_state,
        "reason": report.reason if published else progress.publication_state,
        "publication_state": progress.publication_state, "execution_state": "not_inferred_from_artifacts",
        "cursor": len(chunks), "planned_chunks": len(plan), "manifest_hash": progress.manifest_hash,
        "published_cursor": report.cursor if report is not None else None,
        "published_run_hash": report.artifact_hash if report is not None else None,
        "completed_windows": len(logs), "schema_version": snapshot.version,
        "schema_hash": snapshot.artifact_hash, "run_hash": report.artifact_hash if published else None,
        "last_chunk_id": chunks[-1].chunk.chunk_id if chunks else None,
        "model_call_count": progress.model_call_count, "repair_call_count": progress.repair_call_count,
        "reserved_tokens": progress.reserved_tokens, "token_accounting": "conservative_utf8_byte_reservation",
        "planned_documents": len(prepared.sources), "completed_document_ids": completed_documents,
        "planned_source_units": len(prepared.source_units), "completed_source_unit_ids": completed_units,
        "planned_primary_codepoints": sum(c.slice_end - c.slice_start for c in plan),
        "completed_primary_codepoints": sum(c.chunk.slice_end - c.chunk.slice_start for c in chunks),
        "mapping_counts": dict(Counter(r.status for r in records)),
        "source_statuses": [s.model_dump(mode="json") for s in prepared.sources],
        "accepted_changes": sum(d.status == "accepted_working" for log in logs for d in log.decisions),
        "rejected_changes": sum(len(log.diagnostics) for log in logs),
        "authority": "working_only", "semantic_recall": "not_claimed",
    }


def windowed_history(path):
    return _committed_progress(path).logs


def windowed_schema(path):
    return _committed_progress(path).snapshot
