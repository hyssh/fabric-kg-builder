"""Versioned question execution intentions, never SQL or physical bindings."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_serializer, model_validator

from fabric_kg_builder.contracts.base import (
    ContractModel, RequiredText, Sha256, canonical_sha256,
)

SQL_ROUTING_UNRESOLVED = "lakehouse_sql: physical source bindings unresolved; no SQL executed or answer verified"
QUESTION_ROUTING_PROMPT = """
Collect execution intent for each question. Honor explicit intake routing exactly.
For unclassified questions, propose routing from business meaning and background,
not a keyword-only rule or the presence of digits. Population counts, aggregates
and time-series analysis normally belong to lakehouse_sql; factual, identifier
and relational/conceptual lookup may belong to ontology_graph. A SKU containing
digits or a numeric safety threshold is not automatically a SQL question.
Routing operation is lookup, count, aggregate or trend.
Mixed requests must retain BOTH graph scope and analytical needs in population,
filters, source requirements and rationale. If their coordination or meaning is
uncertain, explicitly report review-needed intent in the available review concerns
or unresolved requirements. Never silently answer or preserve only one part.
Record rationale, population, grain, filter/time requirements and source
requirements. Preserve outstanding interpretation or execution review notes in
per-question pending_requirements (or unresolved_answer_requirements in a design).
These notes remain unresolved until reviewed; ontology compilation is not SQL
readiness and must not discard them. Never hide them in question text or rationale.
All physical bindings remain unresolved: no invented SQL, table IDs,
column bindings, execution results or analytics engine. Preserve original question
wording and business criticality. Routing context is an execution plan, not an answer.
SQL intent does not authorize graph execution: unavailable SQL bindings/backend
must remain blocked, never silently fall back to a graph-only partial answer.
For SQL-directed questions, graph endpoints may remain null, answer-property keys
may be empty, and ontology completeness is not required. Do not create Quantity,
Unit or other ontology definitions merely to make analytical questions graph-
answerable. Keep numeric facts, raw source evidence, and legitimate numeric
identity/ordinal/domain properties where relevant; this is not a numeric-data ban.
If every question is SQL-directed and no ontology is needed, return empty graph
definition lists rather than fabricating entities, edges or completeness checks.
"""


class QuestionRouting(ContractModel):
    version: Literal["1.0.0"] = "1.0.0"
    backend: Literal["ontology_graph", "lakehouse_sql"]
    operation: Literal["lookup", "count", "aggregate", "trend"]
    rationale: RequiredText
    population: RequiredText | None = None
    grain: RequiredText | None = None
    filters: list[RequiredText] = Field(default_factory=list)
    time_requirements: list[RequiredText] = Field(default_factory=list)
    source_requirements: list[RequiredText] = Field(default_factory=list)
    physical_binding_state: Literal["unresolved"] = "unresolved"


class RoutedQuestionContext(ContractModel):
    question_id: str = Field(pattern=r"^cq:[a-z0-9][a-z0-9._:-]*$")
    question: RequiredText
    business_critical: bool
    routing: QuestionRouting
    pending_requirements: list[RequiredText] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        values = handler(self)
        if not self.pending_requirements:
            values.pop("pending_requirements", None)
        return values


class QuestionRoutingContext(ContractModel):
    context_version: Literal["1.0.0", "1.1.0"] = "1.0.0"
    questions: list[RoutedQuestionContext]
    context_hash: Sha256

    @model_validator(mode="after")
    def _binding(self) -> "QuestionRoutingContext":
        ids = [item.question_id for item in self.questions]
        if ids != sorted(set(ids)):
            raise ValueError("routing context question IDs must be unique and sorted")
        if self.context_version == "1.0.0" and any(item.pending_requirements for item in self.questions):
            raise ValueError("pending routing requirements require context version 1.1.0")
        if canonical_sha256(self.model_dump(mode="json", exclude={"context_hash"})) != self.context_hash:
            raise ValueError("question routing context hash mismatch")
        return self

    def validate_against(self, contract_or_intake: Any) -> None:
        if self.model_dump(mode="json") != question_routing_context(contract_or_intake):
            raise ValueError("Routing context differs from its question authority (unknown ID or changed routing)")


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def effective_question_routing(question: Any, proposed: QuestionRouting | None = None) -> QuestionRouting | None:
    explicit = _field(question, "routing")
    if explicit is not None:
        explicit = QuestionRouting.model_validate(
            explicit.model_dump(mode="python") if isinstance(explicit, QuestionRouting) else explicit
        )
        if proposed is not None and proposed != explicit:
            raise ValueError(f"Proposed routing conflicts with explicit intake routing for {_field(question, 'id')}")
        return explicit
    if proposed is not None:
        return QuestionRouting.model_validate(proposed.model_dump(mode="python"))
    return None


def is_sql_question(question: Any, proposed: QuestionRouting | None = None) -> bool:
    routing = effective_question_routing(question, proposed)
    return routing is not None and routing.backend == "lakehouse_sql"


def routed_question_copies(questions: Any, routes: Any) -> list[Any]:
    known = {item.id: item for item in questions}
    proposed = {}
    proposed_pending = {}
    for route in routes:
        if route.question_id not in known or route.question_id in proposed:
            raise ValueError("Routing refers to unknown or duplicate question ID")
        proposed[route.question_id] = getattr(route, "routing", None)
        proposed_pending[route.question_id] = [
            *getattr(route, "pending_requirements", []),
            *getattr(route, "unresolved_answer_requirements", []),
        ]
    result = []
    for question in questions:
        routing = effective_question_routing(question, proposed.get(question.id))
        pending = list(dict.fromkeys([
            *getattr(question, "pending_requirements", []),
            *proposed_pending.get(question.id, []),
        ]))
        updates = {}
        if routing is not None:
            updates["routing"] = routing
        if pending:
            updates["pending_requirements"] = pending
        result.append(question if not updates else question.model_copy(update=updates))
    return result


def question_routing_context(contract_or_intake: Any) -> dict[str, Any] | None:
    """Return only explicitly routed question context; legacy inputs return None."""
    entries = []
    seen = set()
    for question in _field(contract_or_intake, "competency_questions", ()):
        if isinstance(question, str):
            continue
        question_id = _field(question, "id")
        if question_id in seen:
            raise ValueError("Duplicate question ID in routing authority")
        seen.add(question_id)
        routing = effective_question_routing(question)
        if routing is not None:
            entries.append(RoutedQuestionContext(
                question_id=question_id, question=_field(question, "question"),
                business_critical=_field(question, "business_critical", True), routing=routing,
                pending_requirements=_field(question, "pending_requirements", []),
            ))
    if not entries:
        return None
    values = {
        "context_version": "1.1.0" if any(item.pending_requirements for item in entries) else "1.0.0",
        "questions": sorted(entries, key=lambda item: item.question_id),
    }
    context = QuestionRoutingContext(**values, context_hash=canonical_sha256(values))
    return context.model_dump(mode="json")
