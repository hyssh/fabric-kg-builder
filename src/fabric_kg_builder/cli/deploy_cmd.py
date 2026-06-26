"""deploy commands — deploy-lakehouse, deploy-ontology, deploy-search."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from fabric_kg_builder.deploy.fabric_deployer import FabricDeployer
from fabric_kg_builder.deploy.onelake_writer import (
    LAKEHOUSE_TABLE_PROJECTION,
    LAKEHOUSE_TABLES,
)

# Default Lakehouse table list — graph/ontology scope only.
# Imported from onelake_writer so the projection constant and this list stay in sync.
# "chunks" is intentionally absent: pure retrieval text → AI Search (kg-chunks).
_LAKEHOUSE_TABLES = LAKEHOUSE_TABLES


def _read_fabric_env_config(env: str, environments_dir: Path | None = None) -> dict:
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
    return {
        "workspace_id": fabric.get("workspace_id", ""),
        "lakehouse_item_id": fabric.get("lakehouse_item_id", ""),
        "lakehouse_display_name": fabric.get("lakehouse_display_name", ""),
        "onelake_tables_path": fabric.get("onelake_tables_path", ""),
        "schema_name": fabric.get("schema_name", "dbo"),
    }


_DEPLOY_LAKEHOUSE_EPILOG = """\b
Example:
  fabric-kg deploy-lakehouse --env dev --mock
  fabric-kg deploy-lakehouse --env dev --no-mock
  fabric-kg deploy-lakehouse --env dev --tables entities,relationships --mock

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("deploy-lakehouse", epilog=_DEPLOY_LAKEHOUSE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=click.Choice(["dev", "test", "prod"]),
              help="Target deployment environment (reads ontology/environments/{env}.json).")
@click.option("--dist", "dist_path", default="dist", show_default=True, type=click.Path(),
              help="Path to dist directory produced by 'package'. "
                   "Falls back to build/parquet/ if dist is absent.")
@click.option("--tables", default=None,
              show_default=True,
              help="Comma-separated subset of Parquet tables to deploy "
                   "(default: all graph/ontology tables; chunks are excluded).")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing Lakehouse Delta tables.")
@click.option("--mock/--no-mock", "use_mock", default=False, show_default=True,
              help="Mock mode: log planned actions without any network call (--mock). "
                   "Use --no-mock for a live deploy.")
def deploy_lakehouse_cmd(
    env: str, dist_path: str, tables: str | None, force: bool, use_mock: bool
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
        fabric_cfg = _read_fabric_env_config(env)
    except FileNotFoundError as exc:
        click.echo(f"[deploy-lakehouse] ERROR: {exc}", err=True)
        raise SystemExit(1) from exc

    workspace_id = fabric_cfg["workspace_id"]
    lakehouse_item_id = fabric_cfg["lakehouse_item_id"]
    lakehouse_name = fabric_cfg.get("lakehouse_display_name") or "kg_lakehouse"
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

    # Resolve parquet dir: try dist path first, fallback to build/parquet
    parquet_dir = Path(dist_path) / "fabric-kg-package" / "parquet"
    if not parquet_dir.exists():
        fallback = Path("build") / "parquet"
        if fallback.exists():
            parquet_dir = fallback
            click.echo(f"[deploy-lakehouse] Using fallback parquet dir: {parquet_dir}")

    available = (
        sorted(p.stem for p in parquet_dir.glob("*.parquet"))
        if parquet_dir.exists()
        else []
    )

    click.echo(f"[deploy-lakehouse] Environment  : {env}")
    click.echo(f"[deploy-lakehouse] Workspace    : {workspace_id}")
    click.echo(f"[deploy-lakehouse] Lakehouse    : {lakehouse_item_id} ({lakehouse_name})")
    click.echo(f"[deploy-lakehouse] Tables path  : {onelake_tables_path}")
    click.echo(f"[deploy-lakehouse] Schema name  : {schema_name}")
    click.echo(f"[deploy-lakehouse] Force overwrite: {force}")
    click.echo(f"[deploy-lakehouse] Dist dir     : {parquet_dir}")
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
            f"[deploy-lakehouse] NOTE: No parquet files found under {parquet_dir} "
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
            parquet_dir=parquet_dir,
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
Example:
  fabric-kg deploy-ontology --env dev
  fabric-kg deploy-ontology --env dev --no-mock
  fabric-kg deploy-ontology --env dev --multitype --parquet-dir data\\surface_kg\\parquet --no-mock

\b
--multitype models one Fabric EntityType per real domain type (Component,
Procedure, Step, ...) plus typed relationships, instead of a single generic
KGEntity. It materializes per-type Lakehouse tables from --parquet-dir first.

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


@click.command("deploy-ontology", epilog=_DEPLOY_ONTOLOGY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=click.Choice(["dev", "test", "prod"]),
              help="Target deployment environment (reads ontology/environments/{env}.json).")
@click.option("--dist", "dist_path", default="build/ontology", show_default=True, type=click.Path(),
              help="Path to compiled ontology directory (output of compile-ontology). "
                   "Used for the old compile artifact; deploy now uses build_ontology_parts().")
@click.option("--poll-timeout", default=300, show_default=True, type=int,
              help="Seconds to wait for long-running Fabric LRO operations.")
@click.option("--mock/--no-mock", "use_mock", default=True, show_default=True,
              help="Mock mode (default --mock): log what would be deployed without any network call. "
                   "Use --no-mock for a live deploy.")
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
def deploy_ontology_cmd(
    env: str, dist_path: str, poll_timeout: int, use_mock: bool,
    multitype: bool, parquet_dir: str | None, min_pair_count: int,
    create_agent_instruction: bool, agent_instruction_out: str | None,
    domain_brief_path: str | None,
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
        update_ontology_definition,
    )
    from fabric_kg_builder.ontology.fabric_def import build_ontology_parts  # noqa: PLC0415

    # Read env config (workspace_id, lakehouse_item_id, schema_name)
    try:
        fabric_cfg = _read_fabric_env_config(env)
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

    ontology_name = "kg_ontology"

    click.echo(f"[deploy-ontology] env             : {env}")
    click.echo(f"[deploy-ontology] workspace_id    : {workspace_id}")
    click.echo(f"[deploy-ontology] lakehouse_id    : {lakehouse_item_id}")
    click.echo(f"[deploy-ontology] schema          : {schema_name}")
    click.echo(f"[deploy-ontology] ontology name   : {ontology_name}")
    click.echo(f"[deploy-ontology] mock mode       : {use_mock}")
    click.echo(f"[deploy-ontology] multitype       : {multitype}")

    # Plan + materialize per-type tables when --multitype is requested.
    mt_plan = None
    if multitype:
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
        from fabric_kg_builder.ontology.multitype_plan import build_plan  # noqa: PLC0415

        pdir = _Path(parquet_dir)
        if not (pdir / "entities.parquet").exists():
            click.echo(
                f"[deploy-ontology] ERROR: entities.parquet not found in {pdir}", err=True
            )
            sys.exit(1)

        mt_plan = build_plan(pdir, min_pair_count=min_pair_count)
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
        # Build the REAL Fabric format parts (deterministic, idempotent)
        parts = build_ontology_parts(
            workspace_id=workspace_id,
            lakehouse_item_id=lakehouse_item_id,
            schema=schema_name,
            ontology_name=ontology_name,
        )
    parts_count = len(parts)
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
        click.echo("-" * 60)

        # Mock item creation
        item_result = create_or_get_ontology_item(
            workspace_id=workspace_id,
            name=ontology_name,
            mock=True,
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
        click.echo(f"[deploy-ontology] updateDefinition (mock): {upd_result['note']}")
        click.echo("[deploy-ontology] Done. Exit 0.")
        return

    # --- LIVE DEPLOY ---
    click.echo(
        f"[deploy-ontology] LIVE: creating/getting Ontology item '{ontology_name}' ..."
    )

    item_result = create_or_get_ontology_item(
        workspace_id=workspace_id,
        name=ontology_name,
        mock=False,
    )

    item_id = item_result["item_id"]
    action = "CREATED" if item_result["created"] else "REUSED"
    click.echo(f"[deploy-ontology] {action} Ontology item '{ontology_name}'")
    click.echo(f"[deploy-ontology] item_id : {item_id}")

    # If we got an LRO placeholder, resolve the real item_id by name
    if item_id.startswith("lro:"):
        click.echo(
            "[deploy-ontology] LRO response — resolving item by name (GET items) ..."
        )
        import requests  # noqa: PLC0415
        from fabric_kg_builder.deploy.fabric_ontology import (  # noqa: PLC0415
            _default_token_provider,
            _FABRIC_API_BASE,
        )
        token = _default_token_provider()
        headers = {"Authorization": f"Bearer {token}"}
        list_url = f"{_FABRIC_API_BASE}/workspaces/{workspace_id}/items"
        resp = requests.get(list_url, headers=headers, timeout=30)
        if resp.ok:
            items_list = resp.json().get("value", [])
            found = next(
                (
                    i for i in items_list
                    if i.get("displayName") == ontology_name and i.get("type") == "Ontology"
                ),
                None,
            )
            if found:
                item_id = found["id"]
                click.echo(f"[deploy-ontology] Resolved item_id : {item_id}")
            else:
                click.echo(
                    "[deploy-ontology] WARNING: could not resolve LRO item — "
                    "updateDefinition may fail with LRO placeholder id.",
                    err=True,
                )
        else:
            click.echo(
                f"[deploy-ontology] WARNING: GET items returned {resp.status_code} — "
                "proceeding with LRO placeholder (may fail).",
                err=True,
            )

    # Push the REAL Fabric format to populate the graph
    click.echo(
        f"[deploy-ontology] LIVE: calling updateDefinition ({parts_count} parts) ..."
    )
    upd_result = update_ontology_definition(
        workspace_id=workspace_id,
        ontology_item_id=item_id,
        parts=parts,
        mock=False,
    )
    click.echo(f"[deploy-ontology] updateDefinition status : {upd_result['status']}")
    click.echo(f"[deploy-ontology] {upd_result['note']}")
    click.echo("[deploy-ontology] Done. Exit 0.")


_DEPLOY_SEARCH_EPILOG = """\b
Example:
  fabric-kg deploy-search --env dev --mock
  fabric-kg deploy-search --env dev --no-mock
  fabric-kg deploy-search --env dev --indexes kg-chunks --no-mock

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("deploy-search", epilog=_DEPLOY_SEARCH_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--env", required=True, type=click.Choice(["dev", "test", "prod"]),
              help="Target deployment environment (reads ontology/environments/{env}.json).")
@click.option("--dist", "dist_path", default="build/search", show_default=True,
              type=click.Path(),
              help="Path to build/search/ directory (output of compile-search).")
@click.option("--indexes", default=None, show_default=True,
              help="Comma-separated subset of indexes to deploy "
                   "(default: kg-chunks,kg-document-elements).")
@click.option("--recreate", is_flag=True, default=False,
              help="Drop and recreate the index before pushing (caution: loses all documents).")
@click.option("--mock/--no-mock", "use_mock", default=False, show_default=True,
              help="Mock mode: log planned actions without any network call. "
                   "Use --no-mock for a live deploy (default).")
def deploy_search_cmd(
    env: str, dist_path: str, indexes: str | None, recreate: bool, use_mock: bool
) -> None:
    """Upload AI Search index schemas and document batches to Azure AI Search.

    Reads ai_search.endpoint, ai_search.index_prefix, and ai_search.enabled
    from ontology/environments/{env}.json.  Authenticates with
    DefaultAzureCredential (az login for dev, SPN for CI/prod).

    PUTs each index schema then batch-uploads docs.json via the Azure AI
    Search REST API.  Skips silently if ai_search.enabled=false in env config.

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

    _all_indexes = {
        "kg-chunks": f"{index_prefix}{index_chunks}",
        "kg-document-elements": f"{index_prefix}{index_doc_elements}",
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
            from fabric_kg_builder.deploy.search_deployer import deploy_index  # noqa: PLC0415

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

            click.echo(
                f"[deploy-search]   Pushing index={deployed_name!r}, "
                f"docs={len(docs)}, recreate={recreate}"
            )
            try:
                result = deploy_index(
                    endpoint=endpoint,
                    index_name=deployed_name,
                    schema_dict=schema_dict,
                    docs=docs,
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
