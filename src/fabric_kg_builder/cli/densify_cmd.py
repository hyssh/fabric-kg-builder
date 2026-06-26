"""densify command — add source-document DeviceModel hub edges to enriched JSON.

Pipeline helper stage (runs between ``enrich`` and ``compile-data``).

Reads enriched canonical ``*_canonical.json`` files from *input*, links the
device model(s) each document covers to the Component / Part / Procedure /
Symptom entities in that same document, and writes densified copies to *out*.
This makes "X for device Y" queries traversable in the deployed graph.

Deterministic, idempotent, and non-destructive — the input files are never
modified; existing edges are never duplicated.

Exit codes
----------
0  Success
1  I/O or unexpected error
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from fabric_kg_builder.enrichment.densify import (
    densify_document,
    link_procedure_steps,
    link_rca_paths,
    link_symptom_cause_resolution,
    link_umbrella_steps,
)

_DENSIFY_EPILOG = """\b
Example:
  fabric-kg densify --input data\\surface_kg\\enriched --out data\\surface_kg\\enriched_dense

\b
Densification links each document's DeviceModel(s) to the Components, Parts,
Procedures, and Symptoms in that same document, so the data agent can answer
"parts/components/procedures for <device>" questions.

Questions? hyssh@microsoft.com
"""


@click.command("densify", epilog=_DENSIFY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--input", "input_path", default="build/enriched", show_default=True,
              type=click.Path(),
              help="Directory of enriched *_canonical.json files (output of enrich).")
@click.option("--out", "output_path", default="build/enriched_dense", show_default=True,
              type=click.Path(),
              help="Output directory for densified canonical JSON files.")
@click.option("--max-models", default=5, show_default=True, type=int,
              help="Maximum specific device models to use as hubs per document.")
@click.option("--link-scr/--no-link-scr", "link_scr", default=True, show_default=True,
              help="Also link Cause→Symptom→Resolution troubleshooting triples via "
                   "document-scoped keyword overlap (associative edges, confidence 0.45).")
@click.option("--link-steps/--no-link-steps", "link_steps", default=True, show_default=True,
              help="Also link each Procedure to its Steps by document reading order "
                   "(reconstructs has_step edges extraction missed; confidence 0.5).")
@click.option("--link-rca/--no-link-rca", "link_rca", default=True, show_default=True,
              help="Also build RCA diagnostic-path edges: Symptom -> diagnosed_by -> "
                   "diagnostic Procedure and Symptom -> remediated_by -> repair Procedure "
                   "(connects symptoms to actionable fixes; confidence 0.4).")
def densify_cmd(
    input_path: str, output_path: str, max_models: int, link_scr: bool,
    link_steps: bool, link_rca: bool,
) -> None:
    """Add source-document DeviceModel hub edges to enriched JSON.

    For each enriched document, links the specific Surface model(s) it covers to
    the Component / Part / Procedure / Symptom entities in the same document.
    Reuses existing relationship verbs (has_component, has_part, has_procedure,
    has_symptom) so the multi-type ontology folds the new edges into existing
    typed relationships.

    With --link-scr (default), also connects isolated Cause / Symptom /
    Resolution troubleshooting entities within each document. With --link-steps
    (default), reconstructs Procedure -> Step edges by document reading order.
    """
    in_dir = Path(input_path)
    out_dir = Path(output_path)
    if not in_dir.is_dir():
        click.echo(f"[densify] ERROR: input directory not found: {in_dir}", err=True)
        sys.exit(1)

    files = sorted(in_dir.glob("*_canonical.json"))
    if not files:
        click.echo(f"[densify] ERROR: no *_canonical.json files in {in_dir}", err=True)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"[densify] input  : {in_dir}")
    click.echo(f"[densify] output : {out_dir}")
    click.echo(f"[densify] files  : {len(files)}")
    click.echo(f"[densify] link-scr: {link_scr}  link-steps: {link_steps}  link-rca: {link_rca}")

    total_added = 0
    total_scr = 0
    total_steps = 0
    total_rca = 0
    total_docs_linked = 0
    try:
        for f in files:
            doc = json.loads(f.read_text(encoding="utf-8"))
            doc, added = densify_document(doc, max_models=max_models)
            scr = 0
            steps = 0
            rca = 0
            if link_scr:
                doc, scr = link_symptom_cause_resolution(doc)
            if link_steps:
                doc, steps = link_procedure_steps(doc)
                doc, rollup = link_umbrella_steps(doc)
                steps += rollup
            if link_rca:
                doc, rca = link_rca_paths(doc)
            if added or scr or steps or rca:
                total_docs_linked += 1
                total_added += added
                total_scr += scr
                total_steps += steps
                total_rca += rca
            (out_dir / f.name).write_text(
                json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8"
            )
            click.echo(
                f"[densify]   {f.name}: +{added} hub, +{scr} S/C/R, "
                f"+{steps} step, +{rca} RCA edges"
            )
    except Exception as exc:  # noqa: BLE001
        click.echo(f"[densify] ERROR: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"[densify] SUCCESS — added {total_added} hub + {total_scr} S/C/R + "
        f"{total_steps} step + {total_rca} RCA edges across "
        f"{total_docs_linked}/{len(files)} documents → {out_dir}"
    )
