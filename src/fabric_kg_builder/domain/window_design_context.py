"""Bounded representative design context; complete observation authority stays local."""

from collections import Counter

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from .concept_policy import CONCEPT_PROMPT_VERSIONS

WINDOW_DESIGN_CONTEXT_VERSION = "window-design-context/1.0.0"
PATTERN_CHARS = 48_000
CONFLICT_CHARS = 24_000
WORKING_CONTEXT_CHARS = 8_000
QUOTE_CHARS = 320


def _bounded_rows(rows, limit):
    selected, used = [], 2
    for row in rows:
        size = len(canonical_json(row)) + 1
        if used + size <= limit:
            selected.append(row)
            used += size
    return {
        "rows": selected, "total_rows": len(rows), "represented_rows": len(selected),
        "omitted_rows": len(rows) - len(selected), "full_rows_hash": canonical_sha256(rows),
        "selection": "deterministic representative rows that fit; not all facts or semantic recall",
    }


def _quote(record, units):
    unit = units[record.source_unit_id]
    for anchor in record.anchors:
        start, end, quote = anchor.get("span_start"), anchor.get("span_end"), anchor.get("quote")
        if (
            type(start) is int and type(end) is int and isinstance(quote, str)
            and 0 <= start < end <= len(unit.text) and unit.text[start:end] == quote
        ):
            excerpt_end = min(end, start + QUOTE_CHARS)
            return {
                "observation_id": record.observation_id, "chunk_id": record.chunk_id,
                "source_file_id": record.source_file_id, "source_unit_id": record.source_unit_id,
                "source_text_hash": unit.text_content_hash,
                "span_start": start, "span_end": excerpt_end, "quote": unit.text[start:excerpt_end],
                "excerpt_of_anchor": excerpt_end != end,
                "original_anchor_hash": canonical_sha256(anchor),
                "verified_candidate_hash": record.verified_candidate_hash,
                "authority": "source_verified_quote_only_not_verified_candidate_fact",
            }
    return None


def _conflict_context(logs):
    from .window_run import _pending_context

    registry = _pending_context(logs)
    if len(canonical_json(registry)) <= CONFLICT_CHARS:
        return registry
    catalogs = registry["catalogs"]

    def resolve(value, catalog):
        if value is None:
            return None
        if isinstance(value, list):
            return [resolve(item, catalog) for item in value]
        if isinstance(value, dict):
            return value
        return catalogs[catalog][value]

    rows = []
    for original in registry["rows"]:
        row = dict(zip(registry["columns"], original, strict=True))
        scope = catalogs["scopes"][row["scope"]]
        reason = catalogs["reasons"][row["reason"]]
        rows.append({
            "kind": resolve(row["kind"], "kinds"), "term": resolve(row["term"], "terms"),
            "action": resolve(row["action"], "actions"), "count": row["count"],
            "scope": {
                key: resolve(value, registry["scope_column_catalogs"][key])
                for key, value in zip(registry["scope_columns"], scope, strict=True)
            },
            "reason": {
                key: resolve(value, registry["reason_column_catalogs"][key])
                for key, value in zip(registry["reason_columns"], reason, strict=True)
            },
            "example_ids": row["example_ids"],
        })
    rows.sort(key=lambda row: (-row["count"], canonical_sha256(row)))
    return {
        "format_version": registry["format_version"], "full_registry_hash": canonical_sha256(registry),
        **{key: registry[key] for key in ("full_ledger_hash", "ledger_entry_count", "unique_signature_count")},
        "representative_conflicts": _bounded_rows(rows, CONFLICT_CHARS - 2_000),
        "local_artifact": "window_run.logs: pending and diagnostics",
    }


def window_design_context(run, acceptance=None, *, _validation=None):
    """No schema/intake truncation, synthetic discovery, or alteration of candidate values."""
    from fabric_kg_builder.enrichment.window_run_reuse import window_run_binding

    units = {unit.source_unit_id: unit for unit in run.prepared.source_units}
    records = run.final_mapping.records
    patterns = {}
    for record in records:
        # Rejected evidence remains in exact status counts, never quote examples.
        signature = {
            "kind": record.kind, "observed_term": record.observed_term, "concept_id": record.concept_id,
            "source_type_id": record.source_type_id, "target_type_id": record.target_type_id,
            "owner_type_id": record.owner_type_id, "direction": record.direction,
        }
        key = canonical_sha256(signature)
        group = patterns.setdefault(key, {
            **signature, "count": 0, "status_counts": Counter(), "per_source_counts": Counter(),
            "examples": [],
        })
        group["count"] += 1
        group["status_counts"][record.status] += 1
        group["per_source_counts"][record.source_file_id] += 1
        if (
            record.status in {"mapped", "pending"} and record.verified_candidate_hash is not None
            and len(group["examples"]) < 2
            and record.source_file_id not in {example["source_file_id"] for example in group["examples"]}
        ):
            example = _quote(record, units)
            if example is not None:
                group["examples"].append(example)
    rows = []
    for key in sorted(patterns, key=lambda key: (-patterns[key]["count"], key)):
        group = patterns[key]
        rows.append({
            **group, "status_counts": dict(sorted(group["status_counts"].items())),
            "per_source_counts": dict(sorted(group["per_source_counts"].items())),
        })
    by_source = {}
    for chunk in run.chunk_plan:
        by_source.setdefault(chunk.source_file_id, Counter())["planned_chunks"] += 1
    for chunk in run.chunks:
        by_source.setdefault(chunk.chunk.source_file_id, Counter())["committed_chunks"] += 1
    for record in records:
        by_source.setdefault(record.source_file_id, Counter())[record.status + "_candidates"] += 1
    conflicts = _conflict_context(run.logs)
    working = {}
    for item in run.working_context:
        annotations = item.get("annotations", {})
        key = canonical_sha256(annotations)
        group = working.setdefault(key, {
            "annotations": annotations, "occurrences": 0, "example_chunk_ids": [],
            "authority": "working_hypotheses_not_approved_classifications",
        })
        group["occurrences"] += 1
        if len(group["example_chunk_ids"]) < 3:
            group["example_chunk_ids"].append(item["chunk_id"])
    coverage = {
        "state": run.state, "reason": run.reason, "processed_chunks": len(run.chunks),
        "planned_chunks": len(run.chunk_plan), "source_count": len(run.prepared.sources),
        "source_unit_count": len(run.prepared.source_units),
        "source_codepoints": sum(len(unit.text) for unit in run.prepared.source_units),
        "candidate_count": len(records), "status_counts": dict(sorted(Counter(row.status for row in records).items())),
        "source_inventory": [
            {"source_file_id": source.source_file_id, "status": source.status, "reason": source.reason,
             "relative_source_ref": next(
                 entry.relative_source_ref for entry in run.prepared.corpus.entries
                 if entry.source_file_id == source.source_file_id
             ),
             **dict(sorted(by_source.get(source.source_file_id, {}).items()))}
            for source in run.prepared.sources
        ],
        "chunk_plan_hash": canonical_sha256(run.chunk_plan),
        "observation_ledger_hash": canonical_sha256(run.chunks),
        "mapping_records_hash": canonical_sha256(records),
        "working_context_hash": canonical_sha256(run.working_context),
        "working_context_count": len(run.working_context),
    }
    return {
        "format_version": WINDOW_DESIGN_CONTEXT_VERSION,
        "authority": "representative_schema_design_reference_only",
        "binding": window_run_binding(run, acceptance, _validation=_validation).model_dump(mode="json"),
        "context": {"intake_raw": run.context.intake_raw, "context_hash": run.context.artifact_hash,
                    "intake_text_hash": canonical_sha256(run.context.intake_text), "routing": run.context.routing},
        "final_snapshot": run.final_snapshot.model_dump(mode="json"),
        **({"concept_policy": {
            "prompt_version": run.config.prompt_version,
            "guidance": (
                "Keep reusable business classes separate from source-labelled instances. "
                "Do not promote component names, product codes, numbered instructions or values to types. "
                "Preserve distinct domain roles and evidence-backed typed relationships; use parent types "
                "only for IS-A, never containment, location or sequence. Do not invent missing hierarchy links. "
                "Specific labels, exact quotations and scalar detail remain in the observation/retrieval ledger. "
                "No automatic merging of instance identities or approval of facts."
            ),
        }} if run.config.prompt_version in CONCEPT_PROMPT_VERSIONS else {}),
        "coverage": coverage,
        "patterns": _bounded_rows(rows, PATTERN_CHARS),
        "schema_review": conflicts,
        "working_context": _bounded_rows([working[key] for key in sorted(working)], WORKING_CONTEXT_CHARS),
        **({"coverage_acceptance": {
            "acceptance_hash": acceptance.acceptance_hash, "actor": acceptance.actor,
            "rationale": acceptance.rationale, "min_chunk_coverage": str(acceptance.min_chunk_coverage),
            "coverage_hash": canonical_sha256(acceptance.coverage),
            "gap_count": len(acceptance.coverage.gaps),
            "gaps": _bounded_rows([gap.model_dump(mode="json") for gap in acceptance.coverage.gaps], 8_000),
            "authority": acceptance.authority,
        }} if acceptance is not None and acceptance.authority == "prototype_chunk_coverage_only" else {}),
        **({"scope_acceptance": {
            "acceptance_hash": acceptance.acceptance_hash, "actor": acceptance.actor,
            "rationale": acceptance.rationale, "authority": acceptance.authority,
            "cursor": acceptance.cursor, "selected_chunk_count": len(acceptance.selected_committed_chunk_ids),
            "selected_chunk_ids_hash": canonical_sha256(acceptance.selected_committed_chunk_ids),
            "omitted_chunk_count": len(acceptance.omitted_chunk_ids),
            "omitted_chunk_ids_hash": canonical_sha256(acceptance.omitted_chunk_ids),
            "coverage_hash": canonical_sha256(acceptance.coverage), "scope_notice": acceptance.scope_notice,
            "gaps": _bounded_rows([gap.model_dump(mode="json") for gap in acceptance.coverage.gaps], 8_000),
        }} if acceptance is not None and acceptance.authority == "limited_committed_prefix_only" else {}),
        "policy": (
            "All original candidates, quotations, values, rejected proposals and working annotations remain "
            "sealed in the draft's full window_run and local ledgers. These exact pattern/source counts and "
            "source-verified quote excerpts are representative support, not all facts or semantic recall. "
            "Quarantined candidates supply no examples. Preserve compatible working concept names and scopes; "
            "explicitly explain proposed refinements in review_concerns. Do not turn literal identifiers into "
            "entity types merely to retain an invalid proposal. Final domain, mapping and evidence reviews remain separate."
        ),
    }
