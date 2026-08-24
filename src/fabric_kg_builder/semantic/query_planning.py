"""Persisted query schema derivation and bounded decomposition (S8A-QRY-001..003).

Provides:
- ``build_persisted_query_schema`` (S8A-QRY-002 builder): derives a strict,
  sealed ``PersistedQuerySchema`` deterministically from a
  ``SemanticModelManifest`` and, optionally, its ``SemanticCrosswalk``.
- ``decompose_semantic_request`` (S8A-QRY-003): a deterministic bounded
  decomposition API that accepts an over-budget semantic request (before
  ``SemanticQueryPlan`` construction, which would otherwise hard-reject it)
  and produces at most ``budget.max_subqueries`` valid ``SemanticQueryPlan``
  subqueries, preserving optionality/evidence/intent/manifest identity and
  requested properties, and recording canonical boundary type IDs so callers
  can join subquery results back together.  Each produced subquery's plan
  is sealed (``plan_hash`` populated).  Decomposition fails closed (raises
  ``SemanticDecompositionError``, never a raw pydantic ``ValidationError``)
  when a request is disconnected in a way that cannot be safely resolved
  (including when the request as a whole splits into multiple genuinely
  independent components with no shared type to join them back together),
  when an individual bounded group cannot fit within
  ``max_nodes``/``max_relationships`` even after chunking, or when the
  request remains too large (more than ``max_subqueries``) after
  decomposition.

The plan-level resolver that validates a ``SemanticQueryPlan`` against a
``PersistedQuerySchema`` lives in ``query_validation.resolve_query_plan``,
alongside the schema-aware extension of ``validate_physical_query``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from pydantic import ValidationError as _PydanticValidationError

from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.service import compute_contract_hash

from .query_validation import QueryFinding
from .schemas import (
    ApprovedQueryPath,
    BoundedQueryAuthority,
    ComplexityBudget,
    MaterializationPlan,
    PersistedQueryNodeSchema,
    PersistedQueryRelationshipSchema,
    PersistedQuerySchema,
    SemanticCrosswalk,
    SemanticModelManifest,
    SemanticPathStep,
    SemanticQueryPlan,
    compute_bounded_query_authority_hash,
    compute_persisted_query_schema_hash,
    compute_query_plan_hash,
)

# ---------------------------------------------------------------------------
# S8A-QRY-002 — Persisted query schema builder
# ---------------------------------------------------------------------------


def build_persisted_query_schema(
    manifest: SemanticModelManifest,
    crosswalk: SemanticCrosswalk | None = None,
    *,
    materialization_plan: MaterializationPlan | None = None,
    domain_contract: DomainContractV2 | None = None,
) -> PersistedQuerySchema:
    """Deterministically derive and seal a PersistedQuerySchema.

    Node and relationship graph labels are taken from the manifest's own
    ``graph_projection`` fields.  When ``crosswalk`` is supplied, its
    ``graph_label`` entries take precedence for the corresponding semantic ID
    (the crosswalk is the authoritative cross-layer physical binding); a
    crosswalk with a non-empty ``manifest_hash`` that does not match
    ``manifest.manifest_hash`` is rejected to prevent resolving a plan against
    a schema derived from a mismatched crosswalk/manifest pair.

    Owner-scoped graph properties are keyed by canonical property ID
    (``ManifestPropertyEntry.property_id``), mapping to the physical graph
    property key: the crosswalk's owner-scoped ``property_entries`` (matched
    by ``(owner_type_id, semantic_id)``) take precedence when supplied,
    falling back to the manifest's own ``graph_projection.property_key``.
    Only properties with a resolved non-empty physical key are included;
    unmaterialized properties are absent from ``owner_properties`` so a plan
    requesting them fails closed.

    A node or relationship whose graph label cannot be resolved (neither the
    manifest nor the crosswalk projects it) is still included in the schema
    with an empty ``label`` (and, for relationships, empty endpoint labels),
    so plan/query validation can report "not physically projected" rather
    than "unknown" — a distinct, more actionable failure category.

    Args:
        manifest: The sealed SemanticModelManifest to derive the schema from.
        crosswalk: Optional SemanticCrosswalk providing authoritative
            cross-layer graph label bindings.

    Returns:
        A sealed PersistedQuerySchema (``schema_hash`` populated).

    Raises:
        ValueError: If ``crosswalk.manifest_hash`` is non-empty and does not
            match ``manifest.manifest_hash``.
    """
    if (
        crosswalk is not None
        and crosswalk.manifest_hash
        and manifest.manifest_hash
        and crosswalk.manifest_hash != manifest.manifest_hash
    ):
        raise ValueError(
            f"SemanticCrosswalk.manifest_hash '{crosswalk.manifest_hash}' "
            f"does not match SemanticModelManifest.manifest_hash "
            f"'{manifest.manifest_hash}'. A persisted query schema must be "
            "derived from a manifest/crosswalk pair with matching identity."
        )
    if (
        materialization_plan is not None
        and materialization_plan.manifest_hash
        and manifest.manifest_hash
        and materialization_plan.manifest_hash != manifest.manifest_hash
    ):
        raise ValueError(
            "MaterializationPlan.manifest_hash does not match the semantic "
            "manifest used for persisted query schema derivation."
        )
    if domain_contract is not None and (
        crosswalk is None or materialization_plan is None
    ):
        raise ValueError(
            "Schema-2 bounded query authority requires both semantic crosswalk "
            "and materialization plan."
        )

    crosswalk_entity_labels: dict[str, str] = {}
    crosswalk_relationship_labels: dict[str, str] = {}
    crosswalk_property_labels: dict[tuple[str, str], str] = {}
    if crosswalk is not None:
        for entry in crosswalk.entity_type_entries:
            if entry.graph_label:
                crosswalk_entity_labels[entry.semantic_id] = entry.graph_label
        for entry in crosswalk.relationship_type_entries:
            if entry.graph_label:
                crosswalk_relationship_labels[entry.semantic_id] = entry.graph_label
        for entry in crosswalk.property_entries:
            if entry.graph_label and entry.owner_type_id:
                crosswalk_property_labels[
                    (entry.owner_type_id, entry.semantic_id)
                ] = entry.graph_label

    owner_properties: dict[str, dict[str, str]] = {
        entity.semantic_id: {} for entity in manifest.entity_types
    }
    for prop in manifest.property_definitions:
        physical_key = (
            crosswalk_property_labels.get((prop.owner_type_id, prop.property_id))
            or prop.graph_projection.property_key
        )
        if physical_key and prop.owner_type_id in owner_properties:
            owner_properties[prop.owner_type_id][prop.property_id] = physical_key

    table_by_type = {
        table.semantic_id: table
        for table in (
            materialization_plan.entity_tables
            if materialization_plan is not None
            else []
        )
    }
    nodes = [
        PersistedQueryNodeSchema(
            semantic_id=entity.semantic_id,
            label=(
                crosswalk_entity_labels.get(entity.semantic_id)
                or entity.graph_projection.label
                or ""
            ),
            owner_properties=owner_properties.get(entity.semantic_id, {}),
            id_property=(
                table_by_type[entity.semantic_id].entity_id_column
                if entity.semantic_id in table_by_type
                else ""
            ),
            display_property=(
                table_by_type[entity.semantic_id].display_name_column
                if entity.semantic_id in table_by_type
                else ""
            ),
        )
        for entity in manifest.entity_types
    ]

    node_labels_by_id = {node.semantic_id: node.label for node in nodes}
    relationship_table_by_id = {
        table.semantic_id: table
        for table in (
            materialization_plan.relationship_tables
            if materialization_plan is not None
            else []
        )
    }
    relationships = [
        PersistedQueryRelationshipSchema(
            semantic_id=relationship.semantic_id,
            label=(
                crosswalk_relationship_labels.get(relationship.semantic_id)
                or relationship.graph_projection.label
                or ""
            ),
            source_type_id=relationship.source_type_id,
            target_type_id=relationship.target_type_id,
            source_label=node_labels_by_id.get(relationship.source_type_id, ""),
            target_label=node_labels_by_id.get(relationship.target_type_id, ""),
            direction=relationship.direction,
            evidence_property=(
                relationship_table_by_id[relationship.semantic_id].evidence_column
                or ""
                if relationship.semantic_id in relationship_table_by_id
                else ""
            ),
        )
        for relationship in manifest.relationship_types
    ]

    crosswalk_hash = (
        _canonical_payload_hash(crosswalk.model_dump(mode="json"))
        if crosswalk is not None
        else ""
    )
    authority = (
        build_bounded_query_authority(
            domain_contract,
            manifest=manifest,
            crosswalk=crosswalk,
            nodes=nodes,
            relationships=relationships,
        )
        if domain_contract is not None
        else None
    )
    schema = PersistedQuerySchema(
        schema_mode=(
            "schema2_bounded"
            if authority is not None
            else "schema1_compatibility"
        ),
        manifest_hash=manifest.manifest_hash,
        semantic_crosswalk_hash=crosswalk_hash,
        authority=authority,
        nodes=nodes,
        relationships=relationships,
    )
    return schema.model_copy(
        update={"schema_hash": compute_persisted_query_schema_hash(schema)}
    )


def _canonical_payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_bounded_query_authority(
    contract: DomainContractV2,
    *,
    manifest: SemanticModelManifest,
    crosswalk: SemanticCrosswalk,
    nodes: list[PersistedQueryNodeSchema],
    relationships: list[PersistedQueryRelationshipSchema],
) -> BoundedQueryAuthority:
    """Resolve approved schema-2 K/question paths to persisted Graph identity."""
    if contract.approval.status != "approved":
        raise ValueError(
            "Schema-2 query authority requires an approved DomainContractV2."
        )
    computed_contract_hash = compute_contract_hash(contract)
    if contract.approval.contract_hash != computed_contract_hash:
        raise ValueError(
            "Approved schema-2 domain contract hash does not match its contents."
        )
    if crosswalk.manifest_hash != manifest.manifest_hash:
        raise ValueError(
            "Schema-2 query authority requires a crosswalk bound to the "
            "current semantic manifest."
        )

    node_by_id = {node.semantic_id: node for node in nodes}
    relationship_by_id = {
        relationship.semantic_id: relationship
        for relationship in relationships
    }
    resolved_paths: list[ApprovedQueryPath] = []
    for question_plan in contract.question_plans:
        if not question_plan.covered:
            resolved_paths.append(ApprovedQueryPath(
                question_id=question_plan.question_id,
                covered=False,
                unsupported_reason=question_plan.unsupported_reason,
            ))
            continue
        steps: list[SemanticPathStep] = []
        for index, approved_step in enumerate(question_plan.required_path):
            relationship = relationship_by_id.get(
                approved_step.relationship_type
            )
            if relationship is None:
                raise ValueError(
                    "Approved question path references relationship absent from "
                    f"the semantic manifest: {approved_step.relationship_type}."
                )
            from_node = node_by_id.get(approved_step.from_type)
            to_node = node_by_id.get(approved_step.to_type)
            if from_node is None or to_node is None:
                raise ValueError(
                    "Approved question path references an entity type absent "
                    "from the semantic manifest."
                )
            direction = (
                "source_to_target"
                if approved_step.traversal == "forward"
                else "target_to_source"
            )
            expected_from, expected_to = (
                (
                    relationship.source_type_id,
                    relationship.target_type_id,
                )
                if direction == "source_to_target"
                else (
                    relationship.target_type_id,
                    relationship.source_type_id,
                )
            )
            if (
                approved_step.from_type != expected_from
                or approved_step.to_type != expected_to
            ):
                raise ValueError(
                    "Approved question path endpoint/direction differs from the "
                    f"semantic crosswalk for {approved_step.relationship_type}."
                )
            steps.append(SemanticPathStep(
                step_id=f"{question_plan.question_id}:hop:{index + 1}",
                from_type_id=approved_step.from_type,
                via_relationship_id=approved_step.relationship_type,
                to_type_id=approved_step.to_type,
                direction=direction,
                relationship_graph_label=relationship.label,
                from_graph_label=from_node.label,
                to_graph_label=to_node.label,
                from_endpoint_property=from_node.id_property,
                to_endpoint_property=to_node.id_property,
                evidence_property=relationship.evidence_property,
            ))
        resolved_paths.append(ApprovedQueryPath(
            question_id=question_plan.question_id,
            covered=True,
            steps=steps,
        ))

    authority = BoundedQueryAuthority(
        domain_contract_hash=computed_contract_hash,
        reasoning_policy_hash=_canonical_payload_hash(
            contract.reasoning_policy.model_dump(mode="json")
        ),
        question_plans_hash=_canonical_payload_hash([
            plan.model_dump(mode="json")
            for plan in contract.question_plans
        ]),
        semantic_manifest_hash=manifest.manifest_hash,
        semantic_crosswalk_hash=_canonical_payload_hash(
            crosswalk.model_dump(mode="json")
        ),
        approved_max_hops=contract.reasoning_policy.max_hops,
        question_paths=resolved_paths,
    )
    return authority.model_copy(update={
        "authority_hash": compute_bounded_query_authority_hash(authority)
    })


# ---------------------------------------------------------------------------
# S8A-QRY-003 — Bounded decomposition of over-budget semantic requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticQueryRequest:
    """A business-intent semantic request that may exceed the complexity budget.

    Unlike ``SemanticQueryPlan``, this is a plain, unvalidated request shape:
    it is legal for it to violate SemanticQueryPlan's budget invariants
    (that is exactly what makes decomposition necessary).  Once decomposed,
    every produced subquery is a fully valid, sealed ``SemanticQueryPlan``
    carrying this request's ``manifest_hash``.
    """

    intent: str
    requested_concepts: list[str] = field(default_factory=list)
    required_types: list[str] = field(default_factory=list)
    required_relationships: list[str] = field(default_factory=list)
    optional_relationships: list[str] = field(default_factory=list)
    requested_properties: list[str] = field(default_factory=list)
    evidence_required: bool = True
    path_steps: list[SemanticPathStep] = field(default_factory=list)
    budget: ComplexityBudget = field(default_factory=ComplexityBudget)
    manifest_hash: str = ""


@dataclass(frozen=True)
class DecomposedSubquery:
    """One bounded, valid SemanticQueryPlan subquery produced by decomposition."""

    subquery_id: str
    plan: SemanticQueryPlan
    boundary_type_ids: tuple[str, ...] = ()


class SemanticDecompositionError(ValueError):
    """Raised when a semantic request cannot be safely decomposed (fail closed)."""

    def __init__(self, findings: list[QueryFinding]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(f"{f.code}: {f.message}" for f in findings))


def _chunk_required_steps(
    steps: list[SemanticPathStep],
    budget: ComplexityBudget,
) -> list[list[SemanticPathStep]]:
    """Greedily group required steps into budget-bounded chunks.

    Each chunk individually satisfies ``max_hops`` (sum of bounded step
    depths),
    ``max_nodes`` (distinct type IDs touched), and ``max_relationships``
    (distinct relationship IDs) — not step count alone — so a component
    whose steps touch many distinct types (not a simple linear chain) is
    still split before it can exceed the node/relationship budget.
    """
    chunks: list[list[SemanticPathStep]] = []
    current: list[SemanticPathStep] = []
    current_nodes: set[str] = set()
    current_rels: set[str] = set()
    current_hops = 0
    for step in steps:
        candidate_nodes = current_nodes | {step.from_type_id, step.to_type_id}
        candidate_rels = current_rels | {step.via_relationship_id}
        candidate_hops = current_hops + step.max_depth
        if current and (
            candidate_hops > budget.max_hops
            or len(candidate_nodes) > budget.max_nodes
            or len(candidate_rels) > budget.max_relationships
        ):
            chunks.append(current)
            current = []
            current_nodes = set()
            current_rels = set()
            current_hops = 0
            candidate_nodes = {step.from_type_id, step.to_type_id}
            candidate_rels = {step.via_relationship_id}
            candidate_hops = step.max_depth
        current.append(step)
        current_nodes = candidate_nodes
        current_rels = candidate_rels
        current_hops = candidate_hops
    if current:
        chunks.append(current)
    return chunks


def _group_fits_budget(
    group_steps: list[SemanticPathStep],
    budget: ComplexityBudget,
) -> bool:
    """True if a group of steps (required + optional) fits max_nodes/max_relationships."""
    nodes = {
        type_id
        for step in group_steps
        for type_id in (step.from_type_id, step.to_type_id)
    }
    rels = {step.via_relationship_id for step in group_steps}
    required_hops = sum(
        step.max_depth for step in group_steps if not step.optional
    )
    return (
        required_hops <= budget.max_hops
        and len(nodes) <= budget.max_nodes
        and len(rels) <= budget.max_relationships
    )


def _attach_optional_steps(
    groups: list[list[SemanticPathStep]],
    optional_steps: list[SemanticPathStep],
    budget: ComplexityBudget,
) -> list[list[SemanticPathStep]]:
    """Attach each optional step to the first group it touches AND fits.

    Enforces ``max_hops``/``max_nodes``/``max_relationships`` at attachment
    time: an
    optional step is only merged into an existing group if the group,
    including that step, still fits the budget; otherwise a new group is
    started for it (or, if it touches no existing group at all, a new
    standalone group).  This prevents a required-step chunk that already
    sits exactly at the budget boundary from silently exceeding it once
    optional steps are layered on.
    """
    groups = [list(group) for group in groups] or [[]]
    for opt_step in optional_steps:
        placed = False
        for group in groups:
            touched_types = {
                type_id
                for step in group
                for type_id in (step.from_type_id, step.to_type_id)
            }
            if opt_step.from_type_id not in touched_types:
                continue
            candidate = [*group, opt_step]
            if not _group_fits_budget(candidate, budget):
                continue
            group.append(opt_step)
            placed = True
            break
        if not placed:
            groups.append([opt_step])
    return [group for group in groups if group]


def _build_subquery(
    request: SemanticQueryRequest,
    subquery_id: str,
    group_steps: list[SemanticPathStep],
    component_nodes: set[str],
) -> DecomposedSubquery:
    ordered_steps = sorted(group_steps, key=lambda step: step.step_id)
    types = sorted({
        type_id
        for step in ordered_steps
        for type_id in (step.from_type_id, step.to_type_id)
    })
    if not types:
        types = sorted(component_nodes & set(request.required_types))
    required_relationship_ids = sorted({
        step.via_relationship_id for step in ordered_steps if not step.optional
    })
    optional_relationship_ids = sorted({
        step.via_relationship_id for step in ordered_steps if step.optional
    })
    try:
        plan = SemanticQueryPlan(
            manifest_hash=request.manifest_hash,
            intent=request.intent,
            requested_concepts=list(request.requested_concepts),
            required_types=types,
            required_relationships=required_relationship_ids,
            optional_relationships=optional_relationship_ids,
            requested_properties=list(request.requested_properties),
            evidence_required=request.evidence_required,
            path_steps=ordered_steps,
            budget=request.budget,
        )
    except _PydanticValidationError as exc:
        # Fail closed with the project's structured-findings error type;
        # never let a raw pydantic ValidationError leak out of the
        # decomposition API.
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_SUBQUERY_INVALID",
            f"Subquery '{subquery_id}' could not be constructed as a valid "
            f"SemanticQueryPlan: {exc}",
        )]) from exc

    sealed_plan = plan.model_copy(
        update={"plan_hash": compute_query_plan_hash(plan)}
    )
    return DecomposedSubquery(subquery_id=subquery_id, plan=sealed_plan)


def decompose_semantic_request(
    request: SemanticQueryRequest,
    *,
    schema: PersistedQuerySchema | None = None,
) -> list[DecomposedSubquery]:
    """Deterministically decompose an over-budget semantic request.

    Builds a connectivity graph over ``request.path_steps`` (nodes = semantic
    type IDs, edges = path steps).  The *entire* request must form a single
    connected component (or a single isolated type with no relationships at
    all): if it splits into multiple genuinely independent components with
    no shared type, there is no canonical boundary to join their results
    back together, so decomposition fails closed rather than silently
    returning unrelated subqueries.  The one connected component is realized
    as one subquery if it already fits the budget, or split into sequential
    bounded chunks otherwise; a chunk boundary's shared type ID is recorded
    in ``boundary_type_ids`` on both adjacent subqueries so callers can join
    results back together deterministically.  Optional steps are attached to
    the first group that already touches their base type AND still fits
    ``max_nodes``/``max_relationships`` with the step included.

    Every produced subquery carries ``request.manifest_hash``. Requested
    properties are partitioned by owner type when decomposition creates
    multiple subqueries; therefore a persisted query schema is required for
    multi-subquery requests that project properties. Each resulting plan is
    sealed with its own ``plan_hash``.

    Fails closed (raises ``SemanticDecompositionError``, never a raw
    pydantic ``ValidationError``) when:
    - a declared required/optional relationship has no corresponding
      ``path_steps`` entry (its endpoints cannot be resolved, so it cannot be
      placed into any component safely — "disconnected"); or
    - the request's types/path steps form more than one connected component
      (no canonical boundary to join independent results back together); or
    - a bounded group still cannot fit within ``max_nodes``/
      ``max_relationships`` even after chunking and budget-aware optional
      attachment; or
    - the request cannot be decomposed within ``budget.max_subqueries`` even
      after bounded chunking ("too large"); or
    - an individual subquery cannot be constructed as a valid
      SemanticQueryPlan for any other reason.

    Args:
        request: The (possibly over-budget) SemanticQueryRequest.
        schema: Persisted owner-scoped query schema used to assign requested
            properties to the subquery containing their owning type.

    Returns:
        A list of at most ``request.budget.max_subqueries`` DecomposedSubquery
        instances, each wrapping a fully valid, sealed SemanticQueryPlan.

    Raises:
        SemanticDecompositionError: If the request cannot be decomposed safely.
    """
    budget = request.budget
    steps = list(request.path_steps)

    if schema is not None:
        if (
            not schema.schema_hash
            or schema.schema_hash
            != compute_persisted_query_schema_hash(schema)
        ):
            raise SemanticDecompositionError([QueryFinding(
                "DECOMPOSITION_QUERY_SCHEMA_INVALID",
                "Property-aware decomposition requires a sealed persisted "
                "query schema whose hash matches its contents.",
            )])
        if request.manifest_hash != schema.manifest_hash:
            raise SemanticDecompositionError([QueryFinding(
                "DECOMPOSITION_MANIFEST_MISMATCH",
                "Semantic request manifest identity does not match the "
                "persisted query schema.",
            )])

    required_relationship_ids = set(request.required_relationships)
    optional_relationship_ids = set(request.optional_relationships)
    declared_overlap = sorted(
        required_relationship_ids & optional_relationship_ids
    )
    required_step_ids = {
        step.via_relationship_id for step in steps if not step.optional
    }
    optional_step_ids = {
        step.via_relationship_id for step in steps if step.optional
    }
    optionality_mismatches = sorted(
        (required_relationship_ids & optional_step_ids)
        | (optional_relationship_ids & required_step_ids)
    )
    if declared_overlap or optionality_mismatches:
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_RELATIONSHIP_OPTIONALITY_MISMATCH",
            "Required and optional relationship semantics are inconsistent "
            f"for {sorted(set(declared_overlap) | set(optionality_mismatches))}. "
            "An optional path cannot be promoted to required, and a required "
            "path cannot be weakened during decomposition.",
        )])

    step_relationship_ids = {step.via_relationship_id for step in steps}
    declared_relationship_ids = (
        required_relationship_ids | optional_relationship_ids
    )
    undeclared = sorted(step_relationship_ids - declared_relationship_ids)
    if undeclared:
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_UNDECLARED_PATH_RELATIONSHIP",
            "Path steps reference relationships that are absent from the "
            f"request declarations: {undeclared}.",
        )])
    dangling = sorted(declared_relationship_ids - step_relationship_ids)
    if dangling:
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_DISCONNECTED_RELATIONSHIP",
            f"Relationship(s) {dangling} have no corresponding path step; "
            "their endpoints cannot be resolved, so the request cannot be "
            "decomposed safely.",
        )])

    node_ids: set[str] = set(request.required_types)
    for step in steps:
        node_ids.add(step.from_type_id)
        node_ids.add(step.to_type_id)

    parent: dict[str, str] = {node_id: node_id for node_id in node_ids}

    def find(node_id: str) -> str:
        root = node_id
        while parent[root] != root:
            root = parent[root]
        while parent[node_id] != root:
            parent[node_id], node_id = root, parent[node_id]
        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    for step in steps:
        union(step.from_type_id, step.to_type_id)

    components: dict[str, set[str]] = {}
    for node_id in node_ids:
        components.setdefault(find(node_id), set()).add(node_id)

    ordered_components = sorted(
        components.values(),
        key=lambda members: sorted(members)[0] if members else "",
    )

    if len(ordered_components) > 1:
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_DISCONNECTED_COMPONENTS",
            f"Request decomposes into {len(ordered_components)} genuinely "
            "independent components with no shared type to join them back "
            "together (no canonical boundary type ID). Submit each "
            "independent component as its own SemanticQueryRequest instead "
            "of combining unrelated types/relationships into one request.",
        )])

    subqueries: list[DecomposedSubquery] = []
    for component_index, component_nodes in enumerate(ordered_components):
        component_steps = sorted(
            (
                step
                for step in steps
                if step.from_type_id in component_nodes
                and step.to_type_id in component_nodes
            ),
            key=lambda step: step.step_id,
        )
        required_steps = [step for step in component_steps if not step.optional]
        optional_steps = [step for step in component_steps if step.optional]

        if not component_steps:
            # An isolated required type with no relationships at all: one
            # trivial subquery, not silently dropped.
            groups: list[list[SemanticPathStep]] = [[]]
        else:
            chunks = _chunk_required_steps(required_steps, budget) if required_steps else []
            groups = _attach_optional_steps(chunks, optional_steps, budget)
            if not groups:
                groups = [component_steps]

        for group_steps in groups:
            if not _group_fits_budget(group_steps, budget):
                raise SemanticDecompositionError([QueryFinding(
                    "DECOMPOSITION_SUBQUERY_OVER_BUDGET",
                    f"A bounded group with steps "
                    f"{[s.step_id for s in group_steps]} still exceeds "
                    f"budget.max_hops={budget.max_hops}, "
                    f"budget.max_nodes={budget.max_nodes}, or "
                    f"budget.max_relationships={budget.max_relationships} "
                    "after chunking and optional-step attachment. The "
                    "request cannot be decomposed safely with this budget.",
                )])

        for group_index, group_steps in enumerate(groups):
            subquery_id = f"subquery-{component_index}-{group_index}"
            subqueries.append(
                _build_subquery(request, subquery_id, group_steps, component_nodes)
            )

    if len(subqueries) > budget.max_subqueries:
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_TOO_LARGE",
            f"Request requires {len(subqueries)} subquery(ies) but "
            f"budget.max_subqueries={budget.max_subqueries}. Reduce scope or "
            "raise the reviewed complexity budget before resubmitting.",
        )])

    requested_properties = list(request.requested_properties)
    if requested_properties and len(subqueries) > 1 and schema is None:
        raise SemanticDecompositionError([QueryFinding(
            "DECOMPOSITION_PROPERTY_SCHEMA_REQUIRED",
            "A persisted query schema is required to partition requested "
            "properties across multiple bounded subqueries.",
        )])
    if requested_properties and schema is not None:
        property_owners: dict[str, set[str]] = {}
        for node in schema.nodes:
            for canonical_id, physical_key in node.owner_properties.items():
                property_owners.setdefault(canonical_id, set()).add(
                    node.semantic_id
                )
                property_owners.setdefault(physical_key, set()).add(
                    node.semantic_id
                )
        unknown_properties = sorted(
            prop for prop in requested_properties if prop not in property_owners
        )
        if unknown_properties:
            raise SemanticDecompositionError([QueryFinding(
                "DECOMPOSITION_PROPERTY_OWNER_UNKNOWN",
                "Requested properties are absent from the persisted "
                f"owner-scoped query schema: {unknown_properties}.",
            )])

        covered_properties: set[str] = set()
        partitioned_subqueries: list[DecomposedSubquery] = []
        for subquery in subqueries:
            subquery_types = set(subquery.plan.required_types)
            scoped_properties = [
                prop
                for prop in requested_properties
                if property_owners[prop] & subquery_types
            ]
            covered_properties.update(scoped_properties)
            unsealed_plan = subquery.plan.model_copy(
                update={
                    "requested_properties": scoped_properties,
                    "plan_hash": "",
                }
            )
            partitioned_subqueries.append(replace(
                subquery,
                plan=unsealed_plan.model_copy(
                    update={
                        "plan_hash": compute_query_plan_hash(unsealed_plan)
                    }
                ),
            ))
        uncovered_properties = sorted(
            set(requested_properties) - covered_properties
        )
        if uncovered_properties:
            raise SemanticDecompositionError([QueryFinding(
                "DECOMPOSITION_PROPERTY_OWNER_NOT_IN_PLAN",
                "Requested property owners are not present in any bounded "
                f"subquery: {uncovered_properties}.",
            )])
        subqueries = partitioned_subqueries

    type_subquery_count: dict[str, int] = {}
    for subquery in subqueries:
        for type_id in subquery.plan.required_types:
            type_subquery_count[type_id] = type_subquery_count.get(type_id, 0) + 1

    return [
        replace(
            subquery,
            boundary_type_ids=tuple(sorted(
                type_id
                for type_id in subquery.plan.required_types
                if type_subquery_count.get(type_id, 0) > 1
            )),
        )
        for subquery in subqueries
    ]
