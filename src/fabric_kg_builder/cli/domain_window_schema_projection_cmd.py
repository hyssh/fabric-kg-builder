"""Explicit model-free retention of a design's exact bound working vocabulary."""

import json
from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain.design import DesignCompleteness, load_domain_design, save_design_artifact
from fabric_kg_builder.domain.window_schema_projection import build_design_corrections, retain_window_schema
from fabric_kg_builder.domain.window_validation import WindowValidationOperation


def _unique_json_fields(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"Duplicate JSON field: {key}")
        values[key] = value
    return values


@click.command("retain-window-schema")
@click.option("--file", "draft_file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--apply", "apply_projection", is_flag=True)
@click.option("--route-target", multiple=True, metavar="QUESTION=TYPE", help="Explicitly correct only a declared graph route target.")
@click.option("--completeness", "completeness_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="JSON DesignCompleteness object or list to append; never observed counts or members.")
@click.option("--actor", help="Required when supplying explicit schema corrections.")
@click.option("--rationale", help="Required when supplying explicit schema corrections.")
@click.option("--prefer-window-definitions", is_flag=True,
              help="Explicitly prefer exact source definitions only for otherwise compatible names/scopes/identities; requires actor/rationale.")
@click.option("--source-scoped-type", multiple=True, metavar="EXACT_NAME",
              help="Review an unapproved draft type as source-scoped before retention; requires an exact unresolved working entity and actor/rationale. Repeat per type.")
@click.pass_context
def domain_retain_window_schema_cmd(ctx, draft_file, out, apply_projection, route_target, completeness_file, actor, rationale, prefer_window_definitions, source_scoped_type):
    """Retain representable source concepts without another model call or inherited approval."""
    if apply_projection and (ctx.obj or {}).get("dry_run", False):
        raise click.UsageError("--apply conflicts with --dry-run")
    if draft_file.resolve() == out.resolve():
        raise click.UsageError("Projection must not overwrite its original model draft")
    correcting = bool(route_target or completeness_file is not None or prefer_window_definitions or source_scoped_type)
    if correcting and (not actor or not actor.strip() or not rationale or not rationale.strip()):
        raise click.UsageError("Explicit corrections require --actor and --rationale")
    if not correcting and (actor is not None or rationale is not None):
        raise click.UsageError("--actor/--rationale require explicit correction operations")
    try:
        operation = WindowValidationOperation()
        targets = {}
        for value in route_target:
            question, separator, target = value.partition("=")
            if not separator or not question or not target or question in targets:
                raise ValueError("--route-target requires unique QUESTION=TYPE references")
            targets[question] = target
        completeness = []
        if completeness_file is not None:
            raw = json.loads(completeness_file.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_fields)
            raw = raw if isinstance(raw, list) else [raw]
            completeness = [DesignCompleteness.model_validate(item) for item in raw]
        corrections = build_design_corrections(
            actor=actor, rationale=rationale, route_targets=targets, completeness=completeness,
            prefer_window_definitions=prefer_window_definitions,
            source_scoped_types=source_scoped_type,
        ) if correcting else None
        result = retain_window_schema(
            load_domain_design(draft_file, _validation=operation), corrections=corrections, _validation=operation,
        )
        if apply_projection:
            save_design_artifact(out, result, _validation=operation)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    projection = result.schema_projection
    click.echo(canonical_json({
        "status": "projected" if apply_projection else "planned", "writes": int(apply_projection), "model_calls": 0,
        "authority": projection.authority, "parent_draft_hash": projection.parent_draft_hash,
        "window_run_hash": projection.window_run_hash, "snapshot_hash": projection.snapshot_hash,
        "scope_acceptance_hash": projection.scope_acceptance_hash,
        "coverage_acceptance_hash": projection.coverage_acceptance_hash,
        "retained_concept_count": len(projection.retained_concept_keys),
        "unsupported_concept_count": len(projection.unsupported_findings),
        "classification_policy": projection.classification_policy,
        **({"operator_corrections": projection.operator_corrections} if projection.operator_corrections is not None else {}),
        "definition_precedence": projection.definition_precedence,
        "retained_concept_keys": projection.retained_concept_keys, "derived_concept_ids": projection.derived_concept_ids,
        "unsupported_findings": projection.unsupported_findings,
        **({"draft_id": result.draft_id, "draft_hash": result.draft_hash, "projection_hash": projection.projection_hash,
            "out": str(out)} if apply_projection else {}),
    }))
