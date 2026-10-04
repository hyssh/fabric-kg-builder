"""Public review-first independent Graph label repair."""

from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json


@click.command("repair-graph-labels")
@click.option("--workspace-id", required=True)
@click.option("--graph-id", required=True, help="Existing independent Graph UUID; never the managed companion.")
@click.option("--l4-run", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--l3-root", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--publication-plan", "plan_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--prototype-journal", "journal_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--materialize", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--naming-review", type=click.Path(path_type=Path, exists=True, dir_okay=False),
              help="Same complete Copilot naming JSON used for Ontology; actual node/edge labels change, not aliases.")
@click.option("--state", type=click.Path(path_type=Path), required=True, help="NEW directory for immutable backup/plan/proof.")
@click.option("--dry-run", is_flag=True, help="Explicit read-only planning (also the default); writes local evidence only.")
@click.option("--live", is_flag=True, help="Apply the exact reviewed labels-only plan once.")
@click.option("--approve-plan", help="Exact new graph label repair plan SHA256, not publication approval.")
@click.option("--acknowledge-nontransactional", is_flag=True, help="Acknowledge Fabric has no atomic CAS/ETag guard.")
@click.option("--resume", is_flag=True, help="Read/poll durable update intent only; NEVER repeat update.")
@click.pass_context
def repair_graph_labels_cmd(ctx: click.Context, dry_run: bool, **kwargs) -> None:
    """Repair node/edge labels from sealed approved Domain names, not prefixes.

    Default and --dry-run perform Fabric reads and create NEW local evidence.
    --live needs an exact --approve-plan and --acknowledge-nontransactional.
    All aliases, keys, properties, sources, instance identities and data stay
    unchanged. Approval is labels-only, NOT approval of business facts.
    Original publication plans/journals are never rewritten or resumed.
    """
    root = ctx.find_root().obj
    dry_run = dry_run or isinstance(root, dict) and root.get("dry_run", False)
    if dry_run and (kwargs.get("live") or kwargs.get("resume")):
        raise click.UsageError("--dry-run conflicts with --live/--resume; no operations performed")
    from fabric_kg_builder.deploy.schema2_graph_presentation import repair_graph_labels

    try:
        result = repair_graph_labels(**kwargs)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(canonical_json(result))
