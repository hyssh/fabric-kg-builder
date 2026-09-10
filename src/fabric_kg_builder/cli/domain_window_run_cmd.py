"""Public controls for the context-bound raw-text window coordinator."""

from __future__ import annotations

from pathlib import Path

import click

from .domain_window_cmd import _emit, _options


def _core():
    from fabric_kg_builder.domain import window_run

    return window_run


@click.command("window-run")
@click.option("--prepared", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Exact prepared-source artifact; no model-derived observations required.")
@click.option("--discovery", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Use only this discovery's verified prepared sources, not its old candidates.")
@click.option("--intake", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Domain, all questions, routing and pending requirements carried into every window.")
@click.option("--out-state", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--window-size", default=8, show_default=True, type=click.IntRange(1, 1024))
@click.option("--concurrency", default=4, show_default=True, type=click.IntRange(1, 16))
@click.option("--max-request-chars", default=96000, show_default=True, type=click.IntRange(1024))
@click.option("--request-char-budget", type=click.IntRange(1024),
              help="Explicit invocation input-size admission limit; preserves recorded config and request contents.")
@click.option("--max-completion-tokens", default=8192, show_default=True,
              type=click.IntRange(128, 32768))
@click.option("--max-chunk-chars", default=8000, show_default=True,
              type=click.IntRange(128, 64000),
              help="Chunking cap for --prepared; --discovery preserves its recorded chunk plan.")
@click.option("--max-calls", default=32, show_default=True, type=click.IntRange(0, 100000),
              help="Total model-call ceiling for this invocation, including repairs.")
@click.option("--max-repair-calls", default=2, show_default=True, type=click.IntRange(0, 100000),
              help="Run-wide repair-call ceiling; repairs also consume invocation --max-calls.")
@click.option("--max-tokens", default=0, show_default=True, type=click.IntRange(0),
              help="Optional conservative aggregate token reservation cap; zero leaves call caps in force.")
@click.option("--stop-after-document", type=click.IntRange(1),
              help="Pause after this many documents in the recorded scope; resume retains the schema.")
@click.option("--live", is_flag=True, help="Persist windows and permit bounded model calls.")
@click.option("--dry-run", is_flag=True, help="Read-only plan, with no model calls or state writes.")
@click.option("--resume", is_flag=True, help="Continue the exact context/source/schema-bound run.")
@click.option("--retry-uncertain", is_flag=True,
              help="Explicitly allow retry of a dispatched request with no durable response; requires --resume.")
@click.option("--retry-invalid-response", is_flag=True,
              help="Explicitly retry a received invalid JSON response with retained diagnostics; requires --resume.")
@click.pass_context
def domain_window_run_cmd(
    ctx, prepared, discovery, intake, out_state, window_size, concurrency,
    max_request_chars, request_char_budget, max_completion_tokens, max_chunk_chars, max_calls,
    max_repair_calls, max_tokens, stop_after_document, live, dry_run, resume,
    retry_uncertain, retry_invalid_response,
):
    """Bootstrap, extract, evaluate and evolve a working schema over raw chunks.

    Every worker in a window uses the same frozen schema and full intake context.
    Outputs are unapproved observations, never asserted graph facts or deployment.
    Without --live this command only plans the work.
    """
    if (prepared is None) == (discovery is None):
        raise click.UsageError("Supply exactly one of --prepared or --discovery")
    planning = not live or dry_run or bool(_options(ctx).get("dry_run"))
    if live and planning:
        raise click.UsageError("--live conflicts with --dry-run")
    if resume and not out_state.is_dir():
        raise click.UsageError("--resume requires an existing window state directory")
    if not resume and out_state.exists():
        raise click.UsageError("Window state already exists; use --resume or a fresh --out-state")
    if retry_uncertain and not resume:
        raise click.UsageError("--retry-uncertain requires --resume")
    if retry_invalid_response and not resume:
        raise click.UsageError("--retry-invalid-response requires --resume")

    from .domain_design_cmd import _build_client
    from fabric_kg_builder.domain.proposal import compute_model_hash

    try:
        core = _core()
        inputs = core.load_window_inputs(
            prepared_path=prepared, discovery_path=discovery, intake_path=intake,
        )
        destination = out_state.resolve()
        for protected in (Path(inputs.prepared.source_path), prepared or discovery, intake):
            protected = protected.resolve()
            if (destination == protected or destination.is_relative_to(protected)
                    or protected.is_relative_to(destination)):
                raise ValueError("Window output must not overlap source, intake or source-cache authority")
        config = core.RunConfig(
            window_size=window_size, max_concurrency=concurrency,
            max_request_chars=max_request_chars, max_completion_tokens=max_completion_tokens,
            max_chunk_chars=max_chunk_chars,
        )
        budget = core.RunBudget(
            max_calls=max_calls, max_repair_calls=max_repair_calls,
            max_tokens=max_tokens, stop_after_document=stop_after_document,
            retry_uncertain=retry_uncertain, retry_invalid_response=retry_invalid_response,
            request_char_budget=request_char_budget,
        )
        binding = core.windowed_model_binding(out_state) if resume else None
        if binding is not None and (
            binding["inputs_hash"] != inputs.artifact_hash
            or binding["config"] != config.model_dump(mode="json")
        ):
            raise ValueError("Window resume input/context/config binding changed")
        if planning:
            result = core.plan_window_run(inputs, config)
            _emit({
                "operation": "domain.window-run", "status": "planned",
                "model_calls": 0, "writes": 0, "authority": "working_only",
                "ontology_approved": False, "budget": budget.model_dump(mode="json"),
                "result": result,
            })
            return

        client, version, model_hash = None, "none", None
        if max_calls:
            client, version = _build_client(ctx)
            model_hash = compute_model_hash(client, version)
        elif resume:
            version, model_hash = binding["model_version"], binding["model_hash"]
        result = core.run_windowed(
            inputs=inputs, output_dir=out_state, config=config, budget=budget,
            client=client, model_version=version, model_hash=model_hash,
        )
        _emit({
            "operation": "domain.window-run", "status": result.state,
            "state": str(out_state), "authority": "working_only",
            "ontology_approved": False, "model_calls": result.model_call_count,
            "invocation": result.invocation, "result": result.model_dump(mode="json"),
            "request_char_budget": request_char_budget or config.max_request_chars,
        })
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("window-run-status")
@click.option("--state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def domain_window_run_status_cmd(state):
    """Read verified integrated-run progress; never infer activity from a timer."""
    try:
        _emit(_core().windowed_status(state))
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("window-run-history")
@click.option("--state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def domain_window_run_history_cmd(state):
    """Read all verified raw-window transitions, decisions and source accounting."""
    try:
        _emit(_core().windowed_history(state))
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("window-run-schema")
@click.option("--state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
def domain_window_run_schema_cmd(state):
    """Export the current verified working schema, not an approved domain."""
    try:
        _emit(_core().windowed_schema(state))
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
