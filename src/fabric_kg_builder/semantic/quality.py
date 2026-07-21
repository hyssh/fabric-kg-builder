"""Privacy-safe quality reporting for SPEC-008A extraction and enrichment."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from fabric_kg_builder.enrichment.output_schema import LLMOutput

from .models import StrictModel

if TYPE_CHECKING:
    from .enrichment import SemanticEnrichmentContext


class SemanticTypeQuality(StrictModel):
    """Aggregate status counts for one canonical semantic type."""

    semantic_id: str
    candidates: int = 0
    accepted: int = 0
    discovery: int = 0
    rejected: int = 0
    unresolved: int = 0
    evidence_backed: int = 0


class EnrichmentQualityReport(StrictModel):
    """Redacted aggregate quality evidence for one enrichment scope."""

    schema_version: Literal["1.0"] = "1.0"
    semantic_contract_hash: str | None = None
    status: Literal["passed", "failed"]
    entity_counts: dict[str, int]
    property_counts: dict[str, int]
    relationship_counts: dict[str, int]
    property_evidence_coverage: float = Field(ge=0.0, le=1.0)
    relationship_evidence_coverage: float = Field(ge=0.0, le=1.0)
    relationship_endpoint_resolution: float = Field(ge=0.0, le=1.0)
    merge_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    per_type: list[SemanticTypeQuality]
    duplicate_description_findings: list[str]
    placeholder_description_findings: list[str]
    unsupported_type_findings: list[str]
    unsupported_property_findings: list[str]
    unsupported_predicate_findings: list[str]
    deterministic_output_hash: str
    contains_source_content: Literal[False] = False


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _status_counts(statuses: list[str]) -> dict[str, int]:
    counts = Counter(statuses)
    return {
        "candidates": len(statuses),
        "accepted": counts["accepted"],
        "discovery": counts["discovery"],
        "rejected": counts["rejected"],
        "unresolved": counts["unresolved"],
    }


def _coverage(
    statuses: list[str],
    evidence_lists: list[list[str]],
) -> float:
    accepted_indexes = [
        index for index, status in enumerate(statuses) if status == "accepted"
    ]
    if not accepted_indexes:
        return 1.0
    backed = sum(bool(evidence_lists[index]) for index in accepted_indexes)
    return backed / len(accepted_indexes)


def _description_findings(
    context: SemanticEnrichmentContext | None,
) -> tuple[list[str], list[str]]:
    if context is None:
        return [], []
    descriptions: dict[str, list[str]] = defaultdict(list)
    placeholders: list[str] = []
    entries = [
        (
            definition.id,
            definition.name,
            definition.description,
        )
        for definition in context.entity_definitions.values()
    ]
    entries.extend(
        (
            definition.id,
            definition.business_name,
            definition.description,
        )
        for definition in context.relationship_definitions.values()
    )
    seen_ids: set[str] = set()
    for semantic_id, name, description in entries:
        if semantic_id in seen_ids:
            continue
        seen_ids.add(semantic_id)
        normalized = " ".join(description.split()).casefold()
        descriptions[normalized].append(semantic_id)
        name_normalized = " ".join(name.split()).casefold().rstrip(".")
        if (
            not normalized
            or normalized.rstrip(".") == name_normalized
            or normalized in {
                f"definition for {name_normalized}.",
                f"description of {name_normalized}.",
            }
        ):
            placeholders.append(semantic_id)
    duplicates = sorted(
        ",".join(sorted(ids))
        for description, ids in descriptions.items()
        if description and len(ids) > 1
    )
    return duplicates, sorted(placeholders)


def build_enrichment_quality_report(
    outputs: list[LLMOutput],
    context: SemanticEnrichmentContext | None,
    *,
    merge_count: int = 0,
) -> EnrichmentQualityReport:
    """Build deterministic aggregate metrics without retaining source content."""
    entities = [entity for output in outputs for entity in output.entities]
    properties = [
        observation
        for output in outputs
        for observation in output.property_observations
    ]
    relationships = [
        relationship
        for output in outputs
        for relationship in output.relationships
    ]

    entity_statuses = [
        "accepted"
        if entity.semantic_lane == "authoritative"
        and entity.review_status == "approved"
        else "discovery"
        for entity in entities
    ]
    property_statuses = [
        observation.processing_status or "unresolved"
        for observation in properties
    ]
    relationship_statuses = [
        relationship.processing_status or "unresolved"
        for relationship in relationships
    ]
    property_evidence = [
        observation.evidence_id_hints for observation in properties
    ]
    relationship_evidence = [
        relationship.evidence_id_hints for relationship in relationships
    ]

    accepted_relationships = [
        relationship
        for relationship in relationships
        if relationship.processing_status == "accepted"
    ]
    endpoint_resolution = (
        sum(
            bool(
                relationship.source_semantic_type_id
                and relationship.target_semantic_type_id
            )
            for relationship in accepted_relationships
        )
        / len(accepted_relationships)
        if accepted_relationships
        else 1.0
    )

    per_type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for observation in properties:
        semantic_id = (
            observation.semantic_owner_type_id
            or observation.semantic_property_id
            or "discovery:property"
        )
        status = observation.processing_status or "unresolved"
        per_type_counts[semantic_id][status] += 1
        per_type_counts[semantic_id]["candidates"] += 1
        if observation.evidence_id_hints:
            per_type_counts[semantic_id]["evidence_backed"] += 1
    for relationship in relationships:
        semantic_id = (
            relationship.semantic_relationship_id
            or "discovery:relationship"
        )
        status = relationship.processing_status or "unresolved"
        per_type_counts[semantic_id][status] += 1
        per_type_counts[semantic_id]["candidates"] += 1
        if relationship.evidence_id_hints:
            per_type_counts[semantic_id]["evidence_backed"] += 1

    duplicate_descriptions, placeholder_descriptions = _description_findings(
        context
    )
    unsupported_types = sorted(
        {
            entity.observed_type or entity.type
            for entity in entities
            if entity.semantic_lane == "discovery"
        }
    )
    unsupported_properties = sorted(
        {
            observation.observed_property_name or observation.property_name
            for observation in properties
            if observation.semantic_lane == "discovery"
        }
    )
    unsupported_predicates = sorted(
        {
            relationship.observed_relation or relationship.relation
            for relationship in relationships
            if relationship.semantic_lane == "discovery"
        }
    )
    property_coverage = _coverage(property_statuses, property_evidence)
    relationship_coverage = _coverage(
        relationship_statuses,
        relationship_evidence,
    )
    status = (
        "passed"
        if property_coverage == 1.0
        and relationship_coverage == 1.0
        and endpoint_resolution == 1.0
        and not duplicate_descriptions
        and not placeholder_descriptions
        else "failed"
    )
    output_fingerprint = [
        {
            "source_file_id": output.source_file_id,
            "pass": output.pass_,
            "semantic_contract_hash": output.semantic_contract_hash,
            "entities": [
                {
                    "type": entity.semantic_type_id or entity.observed_type,
                    "label": entity.canonical_name or entity.label,
                    "resolution_context_key": entity.resolution_context_key,
                    "review_status": entity.review_status,
                }
                for entity in output.entities
            ],
            "properties": [
                {
                    "entity": observation.entity_id_hint,
                    "property": observation.semantic_property_id
                    or observation.observed_property_name,
                    "normalized_value": observation.normalized_value,
                    "status": observation.processing_status,
                    "evidence": sorted(observation.evidence_id_hints),
                    "conflict_id": observation.conflict_id,
                }
                for observation in output.property_observations
            ],
            "relationships": [
                {
                    "source": relationship.source_id_hint,
                    "relationship": relationship.semantic_relationship_id
                    or relationship.observed_relation,
                    "target": relationship.target_id_hint,
                    "status": relationship.processing_status,
                    "evidence": sorted(relationship.evidence_id_hints),
                    "valid_from": relationship.valid_from,
                    "valid_to": relationship.valid_to,
                }
                for relationship in output.relationships
            ],
        }
        for output in outputs
    ]
    return EnrichmentQualityReport(
        semantic_contract_hash=(
            context.contract_hash if context is not None else None
        ),
        status=status,
        entity_counts=_status_counts(entity_statuses),
        property_counts=_status_counts(property_statuses),
        relationship_counts=_status_counts(relationship_statuses),
        property_evidence_coverage=property_coverage,
        relationship_evidence_coverage=relationship_coverage,
        relationship_endpoint_resolution=endpoint_resolution,
        merge_count=merge_count,
        conflict_count=len(
            {
                observation.conflict_id
                for observation in properties
                if observation.conflict_id
            }
        ),
        per_type=[
            SemanticTypeQuality(
                semantic_id=semantic_id,
                candidates=counts["candidates"],
                accepted=counts["accepted"],
                discovery=counts["discovery"],
                rejected=counts["rejected"],
                unresolved=counts["unresolved"],
                evidence_backed=counts["evidence_backed"],
            )
            for semantic_id, counts in sorted(per_type_counts.items())
        ],
        duplicate_description_findings=duplicate_descriptions,
        placeholder_description_findings=placeholder_descriptions,
        unsupported_type_findings=unsupported_types,
        unsupported_property_findings=unsupported_properties,
        unsupported_predicate_findings=unsupported_predicates,
        deterministic_output_hash=_canonical_hash(output_fingerprint),
    )


def merge_enrichment_quality_reports(
    reports: list[EnrichmentQualityReport],
) -> EnrichmentQualityReport:
    """Combine per-source reports into one deterministic run report."""
    if not reports:
        return build_enrichment_quality_report([], None)

    def combine_counts(field: str) -> dict[str, int]:
        combined: Counter[str] = Counter()
        for report in reports:
            combined.update(getattr(report, field))
        return {
            key: combined[key]
            for key in (
                "candidates",
                "accepted",
                "discovery",
                "rejected",
                "unresolved",
            )
        }

    def weighted_coverage(field: str, count_field: str) -> float:
        accepted = sum(
            getattr(report, count_field)["accepted"] for report in reports
        )
        if not accepted:
            return 1.0
        return sum(
            getattr(report, field)
            * getattr(report, count_field)["accepted"]
            for report in reports
        ) / accepted

    per_type: dict[str, Counter[str]] = defaultdict(Counter)
    for report in reports:
        for item in report.per_type:
            per_type[item.semantic_id].update(
                {
                    "candidates": item.candidates,
                    "accepted": item.accepted,
                    "discovery": item.discovery,
                    "rejected": item.rejected,
                    "unresolved": item.unresolved,
                    "evidence_backed": item.evidence_backed,
                }
            )
    property_coverage = weighted_coverage(
        "property_evidence_coverage",
        "property_counts",
    )
    relationship_coverage = weighted_coverage(
        "relationship_evidence_coverage",
        "relationship_counts",
    )
    accepted_relationships = sum(
        report.relationship_counts["accepted"] for report in reports
    )
    endpoint_resolution = (
        sum(
            report.relationship_endpoint_resolution
            * report.relationship_counts["accepted"]
            for report in reports
        )
        / accepted_relationships
        if accepted_relationships
        else 1.0
    )
    duplicate_findings = sorted(
        {
            finding
            for report in reports
            for finding in report.duplicate_description_findings
        }
    )
    placeholder_findings = sorted(
        {
            finding
            for report in reports
            for finding in report.placeholder_description_findings
        }
    )
    status = (
        "passed"
        if all(report.status == "passed" for report in reports)
        and property_coverage == 1.0
        and relationship_coverage == 1.0
        and endpoint_resolution == 1.0
        else "failed"
    )
    return EnrichmentQualityReport(
        semantic_contract_hash=(
            reports[0].semantic_contract_hash
            if len({report.semantic_contract_hash for report in reports}) == 1
            else None
        ),
        status=status,
        entity_counts=combine_counts("entity_counts"),
        property_counts=combine_counts("property_counts"),
        relationship_counts=combine_counts("relationship_counts"),
        property_evidence_coverage=property_coverage,
        relationship_evidence_coverage=relationship_coverage,
        relationship_endpoint_resolution=endpoint_resolution,
        merge_count=sum(report.merge_count for report in reports),
        conflict_count=sum(report.conflict_count for report in reports),
        per_type=[
            SemanticTypeQuality(
                semantic_id=semantic_id,
                candidates=counts["candidates"],
                accepted=counts["accepted"],
                discovery=counts["discovery"],
                rejected=counts["rejected"],
                unresolved=counts["unresolved"],
                evidence_backed=counts["evidence_backed"],
            )
            for semantic_id, counts in sorted(per_type.items())
        ],
        duplicate_description_findings=duplicate_findings,
        placeholder_description_findings=placeholder_findings,
        unsupported_type_findings=sorted(
            {
                finding
                for report in reports
                for finding in report.unsupported_type_findings
            }
        ),
        unsupported_property_findings=sorted(
            {
                finding
                for report in reports
                for finding in report.unsupported_property_findings
            }
        ),
        unsupported_predicate_findings=sorted(
            {
                finding
                for report in reports
                for finding in report.unsupported_predicate_findings
            }
        ),
        deterministic_output_hash=_canonical_hash(
            sorted(report.deterministic_output_hash for report in reports)
        ),
    )
