"""Golden tests for schema-2 bounded Graph query authority."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fabric_kg_builder.agent.tools.fabric_data import FabricDataAgentAdapter
from fabric_kg_builder.app.api import (
    _answer_question,
    _readiness_passes,
    create_app,
)
from fabric_kg_builder.app.auth import AuthError, InboundAuthVerifier
from fabric_kg_builder.app.config import AppConfigError, load_app_config
from fabric_kg_builder.app.models import ChatRequest
from fabric_kg_builder.agent.tools.kb_tool import KBResult
from fabric_kg_builder.cli.app_cmd import deploy_app_cmd
from click.testing import CliRunner
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.runtime.contract import (
    CompetencyCase,
    ExpectedOutcome,
    GraphEntityBinding,
    GraphProbe,
    GraphRelationshipBinding,
    RouteProbes,
    RouteRequirements,
    compile_competency_contract,
)
from fabric_kg_builder.runtime.executors import FabricGraphExecutor
from fabric_kg_builder.runtime.acceptance import validate_deployment_evidence
from fabric_kg_builder.runtime.collector import (
    DeploymentRuntimeConfig,
    GraphRuntimeConfig,
    McpRuntimeConfig,
    RuntimeConfig,
    SearchRuntimeConfig,
)
from fabric_kg_builder.semantic.instructions import (
    build_contract_agent_instructions,
)
from fabric_kg_builder.semantic.query_planning import build_persisted_query_schema
from fabric_kg_builder.semantic.query_rendering import (
    compile_approved_query_plan,
    render_bounded_gql,
    validate_bounded_query_plan,
)
from fabric_kg_builder.semantic.query_validation import validate_physical_query
from fabric_kg_builder.semantic.schemas import (
    CrosswalkEntry,
    DataAvailability,
    EntityTableSpec,
    GraphEdgeProjection,
    GraphNodeProjection,
    ManifestEntityTypeEntry,
    ManifestRelationshipEntry,
    MaterializationPlan,
    PersistedQuerySchema,
    RelationshipTableSpec,
    SemanticCrosswalk,
    SemanticModelManifest,
    SemanticQueryPlan,
    ComplexityBudget,
    compute_manifest_hash,
)


def _domain_contract(max_hops: int = 3) -> DomainContractV2:
    entity_count = max_hops + 1
    entity_ids = [f"entity-type:t{index}" for index in range(entity_count)]
    relationship_ids = [
        f"relationship-type:r{index}" for index in range(1, max_hops + 1)
    ]
    question_ids = [f"cq:q{index}" for index in range(1, 6)]

    def path(length: int, *, reverse: bool = False) -> list[dict[str, str]]:
        if reverse:
            return [{
                "from_type": entity_ids[1],
                "relationship_type": relationship_ids[0],
                "to_type": entity_ids[0],
                "traversal": "reverse",
            }]
        return [
            {
                "from_type": entity_ids[index],
                "relationship_type": relationship_ids[index],
                "to_type": entity_ids[index + 1],
                "traversal": "forward",
            }
            for index in range(length)
        ]

    plans = [
        path(1),
        path(max_hops),
        path(max(1, max_hops - 1)),
        path(1, reverse=True),
        [path(max_hops)[-1]],
    ]
    relationship_questions: dict[str, list[str]] = {
        relationship_id: [] for relationship_id in relationship_ids
    }
    for question_id, steps in zip(question_ids, plans):
        for step in steps:
            relationship_questions[step["relationship_type"]].append(question_id)

    payload = {
        "schema_version": "2.0",
        "domain": {
            "name": "Bounded test",
            "description": "Bounded query authority test domain.",
            "subdomains": [],
        },
        "business": {
            "organization_context": "Test operations.",
            "users": ["Analyst"],
            "decisions": ["Trace approved paths."],
        },
        "problem": {
            "statement": "Answer approved relationship questions safely.",
            "desired_outcomes": ["Bounded Graph answers."],
            "in_scope": ["Approved paths."],
            "out_of_scope": [],
        },
        "competency_questions": [
            {
                "id": question_id,
                "question": f"Which approved entities answer question {index}?",
                "business_critical": True,
            }
            for index, question_id in enumerate(question_ids, 1)
        ],
        "terminology": {
            "canonical_terms": [],
            "ambiguous_terms": [],
        },
        "candidate_model": {
            "entity_types": [
                {
                    "id": entity_id,
                    "name": f"Type {index}",
                    "description": f"Entity type {index}.",
                    "business_defined": True,
                }
                for index, entity_id in enumerate(entity_ids)
            ],
            "relationship_types": [
                {
                    "id": relationship_id,
                    "predicate": f"r{index}",
                    "description": f"Directed relationship {index}.",
                    "source_types": [entity_ids[index - 1]],
                    "target_types": [entity_ids[index]],
                    "competency_question_ids": relationship_questions[
                        relationship_id
                    ],
                    "governance_rule": "Approved for bounded competency paths.",
                }
                for index, relationship_id in enumerate(
                    relationship_ids,
                    1,
                )
            ],
        },
        "constraints": {
            "temporal": [],
            "regulatory": [],
            "privacy": [],
            "safety": [],
        },
        "examples": {"positive": [], "negative": []},
        "reasoning_policy": {
            "relationship_type_count": len(relationship_ids),
            "max_hops": max_hops,
            "max_hops_rationale": (
                "Question cq:q2 requires four evidence-backed directed hops."
                if max_hops == 4
                else None
            ),
        },
        "question_plans": [
            {
                "question_id": question_id,
                "required_path": steps,
                "hop_count": len(steps),
                "covered": True,
                "shortest_path": True,
            }
            for question_id, steps in zip(question_ids, plans)
        ],
        "approval": {"status": "draft"},
    }
    draft = DomainContractV2.model_validate(payload)
    contract_hash = compute_contract_hash(draft)
    return DomainContractV2.model_validate({
        **draft.model_dump(mode="json"),
        "approval": {
            "status": "approved",
            "approved_by": "tester",
            "approved_at_utc": "2026-08-24T20:00:00Z",
            "contract_hash": contract_hash,
            "proposal_hash": "sha256:proposal",
            "source_profile_hash": "sha256:source",
            "prompt_hash": "sha256:prompt",
            "prompt_version": "v1",
            "model_version": "test",
            "model_hash": "sha256:model",
        },
    })


def _query_schema(max_hops: int = 3):
    contract = _domain_contract(max_hops)
    entity_ids = [
        entity.id for entity in contract.candidate_model.entity_types
    ]
    relationship_types = contract.candidate_model.relationship_types
    manifest = SemanticModelManifest(
        semantic_contract_hash="sha256:" + "a" * 64,
        stable_id_lock_hash="sha256:" + "b" * 64,
        data_version="test",
        entity_types=[
            ManifestEntityTypeEntry(
                semantic_id=entity_id,
                canonical_name=f"T{index}",
                business_name=f"Type {index}",
                aliases=[],
                description=f"Unique entity description {index}.",
                identifier_properties=[f"property:{index}:id"],
                graph_projection=GraphNodeProjection(
                    label=f"T{index}",
                    alias=f"T{index}_node",
                    property_keys=["entity_id", "display_name"],
                ),
            )
            for index, entity_id in enumerate(entity_ids)
        ],
        relationship_types=[
            ManifestRelationshipEntry(
                semantic_id=relationship.id,
                predicate=relationship.predicate,
                business_name=relationship.predicate,
                description=relationship.description,
                source_type_id=relationship.source_types[0],
                target_type_id=relationship.target_types[0],
                graph_projection=GraphEdgeProjection(
                    label=relationship.predicate,
                    alias=f"{relationship.predicate}_edge",
                    source_label=f"T{index - 1}",
                    target_label=f"T{index}",
                ),
                source_endpoint_column="source_entity_id",
                target_endpoint_column="target_entity_id",
            )
            for index, relationship in enumerate(relationship_types, 1)
        ],
    )
    manifest = manifest.model_copy(update={
        "manifest_hash": compute_manifest_hash(manifest)
    })
    crosswalk = SemanticCrosswalk(
        manifest_hash=manifest.manifest_hash,
        entity_type_entries=[
            CrosswalkEntry(
                semantic_id=entity_id,
                element_kind="entity_type",
                graph_label=f"T{index}",
                graph_alias=f"T{index}_node",
            )
            for index, entity_id in enumerate(entity_ids)
        ],
        relationship_type_entries=[
            CrosswalkEntry(
                semantic_id=relationship.id,
                element_kind="relationship_type",
                source_type_id=relationship.source_types[0],
                target_type_id=relationship.target_types[0],
                graph_label=relationship.predicate,
                graph_alias=f"{relationship.predicate}_edge",
                direction="source_to_target",
            )
            for relationship in relationship_types
        ],
    )
    entity_tables = [
        EntityTableSpec(
            semantic_id=entity_id,
            table_name=f"entity_{index}",
            entity_id_column="entity_id",
            display_name_column="display_name",
        )
        for index, entity_id in enumerate(entity_ids)
    ]
    relationship_tables = [
        RelationshipTableSpec(
            semantic_id=relationship.id,
            table_name=f"relationship_{index}",
            evidence_column="evidence_id",
        )
        for index, relationship in enumerate(relationship_types, 1)
    ]
    plan = MaterializationPlan(
        manifest_hash=manifest.manifest_hash,
        entity_tables=entity_tables,
        relationship_tables=relationship_tables,
        data_availability=[
            DataAvailability(semantic_id=semantic_id, status="not_observed")
            for semantic_id in [
                *entity_ids,
                *(relationship.id for relationship in relationship_types),
            ]
        ],
    )
    return build_persisted_query_schema(
        manifest,
        crosswalk,
        materialization_plan=plan,
        domain_contract=contract,
    )


def test_one_hop_and_directed_three_hop_rendering() -> None:
    schema = _query_schema(3)
    one = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q1",
        intent="one hop",
    )
    three = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q2",
        intent="three hops",
    )

    assert len(one.path_steps) == 1
    assert len(three.path_steps) == 3
    assert render_bounded_gql(three, schema).count("]->") == 3
    assert "RETURN n0.`entity_id`" in render_bounded_gql(one, schema)
    assert "LIMIT 100" in render_bounded_gql(three, schema)


def test_justified_four_hop_and_rejected_five_hop() -> None:
    schema = _query_schema(4)
    four = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q2",
        intent="four hops",
    )
    assert len(four.path_steps) == 4
    assert render_bounded_gql(four, schema).count("]->") == 4

    payload = _domain_contract(4).model_dump(mode="json")
    payload["reasoning_policy"]["max_hops"] = 5
    with pytest.raises(ValidationError, match="K must be between 1 and 4"):
        DomainContractV2.model_validate(payload)


def test_reverse_traversal_is_explicit_and_endpoint_compatible() -> None:
    schema = _query_schema(3)
    plan = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q4",
        intent="reverse",
    )
    query = render_bounded_gql(plan, schema)
    assert plan.path_steps[0].direction == "target_to_source"
    assert "<-[r0:`r1`]-" in query


@pytest.mark.parametrize(
    ("query", "code"),
    [
        (
            "MATCH (a:T0)-[:r1*1..3]->(b:T1) "
            "RETURN a.entity_id LIMIT 10",
            "QUERY_VARIABLE_LENGTH_TRAVERSAL",
        ),
        (
            "MATCH (a:T0)-[:unknown]->(b:T1) "
            "RETURN a.entity_id LIMIT 10",
            "QUERY_UNKNOWN_RELATIONSHIP_LABEL",
        ),
        (
            "MATCH (a:T1)-[:r1]->(b:T0) RETURN a.entity_id LIMIT 10",
            "QUERY_RELATIONSHIP_ENDPOINT_MISMATCH",
        ),
        (
            "MATCH (a:T0)-[r:r1]->(b:T1) RETURN a, r, b LIMIT 10",
            "QUERY_WHOLE_GRAPH_VALUE_RETURN",
        ),
        (
            "MATCH (a:T0)-[:r1]->(b:T1) "
            "RETURN a.entity_id LIMIT 101",
            "QUERY_LIMIT_OVER_BUDGET",
        ),
    ],
)
def test_unsafe_raw_gql_is_rejected(query: str, code: str) -> None:
    schema = _query_schema(3)
    plan = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q1",
        intent="raw bypass",
    )
    findings = validate_physical_query(query, plan, schema=schema)
    assert code in {finding.code for finding in findings}


def test_stale_k_and_hash_fail_closed() -> None:
    schema = _query_schema(3)
    plan = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q2",
        intent="stale",
    )
    stale = plan.model_copy(update={
        "query_authority_hash": "sha256:stale",
        "budget": plan.budget.model_copy(update={"max_hops": 2}),
    })
    codes = {
        finding.code for finding in validate_bounded_query_plan(stale, schema)
    }
    assert "PLAN_AUTHORITY_HASH_MISMATCH" in codes
    assert "PLAN_K_MISMATCH" in codes


def test_schema_mode_and_scalar_output_bypasses_fail_closed() -> None:
    schema = _query_schema(3)
    plan = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q1",
        intent="bypass",
    )
    compatibility_mode = plan.model_copy(update={
        "schema_mode": "schema1_compatibility",
    })
    output = plan.outputs[0].model_copy(update={
        "property_name": "display_name",
    })
    wrong_output = plan.model_copy(update={
        "outputs": [output, *plan.outputs[1:]],
    })

    assert "PLAN_SCHEMA_MODE_MISMATCH" in {
        finding.code
        for finding in validate_bounded_query_plan(
            compatibility_mode,
            schema,
        )
    }
    assert "PLAN_OUTPUTS_NOT_APPROVED" in {
        finding.code
        for finding in validate_bounded_query_plan(wrong_output, schema)
    }


class _GraphClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute_gql(self, gql: str) -> dict[str, object]:
        self.queries.append(gql)
        return {"rows": [{"n0_id": "entity:1"}]}

    def execute_query_all_pages(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, object]:
        self.queries.append(query)
        return {
            "status": {"code": "00000", "description": "success"},
            "result": {
                "kind": "TABLE",
                "data": [{
                    "n0_id": "entity:1",
                    "n1_id": "entity:2",
                    "r0_evidence_id": "evidence:1",
                }],
            },
        }


def test_schema2_agent_tool_disables_raw_gql_and_executes_approved_plan() -> None:
    client = _GraphClient()
    adapter = FabricDataAgentAdapter(
        _client=client,
        schema_mode="schema2_bounded",
        query_schema=_query_schema(3),
    )

    blocked = adapter.query_raw_gql("MATCH (n) RETURN n")
    result = adapter.execute_approved_plan(
        "cq:q1",
        intent="approved tool call",
    )

    assert blocked.status == "unsupported"
    assert result.status == "ok"
    assert result.gql == ""
    assert len(client.queries) == 1
    assert "LIMIT 20" in client.queries[0]
    assert result.execution_receipt["actual_hop_count"] == 1
    assert result.execution_receipt["route"] == "direct_graph"
    assert result.execution_receipt["status"] == "ok"
    assert result.execution_receipt["row_count"] == 1
    assert result.execution_receipt["semantic_plan_hash"]
    assert result.execution_receipt["query_authority_hash"]
    assert result.execution_receipt["query_schema_hash"]
    assert result.execution_receipt["domain_contract_hash"]
    serialized_receipt = json.dumps(result.execution_receipt)
    for forbidden in ("MATCH", "filter", "question", "intent", "source_content"):
        assert forbidden not in serialized_receipt


def test_schema1_agent_tool_keeps_explicit_compatibility_mode() -> None:
    client = _GraphClient()
    adapter = FabricDataAgentAdapter(
        _client=client,
        schema_mode="schema1_compatibility",
    )
    result = adapter.query_raw_gql(
        "MATCH (n) RETURN n.entity_id AS entity_id LIMIT 1"
    )
    assert result.status == "ok"
    assert result.gql


def test_schema1_reviewed_row_budget_remains_compatible() -> None:
    schema = PersistedQuerySchema(schema_mode="schema1_compatibility")
    plan = SemanticQueryPlan(
        schema_mode="schema1_compatibility",
        intent="legacy reviewed query",
        budget=ComplexityBudget(max_rows_per_subquery=200),
    )
    findings = validate_physical_query(
        "MATCH (n) RETURN n LIMIT 150",
        plan,
        schema=schema,
    )
    assert "QUERY_LIMIT_OVER_BUDGET" not in {
        finding.code for finding in findings
    }


def test_runtime_renders_structured_plan_and_reports_actual_hops() -> None:
    schema = _query_schema(3)
    plan = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q1",
        intent="runtime",
    )
    query = render_bounded_gql(plan, schema)
    case = CompetencyCase(
        id="cq:q1",
        question="Which approved entities answer question one?",
        semantic_plan=plan,
        expected=ExpectedOutcome(
            entity_types=["entity-type:t0", "entity-type:t1"],
            answer_concepts=["entity"],
        ),
        routes=RouteRequirements(
            direct_graph="required",
            search="optional",
            data_agent_mcp="optional",
            composed="optional",
        ),
        probes=RouteProbes(
            direct_graph=GraphProbe(
                query=query,
                semantic_plan=plan,
                entity_bindings=[
                    GraphEntityBinding(
                        column="n0_id",
                        semantic_id="entity-type:t0",
                    ),
                    GraphEntityBinding(
                        column="n1_id",
                        semantic_id="entity-type:t1",
                    ),
                ],
                relationship_bindings=[
                    GraphRelationshipBinding(
                        semantic_id="relationship-type:r1",
                        source_column="n0_id",
                        target_column="n1_id",
                        direction="source_to_target",
                        evidence_column="r0_evidence_id",
                    )
                ],
                canonical_id_columns=["n0_id", "n1_id"],
                lineage_columns=["r0_evidence_id"],
            )
        ),
    )
    client = _GraphClient()
    result = FabricGraphExecutor(
        workspace_id="workspace",
        graph_model_id="graph",
        client=client,
        query_schema=schema,
    ).execute(case)

    assert result["result_category"] == "success"
    assert result["actual_hop_count"] == 1
    assert result["query_authority_hash"] == schema.authority.authority_hash
    assert client.queries == [query]


def test_runtime_rejects_raw_gql_bypass_before_transport() -> None:
    schema = _query_schema(3)
    plan = compile_approved_query_plan(
        schema=schema,
        question_id="cq:q1",
        intent="runtime bypass",
    )
    case = CompetencyCase(
        id="cq:q1",
        question="Which approved entities answer question one?",
        semantic_plan=plan,
        expected=ExpectedOutcome(
            entity_types=["entity-type:t0", "entity-type:t1"],
            answer_concepts=["entity"],
        ),
        routes=RouteRequirements(
            direct_graph="required",
            search="optional",
            data_agent_mcp="optional",
            composed="optional",
        ),
        probes=RouteProbes(
            direct_graph=GraphProbe(
                query=(
                    "MATCH (a:T0)-[:r1*1..3]->(b:T1) "
                    "RETURN a.entity_id LIMIT 10"
                ),
                semantic_plan=plan,
            )
        ),
    )
    client = _GraphClient()
    result = FabricGraphExecutor(
        workspace_id="workspace",
        graph_model_id="graph",
        client=client,
        query_schema=schema,
    ).execute(case)

    assert result["result_category"] == "invalid_physical_query"
    assert result["error_message"] == "invalid_physical_query"
    assert "MATCH" not in result["error_message"]
    assert client.queries == []


def test_agent_instructions_expose_only_approved_k_and_plan_ids() -> None:
    schema = _query_schema(3)
    assert schema.authority is not None
    context = {
        "schema_mode": "schema2_bounded",
        "contract_name": "Bounded test",
        "contract_hash": "sha256:" + "a" * 64,
        "contract_description": "Approved bounded Graph domain.",
        "query_authority_hash": schema.authority.authority_hash,
        "approved_max_hops": schema.authority.approved_max_hops,
        "approved_query_paths": [
            path.model_dump(mode="json")
            for path in schema.authority.question_paths
        ],
    }
    instructions = build_contract_agent_instructions(context)
    assert "approved K=3" in instructions
    assert "`cq:q1`" in instructions
    assert "never increase K" in instructions


def test_competency_compilation_binds_authority_and_renders_query(
    tmp_path,
) -> None:
    schema = _query_schema(3)
    assert schema.authority is not None
    suite = tmp_path / "competency.json"
    suite.write_text(json.dumps({
        "schema_version": "1.0",
        "cases": [{
            "id": "cq:q1",
            "question": "Which approved entities answer question one?",
            "expected": {
                "entity_types": ["entity-type:t0", "entity-type:t1"],
                "relationship_types": [{
                    "semantic_id": "relationship-type:r1",
                    "requirement": "required",
                    "direction": "source_to_target",
                }],
                "answer_concepts": ["entity"],
            },
            "routes": {
                "direct_graph": "required",
                "search": "optional",
                "data_agent_mcp": "not_expected",
                "composed": "optional",
            },
            "probes": {"direct_graph": {}},
        }],
    }), encoding="utf-8")
    semantic_context = {
        "entity_types": [
            {"semantic_id": "entity-type:t0"},
            {"semantic_id": "entity-type:t1"},
        ],
        "relationship_types": [{
            "semantic_id": "relationship-type:r1",
            "direction": "source_to_target",
        }],
    }
    compiled = compile_competency_contract(
        suite,
        contract_hash="sha256:" + "a" * 64,
        semantic_context=semantic_context,
        query_schema=schema,
    )

    assert compiled.schema_mode == "schema2_bounded"
    assert compiled.query_authority_hash == schema.authority.authority_hash
    assert compiled.approved_max_hops == 3
    probe = compiled.cases[0].probes.direct_graph
    assert probe is not None
    assert probe.query == render_bounded_gql(
        compiled.cases[0].semantic_plan,
        schema,
    )
    assert probe.query_hash


def test_schema2_competency_rejects_free_form_data_agent_mcp(
    tmp_path,
) -> None:
    schema = _query_schema(3)
    suite = tmp_path / "competency.json"
    suite.write_text(json.dumps({
        "schema_version": "1.0",
        "cases": [{
            "id": "cq:q1",
            "question": "Which approved entities answer question one?",
            "expected": {
                "entity_types": ["entity-type:t0", "entity-type:t1"],
                "relationship_types": [],
                "answer_concepts": ["entity"],
            },
            "routes": {
                "direct_graph": "required",
                "search": "optional",
                "data_agent_mcp": "optional",
                "composed": "optional",
            },
            "probes": {
                "direct_graph": {},
                "data_agent_mcp": {},
            },
        }],
    }), encoding="utf-8")
    with pytest.raises(
        Exception,
        match="cannot expose data_agent_mcp",
    ):
        compile_competency_contract(
            suite,
            contract_hash="sha256:" + "a" * 64,
            semantic_context={
                "entity_types": [
                    {"semantic_id": "entity-type:t0"},
                    {"semantic_id": "entity-type:t1"},
                ],
                "relationship_types": [{
                    "semantic_id": "relationship-type:r1",
                    "direction": "source_to_target",
                }],
            },
            query_schema=schema,
        )


class _EmptyKnowledgeBase:
    def retrieve(self, question: str, top_k: int = 5):
        return []


class _SearchKnowledgeBase:
    def retrieve(self, question: str, top_k: int = 5):
        return [
            KBResult(
                chunk_id="chunk:1",
                source_id="search-index",
                text="Pump A is blue.",
                score=1.0,
            )
        ]


def test_authenticated_chat_model_accepts_only_sealed_plan_id_shape() -> None:
    request = ChatRequest(
        question="Run the approved graph question.",
        approved_plan_id="cq:q1",
    )
    assert request.approved_plan_id == "cq:q1"
    with pytest.raises(ValidationError):
        ChatRequest(
            question="Run arbitrary plan.",
            approved_plan_id="MATCH (n) RETURN n",
        )


def test_approved_plan_serving_executes_locally_and_graph_intent_abstains() -> None:
    client = _GraphClient()
    adapter = FabricDataAgentAdapter(
        _client=client,
        schema_mode="schema2_bounded",
        query_schema=_query_schema(3),
    )
    answer, route, citations, refused = _answer_question(
        question="Run approved relationship lookup.",
        kb=_EmptyKnowledgeBase(),
        graph=adapter,
        approved_plan_id="cq:q1",
    )
    assert route == "ontology"
    assert refused is False
    assert "Approved plan `cq:q1`" in answer
    assert client.queries
    assert citations

    client.queries.clear()
    answer, route, citations, refused = _answer_question(
        question="How are these entities connected?",
        kb=_EmptyKnowledgeBase(),
        graph=adapter,
    )
    assert route == "unsupported"
    assert refused is True
    assert "approved bounded Graph plan" in answer
    assert client.queries == []
    assert citations == []


@pytest.mark.parametrize(
    ("question", "expected_route", "expected_refused"),
    [
        ("What color is Pump A?", "search", False),
        ("Find documents about Pump A.", "search", False),
        ("What is the status of component A?", "search", False),
        ("Find documents about component A.", "search", False),
        ("Find documents about graph topology.", "search", False),
        ("What depends on Pump A?", "unsupported", True),
        (
            "Which components are related to Pump A and what document describes them?",
            "unsupported",
            True,
        ),
    ],
)
def test_schema2_routing_preserves_search_and_abstains_graph_intent(
    question: str,
    expected_route: str,
    expected_refused: bool,
) -> None:
    adapter = FabricDataAgentAdapter(
        _client=_GraphClient(),
        schema_mode="schema2_bounded",
        query_schema=_query_schema(3),
    )
    _answer, route, _citations, refused = _answer_question(
        question=question,
        kb=_SearchKnowledgeBase(),
        graph=adapter,
    )
    assert route == expected_route
    assert refused is expected_refused


def test_readiness_requires_configured_graph_only() -> None:
    assert not _readiness_passes(
        live_mode=True,
        kb_ready=True,
        visual_ready=True,
        graph_required=True,
        graph_ready=False,
    )
    assert _readiness_passes(
        live_mode=True,
        kb_ready=True,
        visual_ready=True,
        graph_required=False,
        graph_ready=False,
    )


def test_app_config_requires_explicit_query_mode(monkeypatch) -> None:
    monkeypatch.setenv("FABRIC_KG_ENVIRONMENT", "local")
    monkeypatch.setenv("FABRIC_KG_LOCAL_DEV", "true")
    monkeypatch.delenv("FABRIC_KG_QUERY_SCHEMA_MODE", raising=False)
    with pytest.raises(AppConfigError, match="explicitly set"):
        load_app_config()
    monkeypatch.setenv(
        "FABRIC_KG_QUERY_SCHEMA_MODE",
        "schema1_compatibility",
    )
    assert load_app_config().query_schema_mode == "schema1_compatibility"


def test_deploy_app_requires_explicit_query_mode() -> None:
    result = CliRunner().invoke(
        deploy_app_cmd,
        ["--dry-run"],
    )
    assert result.exit_code != 0
    assert "--query-schema-mode" in result.output


class _HeaderVerifier(InboundAuthVerifier):
    def verify(self, authorization_header: str | None):
        if authorization_header != "Bearer approved":
            raise AuthError("Authentication required.")
        return {"sub": "approved-user"}


def test_approved_plan_serving_path_is_authenticated() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    graph_client = _GraphClient()
    app = create_app(
        auth_verifier=_HeaderVerifier(),
        kb_tool=_EmptyKnowledgeBase(),
        graph_adapter=FabricDataAgentAdapter(
            _client=graph_client,
            schema_mode="schema2_bounded",
            query_schema=_query_schema(3),
        ),
        _allow_all_override=True,
    )
    client = TestClient(app)
    body = {
        "question": "Run approved graph plan.",
        "approved_plan_id": "cq:q1",
    }
    assert client.post("/chat", json=body).status_code == 401
    response = client.post(
        "/chat",
        json=body,
        headers={"Authorization": "Bearer approved"},
    )
    assert response.status_code == 200
    assert response.json()["route_type"] == "ontology"
    receipt = response.json()["execution_receipt"]
    assert receipt["actual_hop_count"] == 1
    assert receipt["query_authority_hash"]
    assert graph_client.queries


class _FailingGraphClient(_GraphClient):
    def execute_gql(self, gql: str) -> dict[str, object]:
        self.queries.append(gql)
        raise RuntimeError("remote details must not leak")


def test_failed_approved_plan_returns_sanitized_receipt() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = create_app(
        auth_verifier=_HeaderVerifier(),
        kb_tool=_EmptyKnowledgeBase(),
        graph_adapter=FabricDataAgentAdapter(
            _client=_FailingGraphClient(),
            schema_mode="schema2_bounded",
            query_schema=_query_schema(3),
        ),
        _allow_all_override=True,
    )
    response = TestClient(app).post(
        "/chat",
        json={
            "question": "Run approved graph plan.",
            "approved_plan_id": "cq:q1",
        },
        headers={"Authorization": "Bearer approved"},
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    receipt = detail["execution_receipt"]
    assert receipt["status"] == "error"
    assert receipt["actual_hop_count"] == 1
    assert receipt["query_authority_hash"]
    assert "remote details" not in json.dumps(detail)
    assert "MATCH" not in json.dumps(detail)

    stream = TestClient(app).post(
        "/stream",
        json={
            "question": "Run approved graph plan.",
            "approved_plan_id": "cq:q1",
        },
        headers={"Authorization": "Bearer approved"},
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert '"type":"error"' in stream.text
    assert '"actual_hop_count":1' in stream.text
    assert "remote details" not in stream.text
    assert "MATCH" not in stream.text


def test_schema2_runtime_config_omits_data_agent_requirements() -> None:
    deployment = DeploymentRuntimeConfig(
        schema_mode="schema2_bounded",
        artifact_validation_status="passed",
        data_agent_published=False,
        compiled_instruction_hash="",
        deployed_instruction_hash="",
    )
    config = RuntimeConfig(
        environment="test",
        contract_hash="sha256:" + "a" * 64,
        deployment=deployment,
        graph=GraphRuntimeConfig(
            workspace_id="workspace",
            graph_model_id="graph",
        ),
        search=SearchRuntimeConfig(
            endpoint="https://search.example.com",
            index_name="index",
        ),
    )
    assert config.data_agent_mcp is None
    with pytest.raises(Exception, match="cannot configure data_agent_mcp"):
        RuntimeConfig(
            environment="test",
            contract_hash="sha256:" + "a" * 64,
            deployment=deployment,
            graph=config.graph,
            search=config.search,
            data_agent_mcp=McpRuntimeConfig(
                endpoint="https://api.fabric.microsoft.com/mcp",
                workspace_id="workspace",
                data_agent_id="agent",
            ),
        )


def test_schema2_acceptance_does_not_require_data_agent_or_instructions() -> None:
    digest = "sha256:" + "a" * 64
    evidence = {
        "contract_hash": digest,
        "environment": "test",
        "deployment": {
            "schema_mode": "schema2_bounded",
            "artifact_validation_status": "passed",
            "receipt_sha256": digest,
            "semantic_contract_hash": digest,
            "semantic_artifact_set_hash": digest,
            "graph_artifact_set_hash": digest,
            "search_artifact_set_hash": digest,
            "semantic_model_manifest_hash": digest,
            "ontology_persisted_projection_hash": digest,
            "graph_persisted_projection_hash": digest,
            "persisted_query_schema_hash": digest,
            "competency_contract_hash": digest,
            "package_hash": digest,
            "contract_hash_consistent": True,
            "graph_model_id": "graph",
            "search_index_name": "index",
            "data_agent_published": False,
            "compiled_instruction_hash": "",
            "deployed_instruction_hash": "",
        },
        "runtime_targets": {
            "graph_model_id": "graph",
            "search_mode": "direct_search",
            "search_index_name": "index",
            "data_agent_id": None,
        },
        "cases": [],
    }
    validation = validate_deployment_evidence(
        evidence,
        require_spec008a_diagnostic=False,
    )
    assert validation["status"] == "passed", validation
    assert validation["checks"]["data_agent_published"] is True
    assert validation["checks"]["instruction_hash_matches"] is True
