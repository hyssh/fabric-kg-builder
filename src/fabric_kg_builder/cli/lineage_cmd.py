"""CLI commands for M2 asset registration, lineage registry, and trace.

Exposes three entry points:
  fabric-kg assets register/list/status/retry   — asset lifecycle management
  fabric-kg lineage trace <record_id>           — canonical lineage traversal
  fabric-kg trace <record_id>                   — deprecated compatibility alias

All lineage internals are imported lazily inside each command so that
``fabric-kg --help`` remains usable while Fenster completes lineage/trace.py.
Direct module imports (not the package __init__.py) avoid the missing
trace.py import-time failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY = "build/registry.json"


def _load_registry_obj(registry_path: str):
    """Return an AssetRegistry bound to *registry_path* or raise ClickException."""
    try:
        from fabric_kg_builder.lineage.registry import AssetRegistry
    except ImportError as exc:
        raise click.ClickException(
            f"Lineage registry module not available: {exc}. "
            "Ensure the lineage package is fully installed."
        ) from exc
    return AssetRegistry(registry_path)


def _make_run_id() -> str:
    try:
        from fabric_kg_builder.model.ids import make_run_id
        return make_run_id()
    except ImportError:
        import uuid
        return f"run-{uuid.uuid4().hex[:16]}"


def _emit(data: object, *, json_output: bool) -> None:
    """Print human-readable or JSON output without exposing secrets."""
    if json_output:
        click.echo(json.dumps(data, indent=2, sort_keys=True, default=str))
    elif isinstance(data, list):
        for item in data:
            click.echo(_format_asset_row(item))
    elif isinstance(data, dict):
        click.echo(_format_asset_row(data))
    else:
        click.echo(str(data))


def _format_asset_row(row: dict) -> str:
    asset_id = row.get("asset_id", "?")
    name = row.get("original_name", "?")
    status = row.get("latest_ingestion_status") or row.get("ingestion_status", "?")
    versions = row.get("version_count", "?")
    blob = row.get("latest_blob_uri") or row.get("blob_uri", "")
    # Redact any key-like values for safety.
    if "key=" in str(blob).lower() or "sig=" in str(blob).lower():
        blob = "<redacted blob URI>"
    return f"{asset_id}  {name!r:40}  status={status}  versions={versions}  blob={blob}"


# ---------------------------------------------------------------------------
# assets command group
# ---------------------------------------------------------------------------


@click.group(
    "assets",
    context_settings={"max_content_width": 120},
)
def assets_cmd() -> None:
    """Register, list, inspect, and retry pipeline asset files.

    \b
    Workflow:
      fabric-kg assets register --input <path>   Register a file into the landing store
      fabric-kg assets list                       List all registered assets
      fabric-kg assets status --asset-id <uuid>   Show current status for one asset
      fabric-kg assets retry --asset-id <uuid>    Re-upload a failed or pending asset
    """


@assets_cmd.command("register")
@click.argument(
    "legacy_input",
    required=False,
    type=click.Path(exists=True, file_okay=True, dir_okay=True),
)
@click.option(
    "--input",
    "input_path",
    required=False,
    type=click.Path(exists=True, file_okay=True, dir_okay=True),
    help="File or directory to register.",
)
@click.option(
    "--recursive",
    is_flag=True,
    default=False,
    help="Recursively register files when --input is a directory.",
)
@click.option(
    "--registry",
    default=_DEFAULT_REGISTRY,
    show_default=True,
    type=click.Path(),
    help="Path to the local asset registry JSON store.",
)
@click.option(
    "--run-id",
    default=None,
    help="Processing run ID to associate with this registration (generated if omitted).",
)
@click.option(
    "--json",
    "--json-output",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit JSON instead of human-readable output.",
)
def assets_register_cmd(
    legacy_input: str | None,
    input_path: str | None,
    recursive: bool,
    registry: str,
    run_id: str | None,
    json_output: bool,
) -> None:
    """Register a file or directory in the immutable asset landing store.

    \b
    Example:
      fabric-kg assets register --input data/report.pdf
      fabric-kg assets register --input data --recursive
    """
    if input_path and legacy_input and Path(input_path) != Path(legacy_input):
        raise click.ClickException(
            "Provide the input through --input, not both --input and the "
            "deprecated positional path."
        )
    input_path = input_path or legacy_input
    if not input_path:
        raise click.ClickException("Missing required option '--input'.")

    reg = _load_registry_obj(registry)
    effective_run_id = run_id or _make_run_id()
    source = Path(input_path)
    if source.is_dir():
        pattern = "**/*" if recursive else "*"
        paths = sorted(path for path in source.glob(pattern) if path.is_file())
        if not paths:
            raise click.ClickException(
                f"No files found under '{source}'"
                + (" recursively." if recursive else ". Use --recursive for subdirectories.")
            )
    else:
        paths = [source]

    results: list[dict[str, object]] = []
    for path in paths:
        try:
            asset_row, version_row, extras = reg.register_file(
                path,
                run_id=effective_run_id,
            )
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc
        except OSError as exc:
            raise click.ClickException(f"Failed to register asset: {exc}") from exc
        results.append(
            {
                "asset_id": asset_row.asset_id,
                "asset_version_id": version_row.asset_version_id,
                "original_name": asset_row.original_name,
                "content_hash": version_row.content_hash,
                "ingestion_status": version_row.ingestion_status,
                "run_id": effective_run_id,
                "idempotent": extras.get("idempotent", False),
            }
        )

    payload: object = results[0] if len(results) == 1 else results
    if json_output:
        _emit(payload, json_output=True)
        return
    for result in results:
        click.echo(f"[assets register] asset_id          : {result['asset_id']}")
        click.echo(f"[assets register] asset_version_id  : {result['asset_version_id']}")
        click.echo(f"[assets register] original_name     : {result['original_name']}")
        click.echo(f"[assets register] ingestion_status  : {result['ingestion_status']}")
        click.echo(f"[assets register] idempotent         : {result['idempotent']}")


@assets_cmd.command("list")
@click.option(
    "--registry",
    default=_DEFAULT_REGISTRY,
    show_default=True,
    type=click.Path(),
    help="Path to the local asset registry JSON store.",
)
@click.option(
    "--asset-id",
    default=None,
    help="Filter to a specific asset ID.",
)
@click.option(
    "--json",
    "--json-output",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit JSON instead of human-readable output.",
)
def assets_list_cmd(
    registry: str,
    asset_id: str | None,
    json_output: bool,
) -> None:
    """List all registered assets in the registry.

    \b
    Example:
      fabric-kg assets list
      fabric-kg assets list --json-output
    """
    reg = _load_registry_obj(registry)
    rows = reg.list_assets(asset_id=asset_id)
    if not rows:
        if json_output:
            _emit([], json_output=True)
            return
        click.echo("[assets list] no assets registered")
        return
    _emit(rows, json_output=json_output)
    if not json_output:
        click.echo(f"[assets list] total: {len(rows)} asset(s)")


@assets_cmd.command("status")
@click.argument("legacy_asset_id", required=False)
@click.option(
    "--asset-id",
    required=False,
    help="Asset UUID to inspect.",
)
@click.option(
    "--registry",
    default=_DEFAULT_REGISTRY,
    show_default=True,
    type=click.Path(),
    help="Path to the local asset registry JSON store.",
)
@click.option(
    "--json",
    "--json-output",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit JSON instead of human-readable output.",
)
def assets_status_cmd(
    legacy_asset_id: str | None,
    asset_id: str | None,
    registry: str,
    json_output: bool,
) -> None:
    """Show registration and ingestion status for ASSET_ID.

    \b
    Example:
      fabric-kg assets status ast-abc123
      fabric-kg assets status ast-abc123 --json-output
    """
    if asset_id and legacy_asset_id and asset_id != legacy_asset_id:
        raise click.ClickException(
            "Provide the asset ID through --asset-id, not both --asset-id and "
            "the deprecated positional argument."
        )
    asset_id = asset_id or legacy_asset_id
    if not asset_id:
        raise click.ClickException("Missing required option '--asset-id'.")

    reg = _load_registry_obj(registry)
    rows = reg.list_assets(asset_id=asset_id)
    if not rows:
        raise click.ClickException(
            f"Asset '{asset_id}' not found in registry '{registry}'. "
            "Run 'fabric-kg assets register <path>' first."
        )
    _emit(rows[0], json_output=json_output)


@assets_cmd.command("retry")
@click.argument("legacy_asset_id", required=False)
@click.option(
    "--asset-id",
    required=False,
    help="Asset UUID to retry.",
)
@click.option(
    "--registry",
    default=_DEFAULT_REGISTRY,
    show_default=True,
    type=click.Path(),
    help="Path to the local asset registry JSON store.",
)
@click.option(
    "--run-id",
    default=None,
    help="Processing run ID to associate with the retry (generated if omitted).",
)
@click.option(
    "--json",
    "--json-output",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit JSON instead of human-readable output.",
)
def assets_retry_cmd(
    legacy_asset_id: str | None,
    asset_id: str | None,
    registry: str,
    run_id: str | None,
    json_output: bool,
) -> None:
    """Re-upload failed or pending versions of ASSET_ID.

    \b
    Example:
      fabric-kg assets retry ast-abc123
      fabric-kg assets retry ast-abc123 --run-id run-xyz
    """
    if asset_id and legacy_asset_id and asset_id != legacy_asset_id:
        raise click.ClickException(
            "Provide the asset ID through --asset-id, not both --asset-id and "
            "the deprecated positional argument."
        )
    asset_id = asset_id or legacy_asset_id
    if not asset_id:
        raise click.ClickException("Missing required option '--asset-id'.")

    reg = _load_registry_obj(registry)
    effective_run_id = run_id or _make_run_id()
    try:
        retried_versions = reg.retry_asset(asset_id, run_id=effective_run_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except (FileNotFoundError, OSError) as exc:
        raise click.ClickException(f"Retry failed: {exc}") from exc

    if not retried_versions:
        click.echo(f"[assets retry] {asset_id}: no versions needed retry (all already registered)")
        return

    result = [v.model_dump() for v in retried_versions]
    if not json_output:
        click.echo(f"[assets retry] retried {len(retried_versions)} version(s) for {asset_id}")
        for v in retried_versions:
            click.echo(f"  version {v.asset_version_id}  status={v.ingestion_status}")
    else:
        _emit(result, json_output=True)


# ---------------------------------------------------------------------------
# lineage command group and trace command
# ---------------------------------------------------------------------------


@click.group(
    "lineage",
    context_settings={"max_content_width": 120},
)
def lineage_cmd() -> None:
    """Inspect lineage paths from derived records to immutable originals."""


@click.command(
    "trace",
    context_settings={"max_content_width": 120},
)
@click.argument("record_id")
@click.option(
    "--registry",
    default=_DEFAULT_REGISTRY,
    show_default=True,
    type=click.Path(),
    help="Path to the local asset registry JSON store.",
)
@click.option(
    "--table",
    "table_name",
    default=None,
    help="Hint the record table name (e.g. entities, chunks, source_files).",
)
@click.option(
    "--direction",
    type=click.Choice(["backward", "forward"], case_sensitive=False),
    default="backward",
    show_default=True,
    help="Traverse toward the source or toward derived records.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
def trace_cmd(
    record_id: str,
    registry: str,
    table_name: str | None,
    direction: str,
    output_format: str,
) -> None:
    """Trace the lineage provenance chain for RECORD_ID.

    Follows asset → version → run → deployment relationships.
    RECORD_ID may be any lineage record identifier (asset_id, run_id, chunk_id, etc.).

    \b
    Example:
      fabric-kg lineage trace <record-id>
      fabric-kg lineage trace <record-id> --format json
      fabric-kg lineage trace <record-id> --table entities
    """
    try:
        from fabric_kg_builder.lineage.trace import trace_record  # type: ignore[import]
    except ImportError as exc:
        raise click.ClickException(
            f"Lineage trace module not available: {exc}."
        ) from exc

    registry_path = Path(registry)
    if not registry_path.exists():
        raise click.ClickException(
            f"Registry not found: {registry_path}. Run 'fabric-kg assets register <path>' first."
        )
    try:
        store = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Failed to read registry '{registry_path}': {exc}") from exc

    try:
        result = trace_record(
            record_id,
            store,
            table_name=table_name,
            direction=direction,
        )
    except (KeyError, LookupError, RuntimeError, ValueError) as exc:
        raise click.ClickException(f"Trace failed for record '{record_id}': {exc}") from exc

    if not result.path:
        raise click.ClickException(
            f"Record '{record_id}' was not found in the lineage registry."
        )

    if output_format == "json":
        click.echo(result.as_json())
        return

    click.echo(f"record_id: {result.record_id}")
    click.echo(f"direction: {result.direction}")
    click.echo(f"is_complete: {str(result.is_complete).lower()}")
    click.echo("path:")
    for table, path_record_id in result.path:
        click.echo(f"  {table:<24} {path_record_id}")
    if result.broken_edge:
        click.echo(
            f"broken_edge: {result.broken_edge[0]}[{result.broken_edge[1]}]",
            err=True,
        )


lineage_cmd.add_command(trace_cmd, name="trace")
