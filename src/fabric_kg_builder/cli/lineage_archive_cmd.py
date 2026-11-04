"""Additive local archive commands; deliberately separate from serving trace."""

from __future__ import annotations

import json
from pathlib import Path

import click
from pydantic_core import to_jsonable_python


@click.command("archive")
@click.option("--l1-state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--domain", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--l2-state", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--l3-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--l4-run", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--publication", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Existing default prototype materialize directory (tables/ and definitions/).")
@click.option("--workspace-id", required=True, help="Workspace identity used by the local materialization.")
@click.option("--name-prefix", required=True, help="Prefix used by the local materialization.")
@click.option("--publication-version", required=True, help="Caller-owned local snapshot ID, not a Delta version.")
@click.option("--out", required=True, type=click.Path(path_type=Path), help="New, disjoint archive directory.")
@click.option("--source-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Explicit authorization to read/hash/copy corpus-listed originals into the archive.")
@click.option("--layout-cache", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Explicit CachedLayout JSON file; repeat for multiple caches. No DI service call.")
def archive_cmd(**kwargs) -> None:
    """Create a private local provenance snapshot; never publish or change trust."""
    from fabric_kg_builder.lineage.archive import create_archive

    try:
        result = create_archive(**kwargs)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@click.command("resolve")
@click.option("--archive", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--publication-version", required=True)
@click.option("--table", required=True)
@click.option("--record", required=True)
@click.option("--field", default=None)
@click.option("--source-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Explicit authorization to read originals here, instead of archived source bytes.")
@click.option("--archive-hash", default=None, help="Expected archive hash from a separately trusted index.")
def resolve_cmd(**kwargs) -> None:
    """Resolve historical metadata and report source-byte access independently."""
    from fabric_kg_builder.lineage.archive import resolve_archive

    try:
        result = resolve_archive(**kwargs)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(to_jsonable_python(result), indent=2, sort_keys=True))
