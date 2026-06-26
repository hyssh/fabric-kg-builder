"""package command — bundle build artifacts into a deployment-ready dist package."""

from __future__ import annotations

import datetime
import json
import shutil
import sys
from pathlib import Path

import click

_REQUIRED_DIRS = ["parquet", "ontology"]


def _dir_summary(directory: Path) -> dict:
    """Return file-count, total-bytes, and file list for a directory."""
    files = [f for f in directory.rglob("*") if f.is_file()]
    return {
        "file_count": len(files),
        "total_bytes": sum(f.stat().st_size for f in files),
        "files": [str(f.relative_to(directory)).replace("\\", "/") for f in sorted(files)],
    }


_PACKAGE_EPILOG = """\b
Example:
  fabric-kg package
  fabric-kg package --include-search --out dist

Questions? hyssh@microsoft.com
"""


@click.command("package", epilog=_PACKAGE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--build-dir", default="build", show_default=True, type=click.Path(),
              help="Source build directory containing parquet/, ontology/, and optionally search/.")
@click.option("--out", "output_path", default="dist", show_default=True,
              type=click.Path(),
              help="Output directory; creates dist/fabric-kg-package/ with a manifest.json.")
@click.option("--include-search", is_flag=True, default=False,
              help="Include build/search/ AI Search artifacts in the dist package.")
def package_cmd(build_dir: str, output_path: str, include_search: bool) -> None:
    """Bundle all build artifacts into dist/fabric-kg-package/ with a manifest.json.

    Requires build/parquet/ and build/ontology/ to exist (run compile-data and
    compile-ontology first).  Optionally bundles build/search/ when
    --include-search is set.

    Exit codes: 0 success · 1 error (missing required artifacts).
    """
    build_path = Path(build_dir)
    dist_path = Path(output_path)

    # Verify required build artifacts are present
    missing = [req for req in _REQUIRED_DIRS if not (build_path / req).exists()]
    if missing:
        click.echo(
            f"[package] ERROR: Required build artifact(s) not found: "
            f"{', '.join(str(build_path / m) for m in missing)}",
            err=True,
        )
        sys.exit(1)

    # Clean + create package directory
    pkg_dir = dist_path / "fabric-kg-package"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)

    manifest_artifacts: dict = {}

    # Bundle required dirs
    for artifact in _REQUIRED_DIRS:
        src = build_path / artifact
        dst = pkg_dir / artifact
        shutil.copytree(src, dst)
        summary = _dir_summary(dst)
        manifest_artifacts[artifact] = summary
        click.echo(
            f"[package]   {artifact}: {summary['file_count']} file(s), {summary['total_bytes']} bytes"
        )

    # Bundle optional search dir
    search_src = build_path / "search"
    if include_search:
        if search_src.exists():
            shutil.copytree(search_src, pkg_dir / "search")
            summary = _dir_summary(pkg_dir / "search")
            manifest_artifacts["search"] = summary
            click.echo(
                f"[package]   search: {summary['file_count']} file(s), {summary['total_bytes']} bytes"
            )
        else:
            click.echo(
                "[package] WARNING: --include-search set but build/search not found; skipping.",
                err=True,
            )

    # Write manifest.json
    manifest = {
        "schema_version": "1",
        "package_version": "0.1.0",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "build_dir": str(build_path),
        "artifacts": manifest_artifacts,
    }
    manifest_path = pkg_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    click.echo(f"[package] Manifest: {manifest_path}")
    click.echo(
        f"[package] SUCCESS — {len(manifest_artifacts)} artifact(s) bundled to {pkg_dir}"
    )
