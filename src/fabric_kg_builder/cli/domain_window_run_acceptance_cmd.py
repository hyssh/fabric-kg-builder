"""Explicit integrated-run processing coverage waiver, never semantic approval."""

from decimal import InvalidOperation
from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain.discovery import _write
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.domain.window_run_acceptance import accept_window_run_partial, preview_window_run_partial


@click.command("accept-window-run-partial")
@click.option("--window-run", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--actor", required=True)
@click.option("--rationale", required=True)
@click.option("--min-chunk-coverage", default="0.99", show_default=True)
@click.option("--accept", is_flag=True)
@click.pass_context
def domain_accept_window_run_partial_cmd(ctx, window_run, out, actor, rationale, min_chunk_coverage, accept):
    """Review at least 99% processing without clearing missing observations or quarantine."""
    if accept and (ctx.obj or {}).get("dry_run", False):
        raise click.UsageError("--accept conflicts with --dry-run")
    try:
        run = load_windowed_run(window_run)
        action = accept_window_run_partial if accept else preview_window_run_partial
        result = action(run, actor=actor, rationale=rationale, min_chunk_coverage=min_chunk_coverage)
        if accept:
            _write(out, result)
    except InvalidOperation as exc:
        raise click.ClickException("--min-chunk-coverage must be a decimal between 0.99 and 1") from exc
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "status": "accepted" if accept else "planned", "writes": int(accept), "model_calls": 0,
        "result": result.model_dump(mode="json"),
    }))
