"""compile-ontology command — generate Fabric Ontology definition parts."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from fabric_kg_builder.config.loader import load_fabric_ids
from fabric_kg_builder.ontology.bridge_validation import validate_bridge
from fabric_kg_builder.ontology.compiler import OntologyCompiler, OntologyCompilerError


_COMPILE_ONTOLOGY_EPILOG = """\b
Example:
  fabric-kg compile-ontology --env dev
  fabric-kg compile-ontology --model ontology\\model.yaml --ids ontology\\ids.lock.json --env prod

Questions? hyssh@microsoft.com
"""


@click.command("compile-ontology", epilog=_COMPILE_ONTOLOGY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--model", "model_path", default=None, type=click.Path(),
              show_default=True,
              help="Path to ontology model YAML. Default: ontology/model.yaml.")
@click.option("--ids", "ids_path", default=None, type=click.Path(),
              show_default=True,
              help="Path to stable-ID lock file. Default: ontology/ids.lock.json.")
@click.option("--out", "output_path", default="build/ontology", show_default=True,
              type=click.Path(), help="Output directory for ontology definition parts.")
@click.option("--env", "env", default="dev", show_default=True,
              type=click.Choice(["dev", "test", "prod"]),
              help="Environment to read lakehouse ID from (ontology/environments/{env}.json).")
@click.option("--include-placeholders", is_flag=True, default=False,
              help="Include placeholder entity/relationship types in the compiled output.")
def compile_ontology_cmd(
    model_path: str | None,
    ids_path: str | None,
    output_path: str,
    env: str,
    include_placeholders: bool,
) -> None:
    """Generate Fabric Ontology definition parts from ontology/model.yaml.

    Reads the domain ontology model and stable-ID lock file, validates bridge
    rules (BRG-001..010), then writes EntityTypes/, RelationshipTypes/,
    definition.json, and .platform marker to --out.

    Exit codes: 0 success · 1 error · 5 model/bridge validation failure.
    """
    cwd = Path.cwd()

    resolved_model = Path(model_path) if model_path else cwd / "ontology" / "model.yaml"
    resolved_ids = Path(ids_path) if ids_path else cwd / "ontology" / "ids.lock.json"

    if not resolved_model.exists():
        click.echo(f"[compile-ontology] ERROR: model file not found: {resolved_model}", err=True)
        sys.exit(1)
    if not resolved_ids.exists():
        click.echo(f"[compile-ontology] ERROR: ids.lock.json not found: {resolved_ids}", err=True)
        sys.exit(1)

    # Read lakehouse ID from env config (graceful — empty string if unavailable)
    _, lakehouse_id = load_fabric_ids(env)

    click.echo(f"[compile-ontology] model   : {resolved_model}")
    click.echo(f"[compile-ontology] ids     : {resolved_ids}")
    click.echo(f"[compile-ontology] out     : {output_path}")
    click.echo(f"[compile-ontology] env     : {env}  (lakehouse_id: {lakehouse_id or '<not set>'})")

    try:
        compiler = OntologyCompiler(
            model_path=resolved_model,
            ids_lock_path=resolved_ids,
            lakehouse_id=lakehouse_id,
        )
    except OntologyCompilerError as exc:
        click.echo(f"[compile-ontology] VALIDATION ERROR: {exc}", err=True)
        sys.exit(5)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"[compile-ontology] ERROR loading model: {exc}", err=True)
        sys.exit(1)

    try:
        out_dir = compiler.compile(output_path)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"[compile-ontology] ERROR during compilation: {exc}", err=True)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Bridge validation (SPEC-003 §12.9  BRG-001..010)
    # ------------------------------------------------------------------
    violations = validate_bridge(compiler.model)
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if warnings:
        click.echo("")
        click.echo(f"[compile-ontology] Bridge validation: {len(warnings)} WARNING(s)")
        for v in warnings:
            click.echo(f"  [{v.gate_id} WARN] {v.message}")

    if errors:
        click.echo("")
        click.echo(f"[compile-ontology] Bridge validation: {len(errors)} ERROR(s) — build blocked", err=True)
        for v in errors:
            click.echo(f"  [{v.gate_id} ERROR] {v.message}", err=True)
        sys.exit(5)

    click.echo(
        f"[compile-ontology] Bridge validation: OK "
        f"(0 errors, {len(warnings)} warning(s))"
    )

    # Build summary counts from the model
    entity_types = compiler.model.get("entityTypes", [])
    rel_types = compiler.model.get("relationshipTypes", [])
    parts = compiler.get_rest_parts()

    click.echo("")
    click.echo("-" * 60)
    click.echo("[compile-ontology] SUMMARY")
    click.echo(f"  Entity types      : {len(entity_types)}")
    click.echo(f"  Relationship types: {len(rel_types)}")
    click.echo(f"  Parts written     : {len(parts) + 1}")  # +1 for definition.json manifest
    click.echo(f"  Bridge validation : 0 errors, {len(warnings)} warning(s)")
    click.echo(f"  Output directory  : {out_dir.resolve()}")
    click.echo("-" * 60)
    click.echo("[compile-ontology] Done. Exit 0.")
