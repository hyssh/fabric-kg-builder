"""Opt-in raw-response continuation; donors and their execution budgets stay immutable."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import stat

from fabric_kg_builder.contracts.base import canonical_sha256

from . import approved_reextraction as core
from .discovery_reuse import _locked_reuse, _persist_exact
from .schema2_extraction import compile_closed_vocabulary, render_extraction_prompt
from .schema2_sources import l2_input_fingerprint
from .schema2_work_units import _relationship_count, split_work_unit
from .window_prefix import plan_approved_work_units

VERSION = "approved-donor-continuation/1.4.0"
CODE_PATH = "enrichment/approved_donor_continuation.py"
AUTHORITY = "approved-reextraction-authority.json"
INTEGRITY = "reextraction-integrity.json"
BUDGET = "reextraction-budget.json"
COUNTERS = ("logical_calls", "physical_calls", "reserved_output_tokens")
PARTIAL_PROFILE_KEY = "budget_limited_partial"


def _fail(reason):
    raise ValueError(f"APPROVED_REEXTRACTION_DONOR_{reason}")


def _partial_profile():
    return {
        "version": "approved-budget-limited-partial/1.0.0",
        "allow_budget_limited_partial": True,
        "completion_expectation": "budget_limited_partial",
        "exhaustion_policy": "fail_closed_no_success_receipt",
    }


def _verified_partial_profile(authority):
    if PARTIAL_PROFILE_KEY not in authority:
        return None
    profile = authority[PARTIAL_PROFILE_KEY]
    if (
        not isinstance(profile, dict) or profile != _partial_profile()
        or profile.get("allow_budget_limited_partial") is not True
        or "continuation" not in authority
        or authority.get("anchor_mode") != core.SOURCE_SPANS_V2_MODE
        or "quote_review" in authority
    ):
        _fail("BUDGET_LIMITED_PARTIAL_PROFILE_INVALID")
    return _partial_profile()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("APPROVED_REEXTRACTION_DONOR_REVIEW_ARTIFACT_INVALID") from exc


def _continuation_anchor_mode(mode, *, sealed_state, resume):
    """The separately sealed helper opts in to v2; the frozen producer does not."""
    try:
        selected = core.resolve_anchor_mode(mode, sealed_state=sealed_state)
    except ValueError as exc:
        if not resume and str(exc).startswith("APPROVED_REEXTRACTION_"):
            raise ValueError(str(exc).replace(
                "APPROVED_REEXTRACTION_", "APPROVED_REEXTRACTION_DONOR_", 1,
            )) from exc
        raise
    if selected in core.SOURCE_SPAN_MODES and selected != core.SOURCE_SPANS_V2_MODE:
        _fail("SOURCE_SPANS_UNSUPPORTED: only exact same-producer source-spans-v2 continuation is supported")
    return selected


@contextmanager
def _read_lock(state):
    """Hold the producer's existing lock, without creating or writing donor files."""
    try:
        import fcntl
    except ImportError:
        _fail("LOCK_UNSUPPORTED")
    lock = state.parent / f".{state.name}-enrichment.lock"
    try:
        descriptor = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        _fail("CHECKPOINT_INCOMPLETE")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _fail("UNSAFE_STATE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            _fail("ACTIVE")
        yield
    finally:
        os.close(descriptor)


def _base_authority(inputs, materialized, cache_hash, config, calls, physical, output, few_shot=None,
                    max_context_tokens=core.DEFAULT_MAX_CONTEXT_TOKENS, anchor_mode=core.OFFSET_MODE):
    roots = plan_approved_work_units(
        materialized.source_units, contract=inputs.domain_contract,
        pass_name="schema-constrained-extraction", authority_fingerprint="0" * 64,
    )
    return {
        "operation": core.VERSION, "domain_authorities": inputs.authority_hashes,
        "l1_receipt_hash": inputs.l1_receipt.receipt_hash,
        "l1_output_manifest_hash": inputs.l1_output_manifest.manifest_hash,
        "source_unit_manifest_hash": materialized.source_unit_manifest.manifest_hash,
        "approved_cache_hash": cache_hash,
        "scope": [{
            "source_unit_id": root.source_unit_id, "source_text_hash": root.source_text_hash,
            "slice_start": root.slice_start, "slice_end": root.slice_end,
        } for root in roots],
        "model_identity": core.configured_identity(config),
        "code_identity": {**core._code_identity(), CODE_PATH: _sha(Path(__file__))},
        **core.prompt_authority(inputs.domain_contract, few_shot=few_shot, anchor_mode=anchor_mode),
        "max_calls": calls, "max_physical_calls": physical, "max_output_tokens": output,
        "input_budget": core.budget_policy(max_context_tokens, output),
        "max_attempts": 1, "concurrency": 1, "sdk_max_retries": 0,
    }


def _roots(authority, inputs, materialized):
    from .schema2_stage import COLLECTION_PARTITION_VERSION, L2_RESPONSE_SCHEMA_HASH

    fingerprint = l2_input_fingerprint(
        inputs, materialized.source_unit_manifest, prompt_version=core.VERSION,
        prompt_hash=authority["fingerprint"],
        model_version=authority["model_identity"]["chat_deployment"],
        model_hash=canonical_sha256(authority["model_identity"]),
        extractor_name="approved-source-reextractor", extractor_version=core.VERSION,
        response_schema_hash=L2_RESPONSE_SCHEMA_HASH,
        split_policy_version="paragraph-sentence-token/1.0.0",
        collection_partition_version=COLLECTION_PARTITION_VERSION,
    )
    return plan_approved_work_units(
        materialized.source_units, contract=inputs.domain_contract,
        pass_name="schema-constrained-extraction", authority_fingerprint=fingerprint,
    )


def _prompt(work_unit, vocabulary, entries, anchor_mode=core.OFFSET_MODE):
    return core.approved_prompt(
        work_unit, vocabulary,
        document_name=Path(entries[work_unit.source_unit_id].relative_source_ref).name,
        section_path=entries[work_unit.source_unit_id].section_path,
        anchor_mode=anchor_mode,
    )


def _request(authority, unit, prompt):
    return core.response_hash({
        "fingerprint": authority["fingerprint"], "work_unit_id": unit.work_unit_id, "prompt": prompt,
    }, authority.get("anchor_mode", core.OFFSET_MODE))


@dataclass(frozen=True)
class PaidResponse:
    response: dict
    donor_fingerprint: str
    request_hash: str
    response_hash: str
    provider_output: dict | None = None
    review_projection: dict | None = None
    review_record: dict | None = None

    def provenance(self):
        return {
            "donor_fingerprint": self.donor_fingerprint, "request_hash": self.request_hash,
            "response_hash": self.response_hash,
        }


@dataclass(frozen=True)
class VerifiedDonor:
    binding: dict
    responses: dict[str, PaidResponse]
    spent: dict[str, int]


def _metrics(ledger, authority):
    if set(ledger) != {"logical_requests", "physical_attempts"}:
        _fail("LEDGER_INVALID")
    for kind, rows in ledger.items():
        if not isinstance(rows, list):
            _fail("LEDGER_INVALID")
        for row in rows:
            if row.get("status") == "started":
                _fail("UNCERTAIN_CALL")
            fields = {"request_hash", "status"}
            if kind == "physical_attempts":
                fields.add("reserved_output_tokens")
                if row.get("reserved_output_tokens") != authority["max_output_tokens"]:
                    _fail("LEDGER_INVALID")
            if set(row) != fields or row["status"] not in {"succeeded", "failed"}:
                _fail("LEDGER_INVALID")
    logical = ledger["logical_requests"]
    physical = ledger["physical_attempts"]
    if len(logical) > authority["max_calls"] or len(physical) > authority["max_physical_calls"]:
        _fail("LEDGER_INVALID")
    logical_hashes = {row["request_hash"] for row in logical}
    if any(row["request_hash"] not in logical_hashes for row in physical):
        _fail("LEDGER_INVALID")
    return {
        "logical_calls": len(logical), "physical_calls": len(physical),
        "reserved_output_tokens": sum(row["reserved_output_tokens"] for row in physical),
    }


def _continuation(donor):
    return {
        "version": VERSION, "donor": donor.binding, "prior_spent": donor.spent,
        "response_manifest_hash": canonical_sha256({
            key: value.provenance() for key, value in sorted(donor.responses.items())
        }),
        "available_responses": len(donor.responses),
        "budget_semantics": "additional_attempts_only_prior_spending_retained",
    }


def _verify_quote_provider_output(state, request_hash, response, ledger):
    """Quote-mode donors must retain exact decoded provider output, not just NFC JSON."""
    for index, row in enumerate(ledger["physical_attempts"], 1):
        if row["request_hash"] != request_hash or row["status"] != "succeeded":
            continue
        path = state / "reextraction-provider-responses" / f"{request_hash}-{index}.json"
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
            output = base64.b64decode(artifact["raw_output_utf8_base64"], validate=True)
            if (
                artifact["request_hash"] != request_hash
                or hashlib.sha256(output).hexdigest() != artifact["raw_output_sha256"]
            ):
                _fail("PROVIDER_RESPONSE_DRIFT")
            decoded = json.loads(output.decode("utf-8"))
        except (OSError, ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise ValueError("APPROVED_REEXTRACTION_DONOR_PROVIDER_RESPONSE_DRIFT") from exc
        if core.response_hash(decoded, core.QUOTE_MODE) == core.response_hash(response, core.QUOTE_MODE):
            return artifact
    _fail("PROVIDER_RESPONSE_PROOF_MISSING")


def _verify_donor(state, *, expected, inputs, materialized, vocabulary, entries, stack, seen,
                  quote_review=None, review_ancestry=False, source_span_continuation=False):
    from . import approved_quote_review as reviews

    if state.is_symlink() or any(p.is_symlink() for p in state.parents):
        _fail("UNSAFE_STATE")
    state = state.resolve()
    if state in seen or len(seen) >= 32:
        _fail("LINEAGE_CYCLE_OR_DEPTH")
    seen.add(state)
    stack.enter_context(_read_lock(state))
    if not state.is_dir() or any(
        p.is_symlink() or not (p.is_file() or p.is_dir()) for p in state.rglob("*")
    ):
        _fail("UNSAFE_STATE")
    try:
        authority = json.loads((state / AUTHORITY).read_text())
        integrity = json.loads((state / INTEGRITY).read_text())
        ledger = json.loads((state / BUDGET).read_text())
    except (OSError, ValueError):
        _fail("CHECKPOINT_INCOMPLETE")
    if integrity != core._inventory(state):
        _fail("CHECKPOINT_DRIFT")
    unsigned = {key: value for key, value in authority.items() if key != "fingerprint"}
    if canonical_sha256(unsigned) != authority.get("fingerprint"):
        _fail("AUTHORITY_DRIFT")
    stored_review = None
    stored_record = None
    if "quote_review" in authority:
        if not review_ancestry:
            _fail("REVIEWED_CHILD_DONOR_UNSUPPORTED: explicit v2 quote review required")
        stored_record = _review_json(state / "approved-quote-correction-review.json")
        stored_review = reviews.QuoteReview.from_record(stored_record)
    if quote_review is not None:
        quote_review.verify_donor(
            authority, authority_sha256=_sha(state / AUTHORITY),
            integrity_sha256=_sha(state / INTEGRITY),
        )
    compare = dict(expected)
    profile = _verified_partial_profile(authority)
    if profile is not None:
        compare[PARTIAL_PROFILE_KEY] = profile
    compare["max_calls"] = authority.get("max_calls")
    compare["max_physical_calls"] = authority.get("max_physical_calls")
    prior = {key: 0 for key in COUNTERS}
    available = {}
    if "continuation" in authority:
        ancestor = _verify_donor(
            Path(authority["continuation"]["donor"]["state_root"]),
            expected=expected, inputs=inputs, materialized=materialized,
            vocabulary=vocabulary, entries=entries, stack=stack, seen=seen,
            quote_review=stored_review, review_ancestry=review_ancestry,
            source_span_continuation=source_span_continuation,
        )
        compare["continuation"] = _continuation(ancestor)
        prior = ancestor.spent
        available.update(ancestor.responses)
    else:
        compare["code_identity"] = {k: v for k, v in compare["code_identity"].items() if k != CODE_PATH}
    if stored_review is not None:
        if "continuation" not in authority:
            _fail("REVIEW_WITHOUT_DONOR")
        if core.response_hash(stored_review.record(), core.QUOTE_MODE) != core.response_hash(stored_record, core.QUOTE_MODE):
            _fail("REVIEW_RECORD_DRIFT")
        compare["quote_review"] = stored_review.authority()
        current_helpers = {
            CODE_PATH: _sha(Path(__file__)),
            reviews.CODE_PATH: _sha(Path(reviews.__file__)),
        }
        identity = authority["code_identity"]
        revision = reviews.verify_helper_identity(
            identity, current_helpers=current_helpers, review_version=stored_review.version,
            has_chain="quote_review_chain" in authority,
        )
        if revision == "current":
            compare["quote_review_chain"] = reviews.chain_authority(available)
            retained_chain = _review_json(state / "approved-quote-review-ancestry.json")
            if core.response_hash(retained_chain, core.QUOTE_MODE) != core.response_hash(reviews.review_chain(available), core.QUOTE_MODE):
                _fail("REVIEW_CHAIN_DRIFT")
        compare["code_identity"] = identity
    if unsigned != compare:
        _fail("SEMANTICS_DRIFT")
    anchor_mode = authority.get("anchor_mode", core.OFFSET_MODE)
    if source_span_continuation and anchor_mode != core.SOURCE_SPANS_V2_MODE:
        _fail("SOURCE_SPAN_MODE_DRIFT")
    own_spent = _metrics(ledger, authority)
    responses = {}
    for path in sorted((state / "reextraction-responses").glob("*.json")):
        cached = json.loads(path.read_text())
        fields = {"request_hash", "response", "response_hash"}
        if "imported_from" in cached:
            fields.add("imported_from")
        if (
            set(cached) != fields or path.stem != cached["request_hash"]
            or not isinstance(cached["response"], dict)
            or core.response_hash(cached["response"], anchor_mode) != cached["response_hash"]
        ):
            _fail("RESPONSE_DRIFT")
        responses[path.stem] = cached
    succeeded = [row["request_hash"] for row in ledger["logical_requests"] if row["status"] == "succeeded"]
    own_responses = {key for key, cached in responses.items() if "imported_from" not in cached}
    physical_successes = {
        row["request_hash"] for row in ledger["physical_attempts"] if row["status"] == "succeeded"
    }
    if len(succeeded) != len(set(succeeded)) or set(succeeded) != own_responses or not own_responses <= physical_successes:
        _fail("SUCCESS_PROOF_MISSING")
    provider_outputs = {}
    if anchor_mode == core.QUOTE_MODE or source_span_continuation:
        for request_hash in own_responses:
            provider_outputs[request_hash] = _verify_quote_provider_output(
                state, request_hash, responses[request_hash]["response"], ledger,
            )
    encountered = set()
    encountered_projections = set()
    request_hashes = set()

    def visit(unit):
        prompt = _prompt(unit, vocabulary, entries, anchor_mode)
        request_hash = _request(authority, unit, prompt)
        request_hashes.add(request_hash)
        key = core.response_hash(prompt, anchor_mode)
        cached = responses.get(request_hash)
        paid = available.get(key)
        if cached is not None:
            encountered.add(request_hash)
            if "imported_from" in cached:
                if (
                    paid is None or cached["imported_from"] != paid.provenance()
                    or cached["response_hash"] != paid.response_hash
                    or cached["response"] != paid.response
                ):
                    _fail("IMPORT_PROOF_INVALID")
            elif paid is not None:
                _fail("DUPLICATE_PAID_REQUEST")
            else:
                paid = PaidResponse(
                    cached["response"], authority["fingerprint"], request_hash, cached["response_hash"],
                    provider_output=provider_outputs.get(request_hash),
                )
            available[key] = paid
            if paid.review_projection is not None:
                projection_path = state / "reextraction-reviewed-responses" / f"{request_hash}.json"
                if not projection_path.is_file() or core.response_hash(
                    _review_json(projection_path), anchor_mode,
                ) != core.response_hash(reviews.reviewed_artifact(paid, unit, request_hash), anchor_mode):
                    _fail("REVIEW_PROJECTION_DRIFT")
                encountered_projections.add(request_hash)
        if paid is not None and quote_review is not None:
            quote_review.verify_response(paid, unit, paid.provider_output)
        if paid is not None and source_span_continuation:
            if paid.provider_output is None or paid.review_projection is not None:
                _fail("SOURCE_SPAN_PROVIDER_PROOF_MISSING")
            # Request-equivalence above binds the complete current catalog.
            # Decode all paid responses, including overflow parents, without
            # trusting proposed partitions or rewriting the immutable donor.
            core.source_span_adapter(anchor_mode).resolve_response(
                paid.response, source_unit_id=unit.source_unit_id,
                source_text_hash=unit.source_text_hash, source_text=unit.text,
                slice_start=unit.slice_start, slice_end=unit.slice_end,
            )
        if paid is not None and _relationship_count(paid.response) > inputs.domain_contract.reasoning_policy.max_relations_per_work_unit:
            children = split_work_unit(unit)
            if children is None:
                _fail("ATOMIC_OVERFLOW")
            for child in children:
                visit(child)

    for unit in _roots(authority, inputs, materialized):
        visit(unit)
    if encountered != set(responses) or any(
        row["request_hash"] not in request_hashes for row in ledger["logical_requests"]
    ):
        _fail("REQUEST_EQUIVALENCE_UNPROVEN")
    if encountered_projections != {
        p.stem for p in (state / "reextraction-reviewed-responses").glob("*.json")
    }:
        _fail("REVIEW_PROJECTION_DRIFT")
    if quote_review is not None:
        quote_review.finish()
        available = {key: quote_review.project(paid) for key, paid in available.items()}
    if integrity != core._inventory(state):
        _fail("CHECKPOINT_DRIFT")
    binding = {
        "state_root": str(state), "fingerprint": authority["fingerprint"],
        "authority_hash": _sha(state / AUTHORITY), "integrity_hash": _sha(state / INTEGRITY),
        "budget_hash": _sha(state / BUDGET), "inventory_hash": canonical_sha256(integrity),
    }
    return VerifiedDonor(binding, available, {key: prior[key] + own_spent[key] for key in COUNTERS})


def _missing(roots, donor, vocabulary, entries, relation_limit, preflight=None,
             anchor_mode=core.OFFSET_MODE):
    def visit(unit):
        prompt = _prompt(unit, vocabulary, entries, anchor_mode)
        if preflight is not None:
            preflight(prompt)
        paid = donor.responses.get(core.response_hash(prompt, anchor_mode))
        if paid is None:
            return 1
        if _relationship_count(paid.response) > relation_limit:
            children = split_work_unit(unit)
            if children is None:
                _fail("ATOMIC_OVERFLOW")
            return sum(visit(child) for child in children)
        return 0
    return sum(visit(root) for root in roots)


def run_approved_continuation(
    *, reuse_approved_run: Path, source_path: Path, l1_state_root: Path,
    domain_path: Path, state_root: Path, foundry_config, max_calls: int,
    max_physical_calls: int | None = None, max_output_tokens: int | None = None,
    window_run_path: Path | None = None, discovery_file: Path | None = None,
    dry_run: bool = False, resume: bool = False, model_override: str | None = None,
    client_factory=None, few_shot=None,
    max_context_tokens: int | None = None, anchor_mode: str | None = None,
    approved_quote_review: Path | None = None,
    allow_budget_limited_partial: bool = False,
):
    """Create a separately sealed child with explicit additional budgets; never amend a donor."""
    from types import SimpleNamespace
    from .schema2_stage import run_l2
    from . import approved_quote_review as review_module
    if type(allow_budget_limited_partial) is not bool:
        raise ValueError("APPROVED_REEXTRACTION_PARTIAL_FLAG_INVALID")
    quote_review = None
    if approved_quote_review is not None:
        quote_review = review_module.QuoteReview(approved_quote_review)

    anchor_mode = _continuation_anchor_mode(
        anchor_mode, sealed_state=state_root if resume else reuse_approved_run, resume=resume,
    )
    source_span_continuation = anchor_mode == core.SOURCE_SPANS_V2_MODE
    if allow_budget_limited_partial and not source_span_continuation:
        raise ValueError("APPROVED_REEXTRACTION_PARTIAL_REQUIRES_SOURCE_SPANS_V2")
    if source_span_continuation and quote_review is not None:
        _fail("SOURCE_SPAN_QUOTE_REVIEW_UNSUPPORTED")
    max_output_tokens, max_context_tokens = core.resolve_token_budgets(
        max_output_tokens=max_output_tokens, max_context_tokens=max_context_tokens,
        sealed_state=state_root if resume else reuse_approved_run,
        error_prefix="APPROVED_REEXTRACTION" if resume else "APPROVED_REEXTRACTION_DONOR",
    )
    physical = max_calls if max_physical_calls is None else max_physical_calls
    if max_calls < 0 or physical < 0 or max_output_tokens < 256:
        raise ValueError("APPROVED_REEXTRACTION_INVALID_BUDGET")
    core.budget_policy(max_context_tokens, max_output_tokens)
    if model_override is not None and model_override != foundry_config.chat_deployment:
        raise ValueError("APPROVED_REEXTRACTION_MODEL_CONFIG_DRIFT")
    if state_root.is_symlink() or any(p.is_symlink() for p in state_root.parents):
        raise ValueError("APPROVED_REEXTRACTION_UNSAFE_STATE")
    child = state_root.resolve()
    protected = [l1_state_root.resolve(), domain_path.resolve(), reuse_approved_run.resolve()]
    protected.extend(p.resolve() for p in (window_run_path, discovery_file) if p is not None)
    if any(child == p or child in p.parents or p in child.parents for p in protected):
        raise ValueError("APPROVED_REEXTRACTION_FRESH_STATE_REQUIRED")
    inputs, reader, materialized, cache_hash = core.prepare_approved_sources(
        source_path=source_path, l1_state_root=l1_state_root, domain_path=domain_path,
        window_run_path=window_run_path, discovery_file=discovery_file,
    )
    vocabulary = compile_closed_vocabulary(inputs.domain_contract)
    files = {entry.source_file_id: entry for entry in inputs.corpus_manifest.entries}
    units = {unit.source_unit_id: unit for unit in materialized.source_units}
    entries = {
        unit.source_unit_id: SimpleNamespace(
            relative_source_ref=files[unit.source_file_id].relative_source_ref,
            section_path=unit.locator.section_path,
        ) for unit in materialized.source_units
    }
    authority = _base_authority(
        inputs, materialized, cache_hash, foundry_config, max_calls, physical,
        max_output_tokens, few_shot, max_context_tokens, anchor_mode,
    )
    with ExitStack() as stack:
        seen = set()
        donor = _verify_donor(
            reuse_approved_run, expected=authority, inputs=inputs, materialized=materialized,
            vocabulary=vocabulary, entries=entries, stack=stack, seen=seen,
            quote_review=quote_review,
            review_ancestry=quote_review is not None and quote_review.version == review_module.ANCESTRY_VERSION,
            source_span_continuation=source_span_continuation,
        )
        if any(child == p or child in p.parents or p in child.parents for p in seen):
            raise ValueError("APPROVED_REEXTRACTION_FRESH_STATE_REQUIRED")
        resume_proof = None
        if resume and source_span_continuation:
            # Verify paid child responses even if L2 would reuse a complete
            # checkpoint without entering Service.complete. Release its read
            # lock before taking the writer lock, then recheck the snapshot.
            with ExitStack() as child_stack:
                resume_proof = _verify_donor(
                    child, expected=authority, inputs=inputs, materialized=materialized,
                    vocabulary=vocabulary, entries=entries, stack=child_stack, seen=set(),
                    source_span_continuation=True,
                )
        authority["continuation"] = _continuation(donor)
        if allow_budget_limited_partial:
            authority[PARTIAL_PROFILE_KEY] = _partial_profile()
        if quote_review is not None:
            authority["quote_review"] = quote_review.authority()
            authority["code_identity"][review_module.CODE_PATH] = _sha(Path(review_module.__file__))
            authority["quote_review_chain"] = review_module.chain_authority(donor.responses)
        authority["fingerprint"] = canonical_sha256(authority)
        roots = _roots(authority, inputs, materialized)
        if not roots:
            raise ValueError("APPROVED_REEXTRACTION_EMPTY_SCOPE")
        # Donor and ancestor read locks remain held before any child lock/path is created.
        return _run_child(
            state_root=child, dry_run=dry_run, resume=resume, authority=authority,
            donor=donor, roots=roots, inputs=inputs, vocabulary=vocabulary, entries=entries,
            units=units, reader=reader, l1_state_root=l1_state_root, domain_path=domain_path,
            foundry_config=foundry_config, client_factory=client_factory, run_l2=run_l2,
            max_calls=max_calls, physical=physical, max_output_tokens=max_output_tokens,
            quote_review=quote_review, resume_proof=resume_proof,
        )


@_locked_reuse
def _run_child(
    *, state_root, dry_run, resume, authority, donor, roots, inputs, vocabulary, entries,
    units, reader, l1_state_root, domain_path, foundry_config, client_factory, run_l2,
    max_calls, physical, max_output_tokens,
    quote_review=None, resume_proof=None,
):
    core._check_state(state_root, authority, resume=resume)
    if resume_proof is not None and canonical_sha256(core._inventory(state_root)) != resume_proof.binding["inventory_hash"]:
        _fail("RESUME_PROVIDER_SNAPSHOT_DRIFT")
    anchor_mode = authority.get("anchor_mode", core.OFFSET_MODE)
    partial_profile = _verified_partial_profile(authority)
    prompts = []

    def preflight(prompt):
        core.preflight_prompt(foundry_config, authority, prompt)
        prompts.append(prompt)

    coverage = resume_proof if partial_profile is not None and resume_proof is not None else donor
    missing = _missing(
        roots, coverage, vocabulary, entries,
        inputs.domain_contract.reasoning_policy.max_relations_per_work_unit,
        preflight=preflight, anchor_mode=anchor_mode,
    )
    accounting = core.summarize_preflight(foundry_config, authority, prompts)
    if not resume and partial_profile is None and (max_calls < missing or physical < missing):
        raise ValueError(f"APPROVED_REEXTRACTION_BUDGET_TOO_SMALL: at least {missing} additional logical and physical calls required")
    plan = {
        "operation": "enrich.approved-reextraction-continuation",
        "fingerprint": authority["fingerprint"], "state_root": str(state_root),
        "donor": donor.binding, "prior_spent": donor.spent,
        "planned_chunks": len(roots), "reused_responses": len(donor.responses),
        "remaining_work_units": missing, "max_calls": max_calls,
        "max_physical_calls": physical, "max_output_tokens": max_output_tokens,
        "input_budget": accounting,
        "additional_budget": {
            "logical_calls": max_calls, "physical_calls": physical,
            "reserved_output_tokens": physical * max_output_tokens,
        },
        "lineage_budget_ceiling": {
            "logical_calls": donor.spent["logical_calls"] + max_calls,
            "physical_calls": donor.spent["physical_calls"] + physical,
            "reserved_output_tokens": donor.spent["reserved_output_tokens"] + physical * max_output_tokens,
        },
        "domain_contract_hash": inputs.authority_hashes["domain_contract_hash"],
        "data_lineage": {
            "enabled_after": "L1_approval",
            "domain_contract_hash": inputs.authority_hashes["domain_contract_hash"],
            "storage": "L2_candidate_lifecycle_and_source_manifests",
        },
        "source_unit_manifest_hash": authority["source_unit_manifest_hash"],
        "model_version": foundry_config.chat_deployment,
        "source_mode": "approved_cached_source_units",
        "anchor_mode": anchor_mode,
        "original_source_reads": 0, "ocr_calls": 0, "reused_candidate_values": 0,
    }
    if partial_profile is not None:
        spent = {
            key: resume_proof.spent[key] - donor.spent[key] if resume_proof is not None else 0
            for key in ("logical_calls", "physical_calls")
        }
        available = {"logical_calls": max_calls - spent["logical_calls"],
                     "physical_calls": physical - spent["physical_calls"]}
        plan.update(
            budget_limited_partial=partial_profile,
            completion_expectation="budget_limited_partial",
            minimum_full_completion_calls={"logical_calls": missing, "physical_calls": missing},
            available_child_calls=available,
            full_completion_budget_sufficient=all(value >= missing for value in available.values()),
            completion_notice=(
                "Explicit resource-bounded execution, not a coverage or quality waiver. "
                "Budget exhaustion remains an error with no successful partial L2 receipt. "
                "No roots are excluded and no partial handoff is automatically sealed."
            ),
        )
    if quote_review is not None:
        plan["quote_review"] = quote_review.authority()
        plan["quote_review_chain"] = authority["quote_review_chain"]
    if dry_run:
        spent = {key: 0 for key in COUNTERS}
        if resume:
            spent = _metrics(json.loads((state_root / BUDGET).read_text()), authority)
        return {
            **plan, "status": "planned", "remote_calls": 0, "writes": 0,
            "current_child_spent": spent,
            "remaining_child_budget": {
                key: plan["additional_budget"][key] - spent[key] for key in COUNTERS
            },
            "lineage_spent": {key: donor.spent[key] + spent[key] for key in COUNTERS},
            "remaining_work_units_basis": (
                "approved units not satisfied by verified donor and current-child responses"
                if partial_profile is not None and resume_proof is not None
                else "approved units not satisfied by donor responses"
            ),
        }
    core.persist_response(state_root / AUTHORITY, authority, anchor_mode)
    try:
        if quote_review is not None:
            quote_review.persist(state_root)
            from .approved_quote_review import review_chain
            core.persist_response(
                state_root / "approved-quote-review-ancestry.json", review_chain(donor.responses), anchor_mode,
            )
        ledger = core._BudgetLedger(
            state_root, max_calls=max_calls, max_physical_calls=physical,
            max_output_tokens=max_output_tokens,
            max_context_tokens=authority["input_budget"]["max_context_tokens"],
        )
        # A fully reused child still needs an explicit zero-spend ledger.
        core._write_state(ledger.path, ledger.data)
        client = None

        class Service:
            def complete(self, *, prompt, work_unit):
                nonlocal client
                if not any(
                    root.source_unit_id == work_unit.source_unit_id
                    and root.source_text_hash == work_unit.source_text_hash
                    and root.slice_start <= work_unit.slice_start < work_unit.slice_end <= root.slice_end
                    for root in roots
                ) or work_unit.source_text != units[work_unit.source_unit_id].text:
                    raise ValueError("APPROVED_REEXTRACTION_OUTSIDE_SCOPE")
                expected_prompt = render_extraction_prompt(
                    vocabulary, source_unit_id=work_unit.source_unit_id,
                    source_text_hash=work_unit.source_text_hash, source_text=work_unit.anchored_text,
                    slice_start=work_unit.slice_start, slice_end=work_unit.slice_end,
                )
                if prompt != expected_prompt:
                    _fail("SEMANTICS_DRIFT")
                prompt = _prompt(work_unit, vocabulary, entries, anchor_mode)
                core.preflight_prompt(foundry_config, authority, prompt)
                request_hash = _request(authority, work_unit, prompt)
                path = state_root / "reextraction-responses" / f"{request_hash}.json"
                paid = donor.responses.get(core.response_hash(prompt, anchor_mode))

                def adapt(raw):
                    if paid is not None and paid.review_projection is not None:
                        from .approved_quote_review import adapt_inherited
                        return adapt_inherited(
                            raw, paid=paid, work_unit=work_unit,
                            state=state_root, request_hash=request_hash,
                        )
                    return core.adapt_response(
                        raw, work_unit=work_unit, state=state_root,
                        request_hash=request_hash, anchor_mode=anchor_mode,
                    )

                if path.exists():
                    cached = json.loads(path.read_text())
                    if cached["request_hash"] != request_hash or core.response_hash(cached["response"], anchor_mode) != cached["response_hash"]:
                        _fail("RESPONSE_DRIFT")
                    return adapt(cached["response"])
                if paid is not None:
                    core.persist_response(path, {
                        "request_hash": request_hash, "response": paid.response,
                        "response_hash": paid.response_hash, "imported_from": paid.provenance(),
                    }, anchor_mode)
                    return adapt(paid.response)
                row = ledger.reserve("logical_requests", request_hash)
                ledger.request_hash = request_hash
                try:
                    if client is None:
                        client = core._bounded_client(foundry_config, ledger, client_factory)
                    raw = client.complete_json(
                        system=authority["system_prompt"], user=prompt,
                        json_schema=authority["response_schema"],
                        max_completion_tokens=max_output_tokens, max_attempts=1,
                    )
                    core.persist_response(path, {
                        "request_hash": request_hash, "response": raw,
                        "response_hash": core.response_hash(raw, anchor_mode),
                    }, anchor_mode)
                except Exception as exc:
                    ledger.finish(row, "failed")
                    core._retain_json_failure(state_root, request_hash, ledger, exc)
                    raise
                ledger.finish(row, "succeeded")
                return adapt(raw)

        result = run_l2(
            reader=reader, service=Service(), state_root=state_root,
            l1_state_root=l1_state_root, domain_path=domain_path,
            prompt_version=core.VERSION, prompt_hash=authority["fingerprint"],
            model_version=foundry_config.chat_deployment,
            model_hash=canonical_sha256(authority["model_identity"]),
            extractor_name="approved-source-reextractor", extractor_version=core.VERSION,
            max_concurrent=1, service_batch_size=1,
        )
        metrics = ledger.metrics()
        summary = {
            **plan, "status": "succeeded", **metrics,
            "lineage_spent": {key: donor.spent[key] + metrics[key] for key in COUNTERS},
            "receipt_hash": result.receipt.receipt_hash,
            "output_manifest_hash": result.output_manifest.manifest_hash,
        }
        _persist_exact(state_root / "approved-reextraction-result.json", summary)
        return summary
    finally:
        core._write_state(state_root / INTEGRITY, core._inventory(state_root))
