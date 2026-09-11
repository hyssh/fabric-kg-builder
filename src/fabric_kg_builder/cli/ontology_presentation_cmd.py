"""Public review-first ontology naming repair."""

from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json


@click.command("repair-ontology-names")
@click.option("--workspace-id", required=True, help="Workspace containing the explicitly owned ontology.")
@click.option("--ontology-id", required=True, help="Existing ontology UUID; no resources are created or deleted.")
@click.option("--l4-run", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--l3-root", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--publication-plan", "plan_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--prototype-journal", "journal_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--materialize", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--state", type=click.Path(path_type=Path), required=True, help="NEW directory for immutable plan and backup.")
@click.option("--previous-repair-state", type=click.Path(path_type=Path, exists=True, file_okay=False),
              help="Completed prior repair directory; verify its full immutable chain before planning another same-item repair.")
@click.option("--live", is_flag=True, help="Apply the exact approved presentation-only plan to the same ontology.")
@click.option("--approve-plan", help="Exact repair plan hash (not the old publication plan hash).")
@click.option("--acknowledge-nontransactional", is_flag=True, help="Accept that Fabric offers no CAS/ETag guard here.")
@click.option("--resume", is_flag=True, help="Read/poll an existing intent only; never repeat the update POST.")
@click.option("--accept-verifier-update", help="Exact CURRENT repair code SHA256; approved resume with durable intent/response only, NEVER authorizes POST.")
@click.pass_context
def repair_ontology_names_cmd(ctx: click.Context, **kwargs) -> None:
    """Plan readable names from sealed L4 authority, preserving IDs and data.

    Default: Fabric GET/getDefinition reads and a NEW local snapshot/plan only.
    Live requires exact approval and acknowledgement of the nontransactional
    same-item update. Retains full backup; never automatically rolls back.
    Original publication/readiness snapshots are superseded, not rewritten.
    For an already repaired ontology, pass its completed --previous-repair-state.
    """
    root_options = ctx.find_root().obj
    if isinstance(root_options, dict) and root_options.get("dry_run"):
        if kwargs.get("live") or kwargs.get("resume"):
            raise click.UsageError("Global --dry-run conflicts with repair --live/--resume; no operations performed.")
        raise click.UsageError(
            "Global --dry-run is unsupported for repair planning because the plain command "
            "creates a local backup/plan. Omit --dry-run to create a NEW plan; "
            "no reads, writes, or updates were performed."
        )
    from fabric_kg_builder.deploy.schema2_ontology_presentation import repair_ontology_names

    try:
        result = repair_ontology_names(**kwargs)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(canonical_json(result))
