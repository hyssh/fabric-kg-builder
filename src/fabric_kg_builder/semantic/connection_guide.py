"""Generate the packaged Ontology, Graph, and Search connection guide."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_documents(search_dir: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    indexes: list[tuple[str, list[dict[str, Any]]]] = []
    if not search_dir.is_dir():
        return indexes
    for docs_path in sorted(search_dir.glob("*/docs.json")):
        payload = json.loads(docs_path.read_text(encoding="utf-8"))
        documents = [
            item for item in payload
            if isinstance(item, dict)
        ] if isinstance(payload, list) else []
        indexes.append((docs_path.parent.name, documents))
    return indexes


def build_ontology_search_connection_guide(build_dir: Path) -> str:
    """Render a deterministic guide from sealed build artifacts."""
    semantic_dir = build_dir / "semantic"
    manifest = _load_json(semantic_dir / "semantic-model-manifest.json")
    crosswalk = _load_json(semantic_dir / "semantic-crosswalk.json")
    contract = _load_json(semantic_dir / "normalized-contract.json")
    search_indexes = _load_documents(build_dir / "search")

    contract_name = str(contract.get("name") or "Semantic model")
    contract_hash = str(manifest.get("semantic_contract_hash") or "not available")
    manifest_hash = str(manifest.get("manifest_hash") or "not available")
    entities = [
        item for item in manifest.get("entity_types", [])
        if isinstance(item, dict)
    ]
    relationships = [
        item for item in manifest.get("relationship_types", [])
        if isinstance(item, dict)
    ]
    entity_by_id = {
        str(item.get("semantic_id")): item for item in entities
    }

    lines = [
        "# Ontology, Graph, and Search Connection",
        "",
        f"Model: **{contract_name}**  ",
        f"Semantic contract: `{contract_hash}`  ",
        f"Semantic manifest: `{manifest_hash}`",
        "",
        "## Responsibility boundary",
        "",
        "| Surface | Role | Reliability rule |",
        "|---|---|---|",
        "| Fabric Ontology | Approved meaning, layered entity nouns, properties, and relationship verb semantics | Required to interpret structured concepts |",
        "| Fabric Graph | Persisted directed entity and relationship instances | Required for reliable structured query execution and proof of relationships |",
        "| Azure AI Search | Detailed definitions, descriptions, passages, table text, and source quotations | Supplies supporting detail after Ontology/Graph resolution; it does not prove graph edges |",
        "",
        "## Ontology layers",
        "",
        "The compiled ontology always exposes three modules: `common-entities`, "
        "`common-relationships`, and `domain`.",
        "",
    ]
    for layer, heading in (
        ("common", "Common entities"),
        ("domain", "Domain entities"),
    ):
        selected = [
            str(item.get("canonical_name"))
            for item in entities
            if item.get("semantic_layer") == layer
        ]
        lines.extend([
            f"### {heading}",
            "",
            ", ".join(f"`{name}`" for name in selected) if selected else "_None defined._",
            "",
        ])

    common_relations = [
        item for item in relationships
        if item.get("semantic_layer") == "common"
    ]
    lines.extend([
        "### Common relationships",
        "",
        ", ".join(
            f"`{item.get('predicate')}`" for item in common_relations
        ) if common_relations else "_None defined._",
        "",
        "## Nouns and directed verbs",
        "",
        "Ontology entity types are nouns representing things or concepts. "
        "Relationship predicates are directed verbs whose instances must be "
        "returned by Fabric Graph before they are asserted.",
        "",
        "| Layer | Source noun | Directed verb | Target noun | Evidence policy |",
        "|---|---|---|---|---|",
    ])
    for relationship in relationships:
        source = entity_by_id.get(str(relationship.get("source_type_id")), {})
        target = entity_by_id.get(str(relationship.get("target_type_id")), {})
        lines.append(
            "| {layer} | `{source}` | `{predicate}` | `{target}` | `{evidence}` |".format(
                layer=relationship.get("semantic_layer", "domain"),
                source=source.get("canonical_name", relationship.get("source_type_id", "")),
                predicate=relationship.get("predicate", ""),
                target=target.get("canonical_name", relationship.get("target_type_id", "")),
                evidence=relationship.get("evidence_policy", "optional"),
            )
        )

    entries = [
        *crosswalk.get("entity_type_entries", []),
        *crosswalk.get("relationship_type_entries", []),
    ]
    lines.extend([
        "",
        "## Cross-surface identity",
        "",
        "| Semantic ID | Kind | Ontology ID | Graph label | Search linkage |",
        "|---|---|---|---|---|",
    ])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lines.append(
            "| `{semantic}` | {kind} | `{ontology}` | `{graph}` | `{search}` |".format(
                semantic=entry.get("semantic_id", ""),
                kind=entry.get("element_kind", ""),
                ontology=entry.get("ontology_type_id") or "",
                graph=entry.get("graph_label") or "",
                search=entry.get("search_field_or_filter") or "",
            )
        )

    lines.extend([
        "",
        "## Search detail and source quotation contract",
        "",
        "Text indexes retain the detailed source passage in `content` and the "
        "explicit quotation copy in `source_quote`. `source_quote_is_verbatim` "
        "distinguishes original source text from derived descriptions. "
        "`source_file_id`, `source_locator_json`, page/section fields, evidence IDs, "
        "and semantic IDs connect the quote back to its source and Graph context.",
        "",
        "| Index | Documents | With source quote | Verbatim source quotes |",
        "|---|---:|---:|---:|",
    ])
    for index_name, documents in search_indexes:
        quoted = sum(bool(str(doc.get("source_quote") or "").strip()) for doc in documents)
        verbatim = sum(doc.get("source_quote_is_verbatim") is True for doc in documents)
        lines.append(f"| `{index_name}` | {len(documents)} | {quoted} | {verbatim} |")
    if not search_indexes:
        lines.append("| _Search artifacts not packaged_ | 0 | 0 | 0 |")

    lines.extend([
        "",
        "## Reliable query execution",
        "",
        "1. Use **Fabric Ontology** to resolve the approved meaning and layer of the requested nouns and verbs.",
        "2. Execute a bounded query against **Fabric Graph** using only sealed labels, directions, and properties.",
        "3. Treat returned Graph entity IDs, relationship IDs, paths, and evidence IDs as the authoritative structured result.",
        "4. Use those IDs as deterministic filters for **Azure AI Search** to retrieve detailed definitions, descriptions, and source quotations.",
        "5. Cite Graph evidence and Search source locators together. If Graph returns no verified relationship, do not infer one from text similarity.",
        "6. Report authentication, timeout, platform, and query failures as failures rather than converting them into no-data answers.",
        "",
        "Search may answer unstructured passage questions directly, but it must not "
        "be used alone to assert structured entity relationships.",
        "",
    ])
    return "\n".join(lines)

