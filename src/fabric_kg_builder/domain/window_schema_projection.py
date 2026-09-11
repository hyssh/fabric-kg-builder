"""Deterministic, unapproved schema retention from a draft's bound working run."""

from fabric_kg_builder.contracts.base import canonical_sha256, deterministic_contract_id
from fabric_kg_builder.domain.compact import CompactProperty, CompactRelationship, CompactType
from fabric_kg_builder.domain.design import (
    DomainDesignDraft, DomainDesignError, DomainDesignSketch, DesignFinding,
    WindowDefinitionPrecedence, WindowDesignCorrections, WindowProjectionFinding, WindowSchemaProjection,
)
from fabric_kg_builder.enrichment.window_run_reuse import window_run_binding
from .window_validation import WindowValidationOperation, validation_context


class WindowSchemaProjectionError(DomainDesignError):
    """A source schema projection is unsupported or its derivation was changed."""


def _key(concept):
    return "ws_" + canonical_sha256(concept.concept_id)


def build_design_corrections(*, actor, rationale, route_targets, completeness, prefer_window_definitions=False):
    values = dict(actor=actor, rationale=rationale, route_targets=route_targets, completeness=completeness,
                  prefer_window_definitions=prefer_window_definitions)
    provisional = WindowDesignCorrections.model_construct(**values, correction_hash="0" * 64)
    return WindowDesignCorrections(**values, correction_hash=canonical_sha256(
        provisional.model_dump(mode="json", exclude={"correction_hash"}),
    ))


def _apply_corrections(parent, sketch, corrections):
    from .question_routing import is_sql_question, routed_question_copies

    corrections = WindowDesignCorrections.model_validate(corrections.model_dump(mode="python"))
    types = {item.key for item in sketch.types}
    routes = {item.question_id: item for item in sketch.question_routes}
    questions = {
        item.id: item for item in routed_question_copies(parent.inputs.intake.competency_questions, sketch.question_routes)
    }
    for question_id, target in corrections.route_targets.items():
        if question_id not in routes or question_id not in questions or target not in types:
            raise WindowSchemaProjectionError("WINDOW_CORRECTION_UNKNOWN_ROUTE_REFERENCE")
        if is_sql_question(questions[question_id]):
            raise WindowSchemaProjectionError("WINDOW_CORRECTION_SQL_ROUTE_MUST_REMAIN_NULL")
        routes[question_id] = routes[question_id].model_copy(update={"target_key": target})
    keys = {item.key for item in sketch.completeness}
    relationships = {item.key for item in sketch.relationships}
    for item in corrections.completeness:
        if item.key in keys:
            raise WindowSchemaProjectionError("WINDOW_CORRECTION_COMPLETENESS_KEY_EXISTS: existing declarations are never replaced")
        if item.relationship_key not in relationships or not item.question_ids or not set(item.question_ids) <= set(questions):
            raise WindowSchemaProjectionError("WINDOW_CORRECTION_UNKNOWN_COMPLETENESS_REFERENCE")
        if item.ordered != (item.ordinal_property_key is not None) or (item.kind == "required_role" and item.ordered):
            raise WindowSchemaProjectionError("WINDOW_CORRECTION_INVALID_ORDERING")
        keys.add(item.key)
    return sketch.model_copy(update={
        "question_routes": [routes[item.question_id] for item in sketch.question_routes],
        "completeness": [*sketch.completeness, *corrections.completeness],
        "review_concerns": [*sketch.review_concerns, DesignFinding(
            code="explicit_window_design_corrections",
            message=(
                f"Unapproved operator schema corrections by {corrections.actor}: {corrections.rationale}. "
                f"Audit hash {corrections.correction_hash}. "
                + ("Declared structural edits and explicitly requested exact source-definition precedence are audited; scopes and identities were not overridden. "
                   "No observed counts, members, ordering facts or approval were supplied."
                   if corrections.prefer_window_definitions else
                   "Only declared route targets and additive completeness definitions changed; no observed counts, members, ordering facts or approval were supplied.")
            ),
        )],
    })


def _derive(parent, corrections=None, *, _validation=None, _projection_version="window-schema-projection/1.2.0"):
    if parent.window_run is None or parent.schema_projection is not None:
        raise WindowSchemaProjectionError("Projection requires an original integrated model draft, not a projected draft")
    sketch = parent.sketch
    types, properties, relationships = list(sketch.types), list(sketch.properties), list(sketch.relationships)
    retained, added, findings, precedence = {}, [], [], []
    concepts = sorted(parent.window_run.final_snapshot.concepts, key=lambda item: item.concept_id)
    by_id = {concept.concept_id: concept for concept in concepts}
    allow_unresolved_annotation = _projection_version != "window-schema-projection/1.0.0"

    def endpoint_scope(key, policy):
        scope = {key}
        if policy == "allow_subtypes":
            type_parents = {item.key: item.parent_key for item in types}
            for item in types:
                ancestor = item.parent_key
                while ancestor is not None:
                    if ancestor == key:
                        scope.add(item.key)
                        break
                    ancestor = type_parents[ancestor]
        return scope

    def relationship_scopes_overlap(concept, source, target, existing):
        prior = [
            by_id[source_id] for source_id, key in retained.items()
            if key == existing.key and by_id[source_id].kind == "relationship"
        ]
        existing_policy = prior[0].endpoint_policy if prior else "exact"
        return bool(
            endpoint_scope(source, concept.endpoint_policy) & endpoint_scope(existing.source_key, existing_policy)
            and endpoint_scope(target, concept.endpoint_policy) & endpoint_scope(existing.target_key, existing_policy)
        )

    def unsupported(concept, code, reason):
        findings.append(WindowProjectionFinding(
            concept_id=concept.concept_id, concept_name=concept.name, kind=concept.kind, code=code, message=reason,
        ))

    def compatible_source_reuse(concept, key):
        for source_id, existing_key in retained.items():
            previous = by_id[source_id]
            if previous.kind == concept.kind and existing_key == key and any(
                getattr(previous, field) != getattr(concept, field)
                for field in ("definition", "identity_policy", "endpoint_policy")
            ):
                unsupported(concept, "working_definition_conflict",
                            "Multiple working concepts would share a schema key but have different definitions or policies; no merge.")
                return False
        return True

    def retained_definition(concept, existing):
        if existing.description == concept.definition:
            return existing
        if corrections is None or not corrections.prefer_window_definitions:
            unsupported(
                concept, "existing_definition_conflict",
                "Exact name/scope is insufficient: model and working definitions differ. Explicit source precedence or separate review is required.",
            )
            return None
        precedence.append(WindowDefinitionPrecedence(
            concept_id=concept.concept_id, kind=concept.kind, schema_key=existing.key,
            parent_definition=existing.description, parent_definition_hash=canonical_sha256(existing.description),
            source_definition=concept.definition, source_definition_hash=canonical_sha256(concept.definition),
        ))
        return existing.model_copy(update={"description": concept.definition})

    entities = [concept for concept in concepts if concept.kind == "entity"]
    while entities:
        deferred = []
        for concept in entities:
            if concept.parent_type_id is not None and concept.parent_type_id not in retained:
                deferred.append(concept)
                continue
            if concept.identity_policy != {"mode": "unresolved"}:
                unsupported(concept, "identity_policy_not_representable",
                            "Only explicitly unresolved working identity can acquire source-scoped compiler identity; no keys were invented.")
                continue
            parent_key = retained.get(concept.parent_type_id)
            matches = [item for item in types if item.display_name.casefold() == concept.name.casefold()]
            if matches:
                match = matches[0]
                root = match
                by_key = {item.key: item for item in types}
                while root.parent_key is not None:
                    root = by_key[root.parent_key]
                if (
                    len(matches) != 1 or match.display_name != concept.name
                    or match.parent_key != parent_key or root.identity_property_keys
                ):
                    unsupported(concept, "existing_type_conflict", "Existing name has incompatible parent/identity or is not unique; no merge or identity downgrade.")
                    continue
                if compatible_source_reuse(concept, match.key):
                    updated = retained_definition(concept, match)
                    if updated is not None:
                        types[types.index(match)] = updated
                        retained[concept.concept_id] = match.key
                continue
            key = _key(concept)
            if any(item.key == key for item in types):
                unsupported(concept, "schema_key_conflict", "Deterministic schema key already denotes a different type.")
                continue
            types.append(CompactType(
                key=key, display_name=concept.name, description=concept.definition, parent_key=parent_key,
                identity_property_keys=[], question_ids=[], evidence_ids=[],
                rationale=f"Deterministically retain working schema concept {concept.concept_id}; unresolved identity stays source-scoped. Domain-scoped layer is a proposal, not inferred common authority; final L1 review is required.",
                classification="domain_specialization" if parent_key is not None else "domain",
            ))
            retained[concept.concept_id] = key
            added.append(concept.concept_id)
        if len(deferred) == len(entities):
            for concept in deferred:
                unsupported(concept, "parent_not_representable", "Working parent is not retained; child was not silently promoted to a root.")
            break
        entities = deferred

    value_types = {"string", "integer", "number", "boolean", "date", "datetime"}
    for concept in concepts:
        if concept.kind == "entity":
            continue
        if concept.kind == "property":
            if len(concept.owner_type_ids) != 1:
                unsupported(concept, "multi_owner_property_not_representable", "Compact properties have one owner; the working owner set was not narrowed.")
                continue
            owner = retained.get(concept.owner_type_ids[0])
            policy_supported = not concept.identity_policy or (
                allow_unresolved_annotation and concept.identity_policy == {"mode": "unresolved"}
            )
            if owner is None or concept.value_type not in value_types or not policy_supported:
                unsupported(concept, "property_definition_not_representable", "Owner, value type or property identity policy cannot be represented exactly.")
                continue
            matches = [item for item in properties if item.owner_key == owner and item.display_name.casefold() == concept.name.casefold()]
            if matches:
                if len(matches) != 1 or matches[0].display_name != concept.name or matches[0].value_type != concept.value_type:
                    unsupported(concept, "existing_property_conflict", "Existing owner-qualified name has incompatible value type or is not unique.")
                    continue
                key = owner + "." + matches[0].key
                if compatible_source_reuse(concept, key):
                    retained[concept.concept_id] = key
                continue
            key = _key(concept)
            if any(item.owner_key == owner and item.key == key for item in properties):
                unsupported(concept, "schema_key_conflict", "Deterministic schema key already denotes a different property.")
                continue
            properties.append(CompactProperty(
                owner_key=owner, key=key, display_name=concept.name, value_type=concept.value_type, required=False,
            ))
            retained[concept.concept_id] = owner + "." + key
        else:
            if len(concept.source_type_ids) != 1 or len(concept.target_type_ids) != 1:
                unsupported(concept, "multi_endpoint_relationship_not_representable",
                            "Compact relationships have singular endpoints; the full working endpoint sets remain unmodified and pending.")
                continue
            source, target = retained.get(concept.source_type_ids[0]), retained.get(concept.target_type_ids[0])
            policy = concept.identity_policy
            if source is None or target is None:
                unsupported(concept, "endpoint_not_representable", "A working endpoint type is not retained; no substitute endpoint was invented.")
                continue
            policy_supported = set(policy) == {"context_policy"} or (
                allow_unresolved_annotation and set(policy) == {"mode", "context_policy"}
                and policy["mode"] == "unresolved"
            )
            if not policy_supported or not isinstance(policy["context_policy"], str) or not policy["context_policy"].strip():
                unsupported(concept, "relationship_identity_not_representable", "Working relationship identity cannot be represented as an exact context policy.")
                continue
            matches = [item for item in relationships if item.display_name.casefold() == concept.name.casefold()]
            if _projection_version == "window-schema-projection/1.2.0":
                if any(
                    item.display_name != concept.name or (
                        (item.source_key, item.target_key) != (source, target)
                        and relationship_scopes_overlap(concept, source, target, item)
                    )
                    for item in matches
                ):
                    unsupported(concept, "existing_relationship_conflict",
                                "Existing relationship name has ambiguous spelling or overlapping endpoint scopes; no merge.")
                    continue
                matches = [item for item in matches if (item.source_key, item.target_key) == (source, target)]
            if matches:
                if len(matches) != 1 or matches[0].display_name != concept.name or (matches[0].source_key, matches[0].target_key) != (source, target):
                    unsupported(concept, "existing_relationship_conflict", "Existing relationship name has incompatible endpoints or is not unique; no merge.")
                    continue
                key = matches[0].key
                if not compatible_source_reuse(concept, key):
                    continue
                updated = retained_definition(concept, matches[0])
                if updated is None:
                    continue
                relationships[relationships.index(matches[0])] = updated
            else:
                key = _key(concept)
                if any(item.key == key for item in relationships):
                    unsupported(concept, "schema_key_conflict", "Deterministic schema key already denotes a different relationship.")
                    continue
                relationships.append(CompactRelationship(
                    key=key, display_name=concept.name, description=concept.definition, source_key=source, target_key=target,
                    question_ids=[], evidence_ids=[],
                    rationale=f"Deterministically retain working relationship {concept.concept_id}; no instance facts or CQ bindings were invented.",
                ))
                added.append(concept.concept_id)
            retained[concept.concept_id] = key
            continue
        added.append(concept.concept_id)

    unsupported_ids = {finding.concept_id for finding in findings}
    if set(retained) & unsupported_ids or set(retained) | unsupported_ids != set(by_id):
        raise WindowSchemaProjectionError("WINDOW_SCHEMA_PROJECTION_ACCOUNTING_MISMATCH")
    concerns = [*sketch.review_concerns, DesignFinding(
        code="window_schema_projection_unapproved",
        message=(
            f"Deterministic source-schema retention: {len(retained)} of {len(concepts)} working concepts represented; "
            f"{len(findings)} unsupported. Parent model routes/intake are unchanged; added concepts have no invented evidence or CQ tags. "
            "Working aliases and unmodeled annotations remain bound source references, not new approved aliases. "
            "The working schema has no approved semantic-layer field: new roots explicitly propose domain scope and children domain specialization, subject to L1 review. "
            "Final evaluation, domain approval, mapping review and evidence validation remain required."
        ),
    ), *[DesignFinding(code="window_projection_" + item.code, message=f"{item.concept_id}: {item.message}") for item in findings]]
    result = DomainDesignSketch(**{
        **sketch.model_dump(mode="python"),
        "types": types, "properties": properties, "relationships": relationships, "review_concerns": concerns,
    })
    if corrections is not None:
        result = _apply_corrections(parent, result, corrections)
    binding = window_run_binding(parent.window_run, parent.window_run_acceptance, _validation=_validation)
    values = {
        "projection_version": _projection_version,
        "parent_draft_hash": parent.draft_hash, "parent_artifact_version": parent.artifact_version,
        "parent_sketch": parent.sketch, "window_run_hash": binding.window_run_hash, "snapshot_hash": binding.snapshot_hash,
        "scope_acceptance_hash": binding.scope_acceptance_hash, "coverage_acceptance_hash": binding.coverage_acceptance_hash,
        "retained_concept_keys": retained, "derived_concept_ids": sorted(added), "unsupported_findings": findings,
        **({"operator_corrections": corrections} if corrections is not None else {}),
        **({"definition_precedence": precedence} if precedence else {}),
        **({"source_identity_policies": {
            concept.concept_id: dict(concept.identity_policy) for concept in concepts
        }} if allow_unresolved_annotation else {}),
    }
    provisional = WindowSchemaProjection.model_construct(**values, projection_hash="0" * 64)
    projection = WindowSchemaProjection(**values, projection_hash=canonical_sha256(
        provisional.model_dump(mode="json", exclude={"projection_hash"}),
    ))
    return result, projection


def retain_window_schema(parent, *, corrections=None, _validation=None):
    operation = _validation or WindowValidationOperation()
    parent = DomainDesignDraft.model_validate(parent.model_dump(mode="python"), context=validation_context(operation))
    sketch, projection = _derive(parent, corrections, _validation=operation)
    values = parent.model_dump(mode="python", exclude={"draft_id", "draft_hash"})
    if parent.window_run_acceptance is not None:
        values["window_run_acceptance"] = parent.window_run_acceptance
    values.update(artifact_version="8.0.0", sketch=sketch, schema_projection=projection, model_call_count=0)
    digest = canonical_sha256(values)
    return DomainDesignDraft.model_validate({
        **values, "draft_hash": digest, "draft_id": deterministic_contract_id("domain-design-draft", {"draft_hash": digest}),
    }, context=validation_context(operation))


def validate_schema_projection(draft, *, _validation=None):
    operation = _validation or WindowValidationOperation()
    projection = draft.schema_projection
    values = draft.model_dump(mode="python", exclude={"schema_projection", "draft_id", "draft_hash"})
    values.update(
        artifact_version=projection.parent_artifact_version, sketch=projection.parent_sketch,
        model_call_count=projection.parent_model_call_count,
        draft_hash=projection.parent_draft_hash,
        draft_id=deterministic_contract_id("domain-design-draft", {"draft_hash": projection.parent_draft_hash}),
    )
    parent = DomainDesignDraft.model_validate(values, context=validation_context(operation))
    expected_sketch, expected_projection = _derive(
        parent, projection.operator_corrections, _validation=operation,
        _projection_version=projection.projection_version,
    )
    if draft.sketch != expected_sketch or projection != expected_projection:
        raise WindowSchemaProjectionError("WINDOW_SCHEMA_PROJECTION_DERIVATION_MISMATCH")


def project_compiler_policies(draft, candidates):
    """Retain policies that the compact model wire format cannot carry itself."""
    policies = {
        "relationship-type:" + draft.schema_projection.retained_concept_keys[concept.concept_id]: concept
        for concept in draft.window_run.final_snapshot.concepts
        if concept.kind == "relationship" and concept.concept_id in draft.schema_projection.retained_concept_keys
    }
    return candidates.model_copy(update={"relationship_candidates": tuple(
        item.model_copy(update={
            "endpoint_policy": policies[item.relationship_type_id].endpoint_policy,
            "identity_context_policy": policies[item.relationship_type_id].identity_policy["context_policy"],
        }) if item.relationship_type_id in policies else item
        for item in candidates.relationship_candidates
    )})


def projection_contract_binding(draft):
    from .models import WindowSchemaProjectionBinding

    projection = draft.schema_projection
    return WindowSchemaProjectionBinding(
        projection_hash=projection.projection_hash, parent_draft_hash=projection.parent_draft_hash,
        window_run_hash=projection.window_run_hash, snapshot_hash=projection.snapshot_hash,
        retained_concept_keys=projection.retained_concept_keys,
        required_retained_type_ids=sorted("semantic-type:" + key for key in projected_type_keys(draft)),
        unsupported_concepts={item.concept_id: f"{item.code}: {item.message}" for item in projection.unsupported_findings},
    )


def projected_type_keys(draft):
    """Read the entity subset of an already validated source projection."""
    if draft.schema_projection is None:
        return set()
    return {
        draft.schema_projection.retained_concept_keys[concept.concept_id]
        for concept in draft.window_run.final_snapshot.concepts
        if concept.kind == "entity" and concept.concept_id in draft.schema_projection.retained_concept_keys
    }


def validated_projection_type_ids(draft, *, intake, candidates, _validation=None):
    from .compact import expand_compact_design
    from .design import _checked_draft

    draft = _checked_draft(draft, _validation=_validation)
    if draft.schema_projection is None or draft.inputs.intake != intake:
        raise WindowSchemaProjectionError("SOURCE_TYPE_RETENTION_REQUIRES_EXACT_PROJECTED_DESIGN")
    expected = expand_compact_design(
        draft.sketch, intake=draft.inputs.intake,
        known_evidence_ids={item.evidence_span_id for item in draft.samples.evidence_spans},
        derive_route_metadata=False,
    )
    if candidates.semantic_type_candidates != expected.semantic_type_candidates:
        raise WindowSchemaProjectionError("SOURCE_TYPE_RETENTION_CANDIDATE_DRIFT")
    return {"semantic-type:" + key for key in projected_type_keys(draft)}
