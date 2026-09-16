"""Explicit, review-first recovery of a lost prototype create response."""

from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json


@click.command("reconcile-prototype-create")
@click.option("--plan", "plan_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--prototype-journal", "journal_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--materialize", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--l4-run", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--l3-root", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--kind", type=click.Choice(["ontology", "graph", "semantic_model"]), required=True)
@click.option("--item-id", required=True, help="Explicit existing item UUID; never adopted by name.")
@click.option("--review", "review_path", type=click.Path(path_type=Path), required=True)
@click.option("--accept-review", help="Exact review hash; omitted means non-authorizing preview.")
@click.option("--returned-id-runtime-repair", is_flag=True, help="Review a runtime-only repair for an identity-verified, returned-ID-owned Graph; preserve ownership.")
@click.option("--actor", help="Operator accepting responsibility for the reconciliation.")
@click.option("--rationale", help="Why the original create or returned-ID runtime repair is justified.")
def reconcile_prototype_create_cmd(**kwargs) -> None:
    """Read back an explicit item and review a journal-only create/runtime repair.

    Performs Fabric GET/getDefinition reads. Never creates, updates or deletes
    Fabric items. Acceptance repeats the reads and requires the unchanged review.
    Original plan and materialized publication artifacts remain immutable.
    """
    from fabric_kg_builder.deploy.schema2_prototype_reconcile import reconcile_prototype_create

    try:
        result = reconcile_prototype_create(**kwargs)
    except (ValueError, OSError, KeyError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(canonical_json(result))


@click.command("verify-prototype-companion")
@click.option("--plan", "plan_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--prototype-journal", "journal_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--materialize", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--l4-run", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--l3-root", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--companion-id", required=True, help="Explicit managed Graph ID; never adopted or mutated.")
@click.option("--companion-definition", "companion_definition_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--verification-plan", "verification_plan_path", type=click.Path(path_type=Path), required=True)
@click.option("--proof", "proof_path", type=click.Path(path_type=Path), required=True)
@click.option("--approve-readback", help="Exact verification plan hash; authorizes reads, never cloud mutation.")
def verify_prototype_companion_cmd(**kwargs) -> None:
    """Preview locally, then prove both Graphs through approved read-only queries.

    Without --approve-readback, write a separate immutable verification plan;
    no Fabric calls. Approval writes a separate proof, never the source journal,
    plan or repair receipt. It does not authorize publication or item ownership.
    """
    from fabric_kg_builder.deploy.schema2_companion_verification import verify_prototype_companion

    try:
        result = verify_prototype_companion(**kwargs)
    except (ValueError, OSError, KeyError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(canonical_json(result))
