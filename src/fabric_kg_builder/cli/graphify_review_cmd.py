"""Explicit offline Graphify review, separate from native hierarchy production."""

from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
    DerivedHierarchyAdapterError,
    _check_output_root_disjoint,
    _check_output_root_not_already_published,
)
from fabric_kg_builder.semantic.graphify_review import review_graphify_import
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import load_sealed_l4_hierarchy_rows


@click.command("review-graphify-import")
@click.option("--source", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--input-manifest-search-root", "search_roots", multiple=True,
              required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--graph", "graph_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--bindings", "bindings_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", "output", required=True, type=click.Path(path_type=Path))
@click.option("--protected-root", "extra_protected", multiple=True,
              type=click.Path(path_type=Path),
              help="Additional custom authority/state root to protect (repeatable).")
def review_graphify_import_cmd(
    source: Path,
    search_roots: tuple[Path, ...],
    graph_path: Path,
    bindings_path: Path,
    output: Path,
    extra_protected: tuple[Path, ...],
) -> None:
    """Review an actual Graphify nodes/links JSON against sealed L4, offline.

    Requires a project-owned identity/type/evidence binding sidecar. Writes a
    create-only, non-authoritative JSON review, never a hierarchy or publication
    receipt. Does not install, import, or run Graphify. No fallback or remote call.
    Exit codes: 0 shadow review written; 1 input, binding, or output rejection.
    """
    try:
        protected = (
            source, *search_roots, *extra_protected, Path(".fkg/l1"), Path(".fkg/l2"),
            Path(".fkg/l3"), Path(".fkg/l4"), Path(".fkg/derived-hierarchy"),
            Path("build/release"),
        )
        _check_output_root_disjoint(
            output,
            {
                **{f"protected authority {index}": root
                   for index, root in enumerate(protected)},
                "Graphify graph input": graph_path,
                "Graphify bindings input": bindings_path,
            },
        )
        _check_output_root_not_already_published(output)
        sealed = load_sealed_l4_hierarchy_rows(
            source, input_manifest_search_roots=search_roots
        )
        report = review_graphify_import(
            sealed=sealed, graph_path=graph_path, bindings_path=bindings_path
        )
        payload = canonical_json(report) + "\n"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except (OSError, ValueError, DerivedHierarchyAdapterError) as exc:
        raise click.ClickException(f"Graphify shadow review rejected: {exc}") from exc
    click.echo(f"status: shadow_review_only (not publishable)\nreview: {output}")
