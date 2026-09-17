"""Explicit prototype coverage acceptance; never ontology or evidence approval."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from fabric_kg_builder.contracts.base import (
    ContractModel, RequiredText, Sha256, canonical_json, canonical_sha256, deterministic_contract_id,
)


class DiscoveryAcceptanceBinding(ContractModel):
    acceptance_hash: Sha256
    discovery_run_hash: Sha256
    actor: RequiredText
    rationale: RequiredText
    min_chunk_coverage: Decimal = Field(ge=Decimal("0.99"), le=Decimal("1"), strict=False)
    accounted_chunks: int = Field(ge=0)
    total_chunks: int = Field(gt=0)
    status: Literal["partial_accepted"] = "partial_accepted"
    authority: Literal["prototype_chunk_coverage_only"] = "prototype_chunk_coverage_only"

    @model_validator(mode="after")
    def _coverage(self):
        _require_coverage(self.accounted_chunks, self.total_chunks, self.min_chunk_coverage)
        return self


class PartialContextNode(ContractModel):
    node_id: str
    kind: Literal["source_summary", "grounded_chunk", "document", "group", "corpus"]
    source_ref: str | None = None
    source_file_id: str | None = None
    child_ids: list[str]
    covered_chunk_count: int
    term_counts: dict[str, dict[str, int]]
    summary_excerpt: str | None = None
    summary_full_chars: int | None = None
    source_hash: str | None = None


class DiscoveryPartialAcceptance(DiscoveryAcceptanceBinding):
    artifact_kind: Literal["domain.discovery_partial_acceptance"] = "domain.discovery_partial_acceptance"
    artifact_version: Literal["1.0.0"] = "1.0.0"
    prepared_corpus_hash: Sha256
    corpus_hash: Sha256
    per_file_coverage: list[dict[str, Any]]
    failed_chunk_ids: list[str]
    deferred_chunk_ids: list[str]
    grounding: dict[str, Any]
    missing_document_summary_ids: list[str]
    corpus_summary_missing: bool
    failed_summary_metadata: list[dict[str, Any]]
    run_issues: list[str]
    context_nodes: list[PartialContextNode]
    context_root_id: str

    @model_validator(mode="after")
    def _digest(self):
        if self.acceptance_hash != canonical_sha256(self.model_dump(mode="json", exclude={"acceptance_hash"})):
            raise ValueError("discovery partial acceptance hash mismatch")
        return self

    @property
    def artifact_hash(self):
        return self.acceptance_hash

    @property
    def binding(self) -> DiscoveryAcceptanceBinding:
        return DiscoveryAcceptanceBinding.model_validate({
            key: getattr(self, key) for key in DiscoveryAcceptanceBinding.model_fields
        })


def _require_coverage(accounted: int, total: int, minimum) -> None:
    threshold = Fraction(str(minimum))
    if not Fraction(99, 100) <= threshold <= 1:
        raise ValueError("partial acceptance requires a threshold of at least 99%")
    if total <= 0 or accounted > total or Fraction(accounted, total) < threshold:
        raise ValueError(f"partial discovery coverage {accounted}/{total} is below the exact required threshold")


def _checked(run):
    from .discovery import DiscoveryRun

    run = DiscoveryRun.model_validate(run.model_dump(mode="python"))
    if any(item.status not in {"processed", "no_candidates"} for item in run.prepared.sources):
        raise ValueError("partial acceptance requires all declared sources prepared; unknown chunk denominators cannot be waived")
    return run


def _frontier(run, good):
    chunks = {item.chunk.chunk_id: item for item in run.chunks}
    descendants = {key: {key} for key in good}
    for summary in run.summaries:
        descendants[summary.node_id] = set().union(*(descendants[key] for key in summary.child_ids))
    nodes = []

    def terms(ids):
        counts = {kind: Counter() for kind in ("entity", "relationship", "property")}
        for key in sorted(ids):
            for candidate in chunks[key].response.candidates:
                field = {"entity": "observed_type", "relationship": "observed_predicate", "property": "observed_property"}[candidate.candidate_kind]
                counts[candidate.candidate_kind][getattr(candidate, field)] += 1
        return {key: dict(sorted(value.items())) for key, value in counts.items()}

    def node(kind, children, covered, **values):
        payload = dict(
            kind=kind, child_ids=children, covered_chunk_count=len(covered),
            term_counts=terms(covered), **values,
        )
        result = PartialContextNode(
            node_id=deterministic_contract_id("partial-discovery-context", {
                "discovery_run_hash": run.run_hash, **payload,
            }), **payload,
        )
        nodes.append(result)
        descendants[result.node_id] = set(covered)
        return result.node_id

    def group(ids, kind, source=None):
        while len(ids) > 6:
            ids = [
                node("group", ids[offset:offset + 6], set().union(*(descendants[key] for key in ids[offset:offset + 6])),
                     source_file_id=source)
                for offset in range(0, len(ids), 6)
            ]
        return node(kind, ids, set().union(*(descendants[key] for key in ids)), source_file_id=source)

    documents = []
    for source in run.prepared.sources:
        available = {key for key in good if chunks[key].chunk.source_file_id == source.source_file_id}
        covered, leaves = set(), []
        summaries = [
            item for item in run.summaries if item.level == "document"
            and item.source_file_id == source.source_file_id and descendants[item.node_id] <= available
        ]
        for summary in sorted(summaries, key=lambda item: (-len(descendants[item.node_id]), item.node_id)):
            ids = descendants[summary.node_id]
            if not ids or ids & covered:
                continue
            leaves.append(node(
                "source_summary", [], ids, source_ref=summary.node_id, source_file_id=source.source_file_id,
                summary_excerpt=summary.response.summary[:512], summary_full_chars=len(summary.response.summary),
                source_hash=summary.artifact_hash,
            ))
            covered.update(ids)
        for key in sorted(available - covered):
            leaves.append(node(
                "grounded_chunk", [], {key}, source_ref=key, source_file_id=source.source_file_id,
                source_hash=chunks[key].artifact_hash,
            ))
            covered.add(key)
        if covered != available:
            raise ValueError("partial frontier omitted successful source observations")
        documents.append(group(leaves, "document", source.source_file_id))
    root = group(documents, "corpus")
    if descendants[root] != good:
        raise ValueError("partial frontier coverage mismatch")
    return nodes, root


def _values(run, *, minimum, actor, rationale):
    from .discovery import discovery_grounding_report

    good = {
        item.chunk.chunk_id for item in run.chunks
        if item.status in {"processed", "no_candidates"} and item.response is not None
    }
    _require_coverage(len(good), len(run.chunks), minimum)
    nodes, root = _frontier(run, good)
    files = []
    for source in run.prepared.sources:
        records = [item for item in run.chunks if item.chunk.source_file_id == source.source_file_id]
        document = next(item for item in nodes if item.kind == "document" and item.source_file_id == source.source_file_id)
        files.append({
            "source_file_id": source.source_file_id, "total_chunks": len(records),
            "accounted_chunks": sum(item.chunk.chunk_id in good for item in records),
            "pending_chunk_ids": [item.chunk.chunk_id for item in records if item.chunk.chunk_id not in good],
            "context_node_id": document.node_id,
            "document_summary_available": source.source_file_id in run.document_summaries,
        })
    return {
        "artifact_kind": "domain.discovery_partial_acceptance", "artifact_version": "1.0.0",
        "discovery_run_hash": run.run_hash, "prepared_corpus_hash": run.prepared.prepared_hash,
        "corpus_hash": run.prepared.corpus.corpus_hash, "actor": actor, "rationale": rationale,
        "min_chunk_coverage": Decimal(str(minimum)), "accounted_chunks": len(good), "total_chunks": len(run.chunks),
        "status": "partial_accepted", "authority": "prototype_chunk_coverage_only",
        "per_file_coverage": files,
        "failed_chunk_ids": [item.chunk.chunk_id for item in run.chunks if item.status == "failed"],
        "deferred_chunk_ids": [item.chunk.chunk_id for item in run.chunks if item.status == "deferred"],
        "grounding": discovery_grounding_report(run, include_ledger_accounting=True),
        "missing_document_summary_ids": [
            item.source_file_id for item in run.prepared.sources if item.source_file_id not in run.document_summaries
        ],
        "corpus_summary_missing": run.corpus_summary_id is None,
        "failed_summary_metadata": [{
            "artifact_hash": item.artifact_hash, "request_hash": item.request_hash,
            "source_file_id": item.source_file_id, "child_ids": item.child_ids, "reason": item.reason,
        } for item in run.failed_summaries],
        "run_issues": list(run.issues), "context_nodes": nodes, "context_root_id": root,
    }


def _seal_acceptance(values):
    draft = DiscoveryPartialAcceptance.model_construct(acceptance_hash="0" * 64, **values)
    digest = canonical_sha256(draft.model_dump(mode="json", exclude={"acceptance_hash"}))
    return DiscoveryPartialAcceptance.model_validate({**values, "acceptance_hash": digest})


def accept_discovery_partial(run, *, min_chunk_coverage=0.99, actor, rationale) -> DiscoveryPartialAcceptance:
    from .discovery import validate_discovery

    run = _checked(run)
    validate_discovery(run, source_path=Path(run.prepared.source_path), reparse=False)
    return _seal_acceptance(_values(run, minimum=min_chunk_coverage, actor=actor, rationale=rationale))


def validate_discovery_acceptance(acceptance: DiscoveryPartialAcceptance, run) -> None:
    acceptance = DiscoveryPartialAcceptance.model_validate(acceptance.model_dump(mode="python"))
    run = _checked(run)
    if acceptance.discovery_run_hash != run.run_hash:
        raise ValueError("partial acceptance belongs to another discovery run")
    expected = _seal_acceptance(_values(
        run, minimum=acceptance.min_chunk_coverage, actor=acceptance.actor, rationale=acceptance.rationale,
    ))
    if acceptance != expected:
        raise ValueError("partial acceptance source/coverage/frontier binding mismatch")


def resolve_discovery_acceptance(binding: DiscoveryAcceptanceBinding, run) -> DiscoveryPartialAcceptance:
    """Recover sealed authority; replay separately validates its actual source path."""
    binding = DiscoveryAcceptanceBinding.model_validate(binding.model_dump(mode="python"))
    run = _checked(run)
    if binding.discovery_run_hash != run.run_hash:
        raise ValueError("partial acceptance belongs to another discovery run")
    acceptance = _seal_acceptance(_values(
        run, minimum=binding.min_chunk_coverage, actor=binding.actor, rationale=binding.rationale,
    ))
    if acceptance.binding != binding:
        raise ValueError("reconstructed partial acceptance binding mismatch")
    return acceptance


def save_discovery_acceptance(path: Path, acceptance: DiscoveryPartialAcceptance) -> None:
    from .discovery import _write

    _write(path, DiscoveryPartialAcceptance.model_validate(acceptance.model_dump(mode="python")))


def load_discovery_acceptance(path: Path) -> DiscoveryPartialAcceptance:
    return DiscoveryPartialAcceptance.model_validate_json(path.read_text(encoding="utf-8"))


def discovery_partial_design_context(run, acceptance, *, node_ids=None, max_chars=64_000):
    validate_discovery_acceptance(acceptance, run)
    index = {item.node_id: item for item in acceptance.context_nodes}
    ids = list(dict.fromkeys([acceptance.context_root_id, *(node_ids or [])]))
    if not set(ids) <= set(index):
        raise ValueError("unknown partial-discovery context node")

    def preview(node):
        terms = {}
        for kind, counts in node.term_counts.items():
            terms[kind] = {
                "distinct_term_count": len(counts), "selection": "top_8_by_frequency_then_label",
                "items": [{
                    "label_excerpt": label[:160], "full_label_chars": len(label),
                    "label_hash": canonical_sha256(label), "count": count,
                } for label, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:8]],
            }
        value = node.model_dump(mode="json", exclude={"term_counts"})
        value["grounded_proposed_term_preview"] = terms
        return value

    root = index[acceptance.context_root_id]
    summaries_by_id = {item.node_id: item for item in run.summaries}
    chunks_by_id = {item.chunk.chunk_id: item for item in run.chunks}
    retrieved = []
    for key in ids:
        if key == root.node_id:
            continue
        node = index[key]
        detail = preview(node)
        detail["children"] = [preview(index[child]) for child in node.child_ids]
        if node.kind == "source_summary":
            detail["summary"] = summaries_by_id[node.source_ref].response.summary
        elif node.kind == "grounded_chunk":
            detail["grounded_candidates"] = chunks_by_id[node.source_ref].response.model_dump(mode="json")
        retrieved.append(detail)
    documents = []
    for file in acceptance.per_file_coverage:
        summaries = [item for item in acceptance.context_nodes if item.kind == "source_summary" and item.source_file_id == file["source_file_id"]]
        documents.append({
            "source_file_id": file["source_file_id"], "context_node_id": file["context_node_id"],
            "available_summary_frontier_count": len(summaries),
            "excerpt_selection": "first_two_disjoint_frontier_summaries",
            "summary_excerpts": [
                item.model_dump(mode="json", exclude={"term_counts", "child_ids"}) for item in summaries[:2]
            ],
        })
    result = {
        "scope": "partial_accepted", "coverage_acceptance": acceptance.binding.model_dump(mode="json"),
        "ontology_approval": "not_granted", "answer_verification": "not_performed", "semantic_recall": "not_claimed",
        "quantitative_criticality": "not_assessed", "missing_chunks_may_contain_unique_or_critical_requirements": True,
        "per_file_coverage": acceptance.per_file_coverage, "grounding": acceptance.grounding,
        "failed_chunk_ids": acceptance.failed_chunk_ids, "deferred_chunk_ids": acceptance.deferred_chunk_ids,
        "missing_document_summary_ids": acceptance.missing_document_summary_ids,
        "failed_summary_metadata": [{
            **{key: value for key, value in item.items() if key != "reason"},
            "reason_hash": canonical_sha256(item["reason"]),
            "reason_code": next((code for code in ("SUMMARY_INPUT_IDS_MISMATCH", "SUMMARY_LENGTH_EXCEEDED") if item["reason"].startswith(code)), "SUMMARY_INVALID_RESPONSE"),
        } for item in acceptance.failed_summary_metadata],
        "run_issue_hashes": [canonical_sha256(issue) for issue in acceptance.run_issues],
        "document_frontier_previews": documents,
        "frontier_root": preview(root), "root_children": [preview(index[key]) for key in root.child_ids],
        "retrieved_nodes": retrieved,
        "retrieval": "Disjoint valid summary/chunk references cover every successful observation across all files. Follow child_ids for bounded detail. Excerpts and term previews are explicitly bounded; no new summary prose or semantic coverage is asserted.",
    }
    if len(canonical_json(result)) > max_chars:
        raise ValueError("partial discovery context exceeds explicit prompt bound; select bounded nodes or raise max_chars")
    return result
