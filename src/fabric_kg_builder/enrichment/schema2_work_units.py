"""Deterministic bounded L2 work units, splitting, and immutable resume."""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from fabric_kg_builder.contracts.base import canonical_sha256, deterministic_contract_id
from fabric_kg_builder.contracts.evidence import SourceUnit

from .schema2_sources import L2StageError

L2_SPLIT_POLICY_VERSION = "paragraph-sentence-token/1.0.0"


@dataclass(frozen=True)
class L2WorkUnit:
    work_unit_id: str
    root_work_unit_id: str
    source_unit_id: str
    source_text_hash: str
    source_text: str
    slice_start: int
    slice_end: int
    pass_name: str
    authority_fingerprint: str
    split_policy_version: str = L2_SPLIT_POLICY_VERSION

    @property
    def text(self) -> str:
        return self.source_text[self.slice_start : self.slice_end]

    @property
    def coverage(self) -> int:
        return self.slice_end - self.slice_start


@dataclass(frozen=True)
class WorkUnitExecution:
    root_work_unit_id: str
    leaf_work_unit_ids: tuple[str, ...]
    leaf_results: tuple[dict[str, Any], ...]
    model_call_count: int
    reused_leaf_count: int


class CandidateModelService(Protocol):
    def complete(self, *, prompt: str, work_unit: L2WorkUnit) -> object:
        """Return one strict response object for the supplied source slice."""


def _work_unit_id(
    *,
    source_unit: SourceUnit,
    slice_start: int,
    slice_end: int,
    pass_name: str,
    authority_fingerprint: str,
    split_policy_version: str,
) -> str:
    return deterministic_contract_id(
        "l2-work-unit",
        {
            "source_unit_id": source_unit.source_unit_id,
            "source_text_hash": source_unit.text_content_hash,
            "slice_start": slice_start,
            "slice_end": slice_end,
            "slice_text_hash": canonical_sha256(
                source_unit.text[slice_start:slice_end]
            ),
            "pass_name": pass_name,
            "authority_fingerprint": authority_fingerprint,
            "split_policy_version": split_policy_version,
        },
    )


def root_work_unit(
    source_unit: SourceUnit,
    *,
    pass_name: str,
    authority_fingerprint: str,
    split_policy_version: str = L2_SPLIT_POLICY_VERSION,
) -> L2WorkUnit:
    work_unit_id = _work_unit_id(
        source_unit=source_unit,
        slice_start=0,
        slice_end=source_unit.codepoint_count,
        pass_name=pass_name,
        authority_fingerprint=authority_fingerprint,
        split_policy_version=split_policy_version,
    )
    return L2WorkUnit(
        work_unit_id=work_unit_id,
        root_work_unit_id=work_unit_id,
        source_unit_id=source_unit.source_unit_id,
        source_text_hash=source_unit.text_content_hash,
        source_text=source_unit.text,
        slice_start=0,
        slice_end=source_unit.codepoint_count,
        pass_name=pass_name,
        authority_fingerprint=authority_fingerprint,
        split_policy_version=split_policy_version,
    )


def _candidate_boundaries(text: str, start: int, end: int) -> list[int]:
    relative = text[start:end]
    paragraph = [
        start + match.end()
        for match in re.finditer(r"\n[ \t]*\n+", relative)
        if 0 < match.end() < len(relative)
    ]
    if paragraph:
        return paragraph
    sentence = [
        start + match.end()
        for match in re.finditer(r"(?<=[.!?])(?:[ \t]+|\n+)", relative)
        if 0 < match.end() < len(relative)
    ]
    if sentence:
        return sentence
    return [
        start + match.end()
        for match in re.finditer(r"\s+", relative)
        if 0 < match.end() < len(relative)
    ]


def split_work_unit(parent: L2WorkUnit) -> tuple[L2WorkUnit, L2WorkUnit] | None:
    """Split at the nearest structural boundary with deterministic overlap."""

    boundaries = _candidate_boundaries(
        parent.source_text,
        parent.slice_start,
        parent.slice_end,
    )
    if not boundaries:
        return None
    midpoint = (parent.slice_start + parent.slice_end) // 2
    pivot_index = min(
        range(len(boundaries)),
        key=lambda index: (abs(boundaries[index] - midpoint), boundaries[index]),
    )
    pivot = boundaries[pivot_index]
    overlap_start = boundaries[pivot_index - 1] if pivot_index > 0 else pivot
    left_start, left_end = parent.slice_start, pivot
    right_start, right_end = overlap_start, parent.slice_end
    if (
        left_start >= left_end
        or right_start >= right_end
        or left_end - left_start >= parent.coverage
        or right_end - right_start >= parent.coverage
    ):
        return None

    source_unit = _source_unit_view(parent)
    children: list[L2WorkUnit] = []
    for child_start, child_end in (
        (left_start, left_end),
        (right_start, right_end),
    ):
        child_id = _work_unit_id(
            source_unit=source_unit,
            slice_start=child_start,
            slice_end=child_end,
            pass_name=parent.pass_name,
            authority_fingerprint=parent.authority_fingerprint,
            split_policy_version=parent.split_policy_version,
        )
        children.append(
            L2WorkUnit(
                work_unit_id=child_id,
                root_work_unit_id=parent.root_work_unit_id,
                source_unit_id=parent.source_unit_id,
                source_text_hash=parent.source_text_hash,
                source_text=parent.source_text,
                slice_start=child_start,
                slice_end=child_end,
                pass_name=parent.pass_name,
                authority_fingerprint=parent.authority_fingerprint,
                split_policy_version=parent.split_policy_version,
            )
        )
    return children[0], children[1]


def _source_unit_view(work_unit: L2WorkUnit):
    class _SourceUnitView:
        source_unit_id = work_unit.source_unit_id
        text_content_hash = work_unit.source_text_hash
        text = work_unit.source_text

    return _SourceUnitView()


def plan_work_units(
    source_units: tuple[SourceUnit, ...],
    *,
    pass_name: str,
    authority_fingerprint: str,
) -> tuple[L2WorkUnit, ...]:
    return tuple(
        root_work_unit(
            source_unit,
            pass_name=pass_name,
            authority_fingerprint=authority_fingerprint,
        )
        for source_unit in sorted(source_units, key=lambda item: item.source_unit_id)
        if source_unit.text
    )


class WorkUnitCheckpoint:
    """Thread-safe deterministic index of immutable successful leaf artifacts."""

    def __init__(self, state_path: Path, artifact_dir: Path) -> None:
        self.state_path = state_path
        self.artifact_dir = artifact_dir
        self._lock = threading.Lock()
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"checkpoint_version": "1.0.0", "work_units": {}}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"checkpoint_version": "1.0.0", "work_units": {}}
        if (
            not isinstance(raw, dict)
            or raw.get("checkpoint_version") != "1.0.0"
            or not isinstance(raw.get("work_units"), dict)
        ):
            return {"checkpoint_version": "1.0.0", "work_units": {}}
        return raw

    def _persist_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            self._state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        temp = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temp.write_text(encoded + "\n", encoding="utf-8")
        os.replace(temp, self.state_path)

    def reuse(self, work_unit: L2WorkUnit) -> dict[str, Any] | None:
        with self._lock:
            entry = self._state["work_units"].get(work_unit.work_unit_id)
            if not isinstance(entry, dict) or entry.get("status") != "succeeded":
                return None
            if entry.get("authority_fingerprint") != work_unit.authority_fingerprint:
                return None
            artifact_name = entry.get("artifact")
            expected_hash = entry.get("artifact_hash")
            if not isinstance(artifact_name, str) or not isinstance(expected_hash, str):
                return None
            path = self.artifact_dir / artifact_name
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if canonical_sha256(result) != expected_hash:
                return None
            return result

    def reuse_split(
        self,
        work_unit: L2WorkUnit,
    ) -> tuple[L2WorkUnit, L2WorkUnit] | None:
        with self._lock:
            entry = self._state["work_units"].get(work_unit.work_unit_id)
            if (
                not isinstance(entry, dict)
                or entry.get("status") != "split"
                or entry.get("authority_fingerprint")
                != work_unit.authority_fingerprint
            ):
                return None
            child_ids = entry.get("child_work_unit_ids")
        children = split_work_unit(work_unit)
        if children is None or child_ids != [
            child.work_unit_id for child in children
        ]:
            return None
        return children

    def record_leaf(
        self, work_unit: L2WorkUnit, result: dict[str, Any]
    ) -> dict[str, Any]:
        artifact_hash = canonical_sha256(result)
        artifact_name = f"{work_unit.work_unit_id.replace(':', '-', 1)}.json"
        with self._lock:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifact_dir / artifact_name
            prior_entry = self._state["work_units"].get(
                work_unit.work_unit_id
            )
            if (
                isinstance(prior_entry, dict)
                and prior_entry.get("status") == "succeeded"
                and prior_entry.get("authority_fingerprint")
                == work_unit.authority_fingerprint
                and prior_entry.get("artifact") == artifact_name
                and path.exists()
            ):
                try:
                    prior_result = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    prior_result = None
                if (
                    prior_result is not None
                    and canonical_sha256(prior_result)
                    == prior_entry.get("artifact_hash")
                ):
                    return prior_result
            existing: object | None = None
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = None
                if existing is not None:
                    try:
                        fresh_state = json.loads(
                            self.state_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        fresh_state = {}
                    fresh_entry = (
                        fresh_state.get("work_units", {}).get(
                            work_unit.work_unit_id
                        )
                        if isinstance(fresh_state, dict)
                        and isinstance(
                            fresh_state.get("work_units"), dict
                        )
                        else None
                    )
                    if (
                        isinstance(fresh_entry, dict)
                        and fresh_entry.get("status") == "succeeded"
                        and fresh_entry.get("authority_fingerprint")
                        == work_unit.authority_fingerprint
                        and fresh_entry.get("artifact") == artifact_name
                        and canonical_sha256(existing)
                        == fresh_entry.get("artifact_hash")
                    ):
                        self._state = fresh_state
                        return existing
                if existing is not None and canonical_sha256(existing) != artifact_hash:
                    raise L2StageError(
                        "L2_CHECKPOINT_STALE",
                        f"immutable leaf artifact collision {artifact_name}",
                    )
            if not path.exists() or existing is None:
                path.write_text(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
            self._state["work_units"][work_unit.work_unit_id] = {
                "status": "succeeded",
                "authority_fingerprint": work_unit.authority_fingerprint,
                "slice_start": work_unit.slice_start,
                "slice_end": work_unit.slice_end,
                "artifact": artifact_name,
                "artifact_hash": artifact_hash,
            }
            self._persist_locked()
            return result

    def record_split(
        self,
        parent: L2WorkUnit,
        children: tuple[L2WorkUnit, L2WorkUnit],
    ) -> None:
        with self._lock:
            self._state["work_units"][parent.work_unit_id] = {
                "status": "split",
                "authority_fingerprint": parent.authority_fingerprint,
                "child_work_unit_ids": [
                    child.work_unit_id for child in children
                ],
            }
            self._persist_locked()


def _candidate_list(response: object) -> list[object]:
    if isinstance(response, list):
        return response
    if isinstance(response, dict) and isinstance(response.get("candidates"), list):
        return response["candidates"]
    raise L2StageError(
        "L2_CANDIDATE_SCHEMA_INVALID",
        "model response must be a candidate array or contain candidates",
    )


def _relationship_count(response: object) -> int:
    return sum(
        isinstance(candidate, dict)
        and candidate.get("candidate_kind") == "relationship"
        for candidate in _candidate_list(response)
    )


def execute_work_unit(
    root: L2WorkUnit,
    *,
    service: CandidateModelService,
    prompt_builder: Callable[[L2WorkUnit], str],
    processor: Callable[[L2WorkUnit, object], dict[str, Any]],
    checkpoint: WorkUnitCheckpoint,
    max_relations_per_work_unit: int,
    transient_error: Callable[[Exception], bool] | None = None,
    max_retries: int = 0,
) -> WorkUnitExecution:
    """Execute/split one root without truncation or repeated successful calls."""

    leaves: list[str] = []
    results: list[dict[str, Any]] = []
    model_calls = 0
    reused = 0

    def visit(work_unit: L2WorkUnit) -> None:
        nonlocal model_calls, reused
        prior = checkpoint.reuse(work_unit)
        if prior is not None:
            leaves.append(work_unit.work_unit_id)
            results.append(prior)
            reused += 1
            return
        prior_split = checkpoint.reuse_split(work_unit)
        if prior_split is not None:
            for child in prior_split:
                visit(child)
            return

        attempts = 0
        while True:
            try:
                response = service.complete(
                    prompt=prompt_builder(work_unit),
                    work_unit=work_unit,
                )
                model_calls += 1
                break
            except Exception as exc:
                if (
                    transient_error is None
                    or not transient_error(exc)
                    or attempts >= max_retries
                ):
                    raise
                attempts += 1

        if _relationship_count(response) > max_relations_per_work_unit:
            children = split_work_unit(work_unit)
            if children is None:
                raise L2StageError(
                    "L2_RELATION_BUDGET_ATOMIC_OVERFLOW",
                    f"indivisible work unit {work_unit.work_unit_id} exceeds policy",
                )
            if any(child.coverage >= work_unit.coverage for child in children):
                raise L2StageError(
                    "L2_WORK_UNIT_INCOMPLETE",
                    "work-unit split did not strictly reduce source coverage",
                )
            checkpoint.record_split(work_unit, children)
            for child in children:
                visit(child)
            return

        result = processor(work_unit, response)
        result = checkpoint.record_leaf(work_unit, result)
        leaves.append(work_unit.work_unit_id)
        results.append(result)

    visit(root)
    return WorkUnitExecution(
        root_work_unit_id=root.work_unit_id,
        leaf_work_unit_ids=tuple(leaves),
        leaf_results=tuple(results),
        model_call_count=model_calls,
        reused_leaf_count=reused,
    )


def execute_work_manifest(
    roots: tuple[L2WorkUnit, ...],
    *,
    service: CandidateModelService,
    prompt_builder: Callable[[L2WorkUnit], str],
    processor: Callable[[L2WorkUnit, object], dict[str, Any]],
    checkpoint: WorkUnitCheckpoint,
    max_relations_per_work_unit: int,
    max_concurrent: int,
    service_batch_size: int,
) -> tuple[WorkUnitExecution, ...]:
    """Apply caller-approved batch/concurrency backpressure with stable output."""

    if max_concurrent < 1 or service_batch_size < 1:
        raise ValueError("scheduler bounds must be positive")
    ordered = tuple(sorted(roots, key=lambda item: item.work_unit_id))
    completed: list[WorkUnitExecution] = []
    for start in range(0, len(ordered), service_batch_size):
        batch = ordered[start : start + service_batch_size]
        with ThreadPoolExecutor(
            max_workers=min(max_concurrent, len(batch)),
            thread_name_prefix="l2-extraction",
        ) as executor:
            futures = [
                executor.submit(
                    execute_work_unit,
                    root,
                    service=service,
                    prompt_builder=prompt_builder,
                    processor=processor,
                    checkpoint=checkpoint,
                    max_relations_per_work_unit=max_relations_per_work_unit,
                )
                for root in batch
            ]
            completed.extend(future.result() for future in futures)
    return tuple(
        sorted(completed, key=lambda item: item.root_work_unit_id)
    )
