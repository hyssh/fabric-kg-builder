"""Thin CLI boundaries for unapproved design, local evaluation and L1 compilation."""

from __future__ import annotations

import uuid
import json
import os
import re
from pathlib import Path
from typing import Any

import click
import yaml
from azure.core.exceptions import ClientAuthenticationError
from openai import APIError

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.proposal import compute_model_hash, load_domain_intake
from fabric_kg_builder.domain.stage import finalize_l1_stage, preflight_l1_inputs

from .domain_assessment_cmd import _model_failure


def _design_core():
    from fabric_kg_builder.domain import design

    return design


def _discovery_core():
    from fabric_kg_builder.domain import discovery

    return discovery


def _validate_discovery_resume_cache(prior, cache_dir: Path) -> None:
    from fabric_kg_builder.domain.discovery import (
        discovery_observation_cache_path, discovery_summary_failure_cache_path,
        discovery_provider_diagnostic_cache_path, DiscoveryProviderDiagnostic,
    )

    records = []
    records.extend(
        (discovery_observation_cache_path(cache_dir, item), item)
        for item in prior.chunks if item.raw_response is not None
    )
    records.extend(
        (cache_dir / "summaries" / f"{item.request_hash}.json", item) for item in prior.summaries
    )
    records.extend(
        (discovery_summary_failure_cache_path(cache_dir, item), item)
        for item in prior.failed_summaries
    )
    for path, expected in records:
        try:
            matches = path.is_file() and type(expected).model_validate_json(
                path.read_text(encoding="utf-8")
            ) == expected
        except ValueError:
            matches = False
        if not matches:
            raise ValueError(f"DISCOVERY_RESUME_CACHE_DRIFT: {path}")
    for record in [*prior.chunks, *prior.failed_summaries]:
        path = discovery_provider_diagnostic_cache_path(cache_dir, record)
        if record.provider_diagnostic_hash is None:
            continue
        try:
            diagnostic = DiscoveryProviderDiagnostic.model_validate_json(
                path.read_text(encoding="utf-8")
            ) if path is not None and path.is_file() else None
            matches = (
                diagnostic is not None
                and diagnostic.artifact_hash == record.provider_diagnostic_hash
                and diagnostic.request_hash == record.request_hash
            )
        except ValueError:
            matches = False
        if not matches:
            raise ValueError(f"DISCOVERY_RESUME_DIAGNOSTIC_DRIFT: {path}")


def _discovery_report(run) -> dict:
    report = _discovery_core().discovery_grounding_report(run, include_ledger_accounting=True)
    report["unaccounted_candidate_count"] = report["unaccounted_raw_candidate_count"]
    report["candidate_ledger_complete"] = report["received_array_ledger_complete"]
    return report


def _discovery_plan_inventory(corpus, source: Path, layout_cache, layout_identity) -> dict:
    """Inspect exact cached PDF coverage without text extraction or disk snapshots."""
    import hashlib
    from fabric_kg_builder.sources.docintel_cache import load_cached_layout, validate_extractor_identity
    from .layout_cache_cmd import _page_count

    if layout_identity is not None:
        validate_extractor_identity(layout_identity)
    inventory = []
    for entry in corpus.entries:
        row = {
            "source_file_id": entry.source_file_id, "source": entry.relative_source_ref,
            "disposition": entry.disposition, "adapter_status": entry.adapter_status,
            "media_type": entry.media_type, "byte_count": entry.byte_count,
            "pdf_pages": None, "cached_pdf_pages": None, "exact_page_coverage": False,
        }
        if entry.media_type == "application/pdf" and entry.disposition == "eligible" and layout_cache is not None:
            path = source if source.is_file() else source / entry.relative_source_ref
            if source.is_dir() and not path.resolve().is_relative_to(source.resolve()):
                raise ValueError("DISCOVERY_PLAN_SOURCE_ESCAPE")
            data = path.read_bytes()
            if len(data) != entry.byte_count or hashlib.sha256(data).hexdigest() != entry.original_byte_hash:
                raise ValueError(f"DISCOVERY_PLAN_SOURCE_DRIFT: {entry.source_file_id}")
            count = _page_count(data, entry.media_type)
            del data
            row["pdf_pages"] = count
            cached = load_cached_layout(
                layout_cache, input_sha256=entry.original_byte_hash, extractor_identity=layout_identity,
            )
            row["layout_cache_status"] = "missing" if cached is None else "exact"
            if cached is not None:
                numbers = [
                    page.get("pageNumber", page.get("page_number"))
                    for page in cached.analyze_result.get("pages", ())
                ]
                expected = set(range(1, count + 1))
                row.update({
                    "layout_cache_key": cached.cache_key,
                    "cached_pdf_pages": len(numbers),
                    "exact_page_coverage": len(numbers) == count and set(numbers) == expected,
                })
                if not row["exact_page_coverage"]:
                    row["layout_cache_status"] = "incomplete_pages"
        inventory.append(row)
    pdfs = [row for row in inventory if row["media_type"] == "application/pdf"]
    return {
        "source_inventory": inventory,
        "page_coverage": {
            "scope": "exact_cached_pdf_pages_not_extraction_or_semantic_recall",
            "pdf_files": len(pdfs),
            "source_pdf_pages": sum(row["pdf_pages"] for row in pdfs) if all(
                row["pdf_pages"] is not None for row in pdfs
            ) else None,
            "verified_cached_pdf_pages": sum(row["pdf_pages"] for row in pdfs if row["exact_page_coverage"]),
            "complete": bool(pdfs) and all(row["exact_page_coverage"] for row in pdfs),
            "pending_source_file_ids": [row["source_file_id"] for row in pdfs if not row["exact_page_coverage"]],
        },
    }


def _root_options(ctx: click.Context) -> dict:
    options = ctx.find_root().obj
    return options if isinstance(options, dict) else {}


def _build_client(ctx: click.Context):
    from .domain_cmd import _build_foundry_client, _resolve_model_version

    options = _root_options(ctx)
    client = options.get("_design_client")
    if client is None:
        client = options.get("_foundry_client")
    if client is None:
        client = _build_foundry_client(options)
    return client, _resolve_model_version(client, options)


def _payload(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _trace_writer(directory: Path, run_id: str):
    def write(event: dict) -> None:
        kind, index, request_hash = (
            event.get("event"), event.get("logical_call_index"), event.get("request_hash"),
        )
        if (
            kind not in {"request_started", "request_failed", "response_completed"}
            or not isinstance(index, int) or index < 1
            or not isinstance(request_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
        ):
            raise ValueError("Invalid design trace event identity")
        data = json.dumps(event, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
        target = directory / run_id.replace(":", "-", 1)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = target / f"{index:03d}-{kind}-{request_hash[:12]}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    return write


@click.command("design-schema")
def domain_design_schema_cmd() -> None:
    """Print design/context/evaluation and model-response schemas; no config or calls."""
    core = _design_core()
    from fabric_kg_builder.domain.question_routing import QuestionRoutingContext
    discovery = _discovery_core()
    click.echo(canonical_json({
        "operation": "domain.design-schema",
        "schemas": {
            model.__name__: model.model_json_schema()
            for model in (
                core.DomainDesignDraft, core.DomainDesignEvaluation,
                core.DomainDesignSketch, core.DesignInputs,
                core.DesignSamples, core.DesignSeedReference,
                QuestionRoutingContext,
                discovery.DiscoveryBudget, discovery.PreparedCorpus, discovery.DiscoveryRun,
            )
        },
    }))


@click.command("question-context")
@click.option("--file", "source_file",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Validated design-draft JSON or Schema-2 domain YAML/JSON.")
@click.option("--l4-run", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Alternatively, export from a sealed L4 serving run without changing it.")
@click.option("--l3-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Required with --l4-run to resolve its exact upstream manifest.")
def domain_question_context_cmd(
    source_file: Path | None, l4_run: Path | None, l3_root: Path | None,
) -> None:
    """Print source-hash-bound routing/background context; no model, SQL or writes.

    Declared Lakehouse SQL requirements are not physical bindings or executable
    readiness. Unrouted question IDs and unresolved requirements remain explicit.
    """
    if (source_file is None) == (l4_run is None):
        raise click.UsageError("Choose exactly one of --file or --l4-run")
    if (l4_run is None) != (l3_root is None):
        raise click.UsageError("--l4-run and --l3-root must be supplied together")
    from fabric_kg_builder.domain.question_routing import (
        question_routing_context, routed_question_copies,
    )
    from fabric_kg_builder.domain.models import DomainContractV2
    from fabric_kg_builder.domain.service import compute_contract_hash
    from fabric_kg_builder.serving.structured_publication import L5aPublicationError

    try:
        if l4_run is not None:
            from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
            from fabric_kg_builder.serving.structured_publication import export_serving_question_context

            source = SealedL4ServingSource.from_run(
                l4_run, input_manifest_search_roots=(l3_root,),
            )
            result = export_serving_question_context(source)
        else:
            from .domain_io import load_cli_domain_contract

            raw = yaml.safe_load(source_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("artifact_kind") == "domain.design_draft":
                draft = _design_core().load_domain_design(source_file)
                questions = routed_question_copies(
                    draft.inputs.intake.competency_questions, draft.sketch.question_routes,
                )
                context = question_routing_context({"competency_questions": questions})
                values = {
                    "source_kind": "design_draft", "source_hash": draft.draft_hash,
                    "domain_contract_hash": None, "approval_status": "unapproved_design",
                    "business_context": draft.inputs.intake.model_dump(mode="json"),
                    "problem_context": {"additional_design_description": draft.inputs.description},
                }
                acceptance = getattr(draft, "discovery_acceptance", None)
                if acceptance is not None:
                    acceptance = acceptance.binding
            else:
                contract = load_cli_domain_contract(source_file)
                if not isinstance(contract, DomainContractV2):
                    raise ValueError("question-context requires a design draft or Schema-2 domain")
                questions = contract.competency_questions
                context = question_routing_context(contract)
                values = {
                    "source_kind": "domain_contract", "source_hash": compute_contract_hash(contract),
                    "domain_contract_hash": compute_contract_hash(contract),
                    "approval_status": contract.approval.status,
                    "business_context": contract.business.model_dump(mode="json"),
                    "problem_context": contract.problem.model_dump(mode="json"),
                }
                acceptance = getattr(contract, "discovery_acceptance", None)
            if acceptance is not None:
                from fabric_kg_builder.serving.structured_publication import discovery_coverage_context

                values["discovery_acceptance"] = acceptance.model_dump(mode="json")
                values["discovery_coverage"] = discovery_coverage_context(acceptance)
            routed = {item["question_id"] for item in context["questions"]} if context else set()
            values.update({
                "artifact_kind": "domain.question_context", "artifact_version": "1.0.0",
                "question_routing_context": context,
                "unrouted_question_ids": sorted(item.id for item in questions if item.id not in routed),
                "execution_verified": False,
            })
            result = {**values, "export_hash": canonical_sha256(values)}
    except (OSError, ValueError, TypeError, yaml.YAMLError, L5aPublicationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json(result))


@click.command("discover")
@click.option("--input", "source", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--intake", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Optional existing business intake; discovery does not require questions or an ontology.")
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--cache-dir", "--state-dir", "cache_dir", default=".fkg/discovery",
              show_default=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--resume", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Resume a prior immutable discovery run into a new --out path.")
@click.option("--retry-missing", is_flag=True,
              help="Raise output capacity only for prior chunks with no received raw response; requires --resume.")
@click.option("--retry-max-completion-tokens", default=16_384, show_default=True,
              type=click.IntRange(256, 32_768),
              help="Per-missing-chunk output ceiling; requires --retry-missing. Global/summary ceilings stay unchanged.")
@click.option("--project-id")
@click.option("--live", is_flag=True, help="Permit bounded full-corpus parsing and model calls.")
@click.option("--dry-run", is_flag=True, help="Read-only inventory/cache coverage; no text extraction, calls or writes.")
@click.option("--max-calls", default=256, show_default=True, type=click.IntRange(0, 100_000))
@click.option("--concurrency", default=4, show_default=True, type=click.IntRange(1, 16))
@click.option("--max-chunk-chars", default=12_000, show_default=True, type=click.IntRange(128, 64_000))
@click.option("--max-tokens", default=2_000_000, show_default=True, type=click.IntRange(0))
@click.option("--ocr-cache", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--ocr-identity", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def domain_discover_cmd(
    ctx, source, intake, out, cache_dir, resume, retry_missing, retry_max_completion_tokens,
    project_id, live, dry_run,
    max_calls, concurrency, max_chunk_chars, max_tokens, ocr_cache, ocr_identity,
) -> None:
    """Visit all corpus chunks before design; observations and summaries are unapproved.

    Planning inventories files and supplied exact cached PDF page coverage.
    Chunk counts remain unknown until explicit preparation; budget-limited runs are partial.
    Resume reuses immutable responses; choose a fresh output path, not overwrite.
    """
    planning = dry_run or bool(_root_options(ctx).get("dry_run")) or not live
    if live and planning:
        raise click.UsageError("--dry-run cannot be combined with --live")
    if retry_missing and resume is None:
        raise click.UsageError("--retry-missing requires --resume")
    if not retry_missing and ctx.get_parameter_source("retry_max_completion_tokens") not in (
        None, click.core.ParameterSource.DEFAULT,
    ):
        raise click.UsageError("--retry-max-completion-tokens requires --retry-missing")
    if (ocr_cache is None) != (ocr_identity is None):
        raise click.UsageError("--ocr-cache and --ocr-identity must be supplied together")
    if not planning and out.exists():
        raise click.ClickException("Discovery output is immutable; choose a new --out path")
    if out.resolve().is_relative_to(source.resolve()) or cache_dir.resolve().is_relative_to(source.resolve()):
        raise click.UsageError("Discovery output/cache must be outside the source corpus")
    try:
        core = _discovery_core()
        prior = core.load_discovery(resume) if resume else None
        retry_policy = core.DiscoveryMissingRetry(
            max_completion_tokens=retry_max_completion_tokens,
        ) if retry_missing else None
        retry_plan = core.plan_missing_discovery_retry(prior, retry_policy) if retry_policy else None
        intake_raw = load_domain_intake(intake) if intake else (prior.business_context if prior else None)
        selected_project = project_id or (
            prior.prepared.base_identity.project_id if prior else f"project:{source.resolve().name}"
        )
        selected_run = prior.prepared.base_identity.run_id if prior else f"run:{uuid.uuid4().hex}"
        if intake is not None:
            preflight = preflight_l1_inputs(
                source_path=source, intake_raw=intake_raw, project_id=selected_project,
                run_id=selected_run,
                model_version="planned-model", model_hash=canonical_sha256({"discovery_plan": True}),
            )
        else:
            preflight = core.preflight_discovery_inputs(
                source_path=source, project_id=selected_project, run_id=selected_run,
            )
        budget = core.DiscoveryBudget(
            max_calls=max_calls, max_concurrency=concurrency, max_chunk_chars=max_chunk_chars,
            max_tokens=max_tokens,
            **({"max_completion_tokens": prior.budget.max_completion_tokens} if prior else {}),
        )
        if prior is not None:
            _validate_discovery_resume_cache(prior, cache_dir)
            if (
                prior.prepared.base_identity.project_id != selected_project
                or prior.prepared.corpus.corpus_hash != preflight.corpus.corpus_hash
                or prior.business_context != intake_raw
                or prior.budget.max_chunk_chars != budget.max_chunk_chars
            ):
                raise ValueError("DISCOVERY_RESUME_DRIFT: corpus, project, intake or chunk policy changed")
        binding = prior.prepared.reader_binding if prior else {}
        layout_identity = json.loads(ocr_identity.read_text(encoding="utf-8")) if ocr_identity else binding.get("layout_identity")
        layout_cache = ocr_cache or (Path(binding["layout_cache"]) if binding.get("layout_cache") else None)
        if planning:
            result = {
                "operation": "domain.discover", "status": "planned",
                "corpus_hash": preflight.corpus.corpus_hash,
                "corpus_entries": preflight.corpus.total_entry_count,
                "budget": budget.model_dump(mode="json"), "model_calls": 0, "writes": 0,
                "source_units_materialized": False,
                "chunk_count": len(prior.chunks) if prior else None,
                "resume_hash": prior.run_hash if prior else None,
                "full_corpus_design_ready": False,
                **_discovery_plan_inventory(preflight.corpus, source, layout_cache, layout_identity),
            }
            if prior is not None:
                result["prior_grounding"] = _discovery_report(prior)
        else:
            from fabric_kg_builder.sources.preparation import indexed_corpus_reader

            reader = indexed_corpus_reader(
                preflight.corpus, source, project_id=selected_project,
                layout_cache=layout_cache, layout_identity=layout_identity,
            )
            if prior:
                prepared = core.resume_discovery_preparation(
                    prior, source_path=source, reader=reader, cache_dir=cache_dir,
                )
            else:
                prepared = core.prepare_discovery_corpus(preflight, reader=reader, cache_dir=cache_dir)
            client, model_version = _build_client(ctx)
            model_hash = compute_model_hash(client, model_version)
            if prior and (prior.model_version != model_version or prior.model_hash != model_hash):
                raise ValueError("DISCOVERY_RESUME_MODEL_DRIFT: model identity changed")
            run = core.run_discovery(
                prepared, client=client, model_version=model_version, model_hash=model_hash,
                cache_dir=cache_dir, budget=budget, business_context=intake_raw,
                prior=prior, retry_missing=retry_policy,
            )
            core.save_discovery(out, run)
            result = {
                "operation": "domain.discover", "status": run.status, "artifact": str(out),
                "discovery_hash": run.run_hash, "prepared_corpus_hash": prepared.prepared_hash,
                "corpus_entries": prepared.corpus.total_entry_count, "chunk_count": len(run.chunks),
                "model_calls": run.model_call_count, "reused_responses": run.reused_response_count,
                "pending_chunks": sum(item.response is None for item in run.chunks),
                "prepared_sources": sum(item.status in {"processed", "no_candidates"} for item in prepared.sources),
                "pending_sources": sum(item.status not in {"processed", "no_candidates"} for item in prepared.sources),
                "full_corpus_design_ready": run.full_corpus_design_ready,
                "retained_failed_summary_attempts": len(run.failed_summaries),
                "issues": run.issues, "authority": run.authority,
                **_discovery_report(run),
            }
        if retry_plan is not None:
            result["missing_retry_plan"] = retry_plan
    except (APIError, ClientAuthenticationError) as exc:
        raise _model_failure(exc) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json(result))


@click.command("accept-discovery-partial")
@click.option("--file", "discovery_file", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--min-chunk-coverage", "--min-coverage", default="0.99", show_default=True,
              type=str,
              help="Exact threshold from 0.99 to 1: typed-response chunks / ALL planned chunks, not candidate counts.")
@click.option("--actor", required=True, help="Reviewer accepting the explicitly reported coverage gaps.")
@click.option("--rationale", required=True, help="Why this partial coverage is sufficient for the reviewed prototype.")
@click.option("--accept", is_flag=True, help="Write the reviewed local acceptance artifact; otherwise plan only.")
@click.option("--dry-run", is_flag=True, help="Read-only review; no model calls or artifact writes.")
@click.pass_context
def domain_accept_discovery_partial_cmd(
    ctx, discovery_file, out, min_chunk_coverage, actor, rationale, accept, dry_run,
) -> None:
    """Review partial discovery without marking it complete or approving an ontology."""
    planning = dry_run or bool(_root_options(ctx).get("dry_run")) or not accept
    if accept and planning:
        raise click.UsageError("--dry-run cannot be combined with --accept")
    try:
        from fabric_kg_builder.domain.discovery_acceptance import (
            accept_discovery_partial, save_discovery_acceptance,
        )

        run = _discovery_core().load_discovery(discovery_file)
        acceptance = accept_discovery_partial(
            run, min_chunk_coverage=min_chunk_coverage, actor=actor, rationale=rationale,
        )
        if not planning:
            save_discovery_acceptance(out, acceptance)
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "operation": "domain.accept-discovery-partial",
        "status": "planned" if planning else "partial_accepted",
        "artifact": None if planning else str(out),
        "model_calls": 0, "writes": 0 if planning else 1,
        "discovery_status": run.status,
        "full_corpus_design_ready": run.full_corpus_design_ready,
        "ontology_approved": False, "evidence_approved": False,
        "result": _payload(acceptance),
    }))


@click.command("design")
@click.option("--input", "source", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--intake", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--seed-domain", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Full safe YAML reference context, including generic sketches; never evidence or approval.")
@click.option("--window-state", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Completed, discovery-bound working schema as unapproved design reference; never evidence.")
@click.option("--window-run", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Completed integrated raw-text run, bound to its exact original intake and prepared sources.")
@click.option("--window-run-acceptance", "window_run_acceptance_file",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Exact explicit >=99% processing waiver for a partial --window-run; not semantic approval.")
@click.option("--description", help="Additional design context; does not replace the full seed.")
@click.option("--discovery", "discovery_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Immutable discovery; complete by default, or explicitly reviewed partial coverage.")
@click.option("--discovery-acceptance", "--coverage-acceptance", "discovery_acceptance_file",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Exact reviewed partial-coverage acceptance; requires --discovery and never approves the draft.")
@click.option("--discovery-node", "discovery_nodes", multiple=True,
              help="Additional exact summary node ID, or acceptance context_nodes ID for partial discovery.")
@click.option("--sample-only", is_flag=True,
              help="Explicit legacy bounded-sample compatibility, NOT full-corpus discovery.")
@click.option("--project-id", help="Stable project identity (default: project:<source name>).")
@click.option("--live", is_flag=True, help="Explicitly permit bounded model generation.")
@click.option("--dry-run", is_flag=True, help="Plan only, without calls or writes (default).")
@click.option("--max-calls", default=2, show_default=True, type=click.IntRange(1, 8),
              help="Logical call ceiling; this design implementation uses one call, no repair loop.")
@click.option("--proposal-trace-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Opt-in private, create-only request/response JSON traces; not a replay cache.")
@click.pass_context
def domain_design_cmd(
    ctx: click.Context, source: Path, intake: Path, out: Path,
    seed_domain: Path | None, description: str | None, project_id: str | None,
    live: bool, dry_run: bool, max_calls: int,
    proposal_trace_dir: Path | None,
    discovery_file: Path | None, sample_only: bool, discovery_nodes: tuple[str, ...],
    discovery_acceptance_file: Path | None,
    window_state: Path | None = None,
    window_run: Path | None = None,
    window_run_acceptance_file: Path | None = None,
) -> None:
    """Design an unapproved ontology from intent, seed and reviewed corpus discovery.

    Saving a draft does not require complete question coverage. Reference examples
    are suggestions, never verified facts. Use evaluate-design for local structural
    diagnostics; only compile-design followed by domain approve can authorize L1.
    Discover artifact and model schemas with domain design-schema.
    """
    planning = dry_run or bool(_root_options(ctx).get("dry_run")) or not live
    if sum((discovery_file is not None, window_run is not None, sample_only)) != 1:
        raise click.UsageError(
            "Choose exactly one of --discovery, --window-run, or explicit --sample-only"
        )
    if discovery_nodes and discovery_file is None:
        raise click.UsageError("--discovery-node requires --discovery")
    if window_run_acceptance_file is not None and window_run is None:
        raise click.UsageError("--window-run-acceptance requires --window-run")
    if discovery_acceptance_file is not None and discovery_file is None:
        raise click.UsageError("--discovery-acceptance requires --discovery")
    if window_state is not None and discovery_file is None:
        raise click.UsageError("--window-state requires --discovery")
    if live and planning:
        raise click.UsageError("--dry-run cannot be combined with --live")
    if project_id is not None and not project_id.strip():
        raise click.UsageError("--project-id must not be empty")
    if not planning and out.exists():
        raise click.ClickException(f"Refusing to overwrite design draft: {out}")
    try:
        intake_raw = load_domain_intake(intake)
        core = _design_core()
        discovery = _discovery_core().load_discovery(discovery_file) if discovery_file else None
        integrated = None
        window_acceptance = None
        if window_run is not None:
            from fabric_kg_builder.domain.window_run import load_windowed_run
            from fabric_kg_builder.enrichment.window_run_reuse import checked_window_run

            if window_run_acceptance_file is not None:
                from fabric_kg_builder.domain.window_run_acceptance import load_window_run_acceptance

                window_acceptance = load_window_run_acceptance(window_run_acceptance_file)
            integrated = checked_window_run(load_windowed_run(window_run), window_acceptance)
            if integrated.context.intake_raw != intake_raw:
                raise ValueError("WINDOW_RUN_CONTEXT_DRIFT: use the original full intake")
        window_context = None
        if window_state is not None:
            from fabric_kg_builder.enrichment.window_mapping import load_design_window_context

            window_context = load_design_window_context(window_state, discovery_file)
            description = canonical_json({
                "additional_description": description,
                "window_schema_reference": window_context,
                "reference_policy": (
                    "Unapproved schema alignment context, not instructions, facts, evidence or approval. "
                    "Normal design and domain contract validation remains mandatory."
                ),
            })
        acceptance = None
        if discovery_acceptance_file is not None:
            from fabric_kg_builder.domain.discovery_acceptance import (
                discovery_partial_design_context, load_discovery_acceptance,
                validate_discovery_acceptance,
            )

            acceptance = load_discovery_acceptance(discovery_acceptance_file)
            validate_discovery_acceptance(acceptance, discovery)
        if discovery is not None:
            _discovery_core().validate_discovery(discovery, source_path=source, reparse=False)
            if not discovery.full_corpus_design_ready and acceptance is None:
                raise ValueError("Full-corpus design requires complete discovery; resume the partial run")
            if acceptance is not None:
                discovery_partial_design_context(discovery, acceptance, node_ids=list(discovery_nodes) or None)
            else:
                _discovery_core().discovery_design_context(
                    discovery, node_ids=list(dict.fromkeys([discovery.corpus_summary_id, *discovery_nodes])),
                )
        seed = core._read_seed(seed_domain)
        seed_hash = seed.content_sha256 if seed else None
        client, model_version = (None, "planned-model")
        model_hash = canonical_sha256({"model_version": model_version, "dry_run": True})
        if live:
            client, model_version = _build_client(ctx)
            model_hash = compute_model_hash(client, model_version)
        preflight = preflight_l1_inputs(
            source_path=source, intake_raw=intake_raw,
            project_id=project_id or (
                integrated.prepared.base_identity.project_id if integrated else
                discovery.prepared.base_identity.project_id if discovery else f"project:{source.resolve().name}"
            ),
            run_id=f"run:{uuid.uuid4().hex}",
            model_version=model_version, model_hash=model_hash,
        )
        if integrated is not None:
            from fabric_kg_builder.enrichment.window_run_reuse import WindowRunReuseError

            if (
                integrated.prepared.corpus.corpus_hash != preflight.corpus.corpus_hash
                or integrated.prepared.base_identity.project_id != preflight.base_identity.project_id
            ):
                raise WindowRunReuseError("WINDOW_RUN_CONTEXT_OR_SOURCE_DRIFT")
        if planning:
            result = {
                "project_id": preflight.base_identity.project_id,
                "corpus_hash": preflight.corpus.corpus_hash,
                "corpus_entries": preflight.corpus.total_entry_count,
                "sampling_budget": _payload(preflight.budget),
                "intake_hash": preflight.intake.intake_hash,
                "seed": _payload(seed),
                "description": description,
                **({"window_schema_reference": window_context} if window_context is not None else {}),
                "model_calls": 0,
                "planned_model_calls": 1,
                "writes": 0,
                "samples_materialized": False,
                "design_mode": "reviewed_partial_window_run" if window_acceptance is not None else "integrated_window_run" if integrated is not None else "sample_only" if sample_only else (
                    "reviewed_partial_discovery" if acceptance is not None else "full_corpus_discovery"
                ),
                **({
                    "discovery_acceptance": _payload(acceptance),
                    "discovery_status": discovery.status,
                    "full_corpus_design_ready": discovery.full_corpus_design_ready,
                    **_discovery_report(discovery),
                } if acceptance is not None else {}),
                **({
                    "discovery_hash": discovery.run_hash,
                    "prepared_corpus_hash": discovery.prepared.prepared_hash,
                    "discovery_chunks": len(discovery.chunks),
                    "discovery_node_ids": list(discovery_nodes),
                } if discovery else {}),
                **({"window_run_hash": integrated.artifact_hash} if integrated is not None else {}),
                **({"window_run_acceptance": _payload(window_acceptance)} if window_acceptance is not None else {}),
            }
            from fabric_kg_builder.domain.question_routing import question_routing_context

            routing = question_routing_context(preflight.intake)
            if routing is not None:
                routed_ids = {item["question_id"] for item in routing["questions"]}
                result.update({
                    "question_routing_context": routing,
                    "unrouted_question_ids": sorted(
                        item.id for item in preflight.intake.competency_questions
                        if item.id not in routed_ids
                    ),
                    "execution_verified": False,
                })
        else:
            result = core.generate_domain_design(
                preflight, client=client, seed_path=seed_domain,
                **({"window_run": integrated} if integrated is not None else
                   {"discovery": discovery} if discovery is not None else {"sample_only": True}),
                **({"window_run_acceptance": window_acceptance} if window_acceptance is not None else {}),
                **({"discovery_acceptance": acceptance} if acceptance is not None else {}),
                **({"discovery_node_ids": list(discovery_nodes)} if discovery_nodes else {}),
                **({"description": description} if description is not None else {}),
                **({
                    "proposal_trace_callback": _trace_writer(proposal_trace_dir, preflight.run_id),
                } if proposal_trace_dir is not None else {}),
            )
            if not isinstance(result, core.DomainDesignDraft):
                raise ValueError("Design generation did not return an unapproved DesignDraft")
            seed_hash = result.seed.content_sha256 if result.seed else None
            core.save_design_artifact(out, result)
    except (APIError, ClientAuthenticationError) as exc:
        failure = _model_failure(exc)
        if failure.message.startswith("MODEL_REQUEST_FAILED:"):
            failure = click.ClickException(
                failure.message.split(". ", 1)[0]
                + ". No design draft was saved; no automatic regeneration is attempted."
            )
        raise failure from exc
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "operation": "domain.design",
        "status": "planned" if planning else "unapproved_design",
        "artifact": None if planning else str(out),
        "max_calls": max_calls,
        "seed_domain_sha256": seed_hash,
        "result": _payload(result),
    }))


@click.command("evaluate-design")
@click.option("--file", "draft_file", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.pass_context
def domain_evaluate_design_cmd(
    ctx: click.Context, draft_file: Path, out: Path,
) -> None:
    """Save local structural diagnostics, NOT a live-answerability evaluation.

    Unsupported questions remain explicit. No model calls, approval or extraction.
    Global --dry-run prints the evaluation without writing it.
    """
    planning = bool(_root_options(ctx).get("dry_run"))
    if not planning and out.exists():
        raise click.ClickException(f"Refusing to overwrite design evaluation: {out}")
    try:
        core = _design_core()
        draft = core.load_domain_design(draft_file)
        evaluation = core.evaluate_domain_design(draft)
        if not planning:
            core.save_design_artifact(out, evaluation)
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "operation": "domain.evaluate-design",
        "evaluation_kind": "local_structural_not_live_answerability",
        "artifact": None if planning else str(out),
        "result": _payload(evaluation),
    }))


@click.command("compile-design")
@click.option("--file", "draft_file", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--input", "source", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--evaluation", "evaluation_file", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--accept-evaluation-hash", required=True,
              help="Acknowledge this exact evaluation; cannot bypass gaps or approve L1.")
@click.option("--out-state", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--out-domain", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.pass_context
def domain_compile_design_cmd(
    ctx: click.Context, draft_file: Path, source: Path, evaluation_file: Path,
    accept_evaluation_hash: str, out_state: Path, out_domain: Path,
) -> None:
    """Compile exact reviewed design into the existing UNAPPROVED Schema-2 L1 path.

    Local and model-free. Rechecks current source identity and refuses semantic
    loss, stale evaluation or compiler incompatibility. Existing domain approve
    is still required. Global --dry-run performs preflight without any writes.
    """
    planning = bool(_root_options(ctx).get("dry_run"))
    if not planning and (out_domain.exists() or out_state.exists()):
        raise click.ClickException("Compilation requires new --out-state and --out-domain paths")
    if out_domain.resolve() == out_state.resolve():
        raise click.UsageError("--out-state and --out-domain must be different paths")
    try:
        core = _design_core()
        draft = core.load_domain_design(draft_file)
        evaluation = core.load_design_evaluation(evaluation_file)
        if accept_evaluation_hash != evaluation.evaluation_hash:
            raise ValueError("DESIGN_EVALUATION_REVIEW_MISMATCH")
        preflight = core.design_preflight(draft, source)
        prepared = core.compile_domain_design(draft, evaluation, preflight=preflight)
        result = {
            "project_id": preflight.base_identity.project_id,
            "run_id": preflight.run_id,
            "proposal_hash": prepared.proposal.proposal_hash,
            "model_calls": prepared.model_call_count,
            "state_dir": str(out_state),
            "domain_file": str(out_domain),
            "approval_required": True,
            "summary": prepared.summary,
        }
        if not planning:
            persisted = finalize_l1_stage(
                prepared, decision=None, actor=None,
                state_root=out_state, domain_path=out_domain,
            )
            result["receipt_status"] = persisted.status
        else:
            result["writes"] = 0
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "operation": "domain.compile-design",
        "dry_run": planning,
        "approval_granted": False,
        "result": _payload(result),
    }))
