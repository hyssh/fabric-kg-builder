"""Route-aware competency contracts compiled against semantic authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fabric_kg_builder.semantic.query_validation import (
    compute_physical_query_hash,
    resolve_query_plan,
    validate_physical_query,
)
from fabric_kg_builder.semantic.query_rendering import (
    compile_approved_query_plan,
    render_bounded_gql,
    validate_bounded_query_plan,
)
from fabric_kg_builder.semantic.schemas import (
    PersistedQuerySchema,
    SemanticQueryPlan,
    compute_query_plan_hash,
    compute_persisted_query_schema_hash,
)


class CompetencyContractError(ValueError):
    """Raised when a competency contract is invalid or semantically unsafe."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


RouteRequirement = Literal["required", "optional", "not_expected"]


class ExpectedRelationship(_StrictModel):
    """One relationship expected from the direct Graph route."""

    semantic_id: str = Field(min_length=1)
    requirement: RouteRequirement = "required"
    direction: str | None = None


class ExpectedOutcome(_StrictModel):
    """Semantic and evidence outcomes for one competency question."""

    entity_types: list[str] = Field(default_factory=list)
    relationship_types: list[ExpectedRelationship] = Field(default_factory=list)
    answer_concepts: list[str] = Field(default_factory=list)
    evidence_required: bool = True
    temporal_required: bool = False


class RouteRequirements(_StrictModel):
    """Required, optional, and unavailable routes for one competency case."""

    direct_graph: RouteRequirement = "required"
    search: RouteRequirement = "required"
    knowledge_base: RouteRequirement = "optional"
    data_agent_ui: RouteRequirement = "optional"
    data_agent_mcp: RouteRequirement = "required"
    foundry_agent: RouteRequirement = "not_expected"
    composed: RouteRequirement = "required"


class GraphEntityBinding(_StrictModel):
    """Map one GQL result column to a semantic entity type."""

    column: str = Field(min_length=1)
    semantic_id: str = Field(min_length=1)


class GraphRelationshipBinding(_StrictModel):
    """Map GQL endpoint columns to one directed semantic relationship."""

    semantic_id: str = Field(min_length=1)
    source_column: str = Field(min_length=1)
    target_column: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    evidence_column: str | None = None


class GraphProbe(_StrictModel):
    """Executable direct Graph query and deterministic result mappings."""

    query: str = Field(min_length=1)
    entity_bindings: list[GraphEntityBinding] = Field(default_factory=list)
    relationship_bindings: list[GraphRelationshipBinding] = Field(
        default_factory=list
    )
    canonical_id_columns: list[str] = Field(default_factory=list)
    lineage_columns: list[str] = Field(default_factory=list)
    semantic_plan: SemanticQueryPlan | None = None
    relationship_labels: dict[str, str] = Field(default_factory=dict)
    type_labels: dict[str, str] = Field(default_factory=dict)
    query_hash: str = ""
    static_validation_passed: bool = False


class SearchProbe(_StrictModel):
    """Executable hybrid Search query and result field mappings."""

    query: str | None = None
    top: int = Field(default=10, ge=1, le=100)
    select_fields: list[str] = Field(default_factory=list)
    vector_fields: list[str] = Field(default_factory=list)
    semantic_configuration: str | None = None
    canonical_id_fields: list[str] = Field(
        default_factory=lambda: ["canonical_id", "linked_entity_ids"]
    )
    citation_id_field: str = "chunk_id"
    asset_version_id_field: str = "asset_version_id"
    source_file_id_field: str = "source_file_id"
    blob_url_field: str = "blob_url"
    source_locator_field: str = "source_locator_json"
    evidence_id_field: str = "evidence_id"


class McpProbe(_StrictModel):
    """MCP tool-selection and invocation mapping."""

    tool_name: str | None = None
    question_argument: str = "question"
    static_arguments: dict[str, Any] = Field(default_factory=dict)


class RouteProbes(_StrictModel):
    """Physical probes for one route-aware competency case."""

    direct_graph: GraphProbe | None = None
    search: SearchProbe | None = None
    data_agent_mcp: McpProbe | None = None


class CompetencyCase(_StrictModel):
    """One executable, semantically typed competency question."""

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    semantic_plan: SemanticQueryPlan | None = None
    expected: ExpectedOutcome
    routes: RouteRequirements = Field(default_factory=RouteRequirements)
    probes: RouteProbes

    @model_validator(mode="after")
    def _required_routes_have_probes(self) -> "CompetencyCase":
        for route in ("direct_graph", "search", "data_agent_mcp"):
            if getattr(self.routes, route) == "required" and getattr(
                self.probes, route
            ) is None:
                raise ValueError(
                    f"Required route '{route}' has no executable probe."
                )
        for route in ("data_agent_ui", "foundry_agent"):
            if getattr(self.routes, route) == "required":
                raise ValueError(
                    f"Required route '{route}' is not supported by the "
                    "current runtime collector. Use optional/not_expected "
                    "or add an executable route implementation."
                )
        if (
            self.routes.knowledge_base == "required"
            and self.routes.search != "required"
        ):
            raise ValueError(
                "Required knowledge_base validation also requires the search "
                "probe; configure runtime search mode as knowledge_base."
            )
        if (
            self.routes.data_agent_mcp == "required"
            and not self.expected.answer_concepts
        ):
            raise ValueError(
                "Required Data Agent MCP routes need expected.answer_concepts "
                "for deterministic answer-relevance scoring."
            )
        graph_probe = self.probes.direct_graph
        if (
            self.semantic_plan is not None
            and graph_probe is not None
            and graph_probe.semantic_plan is not None
            and self.semantic_plan != graph_probe.semantic_plan
        ):
            raise ValueError(
                "Case semantic_plan and direct Graph semantic_plan differ."
            )
        return self


class CompetencyContract(_StrictModel):
    """Compiled route-aware competency suite for one semantic contract."""

    schema_version: Literal["1.0"] = "1.0"
    schema_mode: Literal["schema1_compatibility", "schema2_bounded"] = (
        "schema1_compatibility"
    )
    contract_hash: str = Field(min_length=1)
    domain_contract_hash: str = ""
    query_authority_hash: str = ""
    approved_max_hops: int | None = Field(default=None, ge=1, le=4)
    query_schema: PersistedQuerySchema | None = None
    cases: list[CompetencyCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_case_ids(self) -> "CompetencyContract":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Competency case IDs must be unique.")
        if self.schema_mode == "schema2_bounded":
            if self.query_schema is None or self.query_schema.authority is None:
                raise ValueError(
                    "Schema-2 competency contracts require bounded query authority."
                )
            authority = self.query_schema.authority
            if (
                self.domain_contract_hash != authority.domain_contract_hash
                or self.query_authority_hash != authority.authority_hash
                or self.approved_max_hops != authority.approved_max_hops
            ):
                raise ValueError(
                    "Schema-2 competency authority fields differ from the "
                    "persisted query schema."
                )
        return self


def _load_payload(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        if source.suffix.lower() == ".json":
            payload = json.loads(source.read_text(encoding="utf-8"))
        else:
            payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise CompetencyContractError(
            f"Could not load competency contract {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise CompetencyContractError(
            "Competency contract must be a YAML or JSON object."
        )
    return payload


def load_competency_contract(path: Path | str) -> CompetencyContract:
    """Load a previously compiled competency contract."""
    try:
        contract = CompetencyContract.model_validate(_load_payload(path))
        _validate_compiled_query_contract(contract)
        return contract
    except ValueError as exc:
        raise CompetencyContractError(str(exc)) from exc


def _prepare_query_contract_payload(
    payload: dict[str, Any],
    *,
    query_schema: PersistedQuerySchema | None,
) -> dict[str, Any]:
    prepared = json.loads(json.dumps(payload))
    embedded_schema = prepared.get("query_schema")
    if query_schema is not None:
        if embedded_schema is not None:
            parsed_embedded = PersistedQuerySchema.model_validate(
                embedded_schema
            )
            if parsed_embedded.schema_hash != query_schema.schema_hash:
                raise CompetencyContractError(
                    "Competency query_schema differs from the current "
                    "persisted query schema."
                )
        prepared["query_schema"] = query_schema.model_dump(mode="json")
    active_schema = query_schema
    if active_schema is None and embedded_schema is not None:
        active_schema = PersistedQuerySchema.model_validate(embedded_schema)
    if active_schema is None:
        return prepared

    if active_schema.schema_mode == "schema2_bounded":
        authority = active_schema.authority
        if authority is None:
            raise CompetencyContractError(
                "Schema-2 query schema is missing bounded authority."
            )
        prepared["schema_mode"] = "schema2_bounded"
        prepared["domain_contract_hash"] = authority.domain_contract_hash
        prepared["query_authority_hash"] = authority.authority_hash
        prepared["approved_max_hops"] = authority.approved_max_hops

    for case_payload in prepared.get("cases", []):
        if not isinstance(case_payload, dict):
            continue
        probes = case_payload.get("probes")
        graph_probe = (
            probes.get("direct_graph")
            if isinstance(probes, dict)
            else None
        )
        graph_plan = (
            graph_probe.get("semantic_plan")
            if isinstance(graph_probe, dict)
            else None
        )
        case_plan = case_payload.get("semantic_plan")
        if case_plan is not None and graph_plan is not None:
            if case_plan != graph_plan:
                raise CompetencyContractError(
                    f"{case_payload.get('id')}: case and Graph semantic "
                    "plans differ."
                )
        raw_plan = case_plan or graph_plan
        if active_schema.schema_mode == "schema2_bounded":
            case_id = str(case_payload.get("id") or "")
            try:
                plan = compile_approved_query_plan(
                    schema=active_schema,
                    question_id=case_id,
                    intent=str(
                        (
                            raw_plan.get("intent")
                            if isinstance(raw_plan, dict)
                            else None
                        )
                        or case_payload.get("question")
                        or case_id
                    ),
                    result_limit=int(
                        (
                            raw_plan.get("result_limit")
                            if isinstance(raw_plan, dict)
                            else None
                        )
                        or 100
                    ),
                )
            except ValueError as exc:
                raise CompetencyContractError(
                    f"{case_id}: {exc}"
                ) from exc
            if isinstance(raw_plan, dict) and raw_plan.get("path_steps"):
                authored_steps = [
                    (
                        str(step.get("from_type_id") or ""),
                        str(step.get("via_relationship_id") or ""),
                        str(step.get("to_type_id") or ""),
                        str(step.get("direction") or "source_to_target"),
                    )
                    for step in raw_plan["path_steps"]
                    if isinstance(step, dict)
                ]
                approved_steps = [
                    (
                        step.from_type_id,
                        step.via_relationship_id,
                        step.to_type_id,
                        step.direction,
                    )
                    for step in plan.path_steps
                ]
                if authored_steps != approved_steps:
                    raise CompetencyContractError(
                        f"{case_id}: authored semantic path differs from the "
                        "approved DomainContractV2 question plan."
                    )
            serialized_plan = plan.model_dump(mode="json")
            case_payload["semantic_plan"] = serialized_plan
            if not isinstance(graph_probe, dict):
                raise CompetencyContractError(
                    f"{case_id}: schema-2 Graph route requires direct_graph probe "
                    "bindings."
                )
            rendered_query = render_bounded_gql(plan, active_schema)
            authored_query = str(graph_probe.get("query") or "").strip()
            if authored_query:
                authored_findings = validate_physical_query(
                    authored_query,
                    plan,
                    schema=active_schema,
                )
                if authored_findings:
                    raise CompetencyContractError(
                        f"{case_id}: authored Graph query is not equivalent to "
                        "the approved structured plan: "
                        + "; ".join(
                            f"{finding.code}: {finding.message}"
                            for finding in authored_findings
                        )
                    )
            graph_probe["query"] = rendered_query
            graph_probe["semantic_plan"] = serialized_plan
            node_id_columns = [
                output.alias
                for output in plan.outputs
                if output.owner_kind == "node"
                and output.purpose == "id"
            ]
            graph_probe["entity_bindings"] = [
                {
                    "column": output.alias,
                    "semantic_id": output.semantic_id,
                }
                for output in plan.outputs
                if output.owner_kind == "node"
                and output.purpose == "id"
            ]
            relationships_by_id = {
                relationship.semantic_id: relationship
                for relationship in active_schema.relationships
            }
            graph_probe["relationship_bindings"] = [
                {
                    "semantic_id": step.via_relationship_id,
                    "source_column": (
                        f"n{index}_id"
                        if step.direction == "source_to_target"
                        else f"n{index + 1}_id"
                    ),
                    "target_column": (
                        f"n{index + 1}_id"
                        if step.direction == "source_to_target"
                        else f"n{index}_id"
                    ),
                    "direction": relationships_by_id[
                        step.via_relationship_id
                    ].direction,
                    "evidence_column": f"r{index}_evidence_id",
                }
                for index, step in enumerate(plan.path_steps)
            ]
            graph_probe["canonical_id_columns"] = node_id_columns
            graph_probe["lineage_columns"] = [
                f"r{index}_evidence_id"
                for index, _step in enumerate(plan.path_steps)
            ]
            graph_probe["relationship_labels"] = {
                step.via_relationship_id: step.relationship_graph_label
                for step in plan.path_steps
            }
            graph_probe["type_labels"] = {
                step.from_type_id: step.from_graph_label
                for step in plan.path_steps
            } | {
                step.to_type_id: step.to_graph_label
                for step in plan.path_steps
            }
            continue
        if raw_plan is None:
            raise CompetencyContractError(
                f"{case_payload.get('id')}: a semantic_plan is required "
                "when compiling against a persisted query schema."
            )
        plan_payload = dict(raw_plan)
        declared_manifest_hash = str(
            plan_payload.get("manifest_hash") or ""
        )
        if (
            declared_manifest_hash
            and declared_manifest_hash != active_schema.manifest_hash
        ):
            raise CompetencyContractError(
                f"{case_payload.get('id')}: semantic plan manifest hash "
                "does not match the persisted query schema."
            )
        declared_plan_hash = str(plan_payload.get("plan_hash") or "")
        plan_payload["manifest_hash"] = active_schema.manifest_hash
        plan_payload["plan_hash"] = ""
        plan = SemanticQueryPlan.model_validate(plan_payload)
        computed_plan_hash = compute_query_plan_hash(plan)
        if declared_plan_hash and declared_plan_hash != computed_plan_hash:
            raise CompetencyContractError(
                f"{case_payload.get('id')}: semantic plan hash does not "
                "match the plan compiled against the current query schema."
            )
        plan = SemanticQueryPlan.model_validate({
            **plan.model_dump(mode="json"),
            "plan_hash": computed_plan_hash,
        })
        serialized_plan = plan.model_dump(mode="json")
        case_payload["semantic_plan"] = serialized_plan
        if isinstance(graph_probe, dict):
            graph_probe["semantic_plan"] = serialized_plan
    return prepared


def _validate_compiled_query_contract(
    contract: CompetencyContract,
) -> None:
    schema = contract.query_schema
    if schema is None:
        return
    expected_schema_hash = compute_persisted_query_schema_hash(schema)
    if schema.schema_hash != expected_schema_hash:
        raise CompetencyContractError(
            "Competency query_schema hash does not match its contents."
        )
    errors: list[str] = []
    for case in contract.cases:
        plan = case.semantic_plan
        if plan is None:
            errors.append(
                f"{case.id}: semantic_plan is required by query_schema."
            )
            continue
        expected_plan_hash = compute_query_plan_hash(plan)
        if plan.plan_hash != expected_plan_hash:
            errors.append(
                f"{case.id}: semantic plan hash does not match its contents."
            )
        errors.extend(
            f"{case.id}: {finding.code}: {finding.message}"
            for finding in resolve_query_plan(plan, schema)
        )
        graph_probe = case.probes.direct_graph
        if graph_probe is None:
            continue
        physical_findings = validate_physical_query(
            graph_probe.query,
            plan,
            schema=schema,
        )
        if schema.schema_mode == "schema2_bounded":
            bounded_findings = validate_bounded_query_plan(plan, schema)
            errors.extend(
                f"{case.id}: {finding.code}: {finding.message}"
                for finding in bounded_findings
            )
            expected_query = render_bounded_gql(plan, schema)
            if graph_probe.query != expected_query:
                errors.append(
                    f"{case.id}: Graph query differs from deterministic rendering."
                )
        errors.extend(
            f"{case.id}: {finding.code}: {finding.message}"
            for finding in physical_findings
        )
        expected_query_hash = compute_physical_query_hash(
            graph_probe.query
        )
        if graph_probe.query_hash != expected_query_hash:
            errors.append(
                f"{case.id}: Graph query hash does not match query text."
            )
        if not graph_probe.static_validation_passed:
            errors.append(
                f"{case.id}: Graph static validation receipt is not passed."
            )
    if errors:
        raise CompetencyContractError("; ".join(errors))


def compile_competency_contract(
    path: Path | str,
    *,
    contract_hash: str,
    semantic_context: dict[str, Any],
    query_schema: PersistedQuerySchema | None = None,
) -> CompetencyContract:
    """Validate route expectations against exact semantic IDs and direction."""
    payload = _load_payload(path)
    declared_hash = payload.get("contract_hash")
    if declared_hash and declared_hash != contract_hash:
        raise CompetencyContractError(
            "Competency contract hash does not match the semantic contract."
        )
    payload["contract_hash"] = contract_hash
    payload = _prepare_query_contract_payload(
        payload,
        query_schema=query_schema,
    )
    try:
        contract = CompetencyContract.model_validate(payload)
    except ValueError as exc:
        raise CompetencyContractError(str(exc)) from exc

    entity_ids = {
        str(item.get("semantic_id"))
        for item in semantic_context.get("entity_types", [])
        if isinstance(item, dict) and item.get("semantic_id")
    }
    relationships = {
        str(item.get("semantic_id")): item
        for item in semantic_context.get("relationship_types", [])
        if isinstance(item, dict) and item.get("semantic_id")
    }
    errors: list[str] = []
    for case in contract.cases:
        unknown_entities = sorted(set(case.expected.entity_types) - entity_ids)
        if unknown_entities:
            errors.append(
                f"{case.id}: unknown expected entity IDs {unknown_entities}"
            )
        for expected in case.expected.relationship_types:
            authoritative = relationships.get(expected.semantic_id)
            if authoritative is None:
                errors.append(
                    f"{case.id}: unknown relationship ID {expected.semantic_id}"
                )
                continue
            if (
                expected.direction
                and expected.direction != authoritative.get("direction")
            ):
                errors.append(
                    f"{case.id}: direction for {expected.semantic_id} is "
                    f"{expected.direction!r}; semantic contract requires "
                    f"{authoritative.get('direction')!r}"
                )
        graph_probe = case.probes.direct_graph
        if graph_probe:
            for binding in graph_probe.entity_bindings:
                if binding.semantic_id not in entity_ids:
                    errors.append(
                        f"{case.id}: Graph binding uses unknown entity ID "
                        f"{binding.semantic_id}"
                    )
            for binding in graph_probe.relationship_bindings:
                authoritative = relationships.get(binding.semantic_id)
                if authoritative is None:
                    errors.append(
                        f"{case.id}: Graph binding uses unknown relationship ID "
                        f"{binding.semantic_id}"
                    )
                elif binding.direction != authoritative.get("direction"):
                    errors.append(
                        f"{case.id}: Graph binding direction for "
                        f"{binding.semantic_id} differs from semantic authority"
                    )
    if errors:
        raise CompetencyContractError("; ".join(errors))
    if contract.query_schema is not None:
        updated_cases: list[CompetencyCase] = []
        for case in contract.cases:
            graph_probe = case.probes.direct_graph
            if graph_probe is None:
                updated_cases.append(case)
                continue
            updated_graph_probe = graph_probe.model_copy(update={
                "query_hash": compute_physical_query_hash(
                    graph_probe.query
                ),
                "static_validation_passed": True,
            })
            updated_cases.append(case.model_copy(update={
                "probes": case.probes.model_copy(update={
                    "direct_graph": updated_graph_probe,
                }),
            }))
        contract = contract.model_copy(update={"cases": updated_cases})
        _validate_compiled_query_contract(contract)
    return contract


def write_competency_contract(
    contract: CompetencyContract,
    path: Path | str,
) -> Path:
    """Write stable normalized JSON for deployment and runtime evaluation."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            contract.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return target
