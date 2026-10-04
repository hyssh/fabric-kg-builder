"""Explicit review of integrated-run mappings against the finally approved L1."""

from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.enrichment.window_run_reuse import preview_window_run_mapping, review_window_run_mapping


@click.command("review-window-run-mapping")
@click.option("--window-run", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--target-domain", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--actor", required=True)
@click.option("--rationale", required=True)
@click.option("--accept", is_flag=True, help="Seal exact uniquely matched approved names and scopes.")
@click.pass_context
def domain_review_window_run_mapping_cmd(ctx, window_run, target_domain, out, actor, rationale, accept):
    """Plan by default; neither mapping review nor final working schema approves evidence."""
    if accept and (ctx.obj or {}).get("dry_run", False):
        raise click.UsageError("--accept conflicts with --dry-run")
    try:
        if accept:
            result = review_window_run_mapping(
                window_run_path=window_run, domain_path=target_domain,
                actor=actor, rationale=rationale, output=out,
            )
        else:
            result = preview_window_run_mapping(
                window_run_path=window_run, domain_path=target_domain,
                actor=actor, rationale=rationale,
            )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "status": "accepted" if accept else "planned",
        "model_calls": 0, "writes": int(accept),
        "result": result.model_dump(mode="json"),
    }))
