"""Deterministic schema-2 Graph query planning and rendering."""

from __future__ import annotations

import re

from .query_validation import (
    QueryFinding,
    SemanticQueryValidationError,
    resolve_query_plan,
    validate_physical_query,
)
from .schemas import (
    ComplexityBudget,
    PersistedQuerySchema,
    SemanticQueryOutput,
    SemanticQueryPlan,
    compute_persisted_query_schema_hash,
    compute_query_plan_hash,
)


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


def _identifier(value: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value) or "`" in value:
        raise ValueError(f"Unsafe persisted Graph identifier: {value!r}.")
    return f"`{value}`"


def _outputs_for_path(
    schema: PersistedQuerySchema,
    steps: list,
) -> list[SemanticQueryOutput]:
    node_by_id = {node.semantic_id: node for node in schema.nodes}
    outputs: list[SemanticQueryOutput] = []
    node_ids = [steps[0].from_type_id]
    node_ids.extend(step.to_type_id for step in steps)
    for index, semantic_id in enumerate(node_ids):
        node = node_by_id[semantic_id]
        outputs.extend((
            SemanticQueryOutput(
                alias=f"n{index}_id",
                owner_kind="node",
                owner_index=index,
                semantic_id=semantic_id,
                property_name=node.id_property,
                purpose="id",
            ),
            SemanticQueryOutput(
                alias=f"n{index}_display",
                owner_kind="node",
                owner_index=index,
                semantic_id=semantic_id,
                property_name=node.display_property,
                purpose="display",
            ),
        ))
    for index, step in enumerate(steps):
        outputs.append(SemanticQueryOutput(
            alias=f"r{index}_evidence_id",
            owner_kind="relationship",
            owner_index=index,
            semantic_id=step.via_relationship_id,
            property_name=step.evidence_property,
            purpose="evidence",
        ))
    return outputs


def compile_approved_query_plan(
    *,
    schema: PersistedQuerySchema,
    question_id: str,
    intent: str,
    result_limit: int = 100,
) -> SemanticQueryPlan:
    """Compile one approved question path into a sealed structured plan."""
    if schema.schema_mode != "schema2_bounded" or schema.authority is None:
        raise ValueError(
            "Approved bounded query plans require a schema-2 query authority."
        )
    if schema.schema_hash != compute_persisted_query_schema_hash(schema):
        raise ValueError(
            "Persisted query schema hash does not match its contents."
        )
    authority = schema.authority
    approved = next(
        (
            path
            for path in authority.question_paths
            if path.question_id == question_id
        ),
        None,
    )
    if approved is None:
        raise ValueError(
            f"Question {question_id!r} is not present in approved query authority."
        )
    if not approved.covered:
        raise ValueError(
            f"Question {question_id!r} is explicitly unsupported: "
            f"{approved.unsupported_reason}."
        )
    node_ids = [approved.steps[0].from_type_id]
    node_ids.extend(step.to_type_id for step in approved.steps)
    outputs = _outputs_for_path(schema, list(approved.steps))

    plan = SemanticQueryPlan(
        schema_mode="schema2_bounded",
        manifest_hash=schema.manifest_hash,
        domain_contract_hash=authority.domain_contract_hash,
        semantic_crosswalk_hash=schema.semantic_crosswalk_hash,
        query_authority_hash=authority.authority_hash,
        query_schema_hash=schema.schema_hash,
        question_id=question_id,
        intent=intent,
        required_types=node_ids,
        required_relationships=[
            step.via_relationship_id for step in approved.steps
        ],
        requested_properties=[
            output.property_name
            for output in outputs
            if output.owner_kind == "node"
        ],
        evidence_required=True,
        path_steps=list(approved.steps),
        outputs=outputs,
        result_limit=result_limit,
        budget=ComplexityBudget(
            max_hops=authority.approved_max_hops,
            max_nodes=max(6, len(set(node_ids))),
            max_relationships=max(
                5,
                len({
                    step.via_relationship_id for step in approved.steps
                }),
            ),
            max_rows_per_subquery=100,
            max_subqueries=4,
        ),
    )
    return plan.model_copy(update={"plan_hash": compute_query_plan_hash(plan)})


def validate_bounded_query_plan(
    plan: SemanticQueryPlan,
    schema: PersistedQuerySchema,
    *,
    raise_on_findings: bool = False,
) -> list[QueryFinding]:
    """Validate plan identity and exact equality with its approved question path."""
    findings: list[QueryFinding] = []
    if plan.schema_mode != "schema2_bounded":
        findings.append(QueryFinding(
            "PLAN_SCHEMA_MODE_MISMATCH",
            "Bounded query execution requires plan.schema_mode="
            "'schema2_bounded'.",
        ))
    if schema.schema_mode != "schema2_bounded" or schema.authority is None:
        findings.append(QueryFinding(
            "QUERY_AUTHORITY_MISSING",
            "Schema-2 bounded execution requires sealed query authority.",
        ))
    elif schema.schema_hash != compute_persisted_query_schema_hash(schema):
        findings.append(QueryFinding(
            "QUERY_SCHEMA_HASH_MISMATCH",
            "Persisted query schema hash differs from its contents.",
        ))
    else:
        authority = schema.authority
        expected_identity = {
            "manifest_hash": schema.manifest_hash,
            "domain_contract_hash": authority.domain_contract_hash,
            "semantic_crosswalk_hash": schema.semantic_crosswalk_hash,
            "query_authority_hash": authority.authority_hash,
            "query_schema_hash": schema.schema_hash,
        }
        for field_name, expected in expected_identity.items():
            actual = getattr(plan, field_name)
            if actual != expected:
                findings.append(QueryFinding(
                    "PLAN_AUTHORITY_HASH_MISMATCH",
                    f"Plan {field_name}={actual!r}; expected {expected!r}.",
                ))
        if plan.budget.max_hops != authority.approved_max_hops:
            findings.append(QueryFinding(
                "PLAN_K_MISMATCH",
                "Plan max_hops does not equal the approved derived K.",
            ))
        approved = next(
            (
                path
                for path in authority.question_paths
                if path.question_id == plan.question_id
            ),
            None,
        )
        if approved is None or not approved.covered:
            findings.append(QueryFinding(
                "PLAN_QUESTION_NOT_APPROVED",
                f"Plan question {plan.question_id!r} has no approved covered path.",
            ))
        elif plan.path_steps != approved.steps:
            findings.append(QueryFinding(
                "PLAN_PATH_NOT_APPROVED",
                "Plan hops differ from the sealed approved question path.",
            ))
        elif plan.outputs != _outputs_for_path(schema, list(approved.steps)):
            findings.append(QueryFinding(
                "PLAN_OUTPUTS_NOT_APPROVED",
                "Plan outputs differ from the complete approved scalar "
                "ID/display/evidence projection.",
            ))
        if len(plan.path_steps) > authority.approved_max_hops:
            findings.append(QueryFinding(
                "PLAN_OVER_APPROVED_K",
                f"Plan has {len(plan.path_steps)} hops but approved "
                f"K={authority.approved_max_hops}.",
            ))
        if len(plan.path_steps) > 4:
            findings.append(QueryFinding(
                "PLAN_OVER_UNIVERSAL_K",
                "Graph paths above four hops are universally prohibited.",
            ))
        if plan.plan_hash != compute_query_plan_hash(plan):
            findings.append(QueryFinding(
                "PLAN_HASH_MISMATCH",
                "Semantic query plan hash differs from its contents.",
            ))
    findings.extend(resolve_query_plan(plan, schema))
    if raise_on_findings and findings:
        raise SemanticQueryValidationError(findings)
    return findings


def render_bounded_gql(
    plan: SemanticQueryPlan,
    schema: PersistedQuerySchema,
) -> str:
    """Validate a structured plan and render one deterministic Fabric GQL query."""
    validate_bounded_query_plan(plan, schema, raise_on_findings=True)
    if not plan.path_steps:
        raise ValueError("A bounded Graph query requires at least one hop.")

    fragments: list[str] = []
    first = plan.path_steps[0]
    fragments.append(f"(n0:{_identifier(first.from_graph_label)})")
    for index, step in enumerate(plan.path_steps):
        relation = f"[r{index}:{_identifier(step.relationship_graph_label)}]"
        next_node = f"(n{index + 1}:{_identifier(step.to_graph_label)})"
        if step.direction == "source_to_target":
            fragments.extend((f"-{relation}->", next_node))
        else:
            fragments.extend((f"<-{relation}-", next_node))

    projections: list[str] = []
    for output in plan.outputs:
        owner_prefix = "n" if output.owner_kind == "node" else "r"
        owner = f"{owner_prefix}{output.owner_index}"
        projections.append(
            f"{owner}.{_identifier(output.property_name)} "
            f"AS {_identifier(output.alias)}"
        )
    query = (
        "MATCH "
        + "".join(fragments)
        + "\nRETURN "
        + ", ".join(projections)
        + f"\nLIMIT {plan.result_limit}"
    )
    validate_physical_query(
        query,
        plan,
        schema=schema,
        raise_on_findings=True,
    )
    return query
