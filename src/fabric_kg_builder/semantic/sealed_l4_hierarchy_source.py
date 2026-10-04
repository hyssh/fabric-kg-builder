"""Read-only sealed-L4 input for offline derived community hierarchy work.

This module is a thin, faithful reader over ``SealedL4ServingSource``. It
performs zero additional validation and zero reinterpretation: every
rejection path (stale, tampered, unapproved, partial-scope drift, resealed
authority) is still enforced exclusively by ``SealedL4ServingSource`` itself.
If that source refuses to open, this module refuses too -- it never falls
back, retries with relaxed checks, or synthesizes a result.

It intentionally does NOT define a hierarchy provider or result contract.
The versioned derived-hierarchy contract (``EntityRow``, ``RelationshipRow``,
``CommunityProvider``, ``HierarchyStatus``, ``DerivedClusterRow``,
``DerivedHierarchyOutcome``, etc.) is owned elsewhere
(``graph/community_contracts.py`` / ``graph/community_hierarchy_v2.py``) and
is out of scope here. ``SealedL4HierarchyRows`` below is minimal internal
pass-through data -- raw sealed rows plus their lineage/authority objects --
named to avoid any collision with that forthcoming public contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from fabric_kg_builder.contracts.projection import SemanticServingProjection
from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.enrichment.approved_partial_handoff import read_scope, scope_context
from fabric_kg_builder.graph.community_projection import _compute_input_graph_hash
from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

# Only the facts an offline derived hierarchy could ever need to read.
# ``semantic_publication_authority``, ``semantic_asserted_properties``, and
# the required-member tables are deliberately not read here: they carry no
# entity/relationship rows and widening this set would be scope creep.
_HIERARCHY_INPUT_TABLES: tuple[str, ...] = (
    "semantic_asserted_entities",
    "semantic_entity_type_assertions",
    "semantic_asserted_relationships",
)


@dataclass(frozen=True)
class SealedL4HierarchyRows:
    """Raw sealed-L4 rows plus their lineage, read without reinterpretation.

    Every field here is either a verbatim ``SealedL4ServingSource`` object
    (``receipt``, ``manifest``, ``input_manifest``, ``projection``) or a
    verbatim row tuple decoded from the exact Parquet bytes that source
    already validated. ``partial_scope`` / ``partial_scope_context`` are
    ``None`` when the sealed run carries no partial-extraction scope
    disclosure (i.e. a full, non-partial L4 run).
    """

    root: Path
    receipt: StageReceipt
    manifest: ArtifactManifest
    input_manifest: ArtifactManifest
    projection: SemanticServingProjection
    partial_scope: dict[str, object] | None
    partial_scope_context: dict[str, object] | None
    entities: tuple[dict[str, object], ...]
    entity_type_assertions: tuple[dict[str, object], ...]
    relationships: tuple[dict[str, object], ...]


def _read_rows(
    source: SealedL4ServingSource, table_name: str
) -> tuple[dict[str, object], ...]:
    path = source.resolve(table_name)
    table = pq.read_table(path)
    return tuple(table.to_pylist())


def read_sealed_l4_hierarchy_rows(source: SealedL4ServingSource) -> SealedL4HierarchyRows:
    """Read hierarchy-relevant sealed rows from an already-opened source.

    Use this when the caller already holds a validated
    ``SealedL4ServingSource`` (for example, one shared with another sealed
    reader in the same process) to avoid re-opening the run from disk.
    """

    scope = read_scope(source.root, source.manifest)
    rows = {
        table_name: _read_rows(source, table_name) for table_name in _HIERARCHY_INPUT_TABLES
    }
    return SealedL4HierarchyRows(
        root=source.root,
        receipt=source.receipt,
        manifest=source.manifest,
        input_manifest=source.input_manifest,
        projection=source.projection,
        partial_scope=scope,
        partial_scope_context=scope_context(scope) if scope is not None else None,
        entities=rows["semantic_asserted_entities"],
        entity_type_assertions=rows["semantic_entity_type_assertions"],
        relationships=rows["semantic_asserted_relationships"],
    )


def load_sealed_l4_hierarchy_rows(
    run_root: Path,
    *,
    input_manifest_search_roots: Sequence[Path],
) -> SealedL4HierarchyRows:
    """Open a sealed L4 run and read its hierarchy-relevant rows.

    Raises whatever ``SealedL4ServingSource.from_run`` raises on any
    authority failure (missing/stale/tampered/unapproved/resealed input);
    this function adds no additional success paths and swallows nothing.
    """

    source = SealedL4ServingSource.from_run(
        run_root, input_manifest_search_roots=input_manifest_search_roots
    )
    return read_sealed_l4_hierarchy_rows(source)


@dataclass(frozen=True)
class SealedL4HierarchyCoreInput:
    """W2 hierarchy-core rows built from sealed-L4 facts, plus their hash.

    ``entities``/``relationships`` are real W2 ``EntityRow``/``RelationshipRow``
    instances -- not a parallel duck-typed shape -- so any caller (this
    module's own writer, a CLI command, a test) passes exactly what
    ``build_derived_community_hierarchy``/``build_derived_community_hierarchy_safe``
    already accept. ``input_graph_hash`` is produced by W2's own
    ``community_projection._compute_input_graph_hash`` over these exact
    rows, so it is guaranteed to equal whatever
    ``build_derived_community_hierarchy_safe`` computes for the same rows,
    and therefore whatever the derived-hierarchy writer later persists as
    ``receipt.input_graph_hash`` for the same build.
    """

    entities: tuple[EntityRow, ...]
    relationships: tuple[RelationshipRow, ...]
    input_graph_hash: str


def _require(row: dict[str, object], key: str, *, row_label: str) -> object:
    value = row.get(key)
    if value is None or value == "":
        raise ValueError(
            f"sealed {row_label} row is missing required field {key!r}; "
            "refusing to fabricate a hierarchy-core row from an incomplete "
            "sealed fact"
        )
    return value


def _core_entity_row(row: dict[str, object], completed_at_utc) -> EntityRow:
    entity_id = _require(row, "entity_id", row_label="entity")
    most_specific_type_id = _require(row, "most_specific_type_id", row_label="entity")
    domain_contract_hash = _require(row, "domain_contract_hash", row_label="entity")
    row_hash = _require(row, "row_hash", row_label="entity")
    evidence_span_ids = row.get("evidence_span_ids")
    if evidence_span_ids is None:
        raise ValueError(
            "sealed entity row is missing required field 'evidence_span_ids'; "
            "refusing to fabricate a hierarchy-core row from an incomplete "
            "sealed fact"
        )
    label = row.get("label")
    display_name = label if label else entity_id
    return EntityRow(
        entity_id=entity_id,
        entity_type=most_specific_type_id,
        display_name=display_name,
        canonical_key=entity_id,
        domain_hash=domain_contract_hash,
        content_hash=row_hash,
        evidence_ids=list(evidence_span_ids),
        created_at=completed_at_utc,
        updated_at=completed_at_utc,
    )


def _core_relationship_row(row: dict[str, object], completed_at_utc) -> RelationshipRow:
    relationship_id = _require(row, "relationship_id", row_label="relationship")
    semantic_relationship_id = _require(
        row, "semantic_relationship_id", row_label="relationship"
    )
    source_entity_id = _require(row, "source_entity_id", row_label="relationship")
    target_entity_id = _require(row, "target_entity_id", row_label="relationship")
    domain_contract_hash = _require(row, "domain_contract_hash", row_label="relationship")
    row_hash = _require(row, "row_hash", row_label="relationship")
    evidence_span_ids = row.get("evidence_span_ids")
    if evidence_span_ids is None:
        raise ValueError(
            "sealed relationship row is missing required field "
            "'evidence_span_ids'; refusing to fabricate a hierarchy-core "
            "row from an incomplete sealed fact"
        )
    return RelationshipRow(
        relationship_id=relationship_id,
        # Per the agreed W2 mapping: the sealed row's *semantic*
        # relationship id becomes the core row's ``relationship_type`` --
        # this is distinct from (and does not populate)
        # ``RelationshipRow.semantic_relationship_id`` itself.
        relationship_type=semantic_relationship_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        domain_hash=domain_contract_hash,
        content_hash=row_hash,
        evidence_ids=list(evidence_span_ids),
        created_at=completed_at_utc,
    )


def build_hierarchy_core_rows(sealed: SealedL4HierarchyRows) -> SealedL4HierarchyCoreInput:
    """Map sealed-L4 facts to real W2 hierarchy-core rows, with their hash.

    Frozen agreed mapping (entities): ``entity_type=most_specific_type_id``,
    ``canonical_key=entity_id``, ``display_name=label`` else ``entity_id``,
    ``domain_hash=domain_contract_hash``, ``content_hash=row_hash``,
    ``evidence_ids=evidence_span_ids``, ``created_at``/``updated_at`` from
    the sealed run's ``receipt.completed_at_utc``.

    Frozen agreed mapping (relationships): ``relationship_type=
    semantic_relationship_id``, ``source_entity_id``/``target_entity_id``
    passed through verbatim, ``domain_hash=domain_contract_hash``,
    ``content_hash=row_hash``, ``evidence_ids=evidence_span_ids``,
    ``created_at`` from ``receipt.completed_at_utc`` (``RelationshipRow``
    has no ``updated_at`` field).

    Typed fail-closed: any missing/null required sealed field raises
    ``ValueError`` immediately rather than silently defaulting; a naive
    (timezone-unaware) ``completed_at_utc`` likewise raises rather than
    being treated as UTC by assumption.

    ``input_graph_hash`` is computed by calling W2's own
    ``community_projection._compute_input_graph_hash`` directly over the
    built rows -- never reimplemented here -- which is what guarantees
    equality with whatever ``build_derived_community_hierarchy_safe``
    computes for the same rows, and with whatever the derived-hierarchy
    writer later persists as ``receipt.input_graph_hash``.
    """

    completed_at_utc = sealed.receipt.completed_at_utc
    if completed_at_utc.tzinfo is None or completed_at_utc.tzinfo.utcoffset(completed_at_utc) is None:
        raise ValueError(
            "sealed receipt completed_at_utc is a naive datetime; refusing "
            "to treat it as UTC by assumption"
        )

    entities = [_core_entity_row(row, completed_at_utc) for row in sealed.entities]
    relationships = [
        _core_relationship_row(row, completed_at_utc) for row in sealed.relationships
    ]
    input_graph_hash = _compute_input_graph_hash(entities, relationships)
    return SealedL4HierarchyCoreInput(
        entities=tuple(entities),
        relationships=tuple(relationships),
        input_graph_hash=input_graph_hash,
    )
