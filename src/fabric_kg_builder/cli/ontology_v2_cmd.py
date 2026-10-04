"""Offline conversion of a v1 ontology definition into Fabric Ontology v2 TMDL parts."""

import base64
import json
from pathlib import Path

import click


@click.command("convert-ontology-v2")
@click.option("--v1-ontology", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True,
              help="v1 ontology definition JSON ({'parts': [...]}, e.g. native-bound/ontology.json).")
@click.option("--workspace-id", required=True, help="Workspace of the bound lakehouse.")
@click.option("--lakehouse-id", required=True, help="Lakehouse backing the entity/junction tables.")
@click.option("--display-name", required=True, help="Display name for the v2 ontology item (.platform).")
@click.option("--tables-dir", type=click.Path(path_type=Path, exists=True, file_okay=False),
              help="Optional directory of <table>.parquet files used to type AS columns.")
@click.option("--out", type=click.Path(path_type=Path), required=True,
              help="NEW output path for the v2 definition JSON ({'parts': [...]}).")
@click.option("--explode-dir", type=click.Path(path_type=Path),
              help="Optional NEW directory to write the decoded TMDL files for review.")
def convert_ontology_v2_cmd(v1_ontology, workspace_id, lakehouse_id, display_name, tables_dir, out, explode_dir):
    """Convert v1 ontology parts to v2 TMDL (offline; no Fabric calls).

    Inheritance (baseEntityType/redefines) is preserved. The result is a
    definition payload for POST /ontologies; the default deploy path stays v1.
    """
    from fabric_kg_builder.deploy.ontology_v2_tmdl import (
        OntologyV2ConversionError,
        column_types_from_parquet_dir,
        convert_v1_parts_to_v2_tmdl,
    )

    if out.exists():
        raise click.ClickException(f"--out already exists: {out}")
    if explode_dir is not None and explode_dir.exists():
        raise click.ClickException(f"--explode-dir already exists: {explode_dir}")
    try:
        document = json.loads(v1_ontology.read_text(encoding="utf-8"))
        v1_parts = document["parts"] if isinstance(document, dict) else document
        column_types = column_types_from_parquet_dir(tables_dir) if tables_dir else None
        parts = convert_v1_parts_to_v2_tmdl(
            v1_parts,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            display_name=display_name,
            column_types=column_types,
        )
    except (OntologyV2ConversionError, ValueError, KeyError, TypeError) as error:
        raise click.ClickException(str(error)) from error
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"parts": parts}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if explode_dir is not None:
        for part in parts:
            target = explode_dir / part["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(part["payload"]))
    click.echo(json.dumps({"status": "converted", "parts": len(parts), "out": str(out)}, sort_keys=True))
