"""Explicit working-schema alignment and human-reviewed replay boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256


def _core():
    from fabric_kg_builder.domain import window_schema

    return window_schema


def _options(ctx):
    value = ctx.find_root().obj
    return value if isinstance(value, dict) else {}


def _emit(value):
    if isinstance(value, list):
        value = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value]
    elif hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    click.echo(canonical_json(value))


@click.command("window-bootstrap")
@click.option("--discovery", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out-state", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--source-file-id", help="Select an eligible source; default: first prepared corpus entry.")
@click.option("--intake", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Optional original business intake for focused provisional concept inference; repeat on resume.")
@click.option("--live", is_flag=True, help="Write provisional schema 1 using one bounded model request.")
@click.option("--resume", is_flag=True, help="Hash-check durable results without another model call.")
@click.pass_context
def domain_window_bootstrap_cmd(ctx, discovery, out_state, source_file_id, intake, live, resume):
    """Infer an initial schema from ALL cached slices of ONE document.

    Read-only plan by default. No OCR, prior candidates, seeds or approval.
    Limit: 512000 request characters, 16384 completion tokens, one attempt.
    """
    from fabric_kg_builder.domain.schema_bootstrap import bootstrap
    from fabric_kg_builder.domain.proposal import compute_model_hash
    from .domain_design_cmd import _build_client

    if live and _options(ctx).get("dry_run"):
        raise click.UsageError("--live conflicts with global --dry-run")

    def client_factory():
        client, version = _build_client(ctx)
        return client, version, compute_model_hash(client, version)

    try:
        result = bootstrap(discovery=discovery, out_state=out_state,
                           source_file_id=source_file_id, intake=intake, live=live, resume=resume,
                           client_factory=client_factory)
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(result)


def _run_alignment(ctx, *, discovery, seed_domain, out_state, planning, resume,
                   window_size, concurrency, max_calls, proposal_mode):
    from fabric_kg_builder.domain.discovery import load_discovery
    from fabric_kg_builder.domain.models import DomainContractV2
    from fabric_kg_builder.domain.service import load_domain_contract
    from fabric_kg_builder.domain.proposal import compute_model_hash
    from .domain_design_cmd import _build_client

    core = _core()
    run = load_discovery(discovery)
    seed = load_domain_contract(seed_domain)
    if not isinstance(seed, DomainContractV2):
        raise ValueError("--seed-domain requires a Schema-2 domain (reference, not approval)")
    source = Path(run.prepared.source_path).resolve()
    destination = out_state.resolve()
    if (destination == source or destination.is_relative_to(source)
            or source.is_relative_to(destination)
            or discovery.resolve().is_relative_to(destination)
            or seed_domain.resolve().is_relative_to(destination)):
        raise ValueError("Window output must not overlap source, discovery or seed authority")
    if resume and ctx.get_parameter_source("proposal_mode") == click.core.ParameterSource.DEFAULT:
        manifest = json.loads((out_state / "manifest.json").read_text(encoding="utf-8"))
        proposal_mode = manifest.get("config", {}).get("proposal_mode", "model")
    config = core.WindowConfig(
        window_size=window_size, max_concurrency=concurrency, proposal_mode=proposal_mode,
    )
    windows = core.plan_windows(run, window_size)
    if resume:
        if not (out_state / "manifest.json").is_file():
            raise ValueError("Window resume requires an existing run manifest")
        if planning:
            prior = core.load_window_run(out_state)
            if (prior.discovery_hash != run.run_hash
                    or prior.seed_hash != core.seed_snapshot(seed).seed_hash
                    or prior.config_hash != canonical_sha256(config)):
                raise ValueError("Window resume discovery/seed/config binding changed")
    elif out_state.exists() and any(out_state.iterdir()):
        raise ValueError("Window state already exists; use --resume or a fresh --out-state")
    if planning:
        return {
            "operation": "domain.window-align", "status": "planned",
            "discovery_hash": run.run_hash, "seed_hash": core.seed_snapshot(seed).seed_hash,
            "config": config.model_dump(mode="json"), "total_chunks": sum(map(len, windows)),
            "windows": windows, "model_calls": 0, "writes": 0,
            "max_calls": max_calls, "authority": "working_only",
        }
    client, version, model_hash = None, "none", None
    if max_calls and proposal_mode == "model":
        client, version = _build_client(ctx)
        model_hash = compute_model_hash(client, version)
    elif resume:
        manifest = json.loads((out_state / "manifest.json").read_text(encoding="utf-8"))
        version, model_hash = manifest.get("model_version", "none"), manifest.get("model_hash")
    result = core.run_windows(
        discovery=run, output_dir=out_state, seed=seed, config=config,
        budget=core.WindowBudget(max_calls=max_calls, max_tokens=max_calls * (
            config.max_request_chars * 4 + config.max_completion_tokens
        )),
        client=client, model_version=version, model_hash=model_hash,
    )
    return {
        "operation": "domain.window-align", "status": result.state, "state": str(out_state),
        "model_calls": result.model_call_count, "result": result.model_dump(mode="json"),
        "invocation": result.invocation,
        "authority": "working_only", "ontology_approved": False,
    }


def _review_mapping(*, state, discovery, target_domain, out, actor, rationale, accept):
    from fabric_kg_builder.enrichment.window_mapping import review_window_mapping

    if accept and out.exists():
        raise ValueError("Mapping reviews are immutable; choose a new --out path")
    review = review_window_mapping(
        state_root=state, discovery_path=discovery, domain_path=target_domain,
        actor=actor, rationale=rationale, output=out if accept else None,
    )
    payload = review.model_dump(mode="json")
    if not accept:
        payload.pop("artifact_hash")
        payload.update(accepted=False, authority="review_required")
    return {
        "operation": "domain.review-window-mapping",
        "status": "reviewed" if accept else "planned", "model_calls": 0,
        "writes": int(accept), "artifact": str(out) if accept else None,
        "ontology_approved": False, "evidence_approved": False,
        "result": payload,
    }


@click.command("window-align")
@click.option("--discovery", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--seed-domain", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out-state", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--live", is_flag=True, help="Persist working-schema windows and permit bounded alignment calls.")
@click.option("--dry-run", is_flag=True, help="Read-only plan; no calls or state writes (the default).")
@click.option("--resume", is_flag=True, help="Validate and continue the exact persisted run.")
@click.option("--window-size", default=8, show_default=True, type=click.IntRange(1, 1024))
@click.option("--concurrency", default=4, show_default=True, type=click.IntRange(1, 16))
@click.option("--proposal-mode", type=click.Choice(["model", "deterministic"]), default="model",
              show_default=True,
              help="Deterministic records unique formatting normalization only, without a model; synonyms stay pending.")
@click.option("--max-calls", default=32, show_default=True, type=click.IntRange(0, 100000),
              help="Alignment proposal call ceiling. Zero makes no model calls; unfinished windows stay pending.")
@click.pass_context
def domain_window_align_cmd(ctx, discovery, seed_domain, out_state, live, dry_run,
                            resume, window_size, concurrency, max_calls, proposal_mode):
    """Align already-received observations in shared-schema windows, never re-extract.

    Working vocabulary changes do not approve production ontology or evidence.
    Use review-window-mapping before explicit approved-domain replay.
    """
    planning = not live or dry_run or bool(_options(ctx).get("dry_run"))
    if live and planning:
        raise click.UsageError("--live conflicts with --dry-run")
    try:
        result = _run_alignment(
            ctx, discovery=discovery, seed_domain=seed_domain, out_state=out_state,
            planning=planning, resume=resume, window_size=window_size,
            concurrency=concurrency, max_calls=max_calls, proposal_mode=proposal_mode,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(result)


@click.command("window-status")
@click.option("--state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def domain_window_status_cmd(state):
    """Verify durable window hashes and print progress without calls or writes."""
    try:
        _emit(_core().window_status(state))
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("window-history")
@click.option("--state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def domain_window_history_cmd(state):
    """Verify and print every persisted window, including the final partial window."""
    try:
        _emit(_core().window_history(state))
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("review-window-mapping")
@click.option("--state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--discovery", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--target-domain", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--actor", required=True)
@click.option("--rationale", required=True)
@click.option("--accept", is_flag=True, help="Explicitly seal this schema-only mapping review; never approve a domain.")
@click.pass_context
def domain_review_window_mapping_cmd(ctx, state, discovery, target_domain, out, actor, rationale, accept):
    """Review exact final-snapshot mappings; default is a read-only proposal."""
    if accept and _options(ctx).get("dry_run"):
        raise click.UsageError("--accept conflicts with global --dry-run")
    try:
        result = _review_mapping(
            state=state, discovery=discovery, target_domain=target_domain,
            out=out, actor=actor, rationale=rationale, accept=accept,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(result)


@click.command("window-schema")
def domain_window_schema_cmd():
    """Print window state/proposal/review schemas without configuration or writes."""
    from fabric_kg_builder.enrichment.window_mapping import WindowMappingReview

    core = _core()
    _emit({"operation": "domain.window-schema", "schemas": {
        model.__name__: model.model_json_schema() for model in (
            core.WindowConfig, core.WindowBudget, core.WorkingConcept,
            core.WindowProposal, core.WorkingSchemaSnapshot, core.WindowLog,
            core.WindowRun, core.FinalMapping, WindowMappingReview,
        )
    }})
