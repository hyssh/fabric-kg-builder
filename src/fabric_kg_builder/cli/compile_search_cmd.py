"""compile-search command — generate AI Search index schemas and document batches.

Sprint 2: reads canonical Parquet tables (build/parquet or build/enriched),
derives AI Search documents via search.linkage, optionally attaches embeddings
via search.embeddings, and writes:
  - build/search/{index}/index.schema.json  (always, even when no docs)
  - build/search/{index}/docs.json          (when docs are found)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from fabric_kg_builder.semantic import (
    SemanticCompileError,
    load_semantic_model_artifacts,
)

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
            "name": "semantic_type_ids",
            "type": "Collection(Edm.String)",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": (
                "Approved canonical semantic entity type IDs for Graph/Search "
                "cross-store routing. Discovery-only entities are excluded."
            ),
        },
        {
            "name": "semantic_property_ids",
            "type": "Collection(Edm.String)",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": (
                "Canonical property IDs projected for the linked semantic "
                "entity types through the sealed crosswalk."
            ),
        },
        {
            "name": "semantic_relationship_ids",
            "type": "Collection(Edm.String)",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": (
                "Canonical relationship IDs joined through evidence records "
                "for Graph/Search routing."
            ),
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


def _lineage_security_fields(*, include_document_element_id: bool = True) -> list[dict]:
    """Canonical lineage + security fields shared by all indexes (M6 SRV-002)."""
    return [
        # ── Lineage envelope — filterable for scoped retrieval ────────────────
        {
            "name": "project_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Project scope from CommonLineageRow.project_id.",
        },
        {
            "name": "asset_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Logical asset ID from CommonLineageRow.asset_id.",
        },
        {
            "name": "asset_version_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Immutable version ID from CommonLineageRow.asset_version_id.",
        },
        {
            "name": "run_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Pipeline execution run ID from CommonLineageRow.run_id.",
        },
        {
            "name": "source_file_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Source file record ID; links to source_files Lakehouse table.",
        },
        # ── Source location + provenance — retrievable only ──────────────────
        {
            "name": "source_locator_json",
            "type": "Edm.String",
            "searchable": False,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Serialised source_locator JSON (page, char range, polygon, etc.).",
        },
        {
            "name": "schema_version",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Schema version from CommonLineageRow.schema_version (e.g. '2.0').",
        },
        {
            "name": "domain_hash",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Domain configuration hash; enables per-domain scoped query.",
        },
        {
            "name": "semantic_contract_hash",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": (
                "Approved canonical semantic contract hash shared by Ontology, "
                "Graph, Search, and agent artifacts."
            ),
        },
        # ── Security placeholders — retrievable, not searchable/filterable ────
        {
            "name": "sensitivity_label",
            "type": "Edm.String",
            "searchable": False,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "MIP/Purview sensitivity label (placeholder; populated at push time).",
        },
        {
            "name": "acl_json",
            "type": "Edm.String",
            "searchable": False,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Serialised ACL/permission list (placeholder; populated at push time).",
        },
    ] + (
        [
            {
                "name": "document_element_id",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "sortable": False,
                "facetable": False,
                "retrievable": True,
                "_comment": "Document element record ID; links to document_elements Lakehouse table.",
            },
        ]
        if include_document_element_id
        else []
    )


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
            "name": "source_quote",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": (
                "Source passage retained for quotation and citation. "
                "Use source_quote_is_verbatim before presenting it as a direct quote."
            ),
        },
        {
            "name": "source_quote_is_verbatim",
            "type": "Edm.Boolean",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": (
                "True only when source_quote is original extracted source text; "
                "false for derived descriptions."
            ),
        },
        {
            "name": "evidence_ids",
            "type": "Collection(Edm.String)",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": (
                "Evidence records whose immutable source spans resolve to "
                "this chunk."
            ),
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
    fields.extend(_lineage_security_fields())

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
            "name": "source_quote",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "sortable": False,
            "facetable": False,
            "retrievable": True,
            "_comment": "Original extracted element text retained for quotation.",
        },
        {
            "name": "source_quote_is_verbatim",
            "type": "Edm.Boolean",
            "searchable": False,
            "filterable": True,
            "sortable": False,
            "facetable": True,
            "retrievable": True,
            "_comment": "True when source_quote is original extracted source text.",
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
    fields.extend(_lineage_security_fields(include_document_element_id=False))

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


def _build_visual_assets_schema() -> dict:
    """AI Search index schema for visual assets and regions."""
    fields: list[dict] = [
        {"name": "visual_id", "type": "Edm.String", "key": True, "searchable": False,
         "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
        {"name": "record_type", "type": "Edm.String", "searchable": False,
         "filterable": True, "sortable": False, "facetable": True, "retrievable": True},
        {"name": "image_id", "type": "Edm.String", "searchable": False,
         "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
        {"name": "visual_region_id", "type": "Edm.String", "searchable": False,
         "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
        {"name": "document_element_id", "type": "Edm.String", "searchable": False,
         "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
        {"name": "content", "type": "Edm.String", "searchable": True,
         "filterable": False, "sortable": False, "facetable": False, "retrievable": True},
        {"name": "embedding_text", "type": "Edm.String", "searchable": False,
         "filterable": False, "sortable": False, "facetable": False, "retrievable": False},
        {"name": "asset_type", "type": "Edm.String", "searchable": False,
         "filterable": True, "sortable": False, "facetable": True, "retrievable": True},
        {"name": "region_type", "type": "Edm.String", "searchable": False,
         "filterable": True, "sortable": False, "facetable": True, "retrievable": True},
        {"name": "page_number", "type": "Edm.Int32", "searchable": False,
         "filterable": True, "sortable": True, "facetable": False, "retrievable": True},
        {"name": "section_path", "type": "Edm.String", "searchable": True,
         "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
        {"name": "polygon_json", "type": "Edm.String", "searchable": False,
         "filterable": False, "sortable": False, "facetable": False, "retrievable": True},
        _vector_field("visual_vector"),
    ]
    fields.extend(_common_entity_linkage_fields())
    fields.extend(_lineage_security_fields(include_document_element_id=False))
    return {
        "_schema_version": "1",
        "name": "kg-visual-assets",
        "fields": fields,
        "vectorSearch": _build_chunks_schema()["vectorSearch"],
        "semantic": {
            "defaultConfiguration": "kg-visual-assets-semantic",
            "configurations": [{
                "name": "kg-visual-assets-semantic",
                "prioritizedFields": {
                    "prioritizedContentFields": [{"fieldName": "content"}],
                    "titleField": {"fieldName": "image_id"},
                },
            }],
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
    "kg-visual-assets": {
        "schema_fn": _build_visual_assets_schema,
        "parquet_table": "visual_assets",
        "id_field": "visual_id",
        "vector_field": "visual_vector",
        "text_field": "embedding_text",
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


def _iter_json_array(path: Path):
    """Stream JSON-array items without loading the prior artifact at once."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        buffer = ""
        position = 0
        started = False
        eof = False
        while True:
            if position >= len(buffer) and not eof:
                buffer = handle.read(1024 * 1024)
                position = 0
                eof = not buffer
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    if eof:
                        raise ValueError(f"{path} does not contain a JSON array.")
                    buffer += handle.read(1024 * 1024)
                    continue
                if buffer[position] != "[":
                    raise ValueError(f"{path} does not contain a JSON array.")
                position += 1
                started = True
                continue
            while position < len(buffer) and (
                buffer[position].isspace() or buffer[position] == ","
            ):
                position += 1
            if position < len(buffer) and buffer[position] == "]":
                return
            try:
                item, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError as exc:
                if eof:
                    raise ValueError(
                        f"Could not parse prior Search documents in {path}: {exc}"
                    ) from exc
                buffer = buffer[position:] + handle.read(1024 * 1024)
                position = 0
                eof = handle.tell() == path.stat().st_size
                continue
            if isinstance(item, dict):
                yield item
            position = end
            if position > 4 * 1024 * 1024:
                buffer = buffer[position:]
                position = 0


def _reuse_unchanged_vectors(
    docs: list[dict[str, Any]],
    *,
    prior_docs_path: Path,
    id_field: str,
    text_field: str,
    vector_field: str,
    dimensions: int,
) -> int:
    """Reuse prior vectors only when the key and embedding input are unchanged."""
    if not prior_docs_path.exists():
        return 0
    from fabric_kg_builder.search.embedding_input import (
        EMBEDDING_TOKEN_ENCODING,
        document_embedding_text,
    )
    from fabric_kg_builder.sources.chunker import TiktokenTokenizer

    tokenizer = TiktokenTokenizer(EMBEDDING_TOKEN_ENCODING)
    current = {
        str(document.get(id_field) or ""): document
        for document in docs
        if document.get(id_field)
    }
    reused = 0
    for prior in _iter_json_array(prior_docs_path):
        document = current.get(str(prior.get(id_field) or ""))
        vector = prior.get(vector_field)
        if (
            document is not None
            and document_embedding_text(
                prior,
                text_field=text_field,
                tokenizer=tokenizer,
            )
            == document_embedding_text(
                document,
                text_field=text_field,
                tokenizer=tokenizer,
            )
            and isinstance(vector, list)
            and len(vector) == dimensions
        ):
            document[vector_field] = vector
            reused += 1
    return reused


def _semantic_entity_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    raw = row.get("properties_json")
    if isinstance(raw, dict):
        metadata.update(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            metadata.update(parsed)
    for key in (
        "semantic_contract_hash",
        "semantic_lane",
        "semantic_type_id",
    ):
        if row.get(key) not in {None, ""}:
            metadata[key] = row[key]
    return metadata


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item)]
        return [value]
    return []


def _validate_authoritative_entity_metadata(
    rows: list[dict[str, Any]],
    *,
    semantic_contract_hash: str | None,
    semantic_manifest_path: Path,
    semantic_model_manifest_path: Path | None = None,
) -> None:
    """Reject stale or foreign authoritative metadata before Search compile."""
    authoritative = [
        (row, _semantic_entity_metadata(row))
        for row in rows
        if _semantic_entity_metadata(row).get("semantic_lane")
        == "authoritative"
    ]
    if not authoritative:
        return
    if not semantic_contract_hash:
        raise click.ClickException(
            "Authoritative entity metadata requires an active semantic "
            "contract hash."
        )
    active_type_ids: set[str]
    if (
        semantic_model_manifest_path is not None
        and semantic_model_manifest_path.exists()
    ):
        try:
            model_manifest = json.loads(
                semantic_model_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise click.ClickException(
                "Authoritative entity metadata requires a readable sealed "
                f"semantic model manifest: {exc}"
            ) from exc
        active_type_ids = {
            str(entity.get("semantic_id"))
            for entity in model_manifest.get("entity_types", [])
            if isinstance(entity, dict) and entity.get("semantic_id")
        }
    else:
        contract_path = (
            semantic_manifest_path.parent / "normalized-contract.json"
        )
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise click.ClickException(
                "Authoritative entity metadata requires the normalized "
                f"contract beside the semantic manifest: {exc}"
            ) from exc
        if not isinstance(contract, dict):
            raise click.ClickException(
                "The normalized semantic contract must be a JSON object."
            )
        active_type_ids = {
            str(entity.get("id"))
            for entity in contract.get("entity_types", [])
            if isinstance(entity, dict) and entity.get("id")
        }
    for row, metadata in authoritative:
        row_id = row.get("entity_id") or row.get("canonical_key") or "<unknown>"
        embedded_hash = metadata.get("semantic_contract_hash")
        semantic_type_id = metadata.get("semantic_type_id")
        if embedded_hash != semantic_contract_hash:
            raise click.ClickException(
                f"Authoritative entity '{row_id}' is bound to "
                f"{embedded_hash!r}; expected {semantic_contract_hash!r}."
            )
        if semantic_type_id not in active_type_ids:
            raise click.ClickException(
                f"Authoritative entity '{row_id}' references unknown active "
                f"semantic type {semantic_type_id!r}."
            )


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
                   "(default: kg-chunks,kg-document-elements,kg-visual-assets).")
@click.option("--embed", is_flag=True, default=False,
              help="Attach 1536-dim embeddings to vector fields "
                   "(requires AZURE_AI_FOUNDRY_ENDPOINT env var).")
@click.option(
    "--embedding-deployment",
    default=None,
    help="Embedding deployment name (default: AZURE_AI_EMBEDDING_DEPLOYMENT or embedding).",
)
@click.option(
    "--semantic-manifest",
    default="build/semantic/semantic-manifest.json",
    show_default=True,
    type=click.Path(),
    help="Shared semantic compiler manifest used to stamp Search artifacts.",
)
@click.option(
    "--semantic-model-manifest",
    default="build/semantic/semantic-model-manifest.json",
    show_default=True,
    type=click.Path(),
    help=(
        "Sealed semantic model manifest. When present, Search linkage is "
        "validated against this authority and its canonical crosswalk."
    ),
)
@click.option(
    "--semantic-crosswalk",
    default="build/semantic/semantic-crosswalk.json",
    show_default=True,
    type=click.Path(),
    help="Canonical-to-physical crosswalk paired with the model manifest.",
)
@click.option(
    "--require-semantic-contract",
    is_flag=True,
    default=False,
    help="Fail when the semantic manifest is absent or lacks a contract hash.",
)
@click.option(
    "--require-visual-assets",
    is_flag=True,
    default=False,
    help=(
        "Fail when kg-visual-assets is selected but no visual asset or region "
        "rows are available."
    ),
)
def compile_search_cmd(
    input_path: str,
    output_path: str,
    indexes: str | None,
    embed: bool,
    embedding_deployment: str | None,
    semantic_manifest: str,
    semantic_model_manifest: str,
    semantic_crosswalk: str,
    require_semantic_contract: bool,
    require_visual_assets: bool,
) -> None:
    """Generate AI Search index schemas and document batches from canonical Parquet tables.

    Reads chunks, document_elements, visual_assets, and visual_regions Parquet
    tables from --input, derives
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
            build_entity_mention_index,
            derive_chunk_doc,
            derive_document_element_doc,
            derive_visual_docs,
            build_entity_lookup,
        )
    except ImportError as exc:  # pragma: no cover
        click.echo(f"[compile-search] ERROR: cannot import search.linkage: {exc}", err=True)
        sys.exit(1)

    out_path = Path(output_path)
    in_path = Path(input_path)
    from fabric_kg_builder.search.embedding_input import (
        EMBEDDING_INPUT_VERSION,
    )

    prior_manifest: dict[str, Any] = {}
    prior_manifest_path = out_path / "search-manifest.json"
    if embed and prior_manifest_path.exists():
        try:
            loaded_prior_manifest = json.loads(
                prior_manifest_path.read_text(encoding="utf-8")
            )
            if isinstance(loaded_prior_manifest, dict):
                prior_manifest = loaded_prior_manifest
        except (OSError, json.JSONDecodeError):
            prior_manifest = {}
    can_reuse_vectors = (
        prior_manifest.get("embedding_model") == "text-embedding-3-large"
        and prior_manifest.get("embedding_dimensions") == _VECTOR_DIMS
        and prior_manifest.get("embedding_input_version")
        == EMBEDDING_INPUT_VERSION
        and prior_manifest.get("vectorization_mode") == "pipeline"
    )
    semantic_manifest_path = Path(semantic_manifest)
    semantic_model_manifest_path = Path(semantic_model_manifest)
    semantic_crosswalk_path = Path(semantic_crosswalk)
    semantic_contract_hash: str | None = None
    semantic_model_manifest_hash: str | None = None
    semantic_crosswalk_hash: str | None = None
    semantic_authority = None
    if semantic_model_manifest_path.exists() or semantic_crosswalk_path.exists():
        if not (
            semantic_model_manifest_path.exists()
            and semantic_crosswalk_path.exists()
        ):
            raise click.ClickException(
                "Search semantic linkage requires both "
                "semantic-model-manifest.json and semantic-crosswalk.json."
            )
        try:
            loaded = load_semantic_model_artifacts(
                semantic_model_manifest_path.parent
            )
        except SemanticCompileError as exc:
            raise click.ClickException(str(exc)) from exc
        semantic_contract_hash = loaded.manifest.semantic_contract_hash
        semantic_model_manifest_hash = loaded.manifest.manifest_hash
        semantic_crosswalk_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    loaded.crosswalk.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        semantic_authority = loaded
    elif semantic_manifest_path.exists():
        try:
            semantic_manifest_payload = json.loads(
                semantic_manifest_path.read_text(encoding="utf-8")
            )
            semantic_contract_hash = semantic_manifest_payload.get(
                "contract_hash"
            )
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            raise click.ClickException(
                f"Invalid semantic manifest '{semantic_manifest_path}': {exc}"
            ) from exc
    if require_semantic_contract and not semantic_contract_hash:
        raise click.ClickException(
            "Search compilation requires a shared semantic manifest with a "
            "contract_hash."
        )

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
    click.echo(
        f"[compile-search] Semantic contract: "
        f"{semantic_contract_hash or 'compatibility-unset'}"
    )

    # Load entities once — shared across indexes for entity linkage
    entities_rows = _read_parquet_table(in_path, "entities")
    _validate_authoritative_entity_metadata(
        entities_rows,
        semantic_contract_hash=semantic_contract_hash,
        semantic_manifest_path=semantic_manifest_path,
        semantic_model_manifest_path=(
            semantic_model_manifest_path
            if semantic_model_manifest_path.exists()
            else None
        ),
    )
    entities_by_id = build_entity_lookup(entities_rows)
    entity_mention_index = build_entity_mention_index(entities_rows)
    click.echo(f"[compile-search] Entities loaded: {len(entities_by_id)}")
    evidence_rows = _read_parquet_table(in_path, "evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    evidence_ids_by_chunk: dict[str, list[str]] = {}
    evidence_ids_by_element: dict[str, list[str]] = {}
    for row in evidence_rows:
        chunk_id = str(row.get("chunk_id") or "")
        element_id = str(row.get("document_element_id") or "")
        evidence_id = str(row.get("evidence_id") or "")
        if evidence_id:
            evidence_by_id[evidence_id] = row
        if chunk_id and evidence_id:
            evidence_ids_by_chunk.setdefault(chunk_id, []).append(
                evidence_id
            )
        if element_id and evidence_id:
            evidence_ids_by_element.setdefault(element_id, []).append(
                evidence_id
            )
    evidence_ids_by_chunk = {
        chunk_id: list(dict.fromkeys(evidence_ids))
        for chunk_id, evidence_ids in evidence_ids_by_chunk.items()
    }
    evidence_ids_by_element = {
        element_id: list(dict.fromkeys(evidence_ids))
        for element_id, evidence_ids in evidence_ids_by_element.items()
    }
    property_ids_by_type: dict[str, list[str]] = {}
    relationship_ids_by_evidence: dict[str, list[str]] = {}
    relationship_entity_ids_by_evidence: dict[str, set[str]] = {}
    relationship_descriptions_by_evidence: dict[str, list[str]] = {}
    if semantic_authority is not None:
        for prop in semantic_authority.manifest.property_definitions:
            property_ids_by_type.setdefault(
                prop.owner_type_id,
                [],
            ).append(prop.property_id)
        for semantic_id in property_ids_by_type:
            property_ids_by_type[semantic_id] = sorted(
                set(property_ids_by_type[semantic_id])
            )
        for row in _read_parquet_table(in_path, "semantic_relationships"):
            relationship_id = str(
                row.get("semantic_relationship_id") or ""
            )
            if not relationship_id:
                continue
            evidence_ids = _string_list(row.get("evidence_ids_json"))
            evidence_ids.extend(_string_list(row.get("evidence_id")))
            for evidence_id in evidence_ids:
                relationship_ids_by_evidence.setdefault(
                    evidence_id,
                    [],
                ).append(relationship_id)
                relationship_entity_ids_by_evidence.setdefault(
                    evidence_id,
                    set(),
                ).update(
                    value
                    for value in (
                        str(row.get("source_entity_id") or ""),
                        str(row.get("target_entity_id") or ""),
                    )
                    if value
                )
                description = str(row.get("description") or "").strip()
                if description:
                    relationship_descriptions_by_evidence.setdefault(
                        evidence_id,
                        [],
                    ).append(description)
        relationship_ids_by_evidence = {
            evidence_id: sorted(set(relationship_ids))
            for evidence_id, relationship_ids in (
                relationship_ids_by_evidence.items()
            )
        }

    total_docs = 0
    compiled_indexes: list[dict[str, Any]] = []
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
        schema["_semantic_contract_hash"] = semantic_contract_hash
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
        visual_regions = (
            _read_parquet_table(in_path, "visual_regions")
            if index_name == "kg-visual-assets"
            else []
        )
        if not rows and not visual_regions:
            docs_path = index_dir / "docs.json"
            docs_path.unlink(missing_ok=True)
            if index_name == "kg-visual-assets" and require_visual_assets:
                raise click.ClickException(
                    "kg-visual-assets is required but visual_assets.parquet and "
                    "visual_regions.parquet contain no rows. Review each source "
                    "visual_extraction status and configure Document Intelligence "
                    "plus Blob storage before recompiling."
                )
            click.echo(
                f"[compile-search]   WARNING: {index_name}: no rows found in "
                f"{in_path}/{parquet_table}.parquet — docs.json skipped."
            )
            compiled_indexes.append(
                {
                    "name": index_name,
                    "document_count": 0,
                    "schema_sha256": (
                        "sha256:"
                        + hashlib.sha256(schema_path.read_bytes()).hexdigest()
                    ),
                    "docs_sha256": None,
                }
            )
            continue

        if index_name == "kg-visual-assets":
            docs = derive_visual_docs(
                rows,
                visual_regions,
                entities_by_id,
            )
        else:
            deriver = (
                derive_chunk_doc
                if index_name == "kg-chunks"
                else derive_document_element_doc
            )
            docs = [
                deriver(
                    row,
                    entities_by_id,
                    entity_mention_index=entity_mention_index,
                )
                for row in rows
            ]
            if index_name == "kg-chunks":
                for document in docs:
                    document["evidence_ids"] = (
                        evidence_ids_by_chunk.get(
                            str(document.get("chunk_id") or ""),
                            [],
                        )
                        or evidence_ids_by_element.get(
                            str(document.get("document_element_id") or ""),
                            [],
                        )
                    )
                for evidence_id in sorted(
                    relationship_ids_by_evidence
                ):
                    evidence = evidence_by_id.get(evidence_id)
                    if evidence is None:
                        continue
                    content = str(evidence.get("text") or "").strip()
                    source_quote_is_verbatim = bool(content)
                    if not content:
                        content = "\n".join(
                            dict.fromkeys(
                                relationship_descriptions_by_evidence.get(
                                    evidence_id,
                                    [],
                                )
                            )
                        ).strip()
                    if not content:
                        continue
                    synthetic_row = {
                        **evidence,
                        "chunk_id": (
                            "relationship-evidence-"
                            + hashlib.sha256(
                                evidence_id.encode("utf-8")
                            ).hexdigest()
                        ),
                        "content": content,
                        "embedding_text": content,
                        "related_entity_ids": sorted(
                            relationship_entity_ids_by_evidence.get(
                                evidence_id,
                                set(),
                            )
                        ),
                        "chunk_type": "relationship_evidence",
                    }
                    document = derive_chunk_doc(
                        synthetic_row,
                        entities_by_id,
                        entity_mention_index=entity_mention_index,
                    )
                    document["evidence_ids"] = [evidence_id]
                    document["source_quote"] = content
                    document["source_quote_is_verbatim"] = (
                        source_quote_is_verbatim
                    )
                    docs.append(document)
        for document in docs:
            if index_name in {"kg-chunks", "kg-document-elements"}:
                source_quote = str(
                    document.get("source_quote")
                    or document.get("content")
                    or ""
                ).strip()
                if not source_quote:
                    raise click.ClickException(
                        f"{index_name} document "
                        f"{document.get(id_field)!r} has no source quotation."
                    )
                document["source_quote"] = source_quote
                document.setdefault("source_quote_is_verbatim", True)
            document["semantic_contract_hash"] = semantic_contract_hash
            semantic_type_ids = _string_list(
                document.get("semantic_type_ids")
            )
            document["semantic_property_ids"] = sorted(
                {
                    property_id
                    for semantic_type_id in semantic_type_ids
                    for property_id in property_ids_by_type.get(
                        semantic_type_id,
                        [],
                    )
                }
            )
            evidence_ids = _string_list(document.get("evidence_ids"))
            document["semantic_relationship_ids"] = sorted(
                {
                    relationship_id
                    for evidence_id in evidence_ids
                    for relationship_id in relationship_ids_by_evidence.get(
                        evidence_id,
                        [],
                    )
                }
            )

        # Optionally attach embeddings
        if embed:
            try:
                from fabric_kg_builder.search.embeddings import attach_vectors
                reused = 0
                if can_reuse_vectors:
                    reused = _reuse_unchanged_vectors(
                        docs,
                        prior_docs_path=index_dir / "docs.json",
                        id_field=id_field,
                        text_field=text_field,
                        vector_field=vector_field,
                        dimensions=_VECTOR_DIMS,
                    )
                pending_docs = [
                    document
                    for document in docs
                    if vector_field not in document
                ]
                if reused:
                    click.echo(
                        f"[compile-search]   {index_name}: reused "
                        f"{reused} unchanged vectors"
                    )
                attached = attach_vectors(
                    pending_docs,
                    text_field=text_field,
                    vector_field=vector_field,
                    deployment=(
                        embedding_deployment
                        or os.environ.get("AZURE_AI_EMBEDDING_DEPLOYMENT")
                        or "embedding"
                    ),
                )
                if pending_docs is not attached:
                    raise click.ClickException(
                        "Embedding attachment returned an unexpected document "
                        "collection."
                    )
            except Exception as exc:
                raise click.ClickException(
                    f"Embedding generation failed for {index_name}: {exc}"
                ) from exc

        docs_path = index_dir / "docs.json"
        docs_path.write_text(json.dumps(docs, indent=2, default=str), encoding="utf-8")
        # This is the Blob JSON payload for the optional integrated-vectorization
        # deployment path.  Keep docs.json unchanged for compatible direct uploads.
        from fabric_kg_builder.search.integrated_vectorization import (  # noqa: PLC0415
            prepare_source_documents,
        )

        source_docs = prepare_source_documents(
            docs,
            vector_fields={vector_field},
            id_field=id_field,
        )
        (index_dir / "source-docs.json").write_text(
            json.dumps(source_docs, indent=2, default=str),
            encoding="utf-8",
        )
        total_docs += len(docs)
        compiled_indexes.append(
            {
                "name": index_name,
                "document_count": len(docs),
                "schema_sha256": (
                    "sha256:"
                    + hashlib.sha256(schema_path.read_bytes()).hexdigest()
                ),
                "docs_sha256": (
                    "sha256:"
                    + hashlib.sha256(docs_path.read_bytes()).hexdigest()
                ),
            }
        )

        click.echo(
            f"[compile-search]   {index_name}/docs.json — {len(docs)} documents"
        )

    search_manifest = {
        "schema_version": "1.0",
        "contract_hash": semantic_contract_hash,
        "semantic_model_manifest_hash": semantic_model_manifest_hash,
        "semantic_crosswalk_hash": semantic_crosswalk_hash,
        "embedding_model": "text-embedding-3-large",
        "embedding_dimensions": _VECTOR_DIMS,
        "embedding_input_version": EMBEDDING_INPUT_VERSION,
        "vectorization_mode": "pipeline" if embed else "integrated-or-deferred",
        "indexes": compiled_indexes,
    }
    search_manifest["artifact_set_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                compiled_indexes,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "search-manifest.json").write_text(
        json.dumps(search_manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    click.echo(
        f"[compile-search] SUCCESS — {len(selected)} schema(s), "
        f"{total_docs} total docs written to {out_path}"
    )
