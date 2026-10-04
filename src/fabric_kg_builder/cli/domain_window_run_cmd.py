"""Public controls for the context-bound raw-text window coordinator."""

from __future__ import annotations

from pathlib import Path
import json

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
@click.option("--seed-reference", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Provisional DesignReference JSON for every worker from version zero; resume inherits saved content.")
@click.option("--out-state", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--window-size", default=8, show_default=True, type=click.IntRange(1, 1024))
@click.option("--concurrency", default=4, show_default=True, type=click.IntRange(1, 16))
@click.option("--max-request-chars", default=96000, show_default=True, type=click.IntRange(1024))
@click.option("--request-char-budget", type=click.IntRange(1024),
              help="Explicit invocation input-size admission limit; preserves recorded config and request contents.")
@click.option("--max-completion-tokens", type=click.IntRange(128, 128000),
              help="Output/reasoning allowance: new whole-document runs default to 32768, "
                   "chunked to 8192; resume inherits the recorded value. Reviewed model limits still apply.")
@click.option("--max-chunk-chars", default=8000, show_default=True,
              type=click.IntRange(128, 64000),
              help="Chunking cap for --prepared; --discovery preserves its recorded chunk plan.")
@click.option("--schema-policy", type=click.Choice(["reviewed-concepts", "concepts", "observed-terms"]),
              help="Chunked runs default to reviewed-concepts (independent admission of new types). "
                  "concepts uses self-assessment only. Resume inherits its recorded policy.")
@click.option("--discovery-mode", type=click.Choice(["chunked", "whole-document"]),
              help="New runs default to whole-document: serial complete-document schema discovery, "
                  "without instance extraction. Resume inherits the recorded mode; chunked is explicit.")
@click.option("--model-capabilities", "--capability-profile", "capability_profile",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Required explicit model/deployment context, input and output limits JSON for whole-document mode.")
@click.option("--model-transport", type=click.Choice(["chat_completions", "project_responses"]),
              help="Whole-document request-accounting transport; default chat_completions. Must match Foundry config.")
@click.option("--max-calls", default=32, show_default=True, type=click.IntRange(0, 100000),
              help="Total model-call ceiling for this invocation, including repairs.")
@click.option("--max-repair-calls", default=16, show_default=True, type=click.IntRange(0, 100000),
              help="Run-wide repair/semantic-admission ceiling; reviews also consume invocation --max-calls.")
@click.option("--max-tokens", default=0, show_default=True, type=click.IntRange(0),
              help="Optional conservative aggregate token reservation cap; zero leaves call caps in force.")
@click.option("--stop-after-document", type=click.IntRange(1),
              help="Pause after this many documents in the recorded scope; resume retains the schema.")
@click.option("--max-windows", type=click.IntRange(0),
              help="Commit at most this many additional windows; zero freezes the existing committed prefix.")
@click.option("--live", is_flag=True, help="Persist windows and permit bounded model calls.")
@click.option("--dry-run", is_flag=True, help="Read-only plan, with no model calls or state writes.")
@click.option("--resume", is_flag=True, help="Continue the exact context/source/schema-bound run.")
@click.option("--retry-uncertain", is_flag=True,
              help="Explicitly allow retry of a dispatched request with no durable response; requires --resume.")
@click.option("--retry-invalid-response", is_flag=True,
              help="Explicitly retry a received invalid JSON response with retained diagnostics; requires --resume.")
@click.option("--max-transport-retries", type=click.IntRange(0, 10),
              help="Extra attempts per whole-document request for 429/timeouts/connections/5xx; "
                   "default 3 in whole-document mode, 0 in chunked mode. Every attempt consumes --max-calls/tokens.")
@click.option("--max-transport-retry-wait-seconds", default=900, show_default=True,
              type=click.IntRange(0, 3600),
              help="Total retry sleep allowance per document; longer provider delays stop rather than retry early.")
@click.pass_context
def domain_window_run_cmd(
    ctx, prepared, discovery, intake, seed_reference, out_state, window_size, concurrency,
    max_request_chars, request_char_budget, max_completion_tokens, max_chunk_chars, schema_policy, max_calls,
    max_repair_calls, max_tokens, stop_after_document, max_windows, live, dry_run, resume,
    retry_uncertain, retry_invalid_response,
    discovery_mode, capability_profile, model_transport,
    max_transport_retries, max_transport_retry_wait_seconds,
):
    """Evolve a working schema over raw chunks or complete documents.

    By default, each complete document evolves the prior reusable type schema.
    Add, update or delete working definitions with exact source witnesses and
    reasons. History retains before/after definitions and rejected attempts.
    Content revisions advance only on change; document checkpoints always advance.
    Review and freeze once, then extract instances across all authorized documents.
    Explicit chunked mode retains legacy observation extraction.
    Outputs are unapproved working artifacts, never asserted graph facts.
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
        binding = core.windowed_model_binding(out_state) if resume else None
        saved = binding["config"] if binding is not None else {}
        mode = discovery_mode or (
            saved.get("discovery_mode", "chunked") if resume else "whole-document"
        )
        profile = saved.get("capability_profile")
        if capability_profile is not None:
            profile_path = capability_profile.resolve()
            if profile_path == destination or profile_path.is_relative_to(destination):
                raise ValueError("Capability profile must be outside output state")
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if mode == "whole-document" and schema_policy is not None:
            raise ValueError("--schema-policy is for chunked discovery; whole-document uses its own schema-only policy")
        if mode == "chunked" and (capability_profile is not None or model_transport is not None):
            raise ValueError("--model-capabilities/--capability-profile/--model-transport require whole-document discovery")
        if mode == "chunked" and max_transport_retries:
            raise ValueError("--max-transport-retries requires whole-document discovery")
        reference = binding["config"].get("seed_reference") if binding is not None else None
        if seed_reference is not None:
            reference_path = seed_reference.resolve()
            for protected in (destination, Path(inputs.prepared.source_path).resolve(),
                              (prepared or discovery).resolve(), intake.resolve()):
                if (reference_path == protected or reference_path.is_relative_to(protected)
                        or protected.is_relative_to(reference_path)):
                    raise ValueError("Seed reference must not overlap output, source, intake or source-cache artifacts")
            reference = core.DesignReference.model_validate_json(
                reference_path.read_text(encoding="utf-8"))
        prompt_version = (
            saved["prompt_version"] if binding is not None and schema_policy is None
            else core.document_schema.PROMPT_VERSION if mode == "whole-document"
            else core.RUN_PROMPT_VERSION if schema_policy == "observed-terms"
            else core.CORE_PROMPT_VERSION if schema_policy == "concepts"
            else core.COMPACT_REVIEW_PROMPT_VERSION
        )
        config = core.RunConfig(
            window_size=window_size, max_concurrency=concurrency,
            max_request_chars=max_request_chars,
            max_completion_tokens=(max_completion_tokens if max_completion_tokens is not None else
                                   saved.get("max_completion_tokens", 8192) if resume else
                                   32768 if mode == "whole-document" else 8192),
            max_chunk_chars=max_chunk_chars, prompt_version=prompt_version,
            seed_reference=reference,
            discovery_mode=mode, capability_profile=profile,
            model_transport=model_transport or saved.get("model_transport", "chat_completions"),
        )
        budget = core.RunBudget(
            max_calls=max_calls, max_repair_calls=max_repair_calls,
            max_tokens=max_tokens, stop_after_document=stop_after_document,
            max_windows=max_windows,
            retry_uncertain=retry_uncertain, retry_invalid_response=retry_invalid_response,
            request_char_budget=request_char_budget,
            max_transport_retries=(max_transport_retries if max_transport_retries is not None
                                   else 3 if mode == "whole-document" else 0),
            max_transport_retry_wait_seconds=max_transport_retry_wait_seconds,
        )
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
@click.option("--changes-only", is_flag=True,
              help="Version 1.2.0/1.3.0 document changes: source quotations, reasons, before/after definitions and content revisions.")
def domain_window_run_history_cmd(state, changes_only):
    """Read all verified raw-window transitions, decisions and source accounting."""
    try:
        _emit(_core().windowed_history(state, changes_only=changes_only))
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
