"""Explicit no-model-call partial response handoff."""

import json
from pathlib import Path

import click


@click.command("handoff-partial")
@click.option("--input", "source_path", type=click.Path(path_type=Path), required=True)
@click.option("--domain-file", "domain_path", type=click.Path(path_type=Path), required=True)
@click.option("--l1-state", "l1_state_root", type=click.Path(path_type=Path), required=True)
@click.option("--l2-state", "state_root", type=click.Path(path_type=Path), required=True)
@click.option("--reuse-approved-run", type=click.Path(path_type=Path), required=True)
@click.option("--window-run", "window_run_path", type=click.Path(path_type=Path))
@click.option("--discovery", "discovery_file", type=click.Path(path_type=Path))
@click.option("--approved-quote-review", type=click.Path(path_type=Path))
@click.option("--approved-few-shot", type=click.Path(path_type=Path))
@click.option(
    "--exclude-completed-root", "exclude_completed_roots", multiple=True,
    help="Whole response-complete root ID to withhold; repeat for each root. Bound to plan approval.",
)
@click.option("--exclusion-rationale", help="Required global reason when excluding completed roots.")
@click.option(
    "--qualify-local-identifiers", is_flag=True, default=False,
    help="Scope opaque local references to their verified source slice (source-span modes only). "
    "Preserve native/business identities; bind the projection and helper hash to exact plan approval.",
)
@click.option("--dry-run/--materialize", default=True, show_default=True)
@click.option("--approve-plan-hash", "approved_plan_hash")
@click.option("--approval-actor")
@click.option("--approval-rationale")
@click.pass_context
def handoff_partial_cmd(ctx, approved_few_shot, **options):
    """Seal complete cached roots only; no OCR, remote clients, or new LLM calls.

    Review the default dry-run's exact included/excluded ranges, then repeat
    with --materialize --approve-plan-hash HASH --approval-actor ACTOR
    --approval-rationale TEXT. A fresh L2 state is mandatory. Canonical L3,
    L4 and business-quality gates remain mandatory before deployment.
    """
    from ..config.loader import load_config
    from ..enrichment.approved_partial_handoff import run_partial_handoff

    try:
        obj = ctx.obj or {}
        options["dry_run"] = bool(options["dry_run"] or obj.get("dry_run", False))
        config = load_config(
            env=str(obj.get("env", "dev")),
            yaml_path=Path(str(obj.get("config", "fabric-kg.yaml"))),
        )
        few_shot = json.loads(approved_few_shot.read_text("utf-8")) if approved_few_shot else None
        result = run_partial_handoff(**options, foundry_config=config.foundry, few_shot=few_shot)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
