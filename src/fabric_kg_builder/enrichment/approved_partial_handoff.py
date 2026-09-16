"""Offline, explicitly approved handoff of complete response-backed extraction roots.

The approved domain remains the extraction ceiling, not a claim that the ceiling
was processed. This producer never constructs a model service or edits a donor.
"""

from __future__ import annotations

from contextlib import ExitStack
import base64
import binascii
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.contracts.evidence import SourceUnit
from fabric_kg_builder.domain.service import compute_contract_hash
from . import approved_donor_continuation as donors
from . import approved_quote_review as reviews
from . import approved_reextraction as core
from . import approved_local_identifiers as local_ids
from . import schema2_stage as stage
from .approved_quote_anchors import SourceSpanResolutionError
from .discovery_reuse import _locked_reuse
from .schema2_work_units import _relationship_count, split_work_unit
from .window_prefix import validate_prefix_candidate_anchors

VERSION = "approved-partial-handoff/1.0.0"
EXTRACTOR = "approved-partial-response-handoff"
SCOPE_FILE = "partial-extraction-scope.json"
SCOPE_ID = "partial-extraction-scope"
SCOPE_KIND = "l2.partial_extraction_scope"
INTEGRITY = "partial-handoff-integrity.json"
WITNESS_ID = "partial-extraction-witnesses"
WITNESS_FILE = f"{WITNESS_ID}.json"
WITNESS_DIR = "qualified-witnesses"
WITNESS_PREFIX = "partial-witness:"
WITNESS_KIND = "l2.qualified_identifier_witness_manifest"
WITNESS_FILE_KIND = "l2.qualified_identifier_witness_file"
WITNESS_VERSION = "1.0.0"
_WITNESS_METADATA = {
    SCOPE_FILE, INTEGRITY, "stage-receipt.json", "input-manifest.json",
    "output-manifest.json", "source-unit-manifest.json",
    "l3/stage-receipt.json", "l3/input-manifest.json", "l3/output-manifest.json",
}


def _fail(reason):
    raise ValueError(f"APPROVED_PARTIAL_HANDOFF_{reason}")


def _range(unit):
    return {key: getattr(unit, key) for key in (
        "source_unit_id", "source_text_hash", "slice_start", "slice_end",
    )}


def _coverage(roots, donor, vocabulary, entries, limit, mode):
    """An overflow parent is evidence of a split, never a completed range."""
    included, excluded, accepted = [], [], []

    def visit(unit):
        prompt = donors._prompt(unit, vocabulary, entries, mode)
        paid = donor.responses.get(core.response_hash(prompt, mode))
        node = {**_range(unit), "work_unit_id": unit.work_unit_id}
        if paid is None:
            return False, [], [{**node, "status": "missing_response"}]
        node["response"] = paid.provenance()
        if _relationship_count(paid.response) > limit:
            children = split_work_unit(unit)
            if children is None:
                _fail("ATOMIC_OVERFLOW")
            visits = [visit(child) for child in children]
            return (
                all(v[0] for v in visits),
                [leaf for v in visits for leaf in v[1]],
                [{**node, "status": "overflow_parent"}]
                + [record for v in visits for record in v[2]],
            )
        return True, [(unit, paid)], [{**node, "status": "response_backed_leaf"}]

    for root in roots:
        complete, leaves, nodes = visit(root)
        record = {**_range(root), "root_work_unit_id": root.work_unit_id, "nodes": nodes}
        if complete:
            included.append(record)
            accepted.extend(leaves)
        else:
            excluded.append({**record, "reason": "incomplete_root_missing_responses"})
    return included, excluded, accepted


def _select_completed_roots(included, accepted, root_ids, rationale):
    """Exclude whole verified trees only, preserving their range/provenance audit."""
    ids = tuple(root_ids)
    if len(set(ids)) != len(ids):
        _fail("DUPLICATE_EXCLUDED_ROOT")
    if set(ids) - {root["root_work_unit_id"] for root in included}:
        _fail("EXCLUDED_ROOT_NOT_COMPLETE_OR_UNKNOWN")
    if ids and (not rationale or not rationale.strip()):
        _fail("EXCLUSION_RATIONALE_REQUIRED")
    if not ids:
        if rationale is not None:
            _fail("EXCLUSION_RATIONALE_WITHOUT_ROOTS")
        return included, [], accepted
    if len(ids) == len(included):
        _fail("ALL_COMPLETE_ROOTS_EXCLUDED")
    excluded_ids = set(ids)
    selected = [root for root in included if root["root_work_unit_id"] not in excluded_ids]
    excluded = [
        {**root, "reason": "operator_excluded_completed_root", "exclusion_rationale": rationale}
        for root in included if root["root_work_unit_id"] in excluded_ids
    ]
    leaf_ids = {
        node["work_unit_id"] for root in selected for node in root["nodes"]
        if node["status"] == "response_backed_leaf"
    }
    return selected, excluded, [(unit, paid) for unit, paid in accepted if unit.work_unit_id in leaf_ids]


def _resolved(unit, paid, mode):
    if mode in core.SOURCE_SPAN_MODES:
        if paid.review_projection is not None or paid.review_record is not None:
            _fail("SOURCE_SPAN_QUOTE_REVIEW_UNSUPPORTED")
        return core.source_span_adapter(mode).resolve_response(
            paid.response, source_unit_id=unit.source_unit_id,
            source_text_hash=unit.source_text_hash, source_text=unit.text,
            slice_start=unit.slice_start, slice_end=unit.slice_end,
        )
    if paid.review_projection is not None:
        return deepcopy(reviews.reviewed_artifact(
            paid, unit, paid.request_hash,
        )["resolved_response"])
    if mode == core.QUOTE_MODE:
        return core.resolve_quote_response(
            paid.response, source_text=unit.text,
            slice_start=unit.slice_start, slice_end=unit.slice_end,
        )
    return deepcopy(paid.response)


def _local_identifier_code_hash():
    return hashlib.sha256((Path(__file__).parent.parent / local_ids.CODE_PATH).read_bytes()).hexdigest()


def _verify_local_identifier_projections(root, plan):
    profile = plan.get(local_ids.PROFILE_KEY)
    code_hash = plan["partial_code_identity"].get(local_ids.CODE_PATH)
    if local_ids.PROFILE_KEY not in plan:
        if code_hash is not None or any(
            local_ids.ARTIFACT_KEY in json.loads(path.read_bytes())
            for path in (root / "retained-responses").glob("*.json")
        ):
            _fail("LOCAL_IDENTIFIER_PROFILE_DRIFT")
        return
    if (
        local_ids.lossless_hash(profile) != local_ids.lossless_hash(local_ids.profile())
        or code_hash != _local_identifier_code_hash()
        or plan["anchor_mode"] not in core.SOURCE_SPAN_MODES
    ):
        _fail("LOCAL_IDENTIFIER_PROFILE_DRIFT")
    if any(path.is_symlink() for path in root.rglob("*")):
        _fail("UNSAFE_STATE")
    nodes = [
        node for item in plan["included_roots"] for node in item["nodes"]
        if node["status"] == "response_backed_leaf"
    ]
    expected_files = set()
    projection_proofs = []
    for node in nodes:
        try:
            source_id, work_id = node["source_unit_id"], node["work_unit_id"]
            if any("/" in value or "\\" in value for value in (source_id, work_id)):
                _fail("UNSAFE_STATE")
            source = SourceUnit.model_validate_json(
                (root / "source-units" / f"{source_id.replace(':', '-', 1)}.json").read_bytes(),
            )
            path = root / "retained-responses" / f"{work_id.replace(':', '-', 1)}.json"
            if path in expected_files:
                _fail("LOCAL_IDENTIFIER_SCOPE_DRIFT")
            expected_files.add(path)
            retained = json.loads(path.read_bytes())
            unit = SimpleNamespace(
                source_unit_id=source_id, source_text_hash=node["source_text_hash"],
                slice_start=node["slice_start"], slice_end=node["slice_end"],
                work_unit_id=work_id, text=source.text[node["slice_start"]:node["slice_end"]],
            )
            if (
                source.source_unit_id != source_id or source.text_content_hash != unit.source_text_hash
                or retained["range"] != _range(unit) or retained["provenance"] != node["response"]
                or type(unit.slice_start) is not int or type(unit.slice_end) is not int
                or not 0 <= unit.slice_start < unit.slice_end <= len(source.text)
            ):
                _fail("LOCAL_IDENTIFIER_SCOPE_DRIFT")
            paid = donors.PaidResponse(
                response=retained["response"], **retained["provenance"],
                provider_output=retained["provider_output"],
                review_projection=retained["review_projection"], review_record=retained["review_record"],
            )
            provider = paid.provider_output
            output = base64.b64decode(provider["raw_output_utf8_base64"], validate=True)
            if (
                provider["request_hash"] != paid.request_hash
                or hashlib.sha256(output).hexdigest() != provider["raw_output_sha256"]
                or local_ids.lossless_hash(json.loads(output.decode("utf-8"))) != local_ids.lossless_hash(paid.response)
                or core.response_hash(paid.response, plan["anchor_mode"]) != paid.response_hash
            ):
                _fail("LOCAL_IDENTIFIER_PROVIDER_DRIFT")
            adapter = core.source_span_adapter(plan["anchor_mode"])
            catalog = adapter.catalog(
                source_unit_id=source_id, source_text_hash=unit.source_text_hash, source_text=unit.text,
                slice_start=unit.slice_start, slice_end=unit.slice_end,
            )
            resolved = _resolved(unit, paid, plan["anchor_mode"])
            if (
                retained["anchor_adapter_version"] != adapter.version
                or local_ids.lossless_hash(retained["source_segments"]) != local_ids.lossless_hash(catalog)
                or retained["source_segments_sha256"] != core.response_hash(catalog, plan["anchor_mode"])
                or local_ids.lossless_hash(retained["resolved_response"]) != local_ids.lossless_hash(resolved)
                or retained["resolved_response_hash"] != core.response_hash(resolved, plan["anchor_mode"])
            ):
                _fail("LOCAL_IDENTIFIER_ADAPTER_DRIFT")
            local_ids.verify_projection(
                resolved, retained[local_ids.ARTIFACT_KEY], source_unit_id=source_id,
                slice_start=unit.slice_start, slice_end=unit.slice_end,
            )
            projection_proofs.append(retained[local_ids.ARTIFACT_KEY]["proof"])
        except (OSError, ValueError, TypeError, KeyError, binascii.Error) as exc:
            raise ValueError(f"APPROVED_PARTIAL_HANDOFF_LOCAL_IDENTIFIER_PROOF_INVALID: {exc}") from exc
    if (
        len(nodes) != plan["completed_leaf_count"]
        or set((root / "retained-responses").glob("*.json")) != expected_files
    ):
        _fail("LOCAL_IDENTIFIER_SCOPE_DRIFT")
    local_ids.verify_namespace_collisions(projection_proofs)


def _verify_source_span_outputs(donor, state, *, recursive_v2_proof=False):
    """Retain direct proofs or the exact recursively verified v2 provider outputs."""
    authority = donors._review_json(state / donors.AUTHORITY)
    recursive = (
        recursive_v2_proof and authority.get("anchor_mode") == core.SOURCE_SPANS_V2_MODE
        and "continuation" in authority
    )
    if (
        authority.get("anchor_mode") not in core.SOURCE_SPAN_MODES
        or ("continuation" in authority and not recursive) or "quote_review" in authority
    ):
        _fail("SOURCE_SPAN_DIRECT_PRODUCER_REQUIRED")
    ledger = donors._review_json(state / donors.BUDGET)
    responses = {}
    for key, paid in donor.responses.items():
        if recursive:
            if paid.provider_output is None or paid.review_projection is not None or paid.review_record is not None:
                _fail("SOURCE_SPAN_PROVIDER_PROOF_MISSING")
            responses[key] = paid
            continue
        if (
            paid.donor_fingerprint != donor.binding["fingerprint"]
            or paid.review_projection is not None or paid.review_record is not None
        ):
            _fail("SOURCE_SPAN_DIRECT_PRODUCER_REQUIRED")
        # This existing helper proves decoded provider bytes against raw JSON;
        # despite its historical name it does not resolve/project any quotes.
        provider = donors._verify_quote_provider_output(
            state, paid.request_hash, paid.response, ledger,
        )
        responses[key] = replace(paid, provider_output=provider)
    if (
        donors._sha(state / donors.AUTHORITY) != donor.binding["authority_hash"]
        or donors._sha(state / donors.BUDGET) != donor.binding["budget_hash"]
        or donors._sha(state / donors.INTEGRITY) != donor.binding["integrity_hash"]
        or core._inventory(state) != donors._review_json(state / donors.INTEGRITY)
    ):
        _fail("SOURCE_SPAN_PROVIDER_SNAPSHOT_DRIFT")
    return replace(donor, responses=responses)


def _identity(inputs, plan):
    return stage._clean_identity(
        inputs.l1_receipt.identity, contract_kind="l2.stage",
        prompt_version=VERSION, prompt_hash=plan["plan_hash"],
        model_version=plan["model_identity"]["chat_deployment"],
        model_hash=canonical_sha256(plan["model_identity"]),
        extractor_name=EXTRACTOR, extractor_version=VERSION,
    )


def _build_leaves(inputs, materialized, accepted, plan):
    identity = _identity(inputs, plan)
    vocabulary = stage.compile_closed_vocabulary(inputs.domain_contract)
    leaves = []
    projection_proofs = []
    for unit, paid in accepted:
        try:
            response = _resolved(unit, paid, plan["anchor_mode"])
            if local_ids.PROFILE_KEY in plan:
                response, proof = local_ids.qualify_response(
                    response, source_unit_id=unit.source_unit_id,
                    slice_start=unit.slice_start, slice_end=unit.slice_end,
                )
                projection_proofs.append(proof)
            candidates = response.get("candidates") if isinstance(response, dict) else response
            leaf = stage.build_candidate_batch(
                candidates, vocabulary=vocabulary, contract=inputs.domain_contract,
                authority=stage._authority(inputs, materialized), base_identity=identity,
                source_unit_id=unit.source_unit_id, work_unit_id=unit.work_unit_id,
                classifier_version="closed-vocabulary/1.0.0",
                prompt_hash=plan["plan_hash"], model_hash=identity.model_hash,
                extractor_name=EXTRACTOR, extractor_version=VERSION,
                occurred_at_utc=inputs.l1_receipt.completed_at_utc,
            )
            validate_prefix_candidate_anchors(
                (leaf,), contract=inputs.domain_contract, source_units=materialized.source_units,
            )
            leaves.append(leaf)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(
                f"APPROVED_PARTIAL_HANDOFF_CANONICAL_PARSE_FAILED: {_range(unit)}: {exc}"
            ) from exc
    if local_ids.PROFILE_KEY in plan:
        local_ids.verify_namespace_collisions(projection_proofs)
    return tuple(leaves)


def _fresh(path, protected):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        _fail("UNSAFE_STATE")
    child = path.resolve()
    if child.exists() or any(
        child == p.resolve() or child in p.resolve().parents or p.resolve() in child.parents
        for p in protected
    ):
        _fail("FRESH_STATE_REQUIRED")
    return child


def run_partial_handoff(
    *, reuse_approved_run: Path, source_path: Path, l1_state_root: Path,
    domain_path: Path, state_root: Path, foundry_config,
    window_run_path: Path | None = None, discovery_file: Path | None = None,
    approved_quote_review: Path | None = None, few_shot=None,
    dry_run: bool = True, approved_plan_hash: str | None = None,
    approval_actor: str | None = None, approval_rationale: str | None = None,
    exclude_completed_roots: tuple[str, ...] = (), exclusion_rationale: str | None = None,
    qualify_local_identifiers: bool = False,
):
    """Plan without writes, then seal only after exact scope-hash approval."""
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    if type(qualify_local_identifiers) is not bool:
        _fail("LOCAL_IDENTIFIER_PROFILE_INVALID")
    protected = [source_path, l1_state_root, domain_path, reuse_approved_run]
    protected += [p for p in (window_run_path, discovery_file, approved_quote_review) if p]
    child = _fresh(state_root, protected)
    mode = core.resolve_anchor_mode(None, sealed_state=reuse_approved_run)
    if qualify_local_identifiers and mode not in core.SOURCE_SPAN_MODES:
        _fail("LOCAL_IDENTIFIER_SOURCE_SPAN_PROOF_REQUIRED")
    if mode in core.SOURCE_SPAN_MODES and approved_quote_review is not None:
        _fail("SOURCE_SPAN_QUOTE_REVIEW_UNSUPPORTED")
    output_tokens, context_tokens = core.resolve_token_budgets(
        max_output_tokens=None, max_context_tokens=None, sealed_state=reuse_approved_run,
    )
    inputs, _, materialized, cache_hash = core.prepare_approved_sources(
        source_path=source_path, l1_state_root=l1_state_root, domain_path=domain_path,
        window_run_path=window_run_path, discovery_file=discovery_file,
    )
    vocabulary = stage.compile_closed_vocabulary(inputs.domain_contract)
    files = {item.source_file_id: item for item in inputs.corpus_manifest.entries}
    entries = {
        unit.source_unit_id: SimpleNamespace(
            relative_source_ref=files[unit.source_file_id].relative_source_ref,
            section_path=unit.locator.section_path,
        ) for unit in materialized.source_units
    }
    expected = donors._base_authority(
        inputs, materialized, cache_hash, foundry_config, 0, 0,
        output_tokens, few_shot, context_tokens, mode,
    )
    review = reviews.QuoteReview(approved_quote_review) if approved_quote_review else None
    with ExitStack() as stack:
        seen = set()
        try:
            donor = donors._verify_donor(
                reuse_approved_run, expected=expected, inputs=inputs, materialized=materialized,
                vocabulary=vocabulary, entries=entries, stack=stack, seen=seen,
                quote_review=review,
                review_ancestry=review is not None and review.version == reviews.ANCESTRY_VERSION,
                source_span_continuation=mode == core.SOURCE_SPANS_V2_MODE,
            )
        except SourceSpanResolutionError as exc:
            raise ValueError(f"APPROVED_PARTIAL_HANDOFF_CANONICAL_PARSE_FAILED: {exc}") from exc
        if mode in core.SOURCE_SPAN_MODES:
            donor = _verify_source_span_outputs(
                donor, reuse_approved_run.resolve(),
                recursive_v2_proof=mode == core.SOURCE_SPANS_V2_MODE,
            )
        _fresh(child, protected + list(seen))
        root_authority = {**expected, "fingerprint": canonical_sha256(expected)}
        roots = donors._roots(root_authority, inputs, materialized)
        included, excluded, accepted = _coverage(
            roots, donor, vocabulary, entries,
            inputs.domain_contract.reasoning_policy.max_relations_per_work_unit, mode,
        )
        if not included:
            _fail("NO_COMPLETE_RESPONSE_BACKED_ROOTS")
        originally_completed_root_count = len(included)
        originally_completed_leaf_count = len(accepted)
        missing_root_count = len(excluded)
        included, operator_excluded, accepted = _select_completed_roots(
            included, accepted, exclude_completed_roots, exclusion_rationale,
        )
        excluded = [*excluded, *operator_excluded]
        # Inventory is all cached source units, not selected contract roots or paid responses.
        inventory_ranges = [
            {**_range(root), "reason": "outside_approved_contract_ceiling"}
            for root in _outside_ranges(materialized, roots)
        ]
        acceptance = inputs.domain_contract.window_run_acceptance
        source_chunk_inventory = (
            acceptance.coverage.total_chunks if acceptance is not None else None
        )
        plan = {
            "operation": VERSION, "state_root": str(child),
            "domain_contract_hash": compute_contract_hash(inputs.domain_contract),
            "producer_authority": expected,
            "partial_code_identity": {
                name: hashlib.sha256((Path(__file__).parent.parent / name).read_bytes()).hexdigest()
                for name in (
                    "enrichment/approved_partial_handoff.py",
                    "enrichment/approved_quote_review.py",
                    "enrichment/schema2_validation_stage.py",
                    "serving/lifecycle_projection.py",
                    "serving/structured_publication.py",
                    "semantic/source_tables.py",
                    "deploy/schema2_prototype.py",
                )
            },
            "donor": donor.binding, "prior_spent": donor.spent,
            "model_identity": expected["model_identity"], "anchor_mode": mode,
            "review_chain": reviews.review_chain(donor.responses),
            "latest_quote_review": review.record() if review else None,
            "source_unit_inventory_count": len(materialized.source_units),
            "source_chunk_inventory_count": source_chunk_inventory,
            "source_unit_manifest_hash": materialized.source_unit_manifest.manifest_hash,
            "approved_contract_root_count": len(roots),
            "completed_root_count": len(included),
            "selected_root_count": len(included),
            "originally_completed_root_count": originally_completed_root_count,
            "originally_completed_leaf_count": originally_completed_leaf_count,
            "operator_excluded_completed_root_count": len(operator_excluded),
            "missing_root_count": missing_root_count,
            "exclude_completed_roots": sorted(exclude_completed_roots),
            "exclusion_rationale": exclusion_rationale,
            "excluded_root_count": len(excluded),
            "completed_leaf_count": len(accepted),
            "retained_donor_response_count": len(donor.responses),
            "included_roots": included, "excluded_roots": excluded,
            "outside_contract_ranges": inventory_ranges,
            "new_logical_calls": 0, "new_physical_calls": 0,
            "scope_notice": (
                "PARTIAL EXISTING-DATA EXTRACTION: "
                + (
                    f"{len(included)} selected complete roots, {len(operator_excluded)} completed-but-"
                    f"operator-excluded roots, {missing_root_count} missing/incomplete roots "
                    f"of {len(roots)} approved roots; originally {originally_completed_root_count} "
                    f"response-complete roots, now {len(accepted)} selected response-backed leaves. "
                    "Operator-excluded data is withheld, not missing or observed-empty. "
                    if operator_excluded else
                    f"{len(included)} of {len(roots)} approved extraction roots completed "
                    f"({len(accepted)} response-backed leaves); {len(excluded)} approved roots excluded. "
                )
                + "Source chunk inventory: "
                f"{source_chunk_inventory if source_chunk_inventory is not None else 'not recorded'}. "
                "Retained responses include overflow parents and are not processed chunk counts. "
                "Zero new model calls. Unprocessed ranges are UNKNOWN, not observed-empty. "
                "This is not full-corpus extraction, semantic recall, evidence approval, "
                "business-quality approval or verified answers. Cite validated evidence only; "
                "disclose gaps and abstain from full-scope or completeness claims."
            ),
        }
        profile = donors._verified_partial_profile(donors._review_json(reuse_approved_run / donors.AUTHORITY))
        if profile is not None:
            plan["source_execution_profile"] = profile
        if qualify_local_identifiers:
            plan[local_ids.PROFILE_KEY] = local_ids.profile()
            plan["partial_code_identity"][local_ids.CODE_PATH] = _local_identifier_code_hash()
            plan["scope_notice"] += (
                " Opaque response-local references are source-slice-qualified; native stable identity "
                "groups and business keys are preserved. This technical identity correction is not "
                "new extraction, semantic repair, or global deduplication."
            )
        plan["plan_hash"] = canonical_sha256(plan)
        # Dry runs parse every accepted response in full; failed candidates cannot disappear.
        leaves = _build_leaves(inputs, materialized, accepted, plan)
        if dry_run:
            return {**plan, "status": "planned", "remote_calls": 0, "writes": 0}
        if approved_plan_hash != plan["plan_hash"]:
            _fail("EXACT_PLAN_APPROVAL_REQUIRED")
        if not approval_actor or not approval_actor.strip() or not approval_rationale or not approval_rationale.strip():
            _fail("APPROVAL_ACTOR_AND_RATIONALE_REQUIRED")
        scope = {
            "plan": plan,
            "approval": {
                "approved_plan_hash": approved_plan_hash, "actor": approval_actor,
                "rationale": approval_rationale, "approved_at": started_at.isoformat(),
            },
        }
        return _materialize(
            state_root=child, scope=scope, inputs=inputs, materialized=materialized,
            leaves=leaves, accepted=accepted, donor=donor, started=started, started_at=started_at,
        )


def _outside_ranges(materialized, roots):
    """Exact source ranges beyond the ceiling, with no inference of extraction."""
    from dataclasses import replace
    from .schema2_work_units import root_work_unit
    for unit in materialized.source_units:
        full = root_work_unit(unit, pass_name="inventory", authority_fingerprint="0" * 64)
        cursor = 0
        for root in sorted(
            (r for r in roots if r.source_unit_id == unit.source_unit_id),
            key=lambda r: r.slice_start,
        ):
            if root.slice_start > cursor:
                yield replace(full, slice_start=cursor, slice_end=root.slice_start)
            cursor = max(cursor, root.slice_end)
        if cursor < full.slice_end:
            yield replace(full, slice_start=cursor)


def _scope_entry(scope, payload):
    return stage._artifact_entry(
        artifact_id=SCOPE_ID, contract_kind=SCOPE_KIND, contract_version="1.0.0",
        schema_hash=canonical_sha256({"kind": SCOPE_KIND, "version": "1.0.0"}),
        content_hash=hashlib.sha256(payload).hexdigest(), payload=payload, row_count=1,
    )


def has_qualified_scope(scope):
    return scope is not None and local_ids.PROFILE_KEY in scope["plan"]


def witness_artifact_path(artifact_id):
    if artifact_id == WITNESS_ID:
        return Path(WITNESS_FILE)
    if not artifact_id.startswith(WITNESS_PREFIX):
        _fail("WITNESS_ARTIFACT_INVALID")
    relative = artifact_id.removeprefix(WITNESS_PREFIX)
    parts = relative.split("/")
    if (
        "\\" in relative or any(part in ("", ".", "..") for part in parts)
        or (relative not in _WITNESS_METADATA and not (
            len(parts) == 2 and parts[0] in ("source-units", "retained-responses")
            and parts[1].endswith(".json")
        ))
    ):
        _fail("WITNESS_PATH_INVALID")
    return Path(WITNESS_DIR) / relative


def _witness_dependencies(scope):
    paths = set(_WITNESS_METADATA)
    for item in scope["plan"]["included_roots"]:
        for node in item["nodes"]:
            if node["status"] != "response_backed_leaf":
                continue
            for key, directory in (("source_unit_id", "source-units"), ("work_unit_id", "retained-responses")):
                identifier = node[key]
                if "/" in identifier or "\\" in identifier:
                    _fail("WITNESS_PATH_INVALID")
                relative = f"{directory}/{identifier.replace(':', '-', 1)}.json"
                witness_artifact_path(WITNESS_PREFIX + relative)
                paths.add(relative)
    return paths


def _witness_entry(artifact_id, payload, *, row_count=1):
    kind = WITNESS_KIND if artifact_id == WITNESS_ID else WITNESS_FILE_KIND
    return stage._artifact_entry(
        artifact_id=artifact_id, contract_kind=kind, contract_version=WITNESS_VERSION,
        schema_hash=canonical_sha256({"kind": kind, "version": WITNESS_VERSION}),
        content_hash=hashlib.sha256(payload).hexdigest(), payload=payload, row_count=row_count,
    )


def _one_witness_entry(manifest, artifact_id):
    entries = [entry for entry in manifest.entries if entry.artifact_id == artifact_id]
    if len(entries) != 1:
        _fail("WITNESS_MANIFEST_DRIFT")
    return entries[0]


def _verify_witness_bundle(root, scope, manifest, *, upstream_manifest=None):
    """Resolve only the declared, closed local witness manifest; never search paths."""
    if root.is_symlink() or any(p.is_symlink() for p in root.parents) or any(
        p.is_symlink() for p in root.rglob("*")
    ):
        _fail("UNSAFE_STATE")
    paths = _witness_dependencies(scope)
    payload = (root / WITNESS_FILE).read_bytes()
    bundle = json.loads(payload)
    if (
        set(bundle) != {"version", "scope_hash", "files"}
        or bundle["version"] != WITNESS_VERSION
        or bundle["scope_hash"] != canonical_sha256(scope)
        or not isinstance(bundle["files"], dict) or set(bundle["files"]) != paths
        or _one_witness_entry(manifest, WITNESS_ID) != _witness_entry(
            WITNESS_ID, payload, row_count=len(paths),
        )
    ):
        _fail("WITNESS_MANIFEST_DRIFT")
    expected_ids = {WITNESS_ID, *(WITNESS_PREFIX + path for path in paths)}
    actual_ids = {
        entry.artifact_id for entry in manifest.entries
        if entry.artifact_id == WITNESS_ID or entry.artifact_id.startswith(WITNESS_PREFIX)
    }
    if actual_ids != expected_ids:
        _fail("WITNESS_MANIFEST_DRIFT")
    witness_root = root / WITNESS_DIR
    if {str(path.relative_to(witness_root)) for path in witness_root.rglob("*") if path.is_file()} != paths:
        _fail("WITNESS_FILE_SET_DRIFT")
    data = {}
    for relative in sorted(paths):
        artifact_id = WITNESS_PREFIX + relative
        content = (root / witness_artifact_path(artifact_id)).read_bytes()
        if (
            local_ids.lossless_hash(bundle["files"][relative]) != local_ids.lossless_hash({
                "sha256": hashlib.sha256(content).hexdigest(), "byte_count": len(content),
            })
            or _one_witness_entry(manifest, artifact_id) != _witness_entry(artifact_id, content)
        ):
            _fail("WITNESS_CONTENT_DRIFT")
        data[relative] = content
    integrity = json.loads(data[INTEGRITY])
    if any(
        integrity.get(path) != hashlib.sha256(content).hexdigest()
        for path, content in data.items() if path != INTEGRITY and not path.startswith("l3/")
    ):
        _fail("WITNESS_L2_INTEGRITY_DRIFT")
    l2_receipt = stage.StageReceipt.model_validate_json(data["stage-receipt.json"])
    l2_input = stage.ArtifactManifest.model_validate_json(data["input-manifest.json"])
    l2_output = stage.ArtifactManifest.model_validate_json(data["output-manifest.json"])
    source_manifest = stage.ArtifactManifest.model_validate_json(data["source-unit-manifest.json"])
    l3_receipt = stage.StageReceipt.model_validate_json(data["l3/stage-receipt.json"])
    l3_input = stage.ArtifactManifest.model_validate_json(data["l3/input-manifest.json"])
    l3_output = stage.ArtifactManifest.model_validate_json(data["l3/output-manifest.json"])
    for receipt, stage_id, input_manifest, output_manifest in (
        (l2_receipt, "L2", l2_input, l2_output),
        (l3_receipt, "L3", l3_input, l3_output),
    ):
        if (
            receipt.stage_id != stage_id or receipt.status != "succeeded"
            or receipt.input_manifest_id != input_manifest.artifact_manifest_id
            or receipt.input_manifest_hash != input_manifest.manifest_hash
            or receipt.output_manifest_id != output_manifest.artifact_manifest_id
            or receipt.output_manifest_hash != output_manifest.manifest_hash
        ):
            _fail("WITNESS_RECEIPT_DRIFT")
    if (
        l2_receipt.identity.extractor_name != EXTRACTOR
        or l2_receipt.identity.prompt_hash != scope["plan"]["plan_hash"]
        or json.loads(data[SCOPE_FILE]) != scope
        or source_manifest.manifest_hash != scope["plan"]["source_unit_manifest_hash"]
        or _one_witness_entry(l3_input, l2_receipt.stage_receipt_id).content_hash != l2_receipt.receipt_hash
        or _one_witness_entry(l3_input, source_manifest.artifact_manifest_id).content_hash != source_manifest.manifest_hash
        or any(
            _one_witness_entry(item, SCOPE_ID) != _scope_entry(scope, data[SCOPE_FILE])
            for item in (l2_input, l2_output, l3_output)
        )
    ):
        _fail("WITNESS_LINEAGE_DRIFT")
    if upstream_manifest is None:
        l4_receipt = stage.StageReceipt.model_validate_json((root / "stage-receipt.json").read_bytes())
        if (
            l4_receipt.stage_id != "L4" or l4_receipt.status != "succeeded"
            or l4_receipt.input_manifest_id != l3_output.artifact_manifest_id
            or l4_receipt.input_manifest_hash != l3_output.manifest_hash
            or l4_receipt.output_manifest_id != manifest.artifact_manifest_id
            or l4_receipt.output_manifest_hash != manifest.manifest_hash
        ):
            _fail("WITNESS_LINEAGE_DRIFT")
    elif upstream_manifest != l3_output:
        _fail("WITNESS_LINEAGE_DRIFT")
    _verify_local_identifier_projections(witness_root, scope["plan"])


def export_qualified_witnesses(source, destination):
    """Copy exact witnesses from the approved L2 location tied to real L3 authority."""
    scope = source.inputs.partial_extraction_scope
    if not has_qualified_scope(scope):
        return ()
    if destination.is_symlink() or any(path.is_symlink() for path in destination.parents):
        _fail("UNSAFE_STATE")
    if any(
        (destination / path).exists() or (destination / path).is_symlink()
        for path in (WITNESS_DIR, WITNESS_FILE, "stage-receipt.json", "output-manifest.json")
    ):
        _fail("WITNESS_FRESH_DESTINATION_REQUIRED")
    scope_entry = _scope_entry(scope, (canonical_json(scope) + "\n").encode("utf-8"))
    if any(
        _one_witness_entry(item, SCOPE_ID) != scope_entry
        for item in (source.output_manifest, source.inputs.l2_output_manifest)
    ):
        _fail("WITNESS_SOURCE_AUTHORITY_DRIFT")
    root = Path(scope["plan"]["state_root"])
    if (
        not root.is_absolute() or root.resolve() != root or root.is_symlink()
        or any(path.is_symlink() for path in root.parents)
        or any(path.is_symlink() for path in root.rglob("*"))
    ):
        _fail("UNSAFE_STATE")
    receipt = stage.StageReceipt.model_validate_json((root / "stage-receipt.json").read_bytes())
    output = stage.ArtifactManifest.model_validate_json((root / "output-manifest.json").read_bytes())
    if receipt != source.inputs.l2_receipt or output != source.inputs.l2_output_manifest:
        _fail("WITNESS_SOURCE_AUTHORITY_DRIFT")
    if read_scope(root, output, receipt=receipt) != scope:
        _fail("WITNESS_SOURCE_AUTHORITY_DRIFT")
    for filename, expected in (
        ("stage-receipt.json", source.receipt),
        ("input-manifest.json", source.input_manifest),
        ("output-manifest.json", source.output_manifest),
    ):
        path = source.run_root / filename
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            _fail("UNSAFE_STATE")
        if type(expected).model_validate_json(path.read_bytes()) != expected:
            _fail("WITNESS_SOURCE_AUTHORITY_DRIFT")
    entries, files = [], {}
    for relative in sorted(_witness_dependencies(scope)):
        origin = source.run_root / relative.removeprefix("l3/") if relative.startswith("l3/") else root / relative
        payload = origin.read_bytes()
        artifact_id = WITNESS_PREFIX + relative
        target = destination / witness_artifact_path(artifact_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append(_witness_entry(artifact_id, payload))
        files[relative] = {"sha256": hashlib.sha256(payload).hexdigest(), "byte_count": len(payload)}
    bundle = {"version": WITNESS_VERSION, "scope_hash": canonical_sha256(scope), "files": files}
    payload = stage._persist_json(destination / WITNESS_FILE, bundle)
    entries.append(_witness_entry(WITNESS_ID, payload, row_count=len(files)))
    _verify_witness_bundle(
        destination, scope, SimpleNamespace(entries=entries), upstream_manifest=source.output_manifest,
    )
    return tuple(entries)


@_locked_reuse
def _materialize(*, state_root, scope, inputs, materialized, leaves, accepted, donor,
                 started, started_at):
    _fresh(state_root, [])
    state_root.mkdir()
    plan = scope["plan"]
    identity = _identity(inputs, plan)
    fingerprint = stage.l2_input_fingerprint(
        inputs, materialized.source_unit_manifest,
        prompt_version=VERSION, prompt_hash=plan["plan_hash"],
        model_version=identity.model_version, model_hash=identity.model_hash,
        extractor_name=EXTRACTOR, extractor_version=VERSION,
        response_schema_hash=stage.L2_RESPONSE_SCHEMA_HASH,
        split_policy_version="paragraph-sentence-token/1.0.0",
        collection_partition_version=stage.COLLECTION_PARTITION_VERSION,
    )
    scope_payload = stage._persist_json(state_root / SCOPE_FILE, scope)
    scope_entry = _scope_entry(scope, scope_payload)
    input_manifest = stage._input_manifest(
        inputs=inputs, materialized=materialized, identity=identity, fingerprint=fingerprint,
    )
    input_manifest = stage._manifest(
        identity=identity, label="input", entries=(*input_manifest.entries, scope_entry),
    )
    stage._persist_json(state_root / "input-manifest.json", input_manifest)
    for unit in materialized.source_units:
        stage._persist_json(
            state_root / "source-units" / f"{unit.source_unit_id.replace(':', '-', 1)}.json", unit,
        )
    for unit, paid in accepted:
        adapter_proof = {}
        if plan["anchor_mode"] in core.SOURCE_SPAN_MODES:
            adapter = core.source_span_adapter(plan["anchor_mode"])
            catalog = adapter.catalog(
                source_unit_id=unit.source_unit_id, source_text_hash=unit.source_text_hash,
                source_text=unit.text, slice_start=unit.slice_start, slice_end=unit.slice_end,
            )
            resolved = _resolved(unit, paid, plan["anchor_mode"])
            adapter_proof = {
                "anchor_adapter_version": adapter.version,
                "source_segments": catalog,
                "source_segments_sha256": core.response_hash(catalog, plan["anchor_mode"]),
                "resolved_response": resolved,
                "resolved_response_hash": core.response_hash(resolved, plan["anchor_mode"]),
            }
            if local_ids.PROFILE_KEY in plan:
                projected, proof = local_ids.qualify_response(
                    resolved, source_unit_id=unit.source_unit_id,
                    slice_start=unit.slice_start, slice_end=unit.slice_end,
                )
                adapter_proof[local_ids.ARTIFACT_KEY] = {"response": projected, "proof": proof}
        core.persist_response(
            state_root / "retained-responses" / f"{unit.work_unit_id.replace(':', '-', 1)}.json",
            {"range": _range(unit), "provenance": paid.provenance(),
             "response": paid.response, "provider_output": paid.provider_output,
             "review_projection": paid.review_projection, "review_record": paid.review_record,
             **adapter_proof},
            plan["anchor_mode"],
        )
    stage._persist_json(state_root / "prior-spending.json", donor.spent)
    _verify_local_identifier_projections(state_root, plan)
    fragments = stage.derive_collection_member_fragments(
        tuple(sorted(leaves, key=lambda leaf: leaf.batch.extraction_candidate_batch_id)),
        contract=inputs.domain_contract,
    )
    deferrals = []
    member_sets = stage.build_required_member_set_proposals(
        fragments, leaves=leaves, contract=inputs.domain_contract,
        authority_factory=lambda requirement: stage._authority(inputs, materialized, requirement),
        base_identity=identity, deferrals=deferrals,
    )
    requirements = {r.requirement_id: r for r in inputs.domain_contract.completeness_requirements}
    batches = tuple(
        stage.merge_candidate_batches(
            leaves, authority=stage._authority(inputs, materialized, requirements[view.requirement_id]),
            base_identity=identity, merge_key=f"{view.requirement_id}:{view.aggregate_entity_id}",
        ) for view in member_sets
    )
    if any(
        view.proposal.extraction_candidate_batch_id != batch.extraction_candidate_batch_id
        or view.proposal.extraction_candidate_batch_hash != batch.batch_hash
        for view, batch in zip(member_sets, batches)
    ):
        _fail("REQUIRED_MEMBER_BINDING_DRIFT")
    output_entries = stage._output_artifacts(
        state_root=state_root, materialized=materialized, leaves=leaves,
        required_member_sets=member_sets, required_member_batches=batches,
        collection_deferrals=tuple(deferrals),
    )
    output_manifest = stage._manifest(
        identity=identity, label="output", entries=(*output_entries, scope_entry),
    )
    stage._persist_json(state_root / "output-manifest.json", output_manifest)
    metrics = stage._resource_metrics(
        identity=identity, inputs=inputs, materialized=materialized, model_calls=0,
        cache_hits=len(accepted), storage_write_bytes=sum(
            p.stat().st_size for p in state_root.rglob("*") if p.is_file()
        ), started=started,
    )
    stage._persist_json(state_root / "resource-metrics.json", metrics)
    values = {
        "identity": identity.model_copy(update={"contract_kind": "c0.stage_receipt"}),
        "stage_receipt_id": stage.deterministic_contract_id(
            "stage-receipt", {"stage_id": "L2", "run_id": identity.run_id, "skip_key": fingerprint},
        ),
        "stage_id": "L2", "stage_name": stage.L2_STAGE_NAME, "stage_contract_version": "1.0.0",
        "status": "succeeded", "input_manifest_id": input_manifest.artifact_manifest_id,
        "input_manifest_hash": input_manifest.manifest_hash,
        "output_manifest_id": output_manifest.artifact_manifest_id,
        "output_manifest_hash": output_manifest.manifest_hash, "skip_key": fingerprint,
        "accepted_contract_versions": stage.L2_OUTPUT_ACCEPTED_VERSIONS,
        "resource_metrics_id": metrics.resource_metrics_id, "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1, "remote_operation_refs": (), "error_codes": (),
    }
    receipt = stage.StageReceipt(
        **values, started_at_utc=started_at, completed_at_utc=datetime.now(timezone.utc),
        receipt_hash=canonical_sha256(values),
    )
    stage.validate_receipt_resources(receipt, metrics)
    stage._persist_json(state_root / "stage-receipt.json", receipt)
    stage._persist_json(state_root / INTEGRITY, _inventory(state_root))
    return {
        **plan, "status": "succeeded", "remote_calls": 0,
        "l2_receipt_hash": receipt.receipt_hash, "l2_output_manifest_hash": output_manifest.manifest_hash,
        "scope_hash": canonical_sha256(scope),
    }


def _inventory(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file() and p != root / INTEGRITY
    }


def read_scope(root, manifest, *, receipt=None):
    """Fail closed on missing, modified, detached or unapproved scope disclosures."""
    matches = [entry for entry in manifest.entries if entry.artifact_id == SCOPE_ID]
    required = receipt is not None and receipt.identity.extractor_name == EXTRACTOR
    if not matches:
        if required or (root / SCOPE_FILE).exists() or any(
            entry.artifact_id == WITNESS_ID or entry.artifact_id.startswith(WITNESS_PREFIX)
            for entry in manifest.entries
        ):
            _fail("SCOPE_MISSING_FROM_MANIFEST")
        return None
    if len(matches) != 1:
        _fail("SCOPE_MANIFEST_DRIFT")
    if receipt is not None and not required:
        _fail("SCOPE_PRODUCER_DRIFT")
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        _fail("UNSAFE_STATE")
    if required and any(p.is_symlink() for p in root.rglob("*")):
        _fail("UNSAFE_STATE")
    path = root / SCOPE_FILE
    if path.is_symlink():
        _fail("UNSAFE_STATE")
    payload = path.read_bytes()
    scope = json.loads(payload)
    plan = scope["plan"]
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    approval = scope["approval"]
    if (
        canonical_sha256(unsigned) != plan["plan_hash"]
        or approval["approved_plan_hash"] != plan["plan_hash"]
        or not approval["actor"].strip() or not approval["rationale"].strip()
        or matches[0] != _scope_entry(scope, payload)
        or plan["operation"] != VERSION
        or plan["new_logical_calls"] != 0 or plan["new_physical_calls"] != 0
    ):
        _fail("SCOPE_INTEGRITY_DRIFT")
    if required:
        if receipt.identity.prompt_hash != plan["plan_hash"]:
            _fail("SCOPE_RECEIPT_DRIFT")
        if json.loads((root / INTEGRITY).read_bytes()) != _inventory(root):
            _fail("SNAPSHOT_INTEGRITY_DRIFT")
    witness_entries = [
        entry for entry in manifest.entries
        if entry.artifact_id == WITNESS_ID or entry.artifact_id.startswith(WITNESS_PREFIX)
    ]
    if witness_entries:
        if required or not has_qualified_scope(scope):
            _fail("WITNESS_PROFILE_DRIFT")
        _verify_witness_bundle(root, scope, manifest)
    elif has_qualified_scope(scope) and not required:
        _fail("PORTABLE_WITNESS_REQUIRED")
    else:
        _verify_local_identifier_projections(root, plan)
    return scope


def scope_context(scope):
    """Keep exact ranges/review artifacts sealed locally, not in agent instructions."""
    keys = (
        "plan_hash", "domain_contract_hash", "source_unit_inventory_count",
        "source_chunk_inventory_count", "approved_contract_root_count", "completed_root_count",
        "excluded_root_count", "completed_leaf_count", "retained_donor_response_count",
        "new_logical_calls", "new_physical_calls", "scope_notice",
    )
    selection_keys = (
        "selected_root_count", "originally_completed_root_count", "originally_completed_leaf_count",
        "operator_excluded_completed_root_count", "missing_root_count", "exclusion_rationale",
        local_ids.PROFILE_KEY,
    )
    return {
        "scope_hash": canonical_sha256(scope), "scope_artifact": SCOPE_FILE,
        "approval": scope["approval"],
        "plan": {
            **{key: scope["plan"][key] for key in keys},
            **{key: scope["plan"][key] for key in selection_keys if key in scope["plan"]},
        },
    }
