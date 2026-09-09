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
    click.echo(canonical_json({
        "operation": "domain.design-schema",
        "schemas": {
            model.__name__: model.model_json_schema()
            for model in (
                core.DomainDesignDraft, core.DomainDesignEvaluation,
                core.DomainDesignSketch, core.DesignInputs,
                core.DesignSamples, core.DesignSeedReference,
                QuestionRoutingContext,
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


@click.command("design")
@click.option("--input", "source", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--intake", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--seed-domain", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Full safe YAML reference context, including generic sketches; never evidence or approval.")
@click.option("--description", help="Additional design context; does not replace the full seed.")
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
) -> None:
    """Design an additive, unapproved ontology from intent, seed and bounded samples.

    Saving a draft does not require complete question coverage. Reference examples
    are suggestions, never verified facts. Use evaluate-design for local structural
    diagnostics; only compile-design followed by domain approve can authorize L1.
    Discover artifact and model schemas with domain design-schema.
    """
    planning = dry_run or bool(_root_options(ctx).get("dry_run")) or not live
    if live and planning:
        raise click.UsageError("--dry-run cannot be combined with --live")
    if project_id is not None and not project_id.strip():
        raise click.UsageError("--project-id must not be empty")
    if not planning and out.exists():
        raise click.ClickException(f"Refusing to overwrite design draft: {out}")
    try:
        intake_raw = load_domain_intake(intake)
        core = _design_core()
        seed = core._read_seed(seed_domain)
        seed_hash = seed.content_sha256 if seed else None
        client, model_version = (None, "planned-model")
        model_hash = canonical_sha256({"model_version": model_version, "dry_run": True})
        if live:
            client, model_version = _build_client(ctx)
            model_hash = compute_model_hash(client, model_version)
        preflight = preflight_l1_inputs(
            source_path=source, intake_raw=intake_raw,
            project_id=project_id or f"project:{source.resolve().name}",
            run_id=f"run:{uuid.uuid4().hex}",
            model_version=model_version, model_hash=model_hash,
        )
        if planning:
            result = {
                "project_id": preflight.base_identity.project_id,
                "corpus_hash": preflight.corpus.corpus_hash,
                "corpus_entries": preflight.corpus.total_entry_count,
                "sampling_budget": _payload(preflight.budget),
                "intake_hash": preflight.intake.intake_hash,
                "seed": _payload(seed),
                "description": description,
                "model_calls": 0,
                "planned_model_calls": 1,
                "writes": 0,
                "samples_materialized": False,
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
