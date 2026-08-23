"""package command — bundle build artifacts into a deployment-ready dist package."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import click

from fabric_kg_builder import __version__
from fabric_kg_builder.semantic.artifact_validation import (
    SemanticArtifactValidationError,
    validate_compiled_semantic_artifacts,
)
from fabric_kg_builder.semantic.connection_guide import (
    build_ontology_search_connection_guide,
)

_REQUIRED_DIRS = ["parquet", "semantic", "ontology", "graph", "agents"]
_OPTIONAL_DIRS = ["knowledge", "evidence", "validation", "deployment", "evaluation"]
_REQUIRED_SEMANTIC_FILES = [
    "semantic-model-manifest.json",
    "semantic-crosswalk.json",
    "materialization-plan.json",
    "model-quality-report.json",
    "dependency-graph.json",
]


def _filesystem_path(path: Path) -> Path:
    """Return a Windows extended path so deep semantic trees remain usable."""
    if os.name != "nt":
        return path
    absolute = str(path.resolve())
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute.lstrip("\\"))
    return Path("\\\\?\\" + absolute)


def _dir_summary(directory: Path) -> dict:
    """Return file-count, total-bytes, and file list for a directory."""
    files = [f for f in directory.rglob("*") if f.is_file()]
    files = sorted(files)
    relative_files = [
        str(file.relative_to(directory)).replace("\\", "/") for file in files
    ]
    return {
        "file_count": len(files),
        "total_bytes": sum(f.stat().st_size for f in files),
        "files": relative_files,
        "file_hashes": {
            relative_path: (
                "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
            )
            for relative_path, file in zip(relative_files, files, strict=True)
        },
    }


def _read_contract_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    value = payload.get("contract_hash")
    return str(value) if value else None


_PACKAGE_EPILOG = """\b
Example:
  fabric-kg package
  fabric-kg package --include-search --out dist

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("package", epilog=_PACKAGE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--build-dir", default="build", show_default=True, type=click.Path(),
              help="Source build directory containing the compiled SPEC-008 artifacts.")
@click.option("--out", "output_path", default="dist", show_default=True,
              type=click.Path(),
              help="Output directory; creates dist/fabric-kg-package/ with a manifest.json.")
@click.option("--include-search", is_flag=True, default=False,
              help="Include build/search/ AI Search artifacts in the dist package.")
def package_cmd(build_dir: str, output_path: str, include_search: bool) -> None:
    """Bundle all build artifacts into dist/fabric-kg-package/ with a manifest.json.

    Requires canonical data, semantic, Ontology, Graph, and agent artifacts.
    Optionally bundles Search and any available knowledge, evidence,
    validation, deployment, and evaluation artifacts.

    Exit codes: 0 success · 1 error (missing required artifacts).
    """
    display_build_path = Path(build_dir)
    display_dist_path = Path(output_path)
    build_path = _filesystem_path(display_build_path)
    dist_path = _filesystem_path(display_dist_path)

    # Verify required build artifacts are present
    missing = [req for req in _REQUIRED_DIRS if not (build_path / req).exists()]
    if missing:
        click.echo(
            f"[package] ERROR: Required build artifact(s) not found: "
            f"{', '.join(str(display_build_path / m) for m in missing)}",
            err=True,
        )
        sys.exit(1)
    missing_semantic_files = [
        name
        for name in _REQUIRED_SEMANTIC_FILES
        if not (build_path / "semantic" / name).exists()
    ]
    if missing_semantic_files:
        click.echo(
            "[package] ERROR: Required shared semantic authority artifact(s) "
            f"not found: {', '.join(missing_semantic_files)}",
            err=True,
        )
        sys.exit(1)

    contract_hash_sources = {
        "semantic": _read_contract_hash(
            build_path / "semantic" / "semantic-manifest.json"
        ),
        "ontology": _read_contract_hash(
            build_path / "ontology" / "ontology-manifest.json"
        ),
        "graph": _read_contract_hash(
            build_path / "graph" / "graph-manifest.json"
        ),
        "agents": _read_contract_hash(
            build_path / "agents" / "agent-manifest.json"
        ),
    }
    if include_search and (build_path / "search").exists():
        contract_hash_sources["search"] = _read_contract_hash(
            build_path / "search" / "search-manifest.json"
        )
    missing_hashes = sorted(
        name for name, value in contract_hash_sources.items() if not value
    )
    distinct_hashes = sorted(
        {value for value in contract_hash_sources.values() if value}
    )
    if missing_hashes or len(distinct_hashes) != 1:
        click.echo(
            "[package] ERROR: Semantic contract hash mismatch or omission: "
            f"sources={contract_hash_sources}",
            err=True,
        )
        sys.exit(1)
    contract_hash = distinct_hashes[0]

    try:
        validation_report = validate_compiled_semantic_artifacts(
            build_path,
            require_search=include_search and (build_path / "search").exists(),
            require_model_authority=True,
        )
    except SemanticArtifactValidationError as exc:
        click.echo(
            "[package] ERROR: compiled semantic artifacts failed validation:",
            err=True,
        )
        for finding in exc.findings:
            click.echo(f"  [{finding.code}] {finding.message}", err=True)
        sys.exit(1)
    click.echo(
        "[package] semantic validation: "
        f"{validation_report['status']} ({contract_hash})"
    )

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

    for artifact in _OPTIONAL_DIRS:
        src = build_path / artifact
        if not src.exists():
            continue
        shutil.copytree(src, pkg_dir / artifact)
        summary = _dir_summary(pkg_dir / artifact)
        manifest_artifacts[artifact] = summary
        click.echo(
            f"[package]   {artifact}: {summary['file_count']} file(s), "
            f"{summary['total_bytes']} bytes"
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

    guide_path = pkg_dir / "ONTOLOGY_SEARCH_CONNECTION.md"
    guide_path.write_text(
        build_ontology_search_connection_guide(pkg_dir),
        encoding="utf-8",
    )
    guide_hash = "sha256:" + hashlib.sha256(guide_path.read_bytes()).hexdigest()
    manifest_artifacts["connection_guide"] = {
        "file_count": 1,
        "total_bytes": guide_path.stat().st_size,
        "files": [guide_path.name],
        "file_hashes": {guide_path.name: guide_hash},
    }
    click.echo(f"[package]   connection guide: {guide_path.name}")

    # Write manifest.json
    package_hash = hashlib.sha256(
        json.dumps(
            manifest_artifacts,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": "1",
        "package_version": __version__,
        "contract_hash": contract_hash,
        "semantic_model_manifest_hash": validation_report.get(
            "semantic_model_manifest_hash"
        ),
        "package_hash": f"sha256:{package_hash}",
        "artifacts": manifest_artifacts,
    }
    manifest_path = pkg_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    display_pkg_dir = display_dist_path / "fabric-kg-package"
    click.echo(f"[package] Manifest: {display_pkg_dir / 'manifest.json'}")
    click.echo(
        f"[package] SUCCESS — {len(manifest_artifacts)} artifact(s) bundled to "
        f"{display_pkg_dir}"
    )
