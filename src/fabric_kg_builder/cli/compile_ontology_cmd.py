"""compile-ontology command — generate Fabric Ontology definition parts."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import click
import yaml

from fabric_kg_builder.config.loader import load_fabric_binding_target
from fabric_kg_builder.ontology.bridge_validation import validate_bridge
from fabric_kg_builder.ontology.compiler import OntologyCompiler, OntologyCompilerError
from fabric_kg_builder.semantic import (
    SemanticCompileError,
    SemanticContractError,
    build_ontology_projection,
    compile_semantic_bundle,
    load_semantic_bundle,
    load_semantic_model_artifacts,
)


_COMPILE_ONTOLOGY_EPILOG = """\b
Example:
  fabric-kg compile-ontology --env dev
  fabric-kg compile-ontology --model ontology\\model.yaml --ids ontology\\ids.lock.json --env prod

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("compile-ontology", epilog=_COMPILE_ONTOLOGY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--model", "model_path", default=None, type=click.Path(),
              show_default=True,
              help="Path to ontology model YAML. Default: ontology/model.yaml.")
@click.option("--ids", "ids_path", default=None, type=click.Path(),
              show_default=True,
              help="Path to stable-ID lock file. Default: ontology/ids.lock.json.")
@click.option(
    "--contract",
    "contract_path",
    default=None,
    type=click.Path(),
    help="Approved canonical semantic contract. Enables shared-facade compilation.",
)
@click.option(
    "--mappings",
    "mappings_path",
    default=None,
    type=click.Path(),
    help="Semantic physical mappings. Default with --contract: ontology/mappings.yaml.",
)
@click.option(
    "--vocabulary",
    "vocabulary_path",
    default=None,
    type=click.Path(),
    help="Controlled vocabulary. Default with --contract: ontology/vocabulary.yaml.",
)
@click.option(
    "--semantic-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help=(
        "Sealed compile-semantic output. Ontology compilation consumes the "
        "persisted manifest-derived model and stable IDs from this directory."
    ),
)
@click.option("--out", "output_path", default="build/ontology", show_default=True,
              type=click.Path(), help="Output directory for ontology definition parts.")
@click.option("--env", "env", default="dev", show_default=True,
              type=str,
              help="Environment to read lakehouse ID from (ontology/environments/{env}.json).")
@click.option("--include-placeholders", is_flag=True, default=False,
              help="Include placeholder entity/relationship types in the compiled output.")
def compile_ontology_cmd(
    model_path: str | None,
    ids_path: str | None,
    contract_path: str | None,
    mappings_path: str | None,
    vocabulary_path: str | None,
    semantic_dir: str | None,
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

    resolved_ids = Path(ids_path) if ids_path else cwd / "ontology" / "ids.lock.json"
    semantic_contract_hash: str | None = None
    semantic_model_manifest_hash: str | None = None
    semantic_crosswalk_hash: str | None = None
    materialization_plan_hash: str | None = None
    temp_dir: tempfile.TemporaryDirectory[str] | None = None

    if semantic_dir and (contract_path or model_path):
        click.echo(
            "[compile-ontology] ERROR: --semantic-dir is mutually exclusive "
            "with --contract and --model.",
            err=True,
        )
        sys.exit(1)
    if contract_path and model_path:
        click.echo(
            "[compile-ontology] ERROR: --contract and --model are mutually exclusive.",
            err=True,
        )
        sys.exit(1)

    if semantic_dir:
        semantic_root = Path(semantic_dir)
        try:
            loaded = load_semantic_model_artifacts(semantic_root)
            normalized_contract = json.loads(
                (
                    semantic_root / "normalized-contract.json"
                ).read_text(encoding="utf-8")
            )
            sealed_ontology = yaml.safe_load(
                (
                    semantic_root / "ontology" / "model.yaml"
                ).read_text(encoding="utf-8")
            )
            sealed_ontology_name = str(
                (sealed_ontology or {}).get("ontology", {}).get("name")
                or normalized_contract["name"]
            )
            ontology_model, ontology_ids = build_ontology_projection(
                loaded.manifest,
                loaded.materialization_plan,
                ontology_name=sealed_ontology_name,
                contract_name=str(normalized_contract["name"]),
                contract_description=str(
                    normalized_contract["description"]
                ),
                contract_version=str(
                    normalized_contract["contract_version"]
                ),
            )
        except SemanticCompileError as exc:
            click.echo(
                f"[compile-ontology] VALIDATION ERROR: {exc}",
                err=True,
            )
            sys.exit(5)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            click.echo(
                "[compile-ontology] VALIDATION ERROR: normalized semantic "
                f"metadata is incomplete: {exc}",
                err=True,
            )
            sys.exit(5)
        temp_dir = tempfile.TemporaryDirectory(
            prefix="fabric-kg-manifest-ontology-"
        )
        semantic_source = Path(temp_dir.name)
        (semantic_source / "model.yaml").write_text(
            yaml.safe_dump(
                {"ontology": ontology_model},
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        (semantic_source / "ids.lock.json").write_text(
            json.dumps(ontology_ids, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        resolved_model = semantic_source / "model.yaml"
        resolved_ids = semantic_source / "ids.lock.json"
        semantic_contract_hash = loaded.manifest.semantic_contract_hash
        semantic_model_manifest_hash = loaded.manifest.manifest_hash
        semantic_crosswalk_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                loaded.crosswalk.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        materialization_plan_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                loaded.materialization_plan.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        model_source = f"sealed semantic manifest {semantic_root}"
    elif contract_path:
        resolved_contract = Path(contract_path)
        resolved_mappings = (
            Path(mappings_path)
            if mappings_path
            else cwd / "ontology" / "mappings.yaml"
        )
        resolved_vocabulary = (
            Path(vocabulary_path)
            if vocabulary_path
            else cwd / "ontology" / "vocabulary.yaml"
        )
        try:
            bundle = load_semantic_bundle(
                contract_path=resolved_contract,
                mappings_path=resolved_mappings,
                vocabulary_path=resolved_vocabulary,
                ids_lock_path=resolved_ids,
                require_approval=True,
            )
            compiled = compile_semantic_bundle(bundle)
            temp_dir = tempfile.TemporaryDirectory(
                prefix="fabric-kg-semantic-ontology-"
            )
            semantic_source = compiled.write(Path(temp_dir.name))
            resolved_model = semantic_source / "ontology" / "model.yaml"
            resolved_ids = semantic_source / "ontology" / "ids.lock.json"
            semantic_contract_hash = compiled.contract_hash
            semantic_model_manifest_hash = (
                compiled.semantic_model_manifest.manifest_hash
            )
            semantic_crosswalk_hash = "sha256:" + hashlib.sha256(
                json.dumps(
                    compiled.semantic_crosswalk.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            materialization_plan_hash = "sha256:" + hashlib.sha256(
                json.dumps(
                    compiled.materialization_plan.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            model_source = f"shared semantic contract {resolved_contract}"
        except (SemanticContractError, SemanticCompileError, OSError) as exc:
            if temp_dir is not None:
                temp_dir.cleanup()
            click.echo(
                f"[compile-ontology] VALIDATION ERROR: {exc}",
                err=True,
            )
            sys.exit(5)
    else:
        resolved_model = (
            Path(model_path) if model_path else cwd / "ontology" / "model.yaml"
        )
        model_source = "explicit" if model_path else "auto-resolved from cwd"

    if not resolved_model.exists():
        click.echo(f"[compile-ontology] ERROR: model file not found: {resolved_model}", err=True)
        sys.exit(1)
    if not resolved_ids.exists():
        click.echo(f"[compile-ontology] ERROR: ids.lock.json not found: {resolved_ids}", err=True)
        sys.exit(1)

    # Read the complete public Ontology binding target. Empty IDs remain
    # supported for offline compilation, but live deployment validates them.
    workspace_id, lakehouse_id, schema_name = load_fabric_binding_target(env)

    click.echo(f"[compile-ontology] model   : {resolved_model}  ({model_source})")
    click.echo(f"[compile-ontology] ids     : {resolved_ids}")
    click.echo(f"[compile-ontology] out     : {output_path}")
    click.echo(
        f"[compile-ontology] env     : {env}  "
        f"(workspace_id: {workspace_id or '<not set>'}, "
        f"lakehouse_id: {lakehouse_id or '<not set>'}, "
        f"schema: {schema_name})"
    )

    try:
        compiler = OntologyCompiler(
            model_path=resolved_model,
            ids_lock_path=resolved_ids,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            schema=schema_name,
        )
        if temp_dir is not None:
            temp_dir.cleanup()
    except OntologyCompilerError as exc:
        if temp_dir is not None:
            temp_dir.cleanup()
        click.echo(f"[compile-ontology] VALIDATION ERROR: {exc}", err=True)
        sys.exit(5)
    except Exception as exc:  # noqa: BLE001
        if temp_dir is not None:
            temp_dir.cleanup()
        click.echo(f"[compile-ontology] ERROR loading model: {exc}", err=True)
        sys.exit(1)

    try:
        out_dir = compiler.compile(output_path)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"[compile-ontology] ERROR during compilation: {exc}", err=True)
        sys.exit(1)

    artifact_files = sorted(
        path
        for path in out_dir.rglob("*")
        if path.is_file() and path.name != "ontology-manifest.json"
    )
    artifact_hashes = {
        str(path.relative_to(out_dir)).replace("\\", "/"): (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in artifact_files
    }
    artifact_set_hash = hashlib.sha256(
        json.dumps(
            artifact_hashes,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    (out_dir / "ontology-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "contract_hash": semantic_contract_hash,
                "semantic_model_manifest_hash": (
                    semantic_model_manifest_hash
                ),
                "semantic_crosswalk_hash": semantic_crosswalk_hash,
                "materialization_plan_hash": materialization_plan_hash,
                "artifact_set_hash": f"sha256:{artifact_set_hash}",
                "artifacts": artifact_hashes,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    # ------------------------------------------------------------------
    # Bridge validation (SPEC-003 §12.9  BRG-001..010)
    # ------------------------------------------------------------------
    # The legacy bridge validator requires fixed DocumentChunk/SearchIndexRecord
    # types. Approved semantic contracts use the shared Graph/Search handoff
    # contract instead and must remain domain-neutral.
    violations = (
        []
        if semantic_contract_hash is not None
        else validate_bridge(compiler.model)
    )
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
