"""Explicit limited-prefix scope review; never a full-corpus coverage waiver."""

from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain.discovery import _write
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.domain.window_run_acceptance import accept_window_run_prefix, preview_window_run_prefix


@click.command("accept-window-run-prefix")
@click.option("--window-run", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--actor", required=True)
@click.option("--rationale", required=True)
@click.option("--accept", is_flag=True)
@click.pass_context
def domain_accept_window_run_prefix_cmd(ctx, window_run, out, actor, rationale, accept):
    """Approve only the exact committed prefix for a limited prototype; leave the full run partial."""
    if accept and (ctx.obj or {}).get("dry_run", False):
        raise click.UsageError("--accept conflicts with --dry-run")
    try:
        run = load_windowed_run(window_run)
        action = accept_window_run_prefix if accept else preview_window_run_prefix
        result = action(run, actor=actor, rationale=rationale)
        if accept:
            _write(out, result)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "status": "accepted" if accept else "planned", "writes": int(accept), "model_calls": 0,
        "result": result.model_dump(mode="json"),
    }))
