"""deploy commands — deploy-lakehouse, deploy-ontology, deploy-search."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import click
import yaml

from fabric_kg_builder.deploy.onelake_writer import (
    LAKEHOUSE_TABLE_PROJECTION,
    LAKEHOUSE_TABLES,
)
from fabric_kg_builder.deploy.manifest import (
    DeploymentManifest,
    DeploymentManifestError,
    load_deployment_manifest,
)
from fabric_kg_builder.deploy.name_authority import (
    NameAuthorityConflict,
    ResolvedName,
    manifest_from_env_config,
    render_name_resolution,
    resolve_item_name,
    validate_readback_name,
)

# Default Lakehouse table list — graph/ontology scope only.
# Imported from onelake_writer so the projection constant and this list stay in sync.
# "chunks" is intentionally absent: pure retrieval text → AI Search (kg-chunks).
_LAKEHOUSE_TABLES = LAKEHOUSE_TABLES


def _write_receipt(path: str | None, payload: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(target)


def _sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _usable_config_value(value: object) -> str:
    """Return a configured string, excluding checked-in example placeholders."""
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    lowered = stripped.lower()
    if (
        not stripped
        or stripped.startswith("${")
        or stripped.startswith("<")
        or "placeholder" in lowered
        or "your-" in lowered
        or "your_" in lowered
    ):
        return ""
    return stripped


def _read_fabric_env_config(
    env: str,
    environments_dir: Path | None = None,
    *,
    allow_placeholders: bool = False,
) -> dict:
    """Read just the fabric section of the per-env JSON — no secrets required.

    Returns a dict with at minimum:
      workspace_id, lakehouse_item_id, schema_name
    Raises FileNotFoundError if the env JSON is missing.
    """
    envs_dir = environments_dir or Path("ontology") / "environments"
    env_json_path = envs_dir / f"{env}.json"
    if not env_json_path.exists():
        raise FileNotFoundError(
            f"Environment config not found: {env_json_path}. "
            "Run 'fabric-kg init' or create the file manually."
        )
    raw = json.loads(env_json_path.read_text(encoding="utf-8"))
    fabric = raw.get("fabric", {})
    normalize = (
        (lambda value: str(value).strip() if value is not None else "")
        if allow_placeholders
        else _usable_config_value
    )
    return {
        "workspace_id": normalize(fabric.get("workspace_id", "")),
        "workspace_display_name": (
            _usable_config_value(fabric.get("workspace_display_name", ""))
            or _usable_config_value(fabric.get("workspace_name", ""))
            or env
        ),
        "lakehouse_item_id": normalize(
            fabric.get("lakehouse_item_id", "")
        ),
        "lakehouse_display_name": fabric.get("lakehouse_display_name", ""),
        "onelake_tables_path": fabric.get("onelake_tables_path", ""),
        "schema_name": fabric.get("schema_name", "dbo"),
        "ontology_item_id": normalize(
            fabric.get("ontology_item_id", "")
        ),
        "ontology_display_name": _usable_config_value(
            fabric.get("ontology_display_name", "")
        ),
        "graph_model_id": normalize(
            fabric.get("graph_model_item_id", "")
            or fabric.get("graph_model_id", "")
        ),
        "graph_model_display_name": (
            _usable_config_value(fabric.get("graph_model_display_name", ""))
            or "KG Graph"
        ),
        "data_agent_item_id": normalize(
            fabric.get("data_agent_item_id", "")
        ),
        "data_agent_display_name": _usable_config_value(
            fabric.get("data_agent_display_name", "")
        ),
    }


def _persist_ontology_item_id(env: str, ontology_item_id: str) -> None:
    """Persist a recreated Ontology ID for subsequent CLI deployments."""
    path = Path("ontology") / "environments" / f"{env}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    fabric = payload.setdefault("fabric", {})
    fabric["ontology_item_id"] = ontology_item_id
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _persist_graph_model_item_id(env: str, graph_model_item_id: str) -> None:
    """Persist a recovered Graph Model ID for subsequent CLI deployments."""
    path = Path("ontology") / "environments" / f"{env}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    fabric = payload.setdefault("fabric", {})
    fabric["graph_model_item_id"] = graph_model_item_id
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _persist_data_agent_identity(
    env: str,
    *,
    item_id: str,
    display_name: str,
) -> None:
    """Persist the exact Data Agent identity for subsequent reapplication."""
    path = Path("ontology") / "environments" / f"{env}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    fabric = payload.setdefault("fabric", {})
    fabric["data_agent_item_id"] = item_id
    fabric["data_agent_display_name"] = display_name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_or_synthesize_manifest(
    manifest_path: str | None,
    fabric_cfg: dict,
) -> DeploymentManifest:
    """Load a deployment manifest from file or synthesize from env config.

    When ``--manifest`` is not provided, builds an in-memory
    :class:`DeploymentManifest` from the legacy env JSON names (migration mode).
    When ``--manifest`` is provided, loads and validates the YAML file.

    Raises:
        click.ClickException: On manifest load/parse failures.
    """
    if manifest_path:
        try:
            return load_deployment_manifest(manifest_path)
        except DeploymentManifestError as exc:
            raise click.ClickException(str(exc)) from exc
    return manifest_from_env_config(fabric_cfg)


def _warn_manifest_vs_env(
    cmd_prefix: str,
    item_type: str,
    manifest: DeploymentManifest,
    env_name: str | None,
    field: str,
) -> None:
    """Emit a migration warning when manifest and env config names diverge.

    Called only when ``--manifest`` is explicitly provided (file-loaded
    manifest). The manifest always wins; the warning guides the user to
    remove the legacy env field.
    """
    from fabric_kg_builder.deploy.name_authority import _item_spec

    if not env_name:
        return
    manifest_name = _item_spec(manifest, item_type).display_name
    if manifest_name and manifest_name != env_name:
        click.echo(
            f"[{cmd_prefix}] WARN: deployment.yaml sets {item_type} display name "
            f"'{manifest_name}'; env config has '{env_name}'. "
            "Manifest wins. Remove the legacy env field to silence this warning.",
            err=True,
        )


_DEPLOY_LAKEHOUSE_EPILOG = """\b
Example:
  fabric-kg deploy-lakehouse --env dev --mock
  fabric-kg deploy-lakehouse --env dev --no-mock
  fabric-kg deploy-lakehouse --env dev --tables entities,relationships --mock

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("deploy-lakehouse", epilog=_DEPLOY_LAKEHOUSE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=str,
              help="Target deployment environment (reads ontology/environments/{env}.json).")
@click.option("--dist", "dist_path", default="dist", show_default=True, type=click.Path(),
              help="Path to dist directory produced by 'package'. "
                   "Falls back to build/parquet/ if dist is absent.")
@click.option("--parquet-dir", default=None, type=click.Path(exists=True, file_okay=False),
              help="Run-scoped canonical Parquet directory. Overrides --dist lookup.")
@click.option("--tables", default=None,
              show_default=True,
              help="Comma-separated subset of Parquet tables to deploy "
                   "(default: all graph/ontology tables; chunks are excluded).")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing Lakehouse Delta tables.")
@click.option("--mock/--no-mock", "use_mock", default=False, show_default=True,
              help="Mock mode: log planned actions without any network call (--mock). "
                   "Use --no-mock for a live deploy.")
@click.option("--manifest", "manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority). "
                   "Defaults to env-config names (legacy mode).")
def deploy_lakehouse_cmd(
    env: str,
    dist_path: str,
    parquet_dir: str | None,
    tables: str | None,
    force: bool,
    use_mock: bool,
    manifest_path: str | None,
) -> None:
    """Upload canonical structured Parquet tables to Fabric Lakehouse via OneLake.

    Reads workspace_id, lakehouse_item_id, and schema_name from
    ontology/environments/{env}.json.  Authenticates with DefaultAzureCredential
    (run 'az login' for dev; use a Service Principal for CI/prod).

    Uploads a LEAN graph/ontology projection only — the 'chunks' table is
    intentionally excluded because text retrieval goes to AI Search (kg-chunks).
    The document_elements table is projected: content/content_html/row_index/
    col_index columns are dropped (text → AI Search).

    Exit codes: 0 success · 1 error · 6 auth failure.
    """
    # --- Read env config (fabric section only — no secrets needed for mock) ---
    try:
        fabric_cfg = _read_fabric_env_config(
            env,
            allow_placeholders=use_mock,
        )
    except FileNotFoundError as exc:
        click.echo(f"[deploy-lakehouse] ERROR: {exc}", err=True)
        raise SystemExit(1) from exc

    workspace_id = fabric_cfg["workspace_id"]
    lakehouse_item_id = fabric_cfg["lakehouse_item_id"]

    # --- Name authority: resolve Lakehouse display name ---
    deployment_manifest = _load_or_synthesize_manifest(manifest_path, fabric_cfg)
    if manifest_path:
        _warn_manifest_vs_env(
            "deploy-lakehouse", "Lakehouse", deployment_manifest,
            fabric_cfg.get("lakehouse_display_name"), "lakehouse_display_name",
        )
    try:
        resolved_lakehouse = resolve_item_name(deployment_manifest, "Lakehouse")
    except NameAuthorityConflict as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    lakehouse_name = resolved_lakehouse.display_name or "kg_lakehouse"
    if use_mock:
        click.echo(render_name_resolution(resolved_lakehouse))

    onelake_tables_path = fabric_cfg.get("onelake_tables_path") or (
        f"https://onelake.dfs.fabric.microsoft.com"
        f"/{workspace_id}/{lakehouse_item_id}/Tables"
    )
    schema_name = fabric_cfg.get("schema_name") or "dbo"

    if not workspace_id or not lakehouse_item_id:
        click.echo(
            f"[deploy-lakehouse] ERROR: env '{env}' config is missing "
            "'fabric.workspace_id' or 'fabric.lakehouse_item_id'.",
            err=True,
        )
        raise SystemExit(1)

    # --- Resolve table list ---
    selected_tables = (
        [t.strip() for t in tables.split(",") if t.strip()]
        if tables
        else _LAKEHOUSE_TABLES
    )

    # Resolve parquet dir: explicit run output, then packaged artifact, then
    # legacy global build directory.
    resolved_parquet_dir = Path(parquet_dir) if parquet_dir else (
        Path(dist_path) / "fabric-kg-package" / "parquet"
    )
    if parquet_dir:
        click.echo(
            f"[deploy-lakehouse] Using explicit Parquet dir: {resolved_parquet_dir}"
        )
    elif not resolved_parquet_dir.exists():
        fallback = Path("build") / "parquet"
        if fallback.exists():
            resolved_parquet_dir = fallback
            click.echo(
                f"[deploy-lakehouse] Using fallback parquet dir: {resolved_parquet_dir}"
            )

    available = (
        sorted(p.stem for p in resolved_parquet_dir.glob("*.parquet"))
        if resolved_parquet_dir.exists()
        else []
    )

    click.echo(f"[deploy-lakehouse] Environment  : {env}")
    click.echo(f"[deploy-lakehouse] Workspace    : {workspace_id}")
    click.echo(f"[deploy-lakehouse] Lakehouse    : {lakehouse_item_id} ({lakehouse_name})")
    click.echo(f"[deploy-lakehouse] Tables path  : {onelake_tables_path}")
    click.echo(f"[deploy-lakehouse] Schema name  : {schema_name}")
    click.echo(f"[deploy-lakehouse] Force overwrite: {force}")
    click.echo(f"[deploy-lakehouse] Parquet dir  : {resolved_parquet_dir}")
    click.echo(
        f"[deploy-lakehouse] Tables to deploy ({len(selected_tables)}): "
        + ", ".join(selected_tables)
    )
    if available:
        click.echo(
            "[deploy-lakehouse] Parquet files available: " + ", ".join(available)
        )
    else:
        click.echo(
            f"[deploy-lakehouse] NOTE: No parquet files found under {resolved_parquet_dir} "
            "(run 'fabric-kg package' first)."
        )

    if use_mock:
        # --- MOCK: report lean scope (graph/ontology tables only) ---
        click.echo("[deploy-lakehouse] *** MOCK MODE — no live Fabric call ***")
        click.echo(
            "[deploy-lakehouse] Scope: LEAN (graph/ontology only) — "
            "chunks excluded (text retrieval → AI Search kg-chunks)."
        )
        click.echo(
            "[deploy-lakehouse] document_elements: lean projection applied — "
            "content/content_html/row_index/col_index dropped (text → AI Search)."
        )
        for table in selected_tables:
            if table not in LAKEHOUSE_TABLE_PROJECTION:
                # Table is not in the Lakehouse scope (e.g. chunks passed via --tables)
                click.echo(
                    f"[deploy-lakehouse]   SKIPPED {table} "
                    f"(not in Lakehouse projection — text/retrieval → AI Search)"
                )
                continue
            keep_cols = LAKEHOUSE_TABLE_PROJECTION[table]
            if keep_cols is not None:
                click.echo(
                    f"[deploy-lakehouse]   WOULD upload {table}.parquet "
                    f"-> Tables/{schema_name}/{table} (lean: {len(keep_cols)} cols)"
                )
            else:
                click.echo(
                    f"[deploy-lakehouse]   WOULD upload {table}.parquet "
                    f"-> Tables/{schema_name}/{table}"
                )
        click.echo("[deploy-lakehouse] SUCCESS (mock)")
        return

    # --- LIVE: write Delta tables to OneLake ---
    from fabric_kg_builder.deploy.onelake_writer import deploy_parquet_to_onelake  # noqa: PLC0415

    click.echo("[deploy-lakehouse] LIVE deploy starting (lean graph/ontology projection)...")
    try:
        results = deploy_parquet_to_onelake(
            parquet_dir=resolved_parquet_dir,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema_name,
            tables=selected_tables,
            mock=False,
            projection=LAKEHOUSE_TABLE_PROJECTION,
        )
    except Exception as exc:  # noqa: BLE001
        click.echo(f"[deploy-lakehouse] ERROR: {exc}", err=True)
        raise SystemExit(1) from exc

    errors = [
        f"{table}: {status}"
        for table, status in results.items()
        if status.startswith("error")
    ]
    for table, status in results.items():
        icon = "✓" if status == "ok" else ("⚠" if status.startswith("skipped") else "✗")
        click.echo(f"[deploy-lakehouse]   {icon} {table}: {status}")

    if errors:
        click.echo(
            f"[deploy-lakehouse] FAILED — {len(errors)} error(s): "
            + "; ".join(errors),
            err=True,
        )
        raise SystemExit(1)

    ok_count = sum(1 for s in results.values() if s == "ok")
    skipped = sum(1 for s in results.values() if s.startswith("skipped"))
    click.echo(
        f"[deploy-lakehouse] SUCCESS — {ok_count} table(s) written (lean graph/ontology scope)"
        + (f", {skipped} skipped" if skipped else "")
        + ". chunks intentionally excluded (text → AI Search)."
    )


def _read_search_env_config(env: str, environments_dir: Path | None = None) -> dict:
    """Read fabric + ai_search sections from the per-env JSON.

    Returns a dict with 'ai_search' sub-dict.
    Raises FileNotFoundError when the env JSON is missing.
    """
    envs_dir = environments_dir or Path("ontology") / "environments"
    env_json_path = envs_dir / f"{env}.json"
    if not env_json_path.exists():
        raise FileNotFoundError(
            f"Environment config not found: {env_json_path}. "
            "Run 'fabric-kg init' or create the file manually."
        )
    raw = json.loads(env_json_path.read_text(encoding="utf-8"))
    return {
        "fabric": raw.get("fabric", {}),
        "ai_search": raw.get("ai_search", {}),
    }


_DEPLOY_ONTOLOGY_EPILOG = """\b
Fabric item naming:
  ontology_display_name must start with a letter, contain only letters,
  numbers, and underscores, and be shorter than 90 characters.
  Example: Building_Operations_Ontology (not "Building Operations Ontology").

Sealed semantic deployment:
  --semantic-dir requires --parquet-dir so contract-owned Lakehouse tables can
  be materialized before Ontology mutation and persisted read-back.

Sensitivity labels and read-back:
  A successful updateDefinition is not sufficient deployment evidence. The CLI
  calls Ontology getDefinition afterward. A protected sensitivity label can
  return ItemHasProtectedLabel/403 and block the persisted projection receipt.
  Resolve the workspace information-protection policy or label usage rights;
  do not bypass validate-projection or fabricate a receipt.

PowerShell examples:
\b
  fabric-kg deploy-ontology --env dev
  fabric-kg deploy-ontology --env dev --no-mock
  fabric-kg deploy-ontology --env dev --semantic-dir build\\semantic --parquet-dir build\\parquet --receipt-out build\\release\\ontology-receipt.json --no-mock
  fabric-kg deploy-ontology --env dev --multitype --parquet-dir build\\parquet --no-mock
  fabric-kg deploy-ontology --env dev --multitype --type-profile surface-support --parquet-dir data\\surface_kg\\parquet --no-mock

\b
--multitype models one Fabric EntityType per real domain type (Component,
Procedure, Step, ...) plus typed relationships, instead of a single generic
KGEntity. By default, types are derived from the data. --type-profile applies
an explicit named allowlist for a curated sample domain. The command
materializes per-type Lakehouse tables from --parquet-dir first.

\b
With --multitype, a Data Agent grounding doc is also written by default
(--create-data-agent-instruction) next to --parquet-dir, derived from the live
graph and the domain brief's sample questions (--domain-file).

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


def _write_agent_instructions(
    mt_plan,  # MultitypePlan
    ontology_name: str,
    parquet_dir,  # Path
    out_path: str | None,
    domain_brief_path: str | None,
) -> None:
    """Generate and write the Data Agent grounding doc from the live plan."""
    from pathlib import Path as _Path  # noqa: PLC0415

    from fabric_kg_builder.deploy.agent_instructions import (  # noqa: PLC0415
        build_agent_instructions,
    )

    industry = ""
    business_domain = ""
    questions: list[str] = []
    # Try the explicit --domain-file, else look beside the parquet dir / build/enriched.
    candidates = []
    if domain_brief_path:
        candidates.append(_Path(domain_brief_path))
    candidates.append(_Path(parquet_dir).parent / "enriched" / "domain.json")
    candidates.append(_Path("build/enriched/domain.json"))
    for c in candidates:
        if c.exists():
            try:
                from fabric_kg_builder.enrichment.domain import load_domain_brief  # noqa: PLC0415

                brief = load_domain_brief(c)
                industry = brief.industry
                business_domain = brief.business_domain
                questions = brief.competency_questions
                click.echo(f"[deploy-ontology] agent-instruction: using domain brief {c}")
                break
            except Exception:  # noqa: BLE001
                continue

    doc = build_agent_instructions(
        entity_types=mt_plan.entity_types,
        relationship_pairs=mt_plan.relationship_pairs,
        ontology_name=ontology_name,
        industry=industry,
        business_domain=business_domain,
        competency_questions=questions,
    )
    target = _Path(out_path) if out_path else _Path(parquet_dir) / "data-agent-instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc, encoding="utf-8")
    click.echo(f"[deploy-ontology] Data Agent instructions written → {target}")


def _load_model_yaml(cwd: Path | None = None) -> dict:
    """Load ontology/model.yaml if present; return empty dict on failure."""
    root = cwd or Path.cwd()
    path = root / "ontology" / "model.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw.get("ontology", raw)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _validate_parquet_date_precision(
    parquet_dir: Path,
    model: dict,
) -> list[str]:
    """Scan Parquet data for partial dates on timestamp-typed model properties.

    Returns a list of PARTIAL_DATE_INCOMPATIBLE error strings.  An empty list
    means no incompatible values were found (or no timestamp properties exist).

    Each error includes the exact rejected-value count, the affected entity count,
    and a sample of the offending values for actionable context.
    """
    import re  # noqa: PLC0415

    _YEAR_ONLY = re.compile(r"^\d{4}$")
    _YEAR_MONTH = re.compile(r"^\d{4}-\d{2}$")

    timestamp_props: list[tuple[str, str]] = []
    for et in model.get("entityTypes", []):
        et_name = str(et.get("name", ""))
        for prop in et.get("properties", []):
            if str(prop.get("type", "")) == "timestamp":
                timestamp_props.append((et_name, str(prop.get("name", ""))))

    if not timestamp_props:
        return []

    try:
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError:
        return []

    entities_file = parquet_dir / "entities.parquet"
    if not entities_file.exists():
        return []

    try:
        table = pq.read_table(entities_file)
    except Exception:  # noqa: BLE001
        return []

    errors: list[str] = []
    affected_entity_names: set[str] = set()
    for et_name, prop_name in timestamp_props:
        if prop_name not in table.schema.names:
            continue
        col = table.column(prop_name)
        col_type = str(col.type)
        if "timestamp" in col_type or "date" in col_type:
            continue
        rejected_count = 0
        sample_values: list[str] = []
        for chunk in col.chunks:
            for val in chunk.to_pylist():
                if val is None:
                    continue
                s = str(val)
                if _YEAR_ONLY.match(s) or _YEAR_MONTH.match(s):
                    rejected_count += 1
                    if len(sample_values) < 3:
                        sample_values.append(s)
        if rejected_count > 0:
            affected_entity_names.add(et_name)
            errors.append(
                f"PARTIAL_DATE_INCOMPATIBLE: entity type '{et_name}', "
                f"property '{prop_name}' is declared 'timestamp' (Fabric DateTime) "
                f"but data contains {rejected_count} partial date value(s) "
                f"across 1 entity type. "
                f"Sample values: {sample_values}. "
                "Use type 'string' and a separate precision column to preserve "
                "year-only or year-month dates without inventing missing components."
            )
    # Annotate total affected entity count across all errors when more than one entity is affected
    if len(affected_entity_names) > 1:
        affected_count = len(affected_entity_names)
        errors = [
            e.replace("across 1 entity type", f"across {affected_count} entity type(s)")
            for e in errors
        ]
    return errors


def _check_zero_edge_types(
    model: dict,
    edges_by_type: dict[str, int],
    total_edges: int,
) -> list[str]:
    """Return error strings for required relationship types with zero edges.

    Only fires when the Lakehouse has edges at all (total_edges > 0) — an
    empty Lakehouse is not a deployment error.
    """
    if total_edges <= 0:
        return []

    errors: list[str] = []
    for rt in model.get("relationshipTypes", []):
        name = str(rt.get("name", ""))
        if not name:
            continue
        if edges_by_type.get(name, 0) == 0:
            src = rt.get("sourceType", "?")
            tgt = rt.get("targetType", "?")
            errors.append(
                f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{name}' "
                f"({src} → {tgt}) has zero edges in the deployed relationships "
                "table. Publishing a disconnected ontology is not allowed. "
                "Populate the relationships table or remove this relationship "
                "type from the model before deploying."
            )
    return errors


def _load_compiled_ontology_parts(dist_path: str) -> list[dict]:
    """Load compiler-owned Ontology parts without rebuilding legacy semantics."""
    definition_path = Path(dist_path) / "definition.json"
    if not definition_path.exists():
        return []
    try:
        definition = json.loads(definition_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"Invalid compiled Ontology definition {definition_path}: {exc}"
        ) from exc
    raw_parts = definition.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise click.ClickException(
            f"Compiled Ontology definition has no parts: {definition_path}"
        )
    parts: list[dict] = []
    for ordinal, part in enumerate(raw_parts):
        if not isinstance(part, dict) or not part.get("path"):
            raise click.ClickException(
                f"{definition_path}: invalid part at index {ordinal}"
            )
        try:
            payload = json.loads(
                base64.b64decode(str(part["payload"]), validate=True).decode(
                    "utf-8"
                )
            )
        except (
            KeyError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise click.ClickException(
                f"{definition_path}: invalid payload for part "
                f"{part.get('path')}: {exc}"
            ) from exc
        parts.append({"path": str(part["path"]), "payload_json": payload})
    if not any(part["path"] == "definition.json" for part in parts):
        # The compiler's top-level definition.json is a local Base64 manifest.
        # Fabric's updateDefinition contract separately requires an empty root
        # definition.json part in the uploaded definition archive.
        parts.insert(0, {"path": "definition.json", "payload_json": {}})
    return parts


@click.command("deploy-ontology", epilog=_DEPLOY_ONTOLOGY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=str,
              help="Target deployment environment (reads ontology/environments/{env}.json).")
@click.option("--dist", "dist_path", default="build/ontology", show_default=True, type=click.Path(),
              help="Path to compiled ontology directory (output of compile-ontology). "
                   "Used for the old compile artifact; deploy now uses build_ontology_parts().")
@click.option(
    "--semantic-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Sealed semantic authority directory. When provided, materializes its "
         "contract-owned tables and validates persisted Ontology read-back.",
)
@click.option("--poll-timeout", default=300, show_default=True, type=int,
              help="Seconds to wait for long-running Fabric LRO operations.")
@click.option("--mock/--no-mock", "use_mock", default=True, show_default=True,
              help="Mock mode (default --mock): log what would be deployed without any network call. "
                   "Use --no-mock for a live deploy.")
@click.option("--recreate", is_flag=True, default=False,
              help="Delete the configured Ontology, create a replacement, and persist its new ID.")
@click.option(
    "--delete-ontology-id",
    "legacy_ontology_ids",
    multiple=True,
    help="Delete an additional explicit legacy Ontology ID before deployment. "
         "May be specified more than once.",
)
@click.option("--multitype", is_flag=True, default=False,
              help="Model one Fabric EntityType per real domain type (Component, Procedure, "
                   "Step, ...) plus typed relationships, instead of a single generic KGEntity. "
                   "Materializes per-type Lakehouse tables from --parquet-dir before pushing.")
@click.option("--parquet-dir", "parquet_dir", default=None, type=click.Path(),
              help="Directory with entities.parquet / relationships.parquet (required for "
                   "--multitype). Used to plan types and materialize per-type tables.")
@click.option("--min-pair-count", default=10, show_default=True, type=int,
              help="[--multitype] Minimum edge count for a (source->target) pair to become a "
                   "typed relationship.")
@click.option(
    "--type-profile",
    default=None,
    type=click.Choice(["surface-support"]),
    help="[--multitype] Explicit named entity-type allowlist. Omit to derive all "
         "observed domain types from the canonical data.",
)
@click.option("--create-data-agent-instruction/--no-create-data-agent-instruction",
              "create_agent_instruction", default=True, show_default=True,
              help="[--multitype] Write a Fabric Data Agent grounding doc (instructions, "
                   "entity descriptions, relationship map, example queries) derived from the "
                   "deployed graph and the domain brief's sample questions.")
@click.option("--agent-instruction-out", "agent_instruction_out", default=None,
              type=click.Path(),
              help="Output path for the Data Agent instruction doc "
                   "(default: <parquet-dir>/data-agent-instructions.md).")
@click.option("--domain-file", "domain_brief_path", default=None, type=click.Path(),
              help="Path to domain.json (from set-domain) — its industry, business_domain, "
                   "and sample questions enrich the generated Data Agent instructions.")
@click.option(
    "--receipt-out",
    default=None,
    type=click.Path(),
    help="Write a non-secret deployment receipt containing the Fabric item ID.",
)
@click.option("--manifest", "manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority). "
                   "Defaults to env-config names (legacy mode).")
@click.option("--display-name", "display_name_override", default=None,
              help="Override the Ontology display name. When --manifest is supplied "
                   "this must match the manifest name or NAME_AUTHORITY_CONFLICT is raised.")
def deploy_ontology_cmd(
    env: str, dist_path: str, semantic_dir: str | None,
    poll_timeout: int, use_mock: bool,
    recreate: bool,
    legacy_ontology_ids: tuple[str, ...],
    multitype: bool, parquet_dir: str | None, min_pair_count: int,
    type_profile: str | None,
    create_agent_instruction: bool, agent_instruction_out: str | None,
    domain_brief_path: str | None,
    receipt_out: str | None,
    manifest_path: str | None,
    display_name_override: str | None,
) -> None:
    """Deploy the Fabric Ontology definition to the target workspace.

    Builds the Fabric-format ontology parts from the current ontology/model.yaml
    (EntityType KGEntity → dbo.entities, RelationshipType related_to →
    dbo.relationships) and pushes them via POST updateDefinition to populate
    the Ontology item with nodes and edges.

    Default is --mock (safe dry-run). Use --no-mock for a live deploy.

    Exit codes: 0 success · 1 error · 6 auth failure.
    """
    from fabric_kg_builder.deploy.fabric_ontology import (  # noqa: PLC0415
        create_or_get_ontology_item,
        delete_ontology_item,
        get_ontology_item_display_name,
        update_ontology_definition,
    )
    from fabric_kg_builder.ontology.fabric_def import build_ontology_parts  # noqa: PLC0415

    # --- Early manifest authority check: --display-name must match manifest ---
    # This runs before env config loading so conflict errors are always emitted.
    if manifest_path and display_name_override:
        try:
            _early_manifest = _load_or_synthesize_manifest(manifest_path, {})
            _early_resolved = resolve_item_name(_early_manifest, "Ontology")
            if display_name_override != _early_resolved.display_name:
                conflict = NameAuthorityConflict(
                    item_type="Ontology",
                    manifest_name=_early_resolved.display_name,
                    conflicting_name=display_name_override,
                    source="--display-name",
                )
                click.echo(str(conflict), err=True)
                sys.exit(1)
        except NameAuthorityConflict as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        except Exception:
            pass  # defer full manifest errors to the main resolution below

    # Read env config (workspace_id, lakehouse_item_id, schema_name)
    try:
        fabric_cfg = _read_fabric_env_config(
            env,
            allow_placeholders=use_mock,
        )
    except FileNotFoundError as exc:
        click.echo(f"[deploy-ontology] ERROR: {exc}", err=True)
        sys.exit(1)

    workspace_id = fabric_cfg["workspace_id"]
    lakehouse_item_id = fabric_cfg["lakehouse_item_id"]
    schema_name = fabric_cfg.get("schema_name") or "dbo"

    if not workspace_id:
        click.echo(
            f"[deploy-ontology] ERROR: workspace_id not found in "
            f"ontology/environments/{env}.json",
            err=True,
        )
        sys.exit(1)

    ontology_item_id = fabric_cfg.get("ontology_item_id") or ""

    # --- Name authority: resolve Ontology display name ---
    deployment_manifest = _load_or_synthesize_manifest(manifest_path, fabric_cfg)
    if manifest_path:
        _warn_manifest_vs_env(
            "deploy-ontology", "Ontology", deployment_manifest,
            fabric_cfg.get("ontology_display_name"), "ontology_display_name",
        )
    try:
        resolved_ontology = resolve_item_name(deployment_manifest, "Ontology")
    except NameAuthorityConflict as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if display_name_override and not manifest_path:
        # No manifest: --display-name is used directly.
        resolved_display = display_name_override
    else:
        resolved_display = resolved_ontology.display_name

    ontology_name = resolved_display or "kg_ontology"
    if use_mock:
        click.echo(render_name_resolution(resolved_ontology))

    click.echo(f"[deploy-ontology] env             : {env}")
    click.echo(f"[deploy-ontology] workspace_id    : {workspace_id}")
    click.echo(f"[deploy-ontology] lakehouse_id    : {lakehouse_item_id}")
    click.echo(f"[deploy-ontology] schema          : {schema_name}")
    click.echo(f"[deploy-ontology] ontology name   : {ontology_name}")
    if ontology_item_id:
        click.echo(f"[deploy-ontology] ontology item id: {ontology_item_id} (configured)")
    click.echo(f"[deploy-ontology] mock mode       : {use_mock}")
    click.echo(f"[deploy-ontology] recreate        : {recreate}")
    if legacy_ontology_ids:
        click.echo(
            f"[deploy-ontology] legacy Ontology IDs: {list(legacy_ontology_ids)}"
        )
    click.echo(f"[deploy-ontology] multitype       : {multitype}")

    # Materialize the sealed H2 plan before any Ontology mutation. The legacy
    # --multitype path remains available for projects without semantic authority.
    mt_plan = None
    semantic_loaded = None
    semantic_materialization = {}
    if semantic_dir:
        if multitype:
            raise click.ClickException(
                "--semantic-dir and legacy --multitype are mutually exclusive."
            )
        if not parquet_dir:
            raise click.ClickException(
                "--semantic-dir requires --parquet-dir so contract-owned "
                "tables can be materialized before Ontology mutation."
            )
        from fabric_kg_builder.semantic import (  # noqa: PLC0415
            load_semantic_model_artifacts,
            materialize_semantic_tables,
        )

        semantic_loaded = load_semantic_model_artifacts(semantic_dir)
        parts = _load_compiled_ontology_parts(dist_path)
        if not parts:
            raise click.ClickException(
                "--semantic-dir requires compiler-owned Ontology parts under "
                f"{Path(dist_path) / 'definition.json'}."
            )
        semantic_materialization = materialize_semantic_tables(
            parquet_dir=Path(parquet_dir),
            plan=semantic_loaded.materialization_plan,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema_name,
            mock=use_mock,
        )
        click.echo(
            "[deploy-ontology] semantic manifest : "
            f"{semantic_loaded.manifest.manifest_hash}"
        )
        click.echo(
            "[deploy-ontology] contract tables  : "
            f"{len(semantic_materialization)}/"
            f"{len(semantic_loaded.materialization_plan.entity_tables) + len(semantic_loaded.materialization_plan.relationship_tables)} "
            f"{'planned' if use_mock else 'written'}"
        )
        click.echo(
            "[deploy-ontology] definition source : "
            f"{Path(dist_path) / 'definition.json'}"
        )
    elif multitype:
        if not parquet_dir:
            click.echo(
                "[deploy-ontology] ERROR: --multitype requires --parquet-dir "
                "(directory with entities.parquet / relationships.parquet).",
                err=True,
            )
            sys.exit(1)
        from pathlib import Path as _Path  # noqa: PLC0415

        from fabric_kg_builder.deploy.onelake_multitype import (  # noqa: PLC0415
            materialize_multitype_tables,
        )
        from fabric_kg_builder.ontology.fabric_def import (  # noqa: PLC0415
            build_multitype_ontology_parts,
        )
        from fabric_kg_builder.ontology.multitype_plan import (  # noqa: PLC0415
            build_plan,
            get_type_profile,
        )

        pdir = _Path(parquet_dir)
        if not (
            (pdir / "entities.parquet").exists()
            or (pdir / "semantic_entities.parquet").exists()
        ):
            click.echo(
                "[deploy-ontology] ERROR: neither entities.parquet nor "
                f"semantic_entities.parquet was found in {pdir}",
                err=True,
            )
            sys.exit(1)

        core_types = get_type_profile(type_profile) if type_profile else None
        mt_plan = build_plan(
            pdir,
            core_types=core_types,
            min_pair_count=min_pair_count,
        )
        click.echo(
            f"[deploy-ontology] type source          : "
            f"{type_profile or 'observed entity_type values'}"
        )
        click.echo(
            f"[deploy-ontology] planned entity types : {len(mt_plan.entity_types)} "
            f"-> {mt_plan.type_names}"
        )
        click.echo(
            f"[deploy-ontology] planned relationships : {len(mt_plan.relationship_pairs)} "
            f"-> {[r.name for r in mt_plan.relationship_pairs]}"
        )

        # Generate the Data Agent grounding doc from the live plan + domain brief.
        if create_agent_instruction:
            _write_agent_instructions(
                mt_plan=mt_plan,
                ontology_name=ontology_name,
                parquet_dir=pdir,
                out_path=agent_instruction_out,
                domain_brief_path=domain_brief_path,
            )

        # Materialize per-type tables to OneLake (planned only in mock mode).
        mat = materialize_multitype_tables(
            parquet_dir=pdir,
            plan=mt_plan,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema_name,
            mock=use_mock,
        )
        ok = sum(1 for v in mat.values() if v in ("ok", "planned"))
        click.echo(
            f"[deploy-ontology] per-type tables : {ok}/{len(mat)} "
            f"{'planned' if use_mock else 'written'}"
        )
        errs = {k: v for k, v in mat.items() if str(v).startswith("error")}
        if errs:
            click.echo(f"[deploy-ontology] ERROR materializing tables: {errs}", err=True)
            sys.exit(1)

        parts = build_multitype_ontology_parts(
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            entity_types=[
                {"type_name": e.type_name, "table_name": e.table_name}
                for e in mt_plan.entity_types
            ],
            relationship_pairs=[
                {
                    "name": r.name,
                    "source_type": r.source_type,
                    "target_type": r.target_type,
                    "table_name": r.table_name,
                }
                for r in mt_plan.relationship_pairs
            ],
            schema=schema_name,
            ontology_name=ontology_name,
        )
    else:
        parts = _load_compiled_ontology_parts(dist_path)
        if parts:
            click.echo(
                "[deploy-ontology] definition source : "
                f"{Path(dist_path) / 'definition.json'}"
            )
        else:
            # Compatibility path for projects that have not adopted the shared
            # semantic compiler yet.
            parts = build_ontology_parts(
                workspace_id=workspace_id,
                lakehouse_item_id=lakehouse_item_id,
                schema=schema_name,
                ontology_name=ontology_name,
            )
    parts_count = len(parts)
    ontology_manifest_path = Path(dist_path) / "ontology-manifest.json"
    ontology_manifest = _load_json_object(ontology_manifest_path)
    ontology_definition_path = Path(dist_path) / "definition.json"
    entity_type_names = [
        p["payload_json"].get("name")
        for p in parts
        if "EntityTypes" in p["path"] and p["path"].endswith("definition.json")
    ]
    rel_type_names = [
        p["payload_json"].get("name")
        for p in parts
        if "RelationshipTypes" in p["path"] and p["path"].endswith("definition.json")
    ]

    # Load model for identity mappings and date/key validation
    _model = _load_model_yaml()

    # Model-level identity and partial-date pre-deployment validation (OKV-001 / OKV-002).
    # Fires without requiring a manual datePrecision annotation.
    if _model:
        from fabric_kg_builder.ontology.identity_validation import (  # noqa: PLC0415
            validate_identity,
        )
        _okv_violations = validate_identity(_model)
        _okv_errors = [v for v in _okv_violations if v.severity == "error"]
        if _okv_errors:
            for _v in _okv_errors:
                click.echo(
                    f"[deploy-ontology] [{_v.gate_id} ERROR] {_v.message}", err=True
                )
            click.echo(
                "[deploy-ontology] ERROR: Ontology identity validation failed — "
                "fix the model.yaml violations before deploying.",
                err=True,
            )
            sys.exit(5)

    # Partial date pre-deployment validation (when parquet data is available)
    if parquet_dir and _model:
        _date_errors = _validate_parquet_date_precision(Path(parquet_dir), _model)
        if _date_errors:
            for _err in _date_errors:
                click.echo(f"[deploy-ontology] PARTIAL_DATE_INCOMPATIBLE: {_err}", err=True)
            click.echo(
                "[deploy-ontology] ERROR: Partial date incompatibility detected — "
                "fix the property type in model.yaml before deploying.",
                err=True,
            )
            sys.exit(5)

    # Build entity and relationship identity mappings for dry-run output
    from fabric_kg_builder.ontology.compiler import (  # noqa: PLC0415
        resolve_entity_identity_columns,
    )
    _entity_identity = resolve_entity_identity_columns(_model) if _model else {}
    _rel_identity: dict[str, dict] = {}
    for _rt in _model.get("relationshipTypes", []):
        _rt_name = str(_rt.get("name", ""))
        _rt_db = _rt.get("dataBinding", {}) or {}
        _rel_identity[_rt_name] = {
            "table": str(_rt_db.get("table", "")),
            "source_type": str(_rt.get("sourceType") or ""),
            "source_fk_column": str(_rt_db.get("sourceEntityIdColumn") or ""),
            "target_type": str(_rt.get("targetType") or ""),
            "target_fk_column": str(_rt_db.get("targetEntityIdColumn") or ""),
        }

    click.echo(f"[deploy-ontology] parts built     : {parts_count}")
    click.echo(f"[deploy-ontology] entity types    : {entity_type_names}")
    click.echo(f"[deploy-ontology] relationship types: {rel_type_names}")


    if use_mock:
        click.echo("")
        click.echo("-" * 60)
        click.echo("[deploy-ontology] MOCK DEPLOY -- no network call made")
        click.echo(f"  Would create/get Ontology item : {ontology_name}")
        click.echo(f"  Would call updateDefinition    : {parts_count} parts")
        click.echo(f"  Workspace                      : {workspace_id}")
        click.echo(f"  Entity types                   : {entity_type_names}")
        click.echo(f"  Relationship types             : {rel_type_names}")
        for p in parts:
            click.echo(f"    part: {p['path']}")

        # Identity mappings
        if _entity_identity:
            click.echo("")
            click.echo("[deploy-ontology] ENTITY IDENTITY MAPPINGS")
            for _ent_name, (_tbl, _id_col) in _entity_identity.items():
                click.echo(f"  {_ent_name}: table={_tbl}, identity_column={_id_col}")
        if _rel_identity:
            click.echo("")
            click.echo("[deploy-ontology] RELATIONSHIP IDENTITY MAPPINGS")
            for _rel_name, _rinfo in _rel_identity.items():
                click.echo(
                    f"  {_rel_name}: {_rinfo['source_type']}."
                    f"{_rinfo['source_fk_column']} → "
                    f"{_rinfo['target_type']}.{_rinfo['target_fk_column']} "
                    f"(table={_rinfo['table']})"
                )

        click.echo("-" * 60)

        # Mock item creation
        item_result = (
            {
                "item_id": ontology_item_id,
                "created": False,
                "note": "MOCK: would use configured Ontology item.",
            }
            if ontology_item_id
            else create_or_get_ontology_item(
                workspace_id=workspace_id,
                name=ontology_name,
                mock=True,
            )
        )
        click.echo(
            f"[deploy-ontology] Ontology item '{ontology_name}' : {item_result['item_id']}"
        )

        # Mock updateDefinition
        upd_result = update_ontology_definition(
            workspace_id=workspace_id,
            ontology_item_id=item_result["item_id"],
            parts=parts,
            mock=True,
        )
        _write_receipt(
            receipt_out,
            {
                "schema": "fabric-kg.ontology-deployment.v1",
                "environment": env,
                "workspace_id": workspace_id,
                "lakehouse_item_id": lakehouse_item_id,
                "schema_name": schema_name,
                "ontology_item_id": item_result["item_id"],
                "ontology_name": ontology_name,
                "created": item_result["created"],
                "definition_status": upd_result["status"],
                "parts_count": parts_count,
                "semantic_contract_hash": ontology_manifest.get(
                    "contract_hash"
                ),
                "ontology_manifest_hash": _sha256_file(
                    ontology_manifest_path
                ),
                "ontology_definition_hash": _sha256_file(
                    ontology_definition_path
                ),
                "semantic_model_manifest_hash": (
                    semantic_loaded.manifest.manifest_hash
                    if semantic_loaded is not None
                    else None
                ),
                "materialized_tables": {
                    table_name: evidence.status
                    for table_name, evidence
                    in semantic_materialization.items()
                },
                "mock": True,
            },
        )
        click.echo(f"[deploy-ontology] updateDefinition (mock): {upd_result['note']}")
        click.echo("[deploy-ontology] Done. Exit 0.")
        return

    # --- LIVE DEPLOY ---
    for legacy_ontology_id in dict.fromkeys(legacy_ontology_ids):
        if legacy_ontology_id == ontology_item_id and recreate:
            continue
        click.echo(
            f"[deploy-ontology] LIVE: deleting explicit legacy Ontology "
            f"'{legacy_ontology_id}' ..."
        )
        try:
            delete_ontology_item(
                workspace_id,
                legacy_ontology_id,
                _lro_timeout_s=poll_timeout,
            )
        except PermissionError as exc:
            click.echo(f"[deploy-ontology] AUTH ERROR: {exc}", err=True)
            sys.exit(6)
        except RuntimeError as exc:
            click.echo(f"[deploy-ontology] ERROR: {exc}", err=True)
            sys.exit(1)
    if recreate:
        if not ontology_item_id:
            click.echo(
                "[deploy-ontology] ERROR: --recreate requires a configured ontology_item_id.",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"[deploy-ontology] LIVE: deleting configured Ontology '{ontology_name}' ..."
        )
        try:
            delete_ontology_item(
                workspace_id,
                ontology_item_id,
                _lro_timeout_s=poll_timeout,
            )
        except PermissionError as exc:
            click.echo(f"[deploy-ontology] AUTH ERROR: {exc}", err=True)
            sys.exit(6)
        except RuntimeError as exc:
            click.echo(f"[deploy-ontology] ERROR: {exc}", err=True)
            sys.exit(1)
        ontology_item_id = ""
    if ontology_item_id:
        click.echo(
            f"[deploy-ontology] LIVE: fetching metadata for configured Ontology "
            f"item '{ontology_item_id}' ..."
        )
        try:
            _cfg_display_name = get_ontology_item_display_name(
                workspace_id,
                ontology_item_id,
            )
        except PermissionError as exc:
            click.echo(
                f"[deploy-ontology] AUTH ERROR fetching Ontology item metadata: {exc}",
                err=True,
            )
            sys.exit(6)
        except RuntimeError as exc:
            click.echo(
                f"[deploy-ontology] ERROR fetching Ontology item metadata: {exc}",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"[deploy-ontology] LIVE: using configured Ontology item "
            f"'{_cfg_display_name}' (id={ontology_item_id}) ..."
        )
        item_result = {
            "item_id": ontology_item_id,
            "created": False,
            "display_name": _cfg_display_name,
            "note": (
                f"Using configured Ontology item "
                f"(fetched display_name='{_cfg_display_name}')."
            ),
        }
    else:
        click.echo(
            f"[deploy-ontology] LIVE: creating/getting Ontology item '{ontology_name}' ..."
        )
        item_result = create_or_get_ontology_item(
            workspace_id=workspace_id,
            name=ontology_name,
            mock=False,
            _lro_timeout_s=poll_timeout,
            _create_retry_timeout_s=poll_timeout,
        )

    item_id = item_result["item_id"]
    action = "CREATED" if item_result["created"] else "REUSED"
    click.echo(f"[deploy-ontology] {action} Ontology item '{ontology_name}'")
    click.echo(f"[deploy-ontology] item_id : {item_id}")

    # Read-back name validation: the deployed item must match the manifest name.
    # Only validate when display_name is known (absent means GET was unavailable).
    if not use_mock and item_id and item_id != "MOCK_ITEM_ID_00000000":
        _deployed_name = item_result.get("display_name")
        if _deployed_name:
            try:
                validate_readback_name("Ontology", _deployed_name, deployment_manifest)
            except NameAuthorityConflict as exc:
                click.echo(
                    f"[deploy-ontology] ERROR: {exc}",
                    err=True,
                )
                sys.exit(1)

    if recreate:
        _persist_ontology_item_id(env, item_id)
        click.echo("[deploy-ontology] persisted replacement ontology_item_id")

    # Push the REAL Fabric format to populate the graph
    click.echo(
        f"[deploy-ontology] LIVE: calling updateDefinition ({parts_count} parts) ..."
    )
    upd_result = update_ontology_definition(
        workspace_id=workspace_id,
        ontology_item_id=item_id,
        parts=parts,
        mock=False,
        _lro_timeout_s=poll_timeout,
    )

    # Post-deployment read-back: node and edge counts
    graph_counts: dict = {}
    if lakehouse_item_id:
        from fabric_kg_builder.deploy.fabric_ontology import (  # noqa: PLC0415
            read_graph_counts,
        )
        click.echo("[deploy-ontology] LIVE: reading back graph counts ...")
        try:
            graph_counts = read_graph_counts(
                workspace_id=workspace_id,
                lakehouse_item_id=lakehouse_item_id,
                schema=schema_name,
            )
        except ImportError as exc:
            click.echo(
                f"[deploy-ontology] ERROR: cannot load graph read-back dependency: {exc}",
                err=True,
            )
            sys.exit(1)
        # read_graph_counts returns total_edges=-1 / total_nodes=-1 when internal
        # table reads fail.  Treat incomplete read-back as a hard deployment error so
        # zero-edge validation cannot be silently skipped.
        if graph_counts.get("total_edges", -1) < 0 or graph_counts.get("total_nodes", -1) < 0:
            click.echo(
                "[deploy-ontology] ERROR: post-deployment graph count read-back failed "
                f"(counts: nodes={graph_counts.get('total_nodes')}, "
                f"edges={graph_counts.get('total_edges')}). "
                "Cannot verify zero-edge validation — aborting publication.",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"[deploy-ontology] graph counts: {graph_counts['note']}"
        )
        click.echo(
            f"[deploy-ontology] nodes by type: {graph_counts['nodes_by_type']}"
        )
        click.echo(
            f"[deploy-ontology] edges by type: {graph_counts['edges_by_type']}"
        )
        # Zero-edge detection: fail if any declared relationship type has no edges
        if _model:
            _zero_errors = _check_zero_edge_types(
                _model,
                graph_counts["edges_by_type"],
                graph_counts["total_edges"],
            )
            if _zero_errors:
                for _zerr in _zero_errors:
                    click.echo(f"[deploy-ontology] ERROR: {_zerr}", err=True)
                click.echo(
                    "[deploy-ontology] ERROR: Disconnected ontology detected "
                    "— aborting publication. Populate the relationships table "
                    "or remove zero-edge types from the model.",
                    err=True,
                )
                sys.exit(1)

    ontology_persisted_evidence = None
    bound_table_counts: dict[str, int] = {}
    if semantic_loaded is not None:
        from fabric_kg_builder.deploy.fabric_ontology import (  # noqa: PLC0415
            get_ontology_definition,
        )
        from fabric_kg_builder.semantic import (  # noqa: PLC0415
            PersistedProjectionError,
            validate_bound_tables,
            validate_persisted_ontology,
        )
        from fabric_kg_builder.serving.competency import (  # noqa: PLC0415
            OneLakeDeltaClient,
        )

        try:
            persisted_definition = get_ontology_definition(
                workspace_id,
                item_id,
                _lro_timeout_s=poll_timeout,
            )
            ontology_persisted_evidence = validate_persisted_ontology(
                definition=persisted_definition,
                manifest=semantic_loaded.manifest,
                plan=semantic_loaded.materialization_plan,
                workspace_id=workspace_id,
                lakehouse_item_id=lakehouse_item_id,
                schema=schema_name,
            )
            bound_table_counts = validate_bound_tables(
                plan=semantic_loaded.materialization_plan,
                workspace_id=workspace_id,
                lakehouse_item_id=lakehouse_item_id,
                schema=schema_name,
                table_reader=OneLakeDeltaClient(),
            )
        except (
            PersistedProjectionError,
            PermissionError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise click.ClickException(
                "Persisted Ontology readiness failed after updateDefinition: "
                f"{exc}"
            ) from exc
        click.echo(
            "[deploy-ontology] persisted projection: "
            f"{ontology_persisted_evidence.projection_hash}"
        )
        click.echo(
            "[deploy-ontology] bound tables       : "
            f"{len(bound_table_counts)} validated"
        )
    _write_receipt(
        receipt_out,
        {
            "schema": "fabric-kg.ontology-deployment.v1",
            "environment": env,
            "workspace_id": workspace_id,
            "lakehouse_item_id": lakehouse_item_id,
            "schema_name": schema_name,
            "ontology_item_id": item_id,
            "ontology_name": ontology_name,
            "created": item_result["created"],
            "definition_status": upd_result["status"],
            "operation_location": (
                upd_result.get("location")
                or item_result.get("operation_location")
                or ""
            ),
            "parts_count": parts_count,
            "semantic_contract_hash": ontology_manifest.get("contract_hash"),
            "ontology_manifest_hash": _sha256_file(
                ontology_manifest_path
            ),
            "ontology_definition_hash": _sha256_file(
                ontology_definition_path
            ),
            "semantic_model_manifest_hash": (
                semantic_loaded.manifest.manifest_hash
                if semantic_loaded is not None
                else None
            ),
            "ontology_persisted_projection_hash": (
                ontology_persisted_evidence.projection_hash
                if ontology_persisted_evidence is not None
                else None
            ),
            "ontology_definition_counts": (
                ontology_persisted_evidence.definition_counts
                if ontology_persisted_evidence is not None
                else {}
            ),
            "bound_table_counts": bound_table_counts,
            "graph_counts": {
                "total_nodes": graph_counts.get("total_nodes", -1),
                "total_edges": graph_counts.get("total_edges", -1),
                "nodes_by_type": graph_counts.get("nodes_by_type", {}),
                "edges_by_type": graph_counts.get("edges_by_type", {}),
            },
            "materialized_tables": {
                table_name: {
                    "status": evidence.status,
                    "row_count": evidence.row_count,
                    "source_path": evidence.source_path,
                }
                for table_name, evidence
                in semantic_materialization.items()
            },
            "mock": False,
        },
    )
    click.echo(f"[deploy-ontology] updateDefinition status : {upd_result['status']}")
    click.echo(f"[deploy-ontology] {upd_result['note']}")
    click.echo("[deploy-ontology] Done. Exit 0.")


_DEPLOY_SEARCH_EPILOG = """\b
Example:
  fabric-kg deploy-search --env dev --mock
  fabric-kg deploy-search --env dev --no-mock
  fabric-kg deploy-search --env dev --indexes kg-chunks --no-mock
  fabric-kg deploy-search --env dev --indexes kg-chunks --integrated-vectorization --no-mock

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("deploy-search", epilog=_DEPLOY_SEARCH_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=str,
              help="Target deployment environment (reads ontology/environments/{env}.json).")
@click.option("--dist", "dist_path", default="build/search", show_default=True,
              type=click.Path(),
              help="Path to build/search/ directory (output of compile-search).")
@click.option("--indexes", default=None, show_default=True,
              help="Comma-separated subset of indexes to deploy "
                   "(default: kg-chunks,kg-document-elements,kg-visual-assets).")
@click.option("--recreate", is_flag=True, default=False,
              help="Drop and recreate the index before pushing (caution: loses all documents).")
@click.option("--integrated-vectorization", is_flag=True, default=False,
              help="Recommended for large runs: stage Blob JSON and use an Azure AI Search "
                   "indexer + AzureOpenAIEmbeddingSkill instead of direct client uploads.")
@click.option("--mock/--no-mock", "use_mock", default=False, show_default=True,
              help="Mock mode: log planned actions without any network call. "
                   "Use --no-mock for a live deploy (default).")
@click.option("--manifest", "manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority). "
                   "Defaults to env-config names (legacy mode).")
def deploy_search_cmd(
    env: str,
    dist_path: str,
    indexes: str | None,
    recreate: bool,
    integrated_vectorization: bool,
    use_mock: bool,
    manifest_path: str | None,
) -> None:
    """Upload AI Search index schemas and document batches to Azure AI Search.

    Reads ai_search.endpoint, ai_search.index_prefix, and ai_search.enabled
    from ontology/environments/{env}.json.  Authenticates with
    DefaultAzureCredential (az login for dev, SPN for CI/prod).

    By default, PUTs each index schema then batch-uploads docs.json via the
    Azure AI Search REST API. For large runs, prefer
    --integrated-vectorization: it stages JSON in Blob Storage and creates the
    Search data source, embedding skillset, and indexer. The indexer can keep
    running after the CLI polling window, so check Azure Search indexer status
    before retrying. Skips silently if ai_search.enabled=false in env config.

    Exit codes: 0 success · 1 error · 6 auth failure.
    """
    # Lazy import so offline / no-SDK environments still work for mock mode
    try:
        from fabric_kg_builder.search.push import push_from_build_dir
    except ImportError as exc:
        click.echo(f"[deploy-search] ERROR: cannot import search.push: {exc}", err=True)
        raise SystemExit(1) from exc

    # Read env config (fabric + ai_search sections — no secrets needed for mock)
    try:
        env_cfg = _read_search_env_config(env)
    except FileNotFoundError as exc:
        click.echo(f"[deploy-search] ERROR: {exc}", err=True)
        raise SystemExit(1) from exc

    ai_search = env_cfg.get("ai_search", {})
    enabled: bool = ai_search.get("enabled", True)
    service_name: str = ai_search.get("service_name", "")
    endpoint: str = ai_search.get("endpoint", "")
    index_prefix: str = ai_search.get("index_prefix", "")
    index_chunks: str = ai_search.get("index_chunks", "kg-chunks")
    index_doc_elements: str = ai_search.get("index_document_elements", "kg-document-elements")
    index_visual_assets: str = ai_search.get("index_visual_assets", "kg-visual-assets")

    # --- Name authority: resolve SearchIndex prefix/name from manifest ---
    _fabric_cfg_for_manifest = env_cfg.get("fabric", {})
    deployment_manifest = _load_or_synthesize_manifest(manifest_path, _fabric_cfg_for_manifest)
    try:
        resolved_search = resolve_item_name(deployment_manifest, "SearchIndex")
    except NameAuthorityConflict as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if use_mock:
        click.echo(render_name_resolution(resolved_search))

    _all_indexes = {
        "kg-chunks": f"{index_prefix}{index_chunks}",
        "kg-document-elements": f"{index_prefix}{index_doc_elements}",
        "kg-visual-assets": f"{index_prefix}{index_visual_assets}",
    }


    selected_names = (
        [i.strip() for i in indexes.split(",") if i.strip()]
        if indexes
        else list(_all_indexes.keys())
    )

    click.echo(f"[deploy-search] Environment  : {env}")
    click.echo(f"[deploy-search] Service      : {service_name or '(not set)'}")
    click.echo(f"[deploy-search] Endpoint     : {endpoint or '(not set)'}")
    click.echo(f"[deploy-search] Index prefix : {index_prefix or '(none)'}")
    click.echo(f"[deploy-search] AI Search enabled: {enabled}")
    click.echo(f"[deploy-search] Recreate index: {recreate}")
    click.echo(f"[deploy-search] Integrated vectorization: {integrated_vectorization}")

    if not enabled:
        click.echo(
            "[deploy-search] ai_search.enabled=false — skipping deploy. Exit 0."
        )
        return

    build_dir = Path(dist_path)
    if not build_dir.exists():
        click.echo(
            f"[deploy-search] WARNING: build dir {build_dir} does not exist. "
            "Run 'fabric-kg compile-search' first.",
            err=True,
        )

    total_docs = 0
    any_error = False

    for base_name in selected_names:
        if base_name not in _all_indexes:
            click.echo(
                f"[deploy-search] WARNING: unknown index '{base_name}', skipping.",
                err=True,
            )
            continue

        deployed_name = _all_indexes[base_name]
        index_dir = build_dir / base_name
        docs_path = index_dir / "docs.json"
        schema_path = index_dir / "index.schema.json"

        doc_count = 0
        if docs_path.exists():
            try:
                docs_raw = json.loads(docs_path.read_text(encoding="utf-8"))
                doc_count = len(docs_raw) if isinstance(docs_raw, list) else 0
            except Exception:
                doc_count = 0

        total_docs += doc_count

        if use_mock:
            click.echo(
                f"[deploy-search]   WOULD push index={deployed_name!r}, "
                f"docs={doc_count}, recreate={recreate}"
            )
            try:
                schema_result, docs_result = push_from_build_dir(
                    build_dir,
                    base_name,
                    deployed_name,
                    endpoint=endpoint,
                    mock=True,
                )
                click.echo(f"[deploy-search]     schema: {schema_result}")
                click.echo(f"[deploy-search]     docs  : {docs_result}")
            except FileNotFoundError:
                click.echo(
                    f"[deploy-search]   NOTE: {base_name}/index.schema.json not found; "
                    "run compile-search first."
                )
        else:
            # --- LIVE deploy ---
            from fabric_kg_builder.deploy.search_deployer import (  # noqa: PLC0415
                _get_token,
                deploy_index,
            )

            if not endpoint:
                click.echo(
                    "[deploy-search] ERROR: ai_search.endpoint not set in env config.",
                    err=True,
                )
                raise SystemExit(1)

            if not schema_path.exists():
                click.echo(
                    f"[deploy-search] ERROR: {schema_path} not found — "
                    "run 'fabric-kg compile-search' first.",
                    err=True,
                )
                any_error = True
                continue

            schema_dict = json.loads(schema_path.read_text(encoding="utf-8"))
            docs: list[dict] = []
            if docs_path.exists():
                docs = json.loads(docs_path.read_text(encoding="utf-8"))

            iv_cfg: dict = {}
            integrated_config = None
            if integrated_vectorization and base_name != "kg-chunks":
                click.echo(
                    "[deploy-search] ERROR: --integrated-vectorization currently "
                    "supports kg-chunks only.",
                    err=True,
                )
                any_error = True
                continue
            if integrated_vectorization:
                iv_cfg = ai_search.get("integrated_vectorization") or {}
                required = (
                    "source_container",
                    "source_path",
                    "storage_resource_id",
                    "azure_openai_endpoint",
                    "azure_openai_deployment",
                )
                missing = [key for key in required if not iv_cfg.get(key)]
                if missing:
                    click.echo(
                        "[deploy-search] ERROR: ai_search.integrated_vectorization "
                        f"is missing: {', '.join(missing)}",
                        err=True,
                    )
                    any_error = True
                    continue
                if not (index_dir / "source-docs.json").is_file():
                    click.echo(
                        "[deploy-search] ERROR: integrated-vectorization source file "
                        f"is missing: {index_dir / 'source-docs.json'}. "
                        "Run 'fabric-kg compile-search' first.",
                        err=True,
                    )
                    any_error = True
                    continue
                from fabric_kg_builder.search.integrated_vectorization import (  # noqa: PLC0415
                    IntegratedVectorizationConfig,
                    stage_source_documents,
                )

                try:
                    integrated_config = IntegratedVectorizationConfig(**iv_cfg)
                    stage_result = stage_source_documents(
                        index_dir / "source-docs.json",
                        config=integrated_config,
                        vector_fields={"chunk_vector"},
                        prepared=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    click.echo(
                        f"[deploy-search] ERROR staging integrated vectorization "
                        f"source for {deployed_name}: {exc}",
                        err=True,
                    )
                    any_error = True
                    continue
                click.echo(
                    f"[deploy-search]   Staged {stage_result.byte_count} bytes to "
                    f"{stage_result.container}/{stage_result.blob_name}."
                )

            click.echo(
                f"[deploy-search]   Pushing index={deployed_name!r}, "
                f"docs={0 if integrated_vectorization else len(docs)}, recreate={recreate}"
            )
            try:
                result = deploy_index(
                    endpoint=endpoint,
                    index_name=deployed_name,
                    schema_dict=schema_dict,
                    docs=[] if integrated_vectorization else docs,
                    recreate=recreate,
                    mock=False,
                )
            except Exception as exc:  # noqa: BLE001
                click.echo(
                    f"[deploy-search] ERROR pushing {deployed_name}: {exc}", err=True
                )
                any_error = True
                continue

            if result.get("errors"):
                for err in result["errors"]:
                    click.echo(f"[deploy-search]   ERROR: {err}", err=True)
                any_error = True
            else:
                if integrated_vectorization:
                    from fabric_kg_builder.search.integrated_vectorization import (  # noqa: PLC0415
                        deploy_integrated_vectorization,
                    )

                    vector_field = "chunk_vector"
                    try:
                        iv_result = deploy_integrated_vectorization(
                            endpoint=endpoint,
                            index_name=deployed_name,
                            schema=schema_dict,
                            vector_field=vector_field,
                            config=integrated_config,
                            token_provider=_get_token,
                        )
                    except Exception as exc:  # noqa: BLE001
                        click.echo(
                            f"[deploy-search] ERROR running integrated vectorization "
                            f"for {deployed_name}: {exc}",
                            err=True,
                        )
                        any_error = True
                        continue
                    click.echo(
                        f"[deploy-search]   OK — indexer {iv_result.indexer_name!r} "
                        f"completed with status {iv_result.status}."
                    )
                    continue
                click.echo(
                    f"[deploy-search]   OK — schema pushed, "
                    f"{result.get('docs_pushed', 0)} docs uploaded."
                )

    if any_error:
        click.echo("[deploy-search] FAILED — one or more errors above.", err=True)
        raise SystemExit(1)

    if use_mock:
        click.echo(
            f"[deploy-search] SUCCESS (mock) — {len(selected_names)} index(es), "
            f"{total_docs} total docs."
        )
    else:
        click.echo(
            f"[deploy-search] SUCCESS — {len(selected_names)} index(es) deployed."
        )


# ---------------------------------------------------------------------------
# deploy-graph — refresh an authoritative Graph Model without Search cutover
# ---------------------------------------------------------------------------

_DEPLOY_GRAPH_EPILOG = """\b
Example:
  fabric-kg deploy-graph --env dev --parquet-dir build\\parquet --dry-run
  fabric-kg deploy-graph --env dev --graph-definition-file build\\graph\\graph-definition.json --dry-run
  fabric-kg deploy-graph --env dev --parquet-dir build\\parquet --no-dry-run \
    --graph-preview-acknowledged

Refreshes the configured Graph Model from a compiled graph-definition.json or
from semantic_entities and semantic_relationships. It does not create or modify
AI Search indexes, Ontologies, or Data Agents.
"""


@click.command("deploy-graph", epilog=_DEPLOY_GRAPH_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=str,
              help="Target environment (reads ontology/environments/{env}.json).")
@click.option("--parquet-dir", default="build/parquet", type=click.Path(),
              show_default=True, help="Directory containing compiled Parquet tables.")
@click.option("--graph-artifact-out", default="build/graph", type=click.Path(),
              show_default=True, help="Directory for the Graph Model mapping artifact.")
@click.option(
    "--graph-definition-file",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Deploy an already compiled graph-definition.json instead of rebuilding from Parquet.",
)
@click.option(
    "--label-catalog-file",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Label catalogue paired with --graph-definition-file; defaults to its sibling label-catalog.json.",
)
@click.option("--graph-preview-acknowledged", is_flag=True, default=False,
              help="Acknowledge use of the Fabric GraphModel preview API.")
@click.option("--recreate", is_flag=True, default=False,
              help="Ignore a stale configured Graph Model ID, create or reuse the "
                   "named model, and persist its current ID.")
@click.option("--replace", is_flag=True, default=False,
                   help="Delete the configured Graph Model, create a replacement, and "
                   "persist its new ID.")
@click.option("--dry-run/--no-dry-run", default=False, show_default=True,
              help="Show the Graph Model update without calling Fabric.")
@click.option("--manifest", "manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority). "
                   "Defaults to env-config names (legacy mode).")
def deploy_graph_cmd(
    env: str,
    parquet_dir: str,
    graph_artifact_out: str,
    graph_definition_file: Path | None,
    label_catalog_file: Path | None,
    graph_preview_acknowledged: bool,
    recreate: bool,
    replace: bool,
    dry_run: bool,
    manifest_path: str | None,
) -> None:
    """Refresh the configured Fabric Graph Model from semantic serving tables."""
    try:
        fabric_cfg = _read_fabric_env_config(env)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    workspace_id = fabric_cfg["workspace_id"]
    lakehouse_item_id = fabric_cfg["lakehouse_item_id"]
    graph_model_id = fabric_cfg["graph_model_id"]
    if not workspace_id or not lakehouse_item_id or (not graph_model_id and not recreate):
        raise click.ClickException(
            "fabric.workspace_id and lakehouse_item_id must be configured. "
            "Configure graph_model_item_id or use --recreate."
        )
    if not dry_run and not graph_preview_acknowledged:
        raise click.ClickException(
            "Live GraphModel deployment requires --graph-preview-acknowledged."
        )
    if recreate and replace:
        raise click.ClickException("Use either --recreate or --replace, not both.")

    from fabric_kg_builder.serving.graph_model import (
        build_graph_model_parts,
        create_or_get_graph_model,
        delete_graph_model,
        extract_entity_types_from_parquet,
        extract_relationship_pairs_from_parquet,
        validate_graph_data_source_paths,
        write_graph_mapping_artifact,
    )

    if graph_definition_file is not None:
        graph_artifact = _load_json_object(graph_definition_file)
        parts = graph_artifact.get("parts")
        if not isinstance(parts, list) or not parts:
            raise click.ClickException(
                f"{graph_definition_file} does not contain Graph definition parts."
            )
        catalog_path = label_catalog_file or graph_definition_file.with_name(
            "label-catalog.json"
        )
        if not catalog_path.is_file():
            raise click.ClickException(
                "Compiled Graph deployment requires label-catalog.json."
            )
        _load_json_object(catalog_path)
        try:
            validate_graph_data_source_paths(parts)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        graph_type = next(
            (
                part.get("payload_json", {})
                for part in parts
                if Path(str(part.get("path", ""))).name == "graphType.json"
            ),
            {},
        )
        if not isinstance(graph_type, dict):
            raise click.ClickException(
                f"{graph_definition_file} has an invalid graphType.json payload."
            )
        entity_types = [
            str(labels[0])
            for item in graph_type.get("nodeTypes", [])
            if isinstance(item, dict)
            and isinstance((labels := item.get("labels")), list)
            and labels
        ]
        relationship_pairs = [
            item
            for item in graph_type.get("edgeTypes", [])
            if isinstance(item, dict)
        ]
        entity_row_count = 0
        relationship_row_count = 0
        graph_source = str(graph_definition_file)
    else:
        source_dir = Path(parquet_dir)
        entities_path = source_dir / "semantic_entities.parquet"
        relationships_path = source_dir / "semantic_relationships.parquet"
        if not entities_path.is_file() or not relationships_path.is_file():
            raise click.ClickException(
                "semantic_entities.parquet and semantic_relationships.parquet are "
                f"required in {source_dir}. Run 'fabric-kg compile-data' first."
            )
        import pyarrow.parquet as pq  # type: ignore[import]

        entity_rows = pq.read_table(str(entities_path)).to_pylist()
        relationship_rows = pq.read_table(str(relationships_path)).to_pylist()
        entity_types = extract_entity_types_from_parquet(entity_rows)
        entities_by_id = {
            str(row["entity_id"]) for row in entity_rows if row.get("entity_id")
        }
        relationship_pairs = extract_relationship_pairs_from_parquet(
            relationship_rows,
            {entity_id: row for entity_id, row in (
                (str(row["entity_id"]), row) for row in entity_rows
                if row.get("entity_id")
            )},
            min_pair_count=3,
            max_pairs=40,
        )
        if not entity_types or not entities_by_id:
            raise click.ClickException("Semantic entity table contains no graph nodes.")
        entity_row_count = len(entity_rows)
        relationship_row_count = len(relationship_rows)
        graph_source = str(source_dir)

    graph_name = fabric_cfg["graph_model_display_name"]

    # --- Name authority: resolve GraphModel display name ---
    deployment_manifest = _load_or_synthesize_manifest(manifest_path, fabric_cfg)
    if manifest_path:
        _warn_manifest_vs_env(
            "deploy-graph", "GraphModel", deployment_manifest,
            fabric_cfg.get("graph_model_display_name"), "graph_model_display_name",
        )
    try:
        resolved_graph = resolve_item_name(deployment_manifest, "GraphModel")
    except NameAuthorityConflict as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    graph_name = resolved_graph.display_name or fabric_cfg["graph_model_display_name"]
    if dry_run:
        click.echo(render_name_resolution(resolved_graph))

    if graph_definition_file is None:
        parts = build_graph_model_parts(
            entity_types=entity_types,
            relationship_pairs=relationship_pairs,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=fabric_cfg["schema_name"],
            model_name=graph_name,
            entity_table="semantic_entities",
            relationship_table="semantic_relationships",
            include_semantic_properties=True,
        )
    write_graph_mapping_artifact(
        Path(graph_artifact_out),
        parts,
        workspace_id=workspace_id,
        lakehouse_item_id=lakehouse_item_id,
        schema=fabric_cfg["schema_name"],
        model_name=graph_name,
    )

    click.echo(
        f"[deploy-graph] GraphModel: {graph_name} ({graph_model_id})\n"
        f"[deploy-graph] Source: {graph_source}\n"
        f"[deploy-graph] Semantic rows: {entity_row_count} entities, "
        f"{relationship_row_count} relationships\n"
        f"[deploy-graph] Graph schema: {len(entity_types)} node labels, "
        f"{len(relationship_pairs)} edge bindings"
    )
    if dry_run:
        click.echo("[deploy-graph] SUCCESS (dry-run)")
        return

    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    credential = DefaultAzureCredential()
    if replace:
        click.echo(
            f"[deploy-graph] deleting configured GraphModel {graph_model_id} ..."
        )
        delete_graph_model(
            workspace_id,
            graph_model_id,
            token_provider=lambda: credential.get_token(
                "https://api.fabric.microsoft.com/.default"
            ).token,
        )
        graph_model_id = ""
    result = create_or_get_graph_model(
        workspace_id,
        graph_name,
        parts,
        graph_model_id=None if (recreate or replace) else graph_model_id,
        token_provider=lambda: credential.get_token(
            "https://api.fabric.microsoft.com/.default"
        ).token,
    )
    if recreate or replace:
        _persist_graph_model_item_id(env, result["item_id"])
        click.echo(
            f"[deploy-graph] persisted GraphModel item ID: {result['item_id']}"
        )
    click.echo(
        f"[deploy-graph] SUCCESS — definition {result['status']} "
        f"({result['parts_count']} parts)."
    )


_DEPLOY_DATA_AGENT_EPILOG = """\b
Dependency order:
  deploy-lakehouse -> deploy-ontology + deploy-graph -> validate-projection
  -> deploy-data-agent -> app deploy-agent

This command rejects serving receipts or hand-authored substitutes. Pass the
PersistedProjectionReceipt created by validate-projection. Deploy the Foundry
prompt agent only after this Data Agent is published and its Foundry connection
is recorded in .foundry/agent-metadata.yaml.

Grounding inputs:
\b
  - build/agents/instructions.md: shared routing, evidence, and answer policy
  - build/agents/semantic-context.json: exact entities, relationships, properties
  - build/agents/competency-contract.json: validated business question/GQL pairs
  - --domain-context and repeated --question values: deployment-specific context

The CLI generates Data Agent data-source instructions, compact source
descriptions, selected elements, and up to seven validated Graph few-shots from
these inputs. Do not hand-author labels that disagree with the sealed manifest.

Template to adapt before compile-agent (replace every {{...}} token):
\b
  Agent instruction:
    Answer {{DOMAIN_NAME}} questions from persisted sources only.
    Use Ontology for meaning, Graph for {{RELATIONSHIP_NAME}} traversal, and
    Search/knowledge base for document evidence. Cite evidence_id.

  Graph source description:
    Exact Graph. Nodes: {{ENTITY_NAME}}.
    Properties: {{PROPERTY_NAME_1}},{{PROPERTY_NAME_2}}.
    Source: {{SOURCE_ENTITY}}.
    Relationship: {{RELATIONSHIP_NAME}}.
    Target: {{TARGET_ENTITY}}.

  Example:
    Question: Show {{TARGET_ENTITY}} for {{SOURCE_ENTITY}}.
    GQL:
      MATCH (s:{{SOURCE_ENTITY}})
            -[r:{{RELATIONSHIP_NAME}}]->
            (t:{{TARGET_ENTITY}})
         RETURN s.{{DISPLAY_PROPERTY}}, t.{{DISPLAY_PROPERTY}}, r.evidence_id LIMIT 100

Copilot/AI agent rule:
\b
  Read semantic-context.json and competency-contract.json, replace placeholders
  with exact case-sensitive names, and never invent entity, relationship, or
  property names. If a requested property is not selected, use Search evidence
  or report that the structured source cannot answer it.

PowerShell example:
\b
  fabric-kg validate-projection --semantic-dir build\\semantic --ontology-receipt build\\release\\ontology-receipt.json --serving-receipt build\\release\\serving-receipt.json --out build\\release\\persisted-projection-receipt.json
  fabric-kg deploy-data-agent --env dev --mode create --semantic-dir build\\semantic --projection-receipt build\\release\\persisted-projection-receipt.json --agent-dir build\\agents --domain-context "Building operations" --question "Which equipment is located in the mechanical room?" --question "Which procedure applies to that equipment?" --no-dry-run
  fabric-kg app deploy-agent --env dev
"""


@click.command(
    "deploy-data-agent",
    epilog=_DEPLOY_DATA_AGENT_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option("--env", required=True, type=str,
              help="Target environment (reads ontology/environments/{env}.json).")
@click.option(
    "--mode",
    "target_mode",
    required=True,
    type=click.Choice(["update", "create", "replace"]),
    help="Exact target action; implicit name-based upsert is not allowed.",
)
@click.option(
    "--item-id",
    default=None,
    help="Exact Data Agent item ID for update or approved replace.",
)
@click.option(
    "--display-name",
    default=None,
    help="Stable Data Agent display name; defaults to environment config.",
)
@click.option(
    "--approve-replace",
    is_flag=True,
    default=False,
    help="Approve deletion and replacement of the exact configured item.",
)
@click.option(
    "--semantic-dir",
    required=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    help="Sealed semantic model directory.",
)
@click.option(
    "--projection-receipt",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="H3 persisted semantic projection receipt.",
)
@click.option(
    "--agent-dir",
    required=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    help="Packaged agent directory containing semantic-context and instructions.",
)
@click.option("--domain-context", default="")
@click.option("--question", "questions", multiple=True)
@click.option(
    "--receipt-out",
    default="build/release/agent-publication-receipt.json",
    show_default=True,
    type=click.Path(path_type=Path),
)
@click.option(
    "--definition-out",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write the exact Fabric Data Agent definition JSON for L7 planning.",
)
@click.option("--dry-run/--no-dry-run", default=False, show_default=True,
              help="Build the source-preserving Data Agent definition without updating Fabric.")
@click.option("--manifest", "manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority). "
                   "Defaults to env-config names (legacy mode).")
def deploy_data_agent_cmd(
    env: str,
    target_mode: str,
    item_id: str | None,
    display_name: str | None,
    approve_replace: bool,
    semantic_dir: Path,
    projection_receipt: Path,
    agent_dir: Path,
    domain_context: str,
    questions: tuple[str, ...],
    receipt_out: Path,
    definition_out: Path | None,
    dry_run: bool,
    manifest_path: str | None,
) -> None:
    """Publish one exact Data Agent after persisted Ontology/Graph validation."""
    try:
        fabric_cfg = _read_fabric_env_config(env)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    workspace_id = fabric_cfg["workspace_id"]
    graph_model_id = fabric_cfg["graph_model_id"]
    ontology_item_id = fabric_cfg["ontology_item_id"]
    if not workspace_id or not graph_model_id or not ontology_item_id:
        raise click.ClickException(
            "fabric.workspace_id, graph_model_item_id, and ontology_item_id "
            "must be configured."
        )
    configured_id = str(fabric_cfg.get("data_agent_item_id") or "")
    if item_id and configured_id and item_id != configured_id:
        raise click.ClickException(
            "--item-id differs from fabric.data_agent_item_id."
        )
    configured_id = str(item_id or configured_id or "").strip()
    if target_mode == "create" and configured_id:
        raise click.ClickException(
            "Create mode cannot use a configured Data Agent item ID."
        )
    if target_mode in {"update", "replace"} and not configured_id:
        raise click.ClickException(
            f"{target_mode} mode requires --item-id or configured identity."
        )
    if target_mode == "replace" and not approve_replace:
        raise click.ClickException(
            "Replace mode requires --approve-replace."
        )

    # --- Name authority: resolve DataAgent display name ---
    deployment_manifest = _load_or_synthesize_manifest(manifest_path, fabric_cfg)
    if manifest_path:
        _warn_manifest_vs_env(
            "deploy-data-agent", "DataAgent", deployment_manifest,
            fabric_cfg.get("data_agent_display_name"), "data_agent_display_name",
        )
    try:
        # If --display-name supplied, check it doesn't conflict with manifest.
        resolved_agent = resolve_item_name(
            deployment_manifest,
            "DataAgent",
            command_name=display_name or None,
        )
    except NameAuthorityConflict as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    data_agent_name = str(
        resolved_agent.display_name
        or display_name
        or fabric_cfg.get("data_agent_display_name")
        or f"fkg-{env}-data-agent"
    ).strip()
    if dry_run:
        click.echo(render_name_resolution(resolved_agent))

    if not data_agent_name:
        raise click.ClickException("Data Agent display name must not be empty.")

    from fabric_kg_builder.knowledge.agent_validation import (  # noqa: PLC0415
        AgentPublicationError,
        build_public_graph_source_projection,
        build_public_ontology_source_projection,
        build_persisted_agent_grounding,
        deploy_and_validate_data_agent,
    )
    from fabric_kg_builder.knowledge.data_agent import (  # noqa: PLC0415
        compare_graph_few_shot_semantics,
        DataAgentDefinitionError,
        DataAgentSpec,
        DataAgentTargetError,
        DataSourceSpec,
        FabricDataAgentClient,
        validate_graph_few_shot_examples,
        stage_snapshot_from_spec,
    )
    from fabric_kg_builder.knowledge.transport import RequestsTransport  # noqa: PLC0415
    from fabric_kg_builder.semantic import (  # noqa: PLC0415
        PersistedProjectionReceipt,
        build_contract_agent_instructions,
        build_graph_source_description,
        build_graph_source_instructions,
        build_ontology_source_description,
        build_ontology_source_instructions,
        load_semantic_model_artifacts,
    )
    from fabric_kg_builder.serving.graph_model import GraphModelGQLClient  # noqa: PLC0415
    # Import early so the except clause below can reference the type (#13 blocker fix).
    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        DataAgentExampleValidationFailed,
        DataAgentRequiredExampleEmpty,
    )
    _competency_contract_exists = False
    _competency_contract_payload: dict[str, Any] = {}
    _example_receipts: list[Any] = []
    _example_direct_results: dict[str, dict[str, Any]] = {}
    _example_candidate_count = 0
    credential = None
    try:
        loaded = load_semantic_model_artifacts(semantic_dir)
        persisted = PersistedProjectionReceipt.model_validate_json(
            projection_receipt.read_text(encoding="utf-8")
        )
        persisted_hash = (
            "sha256:"
            + hashlib.sha256(projection_receipt.read_bytes()).hexdigest()
        )
        semantic_context = json.loads(
            (agent_dir / "semantic-context.json").read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(semantic_context, dict):
            raise ValueError("Agent semantic context must be an object.")
        instructions = build_contract_agent_instructions(
            semantic_context,
            competency_questions=questions,
            domain_context=domain_context,
        )
        packaged_instructions = (
            agent_dir / "instructions.md"
        ).read_text(encoding="utf-8")
        if instructions != packaged_instructions:
            raise AgentPublicationError(
                "AGENT_PACKAGE_INSTRUCTION_DRIFT",
                "Post-read-back instruction differs from packaged instructions.",
            )
        package_instruction_hash = (
            "sha256:"
            + hashlib.sha256(
                packaged_instructions.encode("utf-8")
            ).hexdigest()
        )
        grounding = build_persisted_agent_grounding(
            manifest=loaded.manifest,
            crosswalk=loaded.crosswalk,
            semantic_context=semantic_context,
            projection_receipt=persisted,
            projection_receipt_hash=persisted_hash,
            workspace_id=workspace_id,
            graph_model_id=graph_model_id,
        )
        public_elements, public_metadata = (
            build_public_ontology_source_projection(grounding)
        )
        graph_elements, graph_metadata = (
            build_public_graph_source_projection(grounding)
        )
        competency_path = agent_dir / "competency-contract.json"
        _competency_contract_exists = competency_path.exists()
        _competency_contract_payload = (
            json.loads(competency_path.read_text(encoding="utf-8"))
            if _competency_contract_exists
            else {}
        )
        # Build availability dict from the persisted materialization plan for
        # capability-aware example gating (#13).
        _capability_availability = {
            item.semantic_id: item
            for item in loaded.materialization_plan.data_availability
        }
        _graph_executor = None
        if not dry_run:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]

            credential = credential or DefaultAzureCredential()
            _graph_client = GraphModelGQLClient(
                token_provider=lambda: credential.get_token(
                    "https://api.fabric.microsoft.com/.default"
                ).token,
            )
            def _graph_executor(query: str) -> dict[str, Any]:
                return _graph_client.execute_query_all_pages(
                    workspace_id,
                    graph_model_id,
                    query,
                )
        _example_validation = validate_graph_few_shot_examples(
            _competency_contract_payload,
            availability=(
                _capability_availability if _capability_availability else None
            ),
            limit=7,
            dry_run=dry_run,
            execute_graph_query=_graph_executor,
            query_schema=_competency_contract_payload.get("query_schema"),
            require_schema=_competency_contract_exists,
        )
        graph_few_shots = _example_validation.examples
        _example_receipts = _example_validation.receipts
        _example_direct_results = _example_validation.direct_results
        _example_candidate_count = _example_validation.candidate_count
        spec = DataAgentSpec(
            display_name=data_agent_name,
            instruction=instructions,
            sources=[
                DataSourceSpec(
                    source_type="ontology",
                    name=(
                        fabric_cfg["ontology_display_name"]
                        or ontology_item_id
                    ),
                    artifact_id=ontology_item_id,
                    workspace_id=workspace_id,
                    display_name=(
                        fabric_cfg["ontology_display_name"]
                        or ontology_item_id
                    ),
                    instructions=build_ontology_source_instructions(
                        semantic_context
                    ),
                    description=build_ontology_source_description(
                        semantic_context
                    ),
                    metadata=public_metadata,
                    elements=list(public_elements),
                    preview=True,
                ),
                DataSourceSpec(
                    source_type="graph",
                    name=(
                        fabric_cfg["graph_model_display_name"]
                        or graph_model_id
                    ),
                    artifact_id=graph_model_id,
                    workspace_id=workspace_id,
                    display_name=(
                        fabric_cfg["graph_model_display_name"]
                        or graph_model_id
                    ),
                    instructions=build_graph_source_instructions(
                        semantic_context,
                        availability=_capability_availability or None,
                    ),
                    description=build_graph_source_description(
                        semantic_context,
                        availability=_capability_availability or None,
                    ),
                    metadata=graph_metadata,
                    elements=list(graph_elements),
                    few_shots=graph_few_shots,
                ),
            ],
        )
        expected = stage_snapshot_from_spec(spec)
        if definition_out is not None:
            from fabric_kg_builder.knowledge.data_agent import build_definition_parts

            definition_out.parent.mkdir(parents=True, exist_ok=True)
            definition_out.write_text(
                json.dumps(
                    {"definition": {"parts": build_definition_parts(spec)}},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    except DataAgentRequiredExampleEmpty as exc:
        raise click.ClickException(str(exc)) from exc
    except DataAgentExampleValidationFailed as exc:
        raise click.ClickException(str(exc)) from exc
    except (AgentPublicationError, OSError, ValueError) as exc:
        raise click.ClickException(
            f"Data Agent grounding preparation failed: {exc}"
        ) from exc

    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        FewShotContractViolation,
        SourcePolicy,
        SourcePolicyViolation,
        TextLimitViolation,
        validate_data_agent_text,
        validate_graph_few_shots,
        validate_instruction_deduplication,
        validate_source_policy,
    )

    _data_agent_source_policy = SourcePolicy(
        required=frozenset({"ontology", "graph"}),
    )
    try:
        validate_source_policy(spec, _data_agent_source_policy)
    except SourcePolicyViolation as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        validate_graph_few_shots(spec, contract_exists=_competency_contract_exists)
    except FewShotContractViolation as exc:
        raise click.ClickException(str(exc)) from exc

    text_results = validate_data_agent_text(spec)
    dedup_violations = validate_instruction_deduplication(spec)
    text_failures = [r for r in text_results if not r.passed]
    if text_failures:
        first = text_failures[0]
        raise click.ClickException(
            TextLimitViolation(
                field=first.field,
                actual=first.actual,
                limit=first.limit,
                remediation=first.remediation,
            ).args[0]
        )
    if dedup_violations:
        raise click.ClickException(
            "Duplicate instruction blocks detected:\n"
            + "\n".join(f"  - {v}" for v in dedup_violations)
        )

    click.echo(
        f"[deploy-data-agent] workspace: "
        f"{fabric_cfg['workspace_display_name']} ({workspace_id})\n"
        f"[deploy-data-agent] agent: {data_agent_name} "
        f"({configured_id or 'new item'})\n"
        f"[deploy-data-agent] actions: {target_mode}, publish\n"
        f"[deploy-data-agent] source: Ontology {ontology_item_id}\n"
        f"[deploy-data-agent] selected elements: "
        f"{expected.selected_element_count}\n"
        f"[deploy-data-agent] instruction hash: "
        f"{expected.instruction_hash}\n"
        f"[deploy-data-agent] source-selection hash: "
        f"{expected.source_selection_hash}"
    )

    # Report source policy and text validation counts in dry-run
    _source_type_label = {"ontology": "required ✓", "graph": "required ✓"}
    click.echo("\nSource policy:")
    for src in spec.sources:
        label = _source_type_label.get(src.source_type, "present")
        click.echo(f"  {src.source_type}: {label}")
    click.echo("  Source policy: PASS")
    click.echo("\nDefinition text validation:")
    for r in text_results:
        click.echo(f"  {r.field}: {r.actual:,} / {r.limit:,}")
    dup_count = len(dedup_violations)
    click.echo(f"  duplicate instruction blocks: {dup_count}")
    click.echo("  Definition text policy: PASS")

    # Capability reporting (#12 — property selection + grounding text counts)
    _req_prop = grounding.expected_property_child_count
    _comp_prop = expected.property_child_count
    _prop_coverage_pct = (
        int(_comp_prop / _req_prop * 100) if _req_prop > 0 else 100
    )
    click.echo("\nProperty selection:")
    click.echo(f"  required by semantic contract: {_req_prop:,}")
    click.echo(f"  selected in compiled spec:     {_comp_prop:,}")
    click.echo(f"  Property coverage: {_prop_coverage_pct}%")
    _da_global_instr_chars = len(str(spec.instruction or ""))
    _da_graph_src = next(
        (s for s in spec.sources if str(s.source_type) == "graph"), None
    )
    _da_ontology_src = next(
        (s for s in spec.sources if str(s.source_type) == "ontology"), None
    )
    _da_graph_instr_chars = (
        len(str(_da_graph_src.instructions or "")) if _da_graph_src else 0
    )
    _da_graph_desc_chars = (
        len(str(_da_graph_src.description or "")) if _da_graph_src else 0
    )
    _da_ontology_instr_chars = (
        len(str(_da_ontology_src.instructions or "")) if _da_ontology_src else 0
    )
    _da_ontology_desc_chars = (
        len(str(_da_ontology_src.description or "")) if _da_ontology_src else 0
    )
    _da_instruction_chars: dict[str, int] = {}
    _da_description_chars: dict[str, int] = {}
    if _da_graph_src:
        _da_instruction_chars["graph"] = _da_graph_instr_chars
        _da_description_chars["graph"] = _da_graph_desc_chars
    if _da_ontology_src:
        _da_instruction_chars["ontology"] = _da_ontology_instr_chars
        _da_description_chars["ontology"] = _da_ontology_desc_chars
    click.echo("\nGrounding text:")
    click.echo(f"  global instructions:      {_da_global_instr_chars:,} chars")
    click.echo(
        f"  ontology description:     {_da_ontology_desc_chars:,} chars"
    )
    click.echo(f"  graph description:        {_da_graph_desc_chars:,} chars")

    if _competency_contract_exists:
        click.echo("\nGraph example validation:")
        click.echo(f"  candidates discovered: {_example_candidate_count}")
        for receipt in _example_receipts:
            status = "PASS" if receipt.published else "OMIT"
            rows = receipt.direct_graph_row_count or 0
            evidence = (
                f"{int(round(receipt.evidence_coverage * 100)):d}%"
                if receipt.evidence_coverage > 0
                else (
                    "n/a"
                    if receipt.direct_result_category == "dry_run"
                    else "0%"
                )
            )
            click.echo(
                f"  {status:4} {receipt.competency_id} "
                f"rows={rows} evidence={evidence}"
            )
        click.echo(f"  examples selected: {len(graph_few_shots)} / 7")
        if dry_run:
            click.echo(
                "  planned live gates: direct Graph execution, then Data Agent "
                "semantic comparison."
            )

    if dry_run:
        click.echo("[deploy-data-agent] SUCCESS (dry-run)")
        return

    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    credential = credential or DefaultAzureCredential()
    client = FabricDataAgentClient(
        workspace_id=workspace_id,
        transport=RequestsTransport(),
        token=credential.get_token(
            "https://api.fabric.microsoft.com/.default"
        ).token,
    )
    try:
        result, publish_result, receipt = deploy_and_validate_data_agent(
            client=client,
            spec=spec,
            target_mode=target_mode,
            configured_target_item_id=configured_id or None,
            replace_approved=approve_replace,
            workspace_name=fabric_cfg["workspace_display_name"],
            workspace_id=workspace_id,
            package_instruction_hash=package_instruction_hash,
            grounding=grounding,
            projection_receipt=persisted,
            projection_receipt_hash=persisted_hash,
            published_description=(
                f"{data_agent_name} persisted semantic release."
            ),
            required_source_type="graph",
            source_policy=_data_agent_source_policy,
            global_instruction_chars=_da_global_instr_chars,
            instruction_chars=_da_instruction_chars,
            description_chars=_da_description_chars,
            competency_examples=_example_receipts,
        )
        if _competency_contract_exists and _example_receipts:
            from fabric_kg_builder.runtime.contract import CompetencyCase  # noqa: PLC0415
            from fabric_kg_builder.runtime.executors import DataAgentMcpExecutor  # noqa: PLC0415

            mcp_endpoint = (
                "https://api.fabric.microsoft.com/v1/workspaces/"
                f"{workspace_id}/dataagents/{result.item_id}/agent"
            )
            mcp_executor = DataAgentMcpExecutor(
                endpoint=mcp_endpoint,
                token_provider=lambda: credential.get_token(
                    "https://api.fabric.microsoft.com/.default"
                ).token,
            )

            def _execute_data_agent_case(case_payload: dict[str, Any]) -> dict[str, Any]:
                case_model = CompetencyCase.model_validate(case_payload)
                return mcp_executor.execute(case_model)

            _example_receipts = compare_graph_few_shot_semantics(
                _competency_contract_payload,
                _example_receipts,
                direct_results=_example_direct_results,
                execute_data_agent_case=_execute_data_agent_case,
            )
            receipt = receipt.model_copy(update={
                "competency_examples": _example_receipts
            })
    except (
        AgentPublicationError,
        DataAgentExampleValidationFailed,
        DataAgentDefinitionError,
        DataAgentTargetError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise click.ClickException(
            f"Exact Data Agent publication failed: {exc}"
        ) from exc
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    receipt_out.write_text(
        json.dumps(
            receipt.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _persist_data_agent_identity(
        env,
        item_id=result.item_id,
        display_name=data_agent_name,
    )
    click.echo(
        f"[deploy-data-agent] SUCCESS — {result.status}, "
        f"{publish_result.status} (id={result.item_id}) -> {receipt_out}"
    )


# ---------------------------------------------------------------------------
# deploy-serving — SRV-011 full serving deployment (M6)
# ---------------------------------------------------------------------------

_DEPLOY_SERVING_EPILOG = """\b
Example:
  fabric-kg deploy-serving --env dev --dry-run
  fabric-kg deploy-serving --env dev --index-name kg-chunks --no-dry-run

\b
Runs the full idempotent serving deployment pipeline:
  1. OneLake Lakehouse v2 table upload (dry-run: planned only)
  2. Versioned AI Search index create/reuse + schema validation
  3. Pre-cutover gates (count, text-query, citation)
  4. Atomic alias cutover (skipped on gate failure)
  5. Competency and lineage verification
  6. Deployment locator persistence

Uses DefaultAzureCredential for live auth (run 'az login' for dev).
The stable alias always equals the base index name (e.g. kg-chunks).

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("deploy-serving", epilog=_DEPLOY_SERVING_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=str,
              help="Target environment (reads ontology/environments/{env}.json).")
@click.option("--index-name", "base_index_name", default=None,
              help="Base AI Search index name. Defaults to the configured prefixed chunks index.")
@click.option("--schema-file", "schema_file", default=None, type=click.Path(),
              help="Path to index.schema.json. Defaults to build/search/kg-chunks/index.schema.json.")
@click.option("--docs-file", "docs_file", default=None, type=click.Path(),
              help="Path to docs.json. Defaults to the file beside --schema-file.")
@click.option("--embedding-model", "embedding_model",
              default="text-embedding-3-large", show_default=True,
              help="Embedding model name (immutable per versioned index).")
@click.option("--dimensions", default=1536, show_default=True, type=int,
              help="Embedding dimensions (immutable per versioned index).")
@click.option("--run-id", "run_id", default=None,
              help="Processing run ID for lineage tracking. Auto-generated if absent.")
@click.option("--dry-run/--no-dry-run", "dry_run", default=False, show_default=True,
              help="Dry-run: plan all actions without making modifying calls (--dry-run). "
                   "Use --no-dry-run for a live deploy.")
@click.option("--dist", "dist_path", default="dist", show_default=True, type=click.Path(),
              help="Path to dist directory (for Parquet table upload).")
@click.option("--graph-model-name", default=None,
              help="Fabric GraphModel display name. Defaults to environment configuration.")
@click.option(
    "--graph-artifact-out",
    default="build/graph",
    show_default=True,
    type=click.Path(),
    help="Directory for the human-readable GraphModel mapping artifact.",
)
@click.option(
    "--graph-definition-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Shared-compiler graph-definition.json; prevents data-derived label drift.",
)
@click.option(
    "--semantic-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Sealed semantic authority used to validate persisted Graph read-back "
         "and typed GQL readiness.",
)
@click.option(
    "--label-catalog-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Shared-compiler label-catalog.json used with --graph-definition-file.",
)
@click.option(
    "--deploy-lakehouse/--skip-lakehouse",
    default=True,
    show_default=True,
    help="Deploy OneLake tables in this command or use a prior deploy-lakehouse stage.",
)
@click.option(
    "--graph-preview-acknowledged",
    is_flag=True,
    default=False,
    help="Acknowledge use of the Fabric GraphModel/GQL preview APIs.",
)
@click.option(
    "--graph-canvas-visibility",
    type=click.Choice(["not_observed", "visible", "not_visible"]),
    default="not_observed",
    show_default=True,
    help="Optional operator-observed Fabric canvas state. Backend schema and "
         "GQL remain the automated readiness gates.",
)
@click.option(
    "--receipt-out",
    default=None,
    type=click.Path(),
    help="Write a non-secret serving receipt with Search and Fabric item IDs.",
)
@click.option("--manifest", "manifest_path", default=None, type=click.Path(),
              help="Path to deployment.yaml (naming authority). "
                   "Defaults to env-config names (legacy mode).")
def deploy_serving_cmd(
    env: str,
    base_index_name: str | None,
    schema_file: str | None,
    docs_file: str | None,
    embedding_model: str,
    dimensions: int,
    run_id: str | None,
    dry_run: bool,
    dist_path: str,
    graph_model_name: str | None,
    graph_artifact_out: str,
    graph_definition_file: str | None,
    semantic_dir: str | None,
    label_catalog_file: str | None,
    deploy_lakehouse: bool,
    graph_preview_acknowledged: bool,
    graph_canvas_visibility: str,
    receipt_out: str | None,
    manifest_path: str | None,
) -> None:
    """Full serving deployment: Lakehouse + AI Search index + alias + verification.

    Reads workspace_id, lakehouse_item_id, and AI Search endpoint from
    ontology/environments/{env}.json.  Authenticates with DefaultAzureCredential
    (run 'az login' for dev; use a Service Principal for CI/prod).

    Exit codes: 0 success · 1 error · 6 auth failure.
    """
    import uuid

    try:
        fabric_cfg = _read_fabric_env_config(env)
        search_env_cfg = _read_search_env_config(env)
    except FileNotFoundError as exc:
        click.echo(f"[deploy-serving] ERROR: {exc}", err=True)
        raise SystemExit(1) from exc

    workspace_id = fabric_cfg["workspace_id"]
    lakehouse_item_id = fabric_cfg["lakehouse_item_id"]
    schema_name = fabric_cfg.get("schema_name") or "dbo"
    ontology_item_id = fabric_cfg.get("ontology_item_id", "")
    search_cfg = search_env_cfg.get("ai_search", {})
    search_endpoint = _usable_config_value(search_cfg.get("endpoint", ""))
    if not base_index_name:
        base_index_name = (
            f"{search_cfg.get('index_prefix', '')}"
            f"{search_cfg.get('index_chunks', 'kg-chunks')}"
        )
    graph_name = graph_model_name or fabric_cfg["graph_model_display_name"]

    # --- Name authority: resolve GraphModel display name ---
    deployment_manifest = _load_or_synthesize_manifest(manifest_path, fabric_cfg)
    if manifest_path:
        _warn_manifest_vs_env(
            "deploy-serving", "GraphModel", deployment_manifest,
            fabric_cfg.get("graph_model_display_name"), "graph_model_display_name",
        )
    try:
        resolved_graph = resolve_item_name(
            deployment_manifest,
            "GraphModel",
            command_name=graph_model_name or None,
        )
    except NameAuthorityConflict as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    graph_name = resolved_graph.display_name or graph_name
    if dry_run:
        click.echo(render_name_resolution(resolved_graph))

    semantic_loaded = None
    if semantic_dir:
        if not graph_definition_file:
            raise click.ClickException(
                "--semantic-dir requires --graph-definition-file so persisted "
                "Graph state can be compared to the sealed projection."
            )
        from fabric_kg_builder.semantic import (  # noqa: PLC0415
            load_semantic_model_artifacts,
        )

        semantic_loaded = load_semantic_model_artifacts(semantic_dir)

    if not workspace_id or not lakehouse_item_id:
        click.echo("[deploy-serving] ERROR: missing fabric.workspace_id / lakehouse_item_id", err=True)
        raise SystemExit(1)

    if not search_endpoint and not dry_run:
        click.echo("[deploy-serving] ERROR: missing ai_search.endpoint in env config.", err=True)
        raise SystemExit(1)

    if not dry_run and not graph_preview_acknowledged:
        click.echo(
            "[deploy-serving] ERROR: live GraphModel deployment requires "
            "--graph-preview-acknowledged.",
            err=True,
        )
        raise SystemExit(2)

    # Resolve schema dict
    if schema_file:
        schema_path = Path(schema_file)
    else:
        schema_path = Path("build") / "search" / base_index_name / "index.schema.json"

    if not schema_path.exists():
        click.echo(f"[deploy-serving] ERROR: schema file not found: {schema_path}", err=True)
        raise SystemExit(1)

    schema_dict = json.loads(schema_path.read_text(encoding="utf-8"))
    docs_path = Path(docs_file) if docs_file else schema_path.with_name("docs.json")
    if not docs_path.exists():
        click.echo(
            f"[deploy-serving] ERROR: Search documents not found: {docs_path}. "
            "Run 'fabric-kg compile-search' first.",
            err=True,
        )
        raise SystemExit(1)
    docs_payload = json.loads(docs_path.read_text(encoding="utf-8"))
    if not isinstance(docs_payload, list) or not docs_payload:
        click.echo(
            f"[deploy-serving] ERROR: {docs_path} must contain at least one document.",
            err=True,
        )
        raise SystemExit(1)

    # Resolve parquet dir
    parquet_dir = Path(dist_path) / "fabric-kg-package" / "parquet"
    if not parquet_dir.exists():
        fallback = Path("build") / "parquet"
        parquet_dir = fallback if fallback.exists() else None
    if parquet_dir is None:
        click.echo(
            "[deploy-serving] ERROR: canonical Parquet directory not found. "
            "Run 'fabric-kg compile-data' or 'fabric-kg package' first.",
            err=True,
        )
        raise SystemExit(1)

    from fabric_kg_builder.semantic.source_tables import (  # noqa: PLC0415
        resolve_semantic_source_parquet,
    )

    try:
        entities_path = resolve_semantic_source_parquet(
            parquet_dir,
            "semantic_entities",
        )
        relationships_path = resolve_semantic_source_parquet(
            parquet_dir,
            "semantic_relationships",
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(
            "[deploy-serving] ERROR: entities.parquet and relationships.parquet "
            f"(canonical or semantic) are required under {parquet_dir}: {exc}",
            err=True,
        )
        raise SystemExit(1)

    import pyarrow.parquet as pq  # type: ignore[import]
    from fabric_kg_builder.serving.graph_model import (
        build_graph_model_parts,
        extract_entity_types_from_parquet,
        extract_relationship_pairs_from_parquet,
        write_graph_mapping_artifact,
    )
    from fabric_kg_builder.serving.orchestrator import _select_graph_lineage_probe

    graph_lineage_label = ""
    graph_lineage_fields: list[str] = []

    if graph_definition_file:
        graph_artifact = json.loads(
            Path(graph_definition_file).read_text(encoding="utf-8")
        )
        graph_parts = graph_artifact.get("parts")
        if not isinstance(graph_parts, list) or not graph_parts:
            raise click.ClickException(
                f"{graph_definition_file} does not contain Graph definition parts."
            )
        catalog_path = (
            Path(label_catalog_file)
            if label_catalog_file
            else Path(graph_definition_file).with_name("label-catalog.json")
        )
        if not catalog_path.exists():
            raise click.ClickException(
                "Shared Graph deployment requires label-catalog.json."
            )
        label_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        entity_types = [
            str(item["graph_label"])
            for item in label_catalog.get("nodes", [])
            if isinstance(item, dict) and item.get("graph_label")
        ]
        relationship_pairs = [
            {
                "name": str(item["graph_label"]),
                "source_type": str(item["source_graph_label"]),
                "target_type": str(item["target_graph_label"]),
            }
            for item in label_catalog.get("edges", [])
            if isinstance(item, dict)
            and item.get("graph_label")
            and item.get("source_graph_label")
            and item.get("target_graph_label")
        ]
        graph_lineage_label, graph_lineage_fields = (
            _select_graph_lineage_probe(label_catalog)
        )
        if not graph_lineage_fields:
            raise click.ClickException(
                "Shared Graph label catalog declares no queryable lineage "
                "or canonical identity property."
            )
        click.echo(
            "[deploy-serving] Graph source  : "
            f"{Path(graph_definition_file)}"
        )
    else:
        entity_rows = pq.read_table(str(entities_path)).to_pylist()
        relationship_rows = pq.read_table(str(relationships_path)).to_pylist()
        entity_types = extract_entity_types_from_parquet(entity_rows)
        if not entity_types:
            click.echo(
                "[deploy-serving] ERROR: entities.parquet contains no entity types.",
                err=True,
            )
            raise SystemExit(1)
        entities_by_id = {
            str(row["entity_id"]): row
            for row in entity_rows
            if row.get("entity_id")
        }
        relationship_pairs = extract_relationship_pairs_from_parquet(
            relationship_rows,
            entities_by_id,
            min_pair_count=3 if entities_path.stem == "semantic_entities" else 1,
            max_pairs=40 if entities_path.stem == "semantic_entities" else None,
        )
        graph_parts = build_graph_model_parts(
            entity_types=entity_types,
            relationship_pairs=relationship_pairs,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema_name,
            model_name=graph_name,
            entity_table=entities_path.stem,
            relationship_table=relationships_path.stem,
            include_semantic_properties=(
                entities_path.stem == "semantic_entities"
            ),
        )
        write_graph_mapping_artifact(
            Path(graph_artifact_out),
            graph_parts,
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema_name,
            model_name=graph_name,
        )

    effective_run_id = run_id or str(uuid.uuid4())

    click.echo(f"[deploy-serving] Environment   : {env}")
    click.echo(f"[deploy-serving] Workspace     : {workspace_id}")
    click.echo(f"[deploy-serving] Lakehouse     : {lakehouse_item_id}")
    click.echo(f"[deploy-serving] Search        : {search_endpoint or '(not configured)'}")
    click.echo(f"[deploy-serving] Base index    : {base_index_name}")
    click.echo(f"[deploy-serving] Search docs   : {len(docs_payload)}")
    click.echo(
        f"[deploy-serving] GraphModel    : {graph_name} "
        f"({len(entity_types)} node labels, {len(relationship_pairs)} edge bindings)"
    )
    click.echo(f"[deploy-serving] Embedding     : {embedding_model} / {dimensions}d")
    click.echo(f"[deploy-serving] Run ID        : {effective_run_id}")
    click.echo(f"[deploy-serving] Dry-run       : {dry_run}")

    if dry_run:
        click.echo("[deploy-serving] *** DRY-RUN MODE — no modifying calls ***")

    from fabric_kg_builder.serving.orchestrator import (
        FakeOrchestratorTransports,
        OrchestratorConfig,
        OrchestratorTransports,
        deploy_all,
    )
    cfg = OrchestratorConfig(
        workspace_id=workspace_id,
        lakehouse_item_id=lakehouse_item_id,
        search_endpoint=search_endpoint or "https://placeholder.search.windows.net",
        base_index_name=base_index_name,
        schema_dict=schema_dict,
        embedding_model=embedding_model,
        dimensions=dimensions,
        run_id=effective_run_id,
        environment=env,
        parquet_dir=parquet_dir,
        deploy_lakehouse=deploy_lakehouse,
        docs=docs_payload,
        schema=schema_name,
        ontology_item_id=ontology_item_id,
        graph_model_name=graph_name,
        graph_model_id=fabric_cfg.get("graph_model_id", ""),
        graph_model_parts=graph_parts,
        graph_entity_types=entity_types,
        graph_relationship_pairs=relationship_pairs,
        graph_preview_acknowledged=graph_preview_acknowledged or dry_run,
    )
    if graph_lineage_fields:
        cfg.graph_lineage_label = graph_lineage_label
        cfg.graph_lineage_fields = graph_lineage_fields

    if dry_run:
        # Dry-run uses fake transports — no credential needed
        tp = FakeOrchestratorTransports()
    else:
        # Live path: obtain real token and build live transport
        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            credential = DefaultAzureCredential()
            search_token_provider = lambda: credential.get_token(
                "https://search.azure.com/.default"
            ).token
            fabric_token_provider = lambda: credential.get_token(
                "https://api.fabric.microsoft.com/.default"
            ).token
        except Exception as exc:
            click.echo(f"[deploy-serving] AUTH ERROR: {exc}", err=True)
            raise SystemExit(6) from exc

        from fabric_kg_builder.serving.release_manager import _RequestsTransport
        from fabric_kg_builder.serving.competency import OneLakeDeltaClient
        from fabric_kg_builder.serving.graph_model import GraphModelGQLClient
        from fabric_kg_builder.serving.lineage_verifier import AzureSearchSampler

        tp = OrchestratorTransports(
            search_transport=_RequestsTransport(),
            lakehouse_client=OneLakeDeltaClient(),
            search_sampler=AzureSearchSampler(
                search_endpoint,
                token_provider=search_token_provider,
            ),
            token_provider=search_token_provider,
            fabric_token_provider=fabric_token_provider,
            gql_client=GraphModelGQLClient(
                token_provider=fabric_token_provider,
            ),
        )

    result = deploy_all(cfg, transports=tp, dry_run=dry_run)
    if result.errors:
        details = "\n".join(f"  - {error}" for error in result.errors)
        partial = "\n".join(
            f"  - {failure}" for failure in result.partial_failures
        )
        configured_graph_id = str(cfg.graph_model_id or "(not configured)")
        message = (
            "Serving deployment failed before persisted readiness checks.\n"
            f"Configured Graph Model ID: {configured_graph_id}\n"
            f"Returned Graph Model ID: {result.graph_model_id or '(none)'}\n"
            f"Errors:\n{details}"
        )
        if partial:
            message += f"\nPartial failures:\n{partial}"
        raise click.ClickException(message)
    graph_persisted_evidence = None
    graph_query_readiness = None
    if semantic_loaded is not None and not dry_run:
        from fabric_kg_builder.semantic import (  # noqa: PLC0415
            PersistedProjectionError,
            validate_graph_query_readiness,
            validate_persisted_graph,
        )
        from fabric_kg_builder.serving.graph_model import (  # noqa: PLC0415
            get_graph_model_definition,
        )

        if not result.graph_model_id:
            raise click.ClickException(
                "Graph deployment returned no item ID; persisted readiness "
                "cannot be established."
            )
        try:
            persisted_graph = get_graph_model_definition(
                workspace_id,
                result.graph_model_id,
                token_provider=getattr(tp, "fabric_token_provider", None),
                transport=getattr(tp, "graph_model_transport", None),
            )
            graph_persisted_evidence = validate_persisted_graph(
                definition=persisted_graph,
                manifest=semantic_loaded.manifest,
                plan=semantic_loaded.materialization_plan,
            )
            gql_client = getattr(tp, "gql_client", None)
            if gql_client is None:
                raise RuntimeError(
                    "A live GQL client is required for persisted Graph readiness."
                )
            graph_query_readiness = validate_graph_query_readiness(
                manifest=semantic_loaded.manifest,
                plan=semantic_loaded.materialization_plan,
                workspace_id=workspace_id,
                graph_model_id=result.graph_model_id,
                gql_client=gql_client,
                canvas_visibility=graph_canvas_visibility,
            )
        except (
            PersistedProjectionError,
            PermissionError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise click.ClickException(
                "Persisted Graph readiness failed after updateDefinition: "
                f"{exc}"
            ) from exc
        if not fabric_cfg.get("graph_model_id"):
            _persist_graph_model_item_id(env, result.graph_model_id)
            click.echo(
                "[deploy-serving] persisted GraphModel item ID for reapply"
            )
        click.echo(
            "[deploy-serving] Graph persisted : "
            f"{graph_persisted_evidence.projection_hash}"
        )
        click.echo(
            "[deploy-serving] GQL readiness   : "
            f"nodes={graph_query_readiness.gql_node_count}, "
            f"edges={graph_query_readiness.gql_edge_count}"
        )
    graph_definition_path = (
        Path(graph_definition_file) if graph_definition_file else None
    )
    label_catalog_path = (
        Path(label_catalog_file) if label_catalog_file else None
    )
    label_catalog = (
        _load_json_object(label_catalog_path)
        if label_catalog_path is not None
        else {}
    )
    semantic_contract_hash = (
        label_catalog.get("contract_hash")
        or schema_dict.get("_semantic_contract_hash")
    )
    _write_receipt(
        receipt_out,
        {
            "schema": "fabric-kg.serving-deployment.v1",
            "environment": env,
            "run_id": effective_run_id,
            "workspace_id": workspace_id,
            "lakehouse_item_id": lakehouse_item_id,
            "ontology_item_id": ontology_item_id,
            "graph_model_id": result.graph_model_id,
            "graph_model_action": result.graph_model_action,
            "search_endpoint": search_endpoint,
            "physical_index_name": result.physical_index_name,
            "alias": result.alias,
            "index_action": result.index_action,
            "docs_pushed": result.docs_pushed,
            "pre_cutover_gates_ok": result.pre_cutover_gates_ok,
            "semantic_contract_hash": semantic_contract_hash,
            "semantic_model_manifest_hash": (
                semantic_loaded.manifest.manifest_hash
                if semantic_loaded is not None
                else None
            ),
            "graph_definition_hash": _sha256_file(graph_definition_path),
            "label_catalog_hash": _sha256_file(label_catalog_path),
            "graph_persisted_projection_hash": (
                graph_persisted_evidence.projection_hash
                if graph_persisted_evidence is not None
                else None
            ),
            "graph_definition_counts": (
                graph_persisted_evidence.definition_counts
                if graph_persisted_evidence is not None
                else {}
            ),
            "query_readiness": (
                graph_query_readiness.model_dump(mode="json")
                if graph_query_readiness is not None
                else {}
            ),
            "search_schema_hash": _sha256_file(schema_path),
            "search_documents_hash": _sha256_file(docs_path),
            "serving_fingerprint": result.fingerprint,
            "ok": result.ok,
            "dry_run": dry_run,
        },
    )

    click.echo(f"[deploy-serving] Physical index  : {result.physical_index_name}")
    click.echo(f"[deploy-serving] Alias           : {result.alias}")
    click.echo(f"[deploy-serving] Index action    : {result.index_action}")
    click.echo(f"[deploy-serving] Fingerprint     : {result.fingerprint}")
    click.echo(f"[deploy-serving] Docs pushed     : {result.docs_pushed}")
    click.echo(f"[deploy-serving] Pre-cutover ok  : {result.pre_cutover_gates_ok}")
    if result.graph_model_action:
        click.echo(
            f"[deploy-serving] GraphModel      : {result.graph_model_action} "
            f"({result.graph_model_id or 'no item id'})"
        )

    if result.competency:
        status = "ok" if result.competency.ok else "FAILED"
        click.echo(
            f"[deploy-serving] Competency      : {status} "
            f"(entities={result.competency.entity_count}, "
            f"relationships={result.competency.relationship_count})"
        )

    if result.lineage_report:
        status = "ok" if result.lineage_report.ok else "partial"
        click.echo(
            f"[deploy-serving] Lineage         : {status} "
            f"(sampled={result.lineage_report.sample_count}, "
            f"resolved={result.lineage_report.resolved_count})"
        )

    if result.errors:
        for err in result.errors:
            click.echo(f"[deploy-serving] ERROR: {err}", err=True)

    if result.partial_failures:
        for pf in result.partial_failures:
            click.echo(f"[deploy-serving] PARTIAL: {pf}", err=True)

    if result.ok:
        mode = "DRY-RUN" if dry_run else "LIVE"
        click.echo(f"[deploy-serving] SUCCESS ({mode})")
    else:
        click.echo("[deploy-serving] FAILED — see errors above.", err=True)
        raise SystemExit(1)


@click.command(
    "validate-projection",
    context_settings={"max_content_width": 120},
)
@click.option(
    "--semantic-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Sealed semantic authority directory.",
)
@click.option(
    "--ontology-receipt",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Persisted Ontology deployment/read-back receipt.",
)
@click.option(
    "--serving-receipt",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Persisted Graph/Search serving deployment receipt.",
)
@click.option(
    "--out",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Output path for persisted-projection-receipt.json.",
)
def validate_projection_cmd(
    semantic_dir: str,
    ontology_receipt: str,
    serving_receipt: str,
    output_path: str,
) -> None:
    """Seal independently read-back Ontology and Graph evidence for downstream use."""
    from fabric_kg_builder.semantic import (  # noqa: PLC0415
        PersistedSurfaceEvidence,
        QueryReadiness,
        build_persisted_projection_receipt,
        load_semantic_model_artifacts,
    )

    def load_required_object(path: str) -> dict:
        target = Path(path)
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise click.ClickException(
                f"Could not read deployment receipt '{target}': {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise click.ClickException(
                f"Deployment receipt '{target}' must contain an object."
            )
        return value

    loaded = load_semantic_model_artifacts(semantic_dir)
    ontology = load_required_object(ontology_receipt)
    serving = load_required_object(serving_receipt)
    if ontology.get("mock") is not False:
        raise click.ClickException(
            "Ontology receipt is mock or does not prove a live deployment."
        )
    if serving.get("dry_run") is not False:
        raise click.ClickException(
            "Serving receipt is dry-run or does not prove a live deployment."
        )
    manifest_hash = loaded.manifest.manifest_hash
    for label, receipt in (
        ("Ontology", ontology),
        ("Serving", serving),
    ):
        if receipt.get("semantic_model_manifest_hash") != manifest_hash:
            raise click.ClickException(
                f"{label} receipt does not match semantic model manifest "
                f"{manifest_hash}."
            )
    if ontology.get("workspace_id") != serving.get("workspace_id"):
        raise click.ClickException(
            "Ontology and serving receipts target different workspaces."
        )
    if ontology.get("lakehouse_item_id") != serving.get(
        "lakehouse_item_id"
    ):
        raise click.ClickException(
            "Ontology and serving receipts target different Lakehouses."
        )
    if ontology.get("ontology_item_id") != serving.get("ontology_item_id"):
        raise click.ClickException(
            "Ontology and serving receipts target different Ontology items."
        )

    schema_name = str(ontology.get("schema_name") or "")
    if not schema_name:
        raise click.ClickException(
            "Ontology receipt omits the validated Lakehouse schema name."
        )
    expected_tables = {
        f"{schema_name}.{table.table_name}"
        for table in [
            *loaded.materialization_plan.entity_tables,
            *loaded.materialization_plan.relationship_tables,
        ]
    }
    bound_table_counts = ontology.get("bound_table_counts")
    if not isinstance(bound_table_counts, dict):
        raise click.ClickException(
            "Ontology receipt omits bound-table read-back counts."
        )
    if set(bound_table_counts) != expected_tables:
        raise click.ClickException(
            "Ontology receipt bound-table set differs from the materialization "
            f"plan. Missing={sorted(expected_tables - set(bound_table_counts))}; "
            f"extra={sorted(set(bound_table_counts) - expected_tables)}."
        )
    try:
        receipt = build_persisted_projection_receipt(
            manifest=loaded.manifest,
            ontology_item_id=str(ontology.get("ontology_item_id") or ""),
            ontology_evidence=PersistedSurfaceEvidence(
                projection_hash=str(
                    ontology.get("ontology_persisted_projection_hash") or ""
                ),
                definition_counts=dict(
                    ontology.get("ontology_definition_counts") or {}
                ),
            ),
            graph_model_id=str(serving.get("graph_model_id") or ""),
            graph_evidence=PersistedSurfaceEvidence(
                projection_hash=str(
                    serving.get("graph_persisted_projection_hash") or ""
                ),
                definition_counts=dict(
                    serving.get("graph_definition_counts") or {}
                ),
            ),
            bound_table_counts={
                str(key): int(value)
                for key, value in bound_table_counts.items()
            },
            query_readiness=QueryReadiness.model_validate(
                serving.get("query_readiness") or {}
            ),
        )
    except (TypeError, ValueError) as exc:
        raise click.ClickException(
            f"Persisted projection evidence is incomplete: {exc}"
        ) from exc

    _write_receipt(output_path, receipt.model_dump(mode="json"))
    click.echo(
        "[validate-projection] READY "
        f"Ontology={receipt.ontology_item_id} "
        f"Graph={receipt.graph_model_id} -> {output_path}"
    )
