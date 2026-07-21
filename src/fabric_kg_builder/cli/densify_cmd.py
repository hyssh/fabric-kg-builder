"""densify command - apply explicit, domain-approved graph densification rules.

Pipeline helper stage (runs between ``enrich`` and ``compile-data``).

Reads enriched canonical ``*_canonical.json`` files from *input*, applies only
the entity types and relationship verbs declared in an explicit densification
configuration, and writes densified copies to *out*.

Deterministic, idempotent, and non-destructive - the input files are never
modified; existing edges are never duplicated.

Exit codes
----------
0  Success
1  I/O or unexpected error
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from fabric_kg_builder.domain import EnrichmentContractError, require_ready_domain_contract
from fabric_kg_builder.enrichment.densify import (
    densify_document,
    load_densify_config,
    link_procedure_steps,
    link_rca_paths,
    link_symptom_cause_resolution,
    link_umbrella_steps,
)

_DENSIFY_EPILOG = """\b
Example:
  fabric-kg densify --input build\\enriched --out build\\enriched_dense
    --domain-file domain.yaml --densify-config densify.yaml

\b
Densification never selects a sample taxonomy by default. The approved
domain.yaml must declare every configured entity type and relationship verb.

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("densify", epilog=_DENSIFY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--input", "input_path", default="build/enriched", show_default=True,
              type=click.Path(),
              help="Directory of enriched *_canonical.json files (output of enrich).")
@click.option("--out", "output_path", default="build/enriched_dense", show_default=True,
              type=click.Path(),
              help="Output directory for densified canonical JSON files.")
@click.option("--max-hubs", "--max-models", default=5, show_default=True, type=click.IntRange(min=1),
              help="Maximum configured hub entities to use per document.")
@click.option("--domain-file", default="domain.yaml", show_default=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Approved domain contract used to validate configured types and verbs.")
@click.option("--densify-config", required=True, type=click.Path(exists=True, dir_okay=False),
              help="YAML file defining explicit densification types and relationship mappings.")
@click.option("--link-associations/--no-link-associations", "link_scr", default=False,
              show_default=True,
              help="Apply the explicitly configured three-stage associative rule.")
@click.option("--link-steps/--no-link-steps", "link_steps", default=False, show_default=True,
              help="Apply the configured parent/child reading-order and umbrella rules.")
@click.option("--link-diagnostics/--no-link-diagnostics", "link_rca", default=False,
              show_default=True,
              help="Apply the explicitly configured diagnostic/remediation path rule.")
def densify_cmd(
    input_path: str,
    output_path: str,
    max_hubs: int,
    domain_file: str,
    densify_config: str,
    link_scr: bool,
    link_steps: bool,
    link_rca: bool,
) -> None:
    """Apply explicit domain-approved densification rules to enriched JSON.

    No rule runs unless it is present in ``--densify-config``. Every configured
    type and verb must also be declared by the approved domain contract.
    """
    in_dir = Path(input_path)
    out_dir = Path(output_path)
    if not in_dir.is_dir():
        raise click.ClickException(f"input directory not found: {in_dir}")

    files = sorted(in_dir.glob("*_canonical.json"))
    if not files:
        raise click.ClickException(f"no *_canonical.json files in {in_dir}")
    try:
        contract, _review, _status = require_ready_domain_contract(domain_file)
        config = load_densify_config(densify_config)
    except EnrichmentContractError as exc:
        raise click.UsageError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise click.UsageError(f"invalid --densify-config: {exc}") from exc

    if not config.has_rules:
        raise click.UsageError("--densify-config does not define any densification rules")

    undeclared_entities = sorted(
        config.entity_types - set(contract.candidate_model.entity_categories)
    )
    undeclared_relationships = sorted(
        config.relationship_types - set(contract.candidate_model.relationship_categories)
    )
    if undeclared_entities or undeclared_relationships:
        details: list[str] = []
        if undeclared_entities:
            details.append(f"undeclared entity types: {', '.join(undeclared_entities)}")
        if undeclared_relationships:
            details.append(
                f"undeclared relationship types: {', '.join(undeclared_relationships)}"
            )
        raise click.UsageError(
            "densify rules must be declared in approved domain.yaml; " + "; ".join(details)
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise click.ClickException(f"could not create output directory '{out_dir}': {exc}") from exc
    click.echo(f"[densify] input  : {in_dir}")
    click.echo(f"[densify] output : {out_dir}")
    click.echo(f"[densify] files  : {len(files)}")
    click.echo(f"[densify] domain : {domain_file}")
    click.echo(f"[densify] config : {densify_config}")
    click.echo(
        f"[densify] associative: {link_scr}  sequence: {link_steps}  diagnostic: {link_rca}"
    )

    total_added = 0
    total_scr = 0
    total_steps = 0
    total_rca = 0
    total_docs_linked = 0
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise click.ClickException(
                f"could not read canonical document '{f}': {exc}"
            ) from exc

        try:
            doc, added = densify_document(doc, max_hubs=max_hubs, config=config)
            scr = 0
            steps = 0
            rca = 0
            if link_scr:
                doc, scr = link_symptom_cause_resolution(doc, config=config)
            if link_steps:
                doc, steps = link_procedure_steps(doc, config=config)
                doc, rollup = link_umbrella_steps(doc, config=config)
                steps += rollup
            if link_rca:
                doc, rca = link_rca_paths(doc, config=config)
        except (KeyError, TypeError, ValueError) as exc:
            raise click.ClickException(f"invalid canonical data in '{f}': {exc}") from exc

        if added or scr or steps or rca:
            total_docs_linked += 1
            total_added += added
            total_scr += scr
            total_steps += steps
            total_rca += rca
        try:
            (out_dir / f.name).write_text(
                json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except OSError as exc:
            raise click.ClickException(
                f"could not write densified document '{f.name}': {exc}"
            ) from exc
        click.echo(
            f"[densify]   {f.name}: +{added} hub, +{scr} associative, "
            f"+{steps} sequence, +{rca} diagnostic edges"
        )

    click.echo(
        f"[densify] SUCCESS - added {total_added} hub + {total_scr} associative + "
        f"{total_steps} sequence + {total_rca} diagnostic edges across "
        f"{total_docs_linked}/{len(files)} documents -> {out_dir}"
    )
