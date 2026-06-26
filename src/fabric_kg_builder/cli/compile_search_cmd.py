"""compile-search command — generate AI Search index schemas and document batches.

Sprint 2: reads canonical Parquet tables (build/parquet or build/enriched),
derives AI Search documents via search.linkage, optionally attaches embeddings
via search.embeddings, and writes:
  - build/search/{index}/index.schema.json  (always, even when no docs)
  - build/search/{index}/docs.json          (when docs are found)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

# ---------------------------------------------------------------------------
# Schema building helpers
# SPEC-001 §7, SPEC-002 §11 — text/visual tables only; structured tables
# (entities, relationships, evidence, source_files) stay in the Lakehouse.
# ---------------------------------------------------------------------------

_VECTOR_DIMS = 1536  # LOCKED — must match text-embedding-3-large@1536 (SPEC-002 §11.7)


def _common_entity_linkage_fields() -> list[dict]:
    """Entity-linkage fields shared by both indexes (SPEC-002 §11.3)."""
    return [
        {
            "name": "entity_ids",
            "type": "Collection(Edm.String)",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": (
                "Opaque stable entity IDs from chunks.related_entity_ids. "
                "Use search.in() filter only — never BM25 text."
            ),
        },
        {
            "name": "entity_aliases",
            "type": "Collection(Edm.String)",
            "searchable": True,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": (
                "Human-readable aliases from entities.search_aliases denormalized "
                "via chunks.entity_search_keys. BM25 keyword matching only."
            ),
        },
        {
            "name": "canonical_key",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Normalized canonical key of primary entity. Stable exact-match filter.",
        },
        {
            "name": "entity_types",
            "type": "Collection(Edm.String)",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": "entity_type values for all linked entity_ids; enables faceting by kind.",
        },
        {
            "name": "graph_path",
            "type": "Edm.String",
            "searchable": False,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": (
                "Serialized GQL traversal path injected at push time (not in Parquet). "
                "E.g. 'Device --[has_component]--> Component'."
            ),
        },
        {
            "name": "blob_url",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Blob Storage URL for image/figure chunks. Null for text-only chunks.",
        },
        {
            "name": "source_path",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Relative source file path; enables per-document scoping.",
        },
        {
            "name": "last_modified",
            "type": "Edm.DateTimeOffset",
            "searchable": False,
            "filterable": True,
            "sortable": True,
            "facetable": False,
            "retrievable": True,
            "_comment": "Maps to chunks.created_at; content change -> new chunk_id + new timestamp.",
        },
        {
            "name": "content_type",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": (
                "From chunks.chunk_type: section_text, table_html, image_description, "
                "procedure_step, figure_caption. Enables result-type faceting."
            ),
        },
    ]


def _vector_field(name: str = "chunk_vector") -> dict:
    """Return a 1536-dim HNSW vector field (text-embedding-3-large, LOCKED)."""
    return {
        "name": name,
        "type": "Collection(Edm.Single)",
        "searchable": True,
        "filterable": False,
        "sortable": False,
        "facetable": False,
        "retrievable": False,
        "dimensions": _VECTOR_DIMS,
        "vectorSearchProfile": "hnsw-text-embedding-3-large",
        "_comment": (
            f"LOCKED: {_VECTOR_DIMS}-dim vector from text-embedding-3-large. "
            "Changing model/dims requires full reindex (SPEC-002 §11.7)."
        ),
    }


def _build_chunks_schema() -> dict:
    """AI Search index schema for the kg-chunks index (SPEC-001 §6, SPEC-002 §11)."""
    fields: list[dict] = [
        {
            "name": "chunk_id",
            "type": "Edm.String",
            "key": True,
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
        },
        {
            "name": "content",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Full chunk text; primary BM25 search field.",
        },
        {
            "name": "embedding_text",
            "type": "Edm.String",
            "searchable": False,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": False,
            "_comment": "Cleaned embedding input text; not exposed to end-users.",
        },
        _vector_field("chunk_vector"),
    ]
    fields.extend(_common_entity_linkage_fields())

    return {
        "_schema_version": "1",
        "_sprint": "1 — placeholder schema only; documents generated in Sprint 2",
        "name": "kg-chunks",
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                }
            ],
            "profiles": [
                {
                    "name": "hnsw-text-embedding-3-large",
                    "algorithm": "hnsw-config",
                }
            ],
        },
        "semantic": {
            "defaultConfiguration": "kg-chunks-semantic",
            "configurations": [
                {
                    "name": "kg-chunks-semantic",
                    "prioritizedFields": {
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [{"fieldName": "entity_aliases"}],
                        "titleField": {"fieldName": "canonical_key"},
                    },
                }
            ],
        },
    }


def _build_document_elements_schema() -> dict:
    """AI Search index schema for kg-document-elements (SPEC-001 §6, SPEC-002 §11)."""
    fields: list[dict] = [
        {
            "name": "document_element_id",
            "type": "Edm.String",
            "key": True,
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
        },
        {
            "name": "content",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Element text content or HTML rendering; primary BM25 field.",
        },
        {
            "name": "content_html",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "HTML rendering for table elements.",
        },
        {
            "name": "element_type",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": "table_row | figure | table | section | paragraph | heading | caption",
        },
        {
            "name": "page_number",
            "type": "Edm.Int32",
            "searchable": False,
            "filterable": True,
            "sortable": True,
            "facetable": False,
            "retrievable": True,
        },
        {
            "name": "section_path",
            "type": "Edm.String",
            "searchable": True,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
        },
        _vector_field("element_vector"),
    ]
    fields.extend(_common_entity_linkage_fields())

    return {
        "_schema_version": "1",
        "_sprint": "1 — placeholder schema only; documents generated in Sprint 2",
        "name": "kg-document-elements",
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                }
            ],
            "profiles": [
                {
                    "name": "hnsw-text-embedding-3-large",
                    "algorithm": "hnsw-config",
                }
            ],
        },
        "semantic": {
            "defaultConfiguration": "kg-doc-elements-semantic",
            "configurations": [
                {
                    "name": "kg-doc-elements-semantic",
                    "prioritizedFields": {
                        "prioritizedContentFields": [
                            {"fieldName": "content"},
                            {"fieldName": "content_html"},
                        ],
                        "prioritizedKeywordsFields": [{"fieldName": "entity_aliases"}],
                        "titleField": {"fieldName": "section_path"},
                    },
                }
            ],
        },
    }


# Registry: index name -> (schema_builder_fn, parquet_table, doc_deriver_fn)
_INDEXES: dict[str, dict[str, Any]] = {
    "kg-chunks": {
        "schema_fn": _build_chunks_schema,
        "parquet_table": "chunks",
        "id_field": "chunk_id",
        "vector_field": "chunk_vector",
        "text_field": "embedding_text",
    },
    "kg-document-elements": {
        "schema_fn": _build_document_elements_schema,
        "parquet_table": "document_elements",
        "id_field": "document_element_id",
        "vector_field": "element_vector",
        "text_field": "content",
    },
}


def _read_parquet_table(parquet_dir: Path, table_name: str) -> list[dict[str, Any]]:
    """Read a Parquet table into a list of row dicts. Returns [] if missing."""
    path = parquet_dir / f"{table_name}.parquet"
    if not path.exists():
        return []
    try:
        import pyarrow.parquet as pq  # type: ignore[import]
        table = pq.read_table(str(path))
        return table.to_pylist()
    except Exception:
        return []


_COMPILE_SEARCH_EPILOG = """\b
Example:
  fabric-kg compile-search
  fabric-kg compile-search --input build/parquet --embed
  fabric-kg compile-search --indexes kg-chunks

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("compile-search", epilog=_COMPILE_SEARCH_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--input", "input_path", default="build/parquet", show_default=True,
              type=click.Path(),
              help="Directory containing canonical Parquet tables (output of compile-data).")
@click.option("--out", "output_path", default="build/search", show_default=True,
              type=click.Path(),
              help="Output directory; writes {index}/index.schema.json and {index}/docs.json.")
@click.option("--indexes", default=None, show_default=True,
              help="Comma-separated subset of indexes to compile "
                   "(default: kg-chunks,kg-document-elements).")
@click.option("--embed", is_flag=True, default=False,
              help="Attach 1536-dim embeddings to vector fields "
                   "(requires AZURE_AI_FOUNDRY_ENDPOINT env var).")
def compile_search_cmd(
    input_path: str,
    output_path: str,
    indexes: str | None,
    embed: bool,
) -> None:
    """Generate AI Search index schemas and document batches from canonical Parquet tables.

    Reads chunks and document_elements Parquet tables from --input, derives
    AI Search documents with entity linkage fields (entity_ids, entity_aliases,
    canonical_key, graph_path, blob_url, content_type), optionally attaches
    1536-dim embeddings (text-embedding-3-large, LOCKED — SPEC-002 §11.7), and
    writes index.schema.json + docs.json to --out/{index}/.

    Only text/visual tables are indexed here; structured tables (entities,
    relationships, evidence, source_files) remain in the Fabric Lakehouse.

    Exit codes: 0 success · 1 error.
    """
    # Lazy imports — modules may not exist yet in early sprint environments
    try:
        from fabric_kg_builder.search.linkage import (
            derive_chunk_doc,
            derive_document_element_doc,
            build_entity_lookup,
        )
    except ImportError as exc:  # pragma: no cover
        click.echo(f"[compile-search] ERROR: cannot import search.linkage: {exc}", err=True)
        sys.exit(1)

    out_path = Path(output_path)
    in_path = Path(input_path)

    selected = (
        [i.strip() for i in indexes.split(",") if i.strip()]
        if indexes
        else list(_INDEXES.keys())
    )

    unknown = [i for i in selected if i not in _INDEXES]
    if unknown:
        click.echo(
            f"[compile-search] ERROR: Unknown index name(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(_INDEXES.keys())}",
            err=True,
        )
        sys.exit(1)

    click.echo(f"[compile-search] Input  : {in_path}")
    click.echo(f"[compile-search] Output : {out_path}")
    click.echo(f"[compile-search] Embed  : {embed}")

    # Load entities once — shared across both indexes for entity linkage
    entities_rows = _read_parquet_table(in_path, "entities")
    entities_by_id = build_entity_lookup(entities_rows)
    click.echo(f"[compile-search] Entities loaded: {len(entities_by_id)}")

    total_docs = 0
    for index_name in selected:
        cfg = _INDEXES[index_name]
        schema_fn = cfg["schema_fn"]
        parquet_table: str = cfg["parquet_table"]
        id_field: str = cfg["id_field"]
        vector_field: str = cfg["vector_field"]
        text_field: str = cfg["text_field"]

        # Always write the schema
        schema = schema_fn()
        # Update sprint marker now that docs are generated
        schema["_sprint"] = "2"
        index_dir = out_path / index_name
        index_dir.mkdir(parents=True, exist_ok=True)
        schema_path = index_dir / "index.schema.json"
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")

        vec_dims = next(
            (f.get("dimensions") for f in schema.get("fields", []) if f.get("dimensions")),
            "n/a",
        )
        click.echo(
            f"[compile-search]   {index_name}/index.schema.json "
            f"({len(schema['fields'])} fields, vector dims={vec_dims})"
        )

        # Read source Parquet and derive docs
        rows = _read_parquet_table(in_path, parquet_table)
        if not rows:
            click.echo(
                f"[compile-search]   {index_name}: no rows found in "
                f"{in_path}/{parquet_table}.parquet — docs.json skipped."
            )
            continue

        deriver = (
            derive_chunk_doc
            if index_name == "kg-chunks"
            else derive_document_element_doc
        )
        docs: list[dict[str, Any]] = [
            deriver(row, entities_by_id) for row in rows
        ]

        # Optionally attach embeddings
        if embed:
            try:
                from fabric_kg_builder.search.embeddings import attach_vectors
                docs = attach_vectors(
                    docs,
                    text_field=text_field,
                    vector_field=vector_field,
                )
            except Exception as exc:
                click.echo(
                    f"[compile-search] WARNING: embeddings failed for {index_name}: {exc}",
                    err=True,
                )

        docs_path = index_dir / "docs.json"
        docs_path.write_text(json.dumps(docs, indent=2, default=str), encoding="utf-8")
        total_docs += len(docs)

        click.echo(
            f"[compile-search]   {index_name}/docs.json — {len(docs)} documents"
        )

    click.echo(
        f"[compile-search] SUCCESS — {len(selected)} schema(s), "
        f"{total_docs} total docs written to {out_path}"
    )
