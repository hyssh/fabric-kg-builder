"""Local, contract-driven business quality; never infer missing source facts.

Policies live outside DomainContractV2 so enabling this gate does not change
historical contract serialization. A publication approval binds both the exact
policy and a freshly computed report, not an operator-editable assessment file.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_serializer

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, normalize_nfc
if TYPE_CHECKING:
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

REPORT_VERSION = "1.0.0"
DERIVED_ENTITY_TABLE = "presentation_semantic_entities"


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelRule(_PolicyModel):
    property_ids: list[str] = Field(default_factory=list)
    allow_evidence_mention: bool = False
    display_templates: list[str] = Field(default_factory=list)
    derive_display_from_properties: bool = False

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        value = handler(self)
        # Preserve version 1.0.0 approval hashes when the feature is not opted in.
        if isinstance(self.display_templates, list) and not self.display_templates:
            value.pop("display_templates", None)
        if self.derive_display_from_properties is False:
            value.pop("derive_display_from_properties", None)
        return value


class EndpointCoverageRule(_PolicyModel):
    relationship_type_id: str
    semantic_type_id: str
    endpoint: Literal["source", "target"]
    minimum_per_entity: int = Field(default=1, ge=1)


class RequirementPathRule(_PolicyModel):
    procedure_type_id: str
    requirement_type_id: str
    procedure_relationship_type_id: str
    item_relationship_type_ids: list[str] = Field(min_length=1)


class BusinessQualityPolicy(_PolicyModel):
    policy_version: Literal["1.0.0"] = "1.0.0"
    domain_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    label_rules: dict[str, LabelRule] = Field(default_factory=dict)
    endpoint_coverage: list[EndpointCoverageRule] = Field(default_factory=list)
    requirement_paths: list[RequirementPathRule] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        value = handler(self)
        if isinstance(self.requirement_paths, list) and not self.requirement_paths:
            value.pop("requirement_paths", None)
        return value


class BusinessQualityError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(
            f"BUSINESS_QUALITY_BLOCKED: {report['summary']['blocker_count']} findings; "
            f"report_hash={report['report_hash']}"
        )


def parse_quality_policy(
    value: BusinessQualityPolicy | Mapping[str, Any],
) -> BusinessQualityPolicy:
    # Revalidate even model instances: model_copy/update and mutable nested
    # collections must not bypass the versioned policy schema.
    raw = value.model_dump(mode="json") if isinstance(value, BusinessQualityPolicy) else dict(value)
    return BusinessQualityPolicy.model_validate(raw)


def _present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _evidence_linked(row: Mapping[str, Any]) -> bool:
    links = row.get("evidence_span_ids")
    return isinstance(links, (list, tuple)) and bool(links) and all(
        isinstance(link, str) and bool(link.strip()) for link in links
    )


def _template_parts(template: str) -> list[tuple[str, str | None]]:
    """Treat placeholders as literal property IDs, never Python expressions."""
    parts = []
    cursor = 0
    for match in re.finditer(r"\{([^{}]+)\}", template):
        literal = template[cursor:match.start()]
        if "{" in literal or "}" in literal:
            raise ValueError("BUSINESS_QUALITY_INVALID_DISPLAY_TEMPLATE")
        parts.append((literal, match.group(1)))
        cursor = match.end()
    tail = template[cursor:]
    if not parts or "{" in tail or "}" in tail:
        raise ValueError("BUSINESS_QUALITY_INVALID_DISPLAY_TEMPLATE")
    parts.append((tail, None))
    return parts


def _normalized_label(value: str) -> str:
    return " ".join(normalize_nfc(value).split())


def _render_display_template(
    entity_id: str,
    template: str,
    parts: list[tuple[str, str | None]],
    values: Mapping[tuple[str, str], Sequence[tuple[Any, Mapping[str, Any]]]],
    label: Any,
) -> dict[str, Any]:
    fragments = []
    assertion_ids: set[str] = set()
    evidence_ids: set[str] = set()
    unavailable: set[str] = set()
    for literal, property_id in parts:
        fragments.append(literal)
        if property_id is None:
            continue
        observed = values.get((entity_id, property_id), [])
        candidates = [
            (value, row) for value, row in observed
            if _present(value) and _evidence_linked(row)
            and isinstance(value, (str, bool, int, float, date, datetime))
        ]
        if not candidates or len({row["normalized_value_json"] for _, row in observed}) != 1:
            unavailable.add(property_id)
            continue
        value = candidates[0][0]
        fragments.append(
            value if isinstance(value, str) else
            value.isoformat() if isinstance(value, (date, datetime)) else canonical_json(value)
        )
        assertion_ids.update(row["property_assertion_id"] for _, row in candidates)
        evidence_ids.update(link for _, row in candidates for link in row["evidence_span_ids"])
    rendered = None if unavailable else "".join(fragments)
    return {
        "template": template,
        "property_ids": sorted({prop for _, prop in parts if prop is not None}),
        "unavailable_property_ids": sorted(unavailable),
        "property_assertion_ids": sorted(assertion_ids),
        "evidence_span_ids": sorted(evidence_ids),
        "rendered_label": rendered,
        "status": "unavailable" if unavailable else "matched" if (
            isinstance(label, str) and _normalized_label(label) == _normalized_label(rendered)
        ) else "mismatch",
    }


def _qualified_base(value: str) -> str:
    match = re.fullmatch(r"(.+?)\s+\([^()]+\)(?:\s*\([^()]+\))*", value)
    return match.group(1) if match else value


def _qualified_prefix(mention: str, value: str) -> bool:
    if not mention.startswith(value):
        return False
    suffix = mention[len(value):]
    return bool(suffix) and bool(re.fullmatch(
        r"(?:\s+(?:[x×]\s*\d+(?:\.\d+)?|qty\s*=\s*\d+(?:\.\d+)?))?"
        r"\s*(?:\([^()]+\)\s*)*", suffix,
    ))


def _whole_identifier(mention: str, identifier: str) -> bool:
    return bool(re.search(r"(?<![\w./-])" + re.escape(identifier) + r"(?![\w./-])", mention))


def _derive_instance_display(
    entity: Mapping[str, Any], rule: LabelRule,
    templates: Sequence[Mapping[str, Any]],
    values: Mapping[tuple[str, str], Sequence[tuple[Any, Mapping[str, Any]]]],
) -> dict[str, Any]:
    raw = entity.get("label")
    audit = {
        "entity_id": entity["entity_id"], "semantic_type_id": entity["most_specific_type_id"],
        "original_mention": raw, "original_label_evidence_span_id": entity.get("label_evidence_span_id"),
        "derived_display_label": None, "status": "blocked",
        "property_ids": [], "property_assertion_ids": [], "evidence_span_ids": [],
        "template": None,
    }

    def failed(reason):
        return {**audit, "reason": reason}

    if not isinstance(raw, str) or not raw.strip() or not _evidence_linked(entity) or (
        entity.get("label_evidence_span_id") not in entity["evidence_span_ids"]
    ):
        return failed("ORIGINAL_MENTION_EVIDENCE_MISSING")
    mention = _normalized_label(raw)
    selected_ids = set(rule.property_ids) | {
        key for template in templates for key in template["property_ids"]
    }
    # A SKU-only rule must not hide a contradictory asserted Name on its owner.
    naming_suffixes = {"name", "title", "part_number", "alternate_part_number", "sku"}
    guard_ids = selected_ids | {
        prop for owner, prop in values
        if owner == entity["entity_id"] and prop.rsplit(".", 1)[-1] in naming_suffixes
    }
    grounded = {}
    for key in sorted(guard_ids):
        observed = values.get((entity["entity_id"], key), [])
        if not observed:
            continue
        if len({row["normalized_value_json"] for _, row in observed}) != 1:
            return failed("AMBIGUOUS_NAMING_PROPERTY")
        if not all(isinstance(value, str) and value.strip() and _evidence_linked(row)
                   for value, row in observed):
            return failed("NAMING_PROPERTY_EVIDENCE_OR_VALUE_MISSING")
        grounded[key] = (_normalized_label(observed[0][0]), observed)
    identifiers = {
        key: value for key, (value, _) in grounded.items()
        if key.rsplit(".", 1)[-1] in {"part_number", "alternate_part_number", "sku"}
    }
    names = {key: value for key, (value, _) in grounded.items() if key not in identifiers}
    identifier_mention = any(mention == value for value in identifiers.values())
    matched_identifiers = [value for value in identifiers.values() if _whole_identifier(mention, value)]
    for name in names.values():
        if mention in (name, _qualified_base(name)) or identifier_mention:
            continue
        if _qualified_prefix(mention, name):
            continue
        if any(
            mention == f"{identifier} {name}" or _qualified_prefix(mention, f"{identifier} {name}")
            for identifier in matched_identifiers
        ):
            continue
        return failed("ASSERTED_NAME_CONTRADICTS_MENTION")
    candidates = []
    for template in templates:
        if template["rendered_label"] is not None and all(
            key in grounded for key in template["property_ids"]
        ):
            candidates.append((template["rendered_label"], template["property_ids"], template["template"]))
    candidates.extend(
        (grounded[key][1][0][0], [key], None) for key in rule.property_ids if key in grounded
    )
    for display, property_ids, template in candidates:
        normalized = _normalized_label(display)
        supported = (
            mention == normalized or _qualified_prefix(mention, normalized)
            or any(mention in (value, _qualified_base(value)) for value, _ in grounded.values())
            or (template is None and property_ids[0] in identifiers
                and _whole_identifier(mention, normalized))
        )
        if not supported:
            continue
        # Include the guard assertions too: they authorize rejecting contradictions.
        proof_ids = sorted(grounded)
        return {
            **audit, "status": "derived", "reason": "GROUNDED_SYNTACTIC_VARIANT",
            "derived_display_label": display, "template": template,
            "property_ids": list(property_ids), "guard_property_ids": proof_ids,
            "property_assertion_ids": sorted({
                row["property_assertion_id"] for key in proof_ids for _, row in grounded[key][1]
            }),
            "evidence_span_ids": sorted({
                link for key in proof_ids for _, row in grounded[key][1] for link in row["evidence_span_ids"]
            }),
        }
    return failed("NO_SUPPORTED_GROUNDED_DISPLAY")


def derived_instance_display_labels(report: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Consume only a freshly recomputed quality report inside the compiler."""
    rows = (report or {}).get("presentation_coverage", {}).get("derived_instance_displays")
    if rows is None:
        return None
    return {
        row["entity_id"]: row["derived_display_label"]
        for row in rows if row["status"] == "derived"
    }


def assess_requirement_path_readiness(
    *,
    contract: Mapping[str, Any],
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    rules: Sequence[RequirementPathRule],
) -> dict[str, Any]:
    """Count observed typed paths, independently of quality gate/answer accuracy."""
    types = {
        t["type_id"]: t for t in contract["candidate_model"]["entity_types"]
        if not t.get("tombstoned")
    }
    relationships = {
        r["relationship_type_id"]: r for r in contract["candidate_model"]["relationship_types"]
    }

    def lineage(type_id):
        seen = set()
        while type_id in types and type_id not in seen:
            seen.add(type_id)
            type_id = types[type_id].get("parent_type_id")
        return seen

    def compatible(type_id, definition, endpoint):
        candidates = lineage(type_id) if definition.get("endpoint_policy") == "allow_subtypes" else {type_id}
        return bool(candidates & set(definition[f"{endpoint}_type_ids"]))

    entities = {e["entity_id"]: e for e in tables.get("semantic_asserted_entities", [])}
    reports = []
    for raw in rules:
        rule = RequirementPathRule.model_validate(
            raw.model_dump(mode="json") if isinstance(raw, RequirementPathRule) else raw
        )
        ids = [rule.procedure_relationship_type_id, *rule.item_relationship_type_ids]
        if (
            rule.procedure_type_id not in types or rule.requirement_type_id not in types
            or any(rel_id not in relationships for rel_id in ids)
            or len(set(rule.item_relationship_type_ids)) != len(rule.item_relationship_type_ids)
        ):
            raise ValueError("BUSINESS_QUALITY_INVALID_REQUIREMENT_PATH_RULE")
        first = relationships[rule.procedure_relationship_type_id]
        if (
            not compatible(rule.procedure_type_id, first, "source")
            or not compatible(rule.requirement_type_id, first, "target")
            or any(not compatible(rule.requirement_type_id, relationships[key], "source")
                   for key in rule.item_relationship_type_ids)
            or any(not relationships[key].get("target_type_ids")
                   or not set(relationships[key]["target_type_ids"]) <= types.keys()
                   for key in rule.item_relationship_type_ids)
        ):
            raise ValueError("BUSINESS_QUALITY_INCOMPATIBLE_REQUIREMENT_PATH_RULE")
        procedures = {
            key for key, entity in entities.items()
            if rule.procedure_type_id in lineage(entity["most_specific_type_id"])
        }
        requirements = {
            key for key, entity in entities.items()
            if rule.requirement_type_id in lineage(entity["most_specific_type_id"])
        }
        owners: dict[str, set[str]] = defaultdict(set)
        items: dict[str, set[str]] = defaultdict(set)
        edge_ids: dict[str, set[str]] = defaultdict(set)
        invalid = []
        for edge in tables.get("semantic_asserted_relationships", []):
            rel_id = edge["semantic_relationship_id"]
            if rel_id not in ids:
                continue
            definition = relationships[rel_id]
            source, target = edge["source_entity_id"], edge["target_entity_id"]
            is_owner = rel_id == rule.procedure_relationship_type_id
            valid = (
                source in entities and target in entities
                and _evidence_linked(edge)
                and _evidence_linked(entities[source]) and _evidence_linked(entities[target])
                and compatible(entities[source]["most_specific_type_id"], definition, "source")
                and compatible(entities[target]["most_specific_type_id"], definition, "target")
                and (source in procedures and target in requirements if is_owner else source in requirements)
            )
            if not valid:
                invalid.append(edge["relationship_id"])
                continue
            requirement_id = target if is_owner else source
            (owners if is_owner else items)[requirement_id].add(source if is_owner else target)
            edge_ids[requirement_id].add(edge["relationship_id"])
        rows = []
        ready_procedures: set[str] = set()
        for key in sorted(requirements):
            ready = bool(owners[key]) and len(items[key]) == 1
            if ready:
                ready_procedures.update(owners[key])
            rows.append({
                "requirement_entity_id": key, "procedure_entity_ids": sorted(owners[key]),
                "item_entity_ids": sorted(items[key]), "relationship_ids": sorted(edge_ids[key]),
                "status": "ready" if ready else "not-ready",
                "reasons": ([] if owners[key] else ["PROCEDURE_LINK_MISSING"]) + (
                    [] if len(items[key]) == 1 else
                    ["ITEM_LINK_MISSING"] if not items[key] else ["MULTIPLE_DISTINCT_ITEMS"]
                ),
            })
        ready_count = sum(row["status"] == "ready" for row in rows)
        reports.append({
            "rule": rule.model_dump(mode="json"),
            "relationship_contracts": [
                {key: relationships[rel_id].get(key) for key in (
                    "relationship_type_id", "predicate_id", "source_type_ids",
                    "target_type_ids", "endpoint_policy",
                )} for rel_id in ids
            ],
            "status": "unobserved" if not requirements else (
                "ready" if ready_count == len(requirements) and ready_procedures == procedures
                and not invalid else "not-ready"
            ),
            "procedure_count": len(procedures), "requirement_count": len(requirements),
            "ready_requirement_count": ready_count,
            "procedures_with_ready_paths": len(ready_procedures),
            "procedure_ids_without_ready_paths": sorted(procedures - ready_procedures),
            "invalid_relationship_ids": sorted(invalid),
            "requirements": rows,
        })
    return {
        "status": "assessed" if reports else "not-assessed",
        "gate_effect": "informational-only",
        "scope": "asserted L4 population only",
        "question_accuracy": "not-assessed", "source_scope_completeness": "not-assessed",
        "reason": "Typed evidence-linked paths do not prove quantities, applicability, answer accuracy, or complete source scope.",
        "paths": reports,
    }


def _graph_columns(definition: Mapping[str, Any]) -> dict[str, set[str]]:
    """Read local native Graph parts; this does not attest a deployed Graph."""
    parts = {
        part["path"]: json.loads(base64.b64decode(part["payload"], validate=True))
        for part in definition["parts"]
    }
    sources = {
        item["name"]: item["properties"]["path"].rstrip("/").rsplit("/", 1)[-1]
        for item in parts["dataSources.json"]["dataSources"]
    }
    result: dict[str, set[str]] = defaultdict(set)
    for node in parts["graphDefinition.json"]["nodeTables"]:
        result[sources[node["dataSourceName"]]].update(
            item["sourceColumn"] for item in node["propertyMappings"]
        )
    return dict(result)


def assess_business_quality_rows(
    *,
    contract: Mapping[str, Any],
    domain_contract_hash: str,
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    input_hashes: Mapping[str, str],
    policy: BusinessQualityPolicy | Mapping[str, Any] | None = None,
    crosswalks: Sequence[Any] | None = None,
    graph_definition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess canonical rows; the public sealed-source wrapper verifies inputs.

    Absence of optional values is a warning, not invented evidence or a required
    fact. Relationship participation requirements exist only in approved rules.
    """
    from fabric_kg_builder.semantic.source_tables import decode_property_scalar

    approved = parse_quality_policy(policy) if policy is not None else None
    if approved and approved.domain_contract_hash != domain_contract_hash:
        raise ValueError("BUSINESS_QUALITY_POLICY_CONTRACT_MISMATCH")
    model = contract["candidate_model"]
    types = {t["type_id"]: t for t in model["entity_types"] if not t.get("tombstoned")}
    relationships = {r["relationship_type_id"]: r for r in model["relationship_types"]}
    properties = {
        p["property_id"]: (t["type_id"], p)
        for t in types.values() for p in t.get("declared_properties", [])
    }
    effective = contract["hierarchy_closure"]["effective_property_ids_by_type"]
    findings: list[dict[str, Any]] = []
    templates: dict[str, list[tuple[str, list[tuple[str, str | None]]]]] = {}

    def finding(code: str, row_id: str, reason: str, *, severity="blocker", **context):
        findings.append({
            "severity": severity, "code": code, "row_id": row_id,
            "reason": reason, **context,
        })

    if approved:
        for type_id, rule in approved.label_rules.items():
            if type_id not in types or (
                not rule.property_ids and not rule.allow_evidence_mention and not rule.display_templates
            ):
                raise ValueError(f"BUSINESS_QUALITY_INVALID_LABEL_RULE: {type_id}")
            for property_id in rule.property_ids:
                if (
                    property_id not in effective.get(type_id, [])
                    or property_id not in properties
                    or properties[property_id][1]["value_type"] != "string"
                ):
                    raise ValueError(f"BUSINESS_QUALITY_INVALID_LABEL_PROPERTY: {property_id}")
            templates[type_id] = []
            for template in rule.display_templates:
                parts = _template_parts(template)
                for _, property_id in parts:
                    if property_id is not None and (
                        property_id not in effective.get(type_id, []) or property_id not in properties
                    ):
                        raise ValueError(f"BUSINESS_QUALITY_INVALID_TEMPLATE_PROPERTY: {property_id}")
                templates[type_id].append((template, parts))
        for rule in approved.endpoint_coverage:
            if rule.relationship_type_id not in relationships or rule.semantic_type_id not in types:
                raise ValueError("BUSINESS_QUALITY_INVALID_ENDPOINT_RULE")
            rel = relationships[rule.relationship_type_id]
            allowed = set(rel[f"{rule.endpoint}_type_ids"])
            current = rule.semantic_type_id
            lineage = {current}
            while types[current].get("parent_type_id"):
                current = types[current]["parent_type_id"]
                lineage.add(current)
            if not ((lineage if rel.get("endpoint_policy") == "allow_subtypes" else {rule.semantic_type_id}) & allowed):
                raise ValueError("BUSINESS_QUALITY_INCOMPATIBLE_ENDPOINT_RULE")

    entity_rows = list(tables.get("semantic_asserted_entities", []))
    property_rows = list(tables.get("semantic_asserted_properties", []))
    relationship_rows = list(tables.get("semantic_asserted_relationships", []))
    entities = {r["entity_id"]: r for r in entity_rows}
    if not entities:
        finding("EMPTY_ENTITY_POPULATION", "population", "No asserted entities are available.")
    values: dict[tuple[str, str], list[tuple[Any, Mapping[str, Any]]]] = defaultdict(list)
    evidence_coverage = []
    for table_name, rows, id_column in (
        ("entities", entity_rows, "entity_id"),
        ("properties", property_rows, "property_assertion_id"),
        ("relationships", relationship_rows, "relationship_id"),
    ):
        linked_count = 0
        for row in rows:
            linked = bool(row.get("evidence_span_ids")) and all(
                isinstance(e, str) and bool(e.strip()) for e in row["evidence_span_ids"]
            )
            linked_count += int(linked)
            if not linked:
                finding("EVIDENCE_LINK_MISSING", row[id_column],
                        "Asserted row lacks evidence-span links.", table=table_name)
        evidence_coverage.append({
            "table": table_name, "row_count": len(rows),
            "rows_with_evidence_links": linked_count,
            "rows_missing_evidence_links": len(rows) - linked_count,
        })
    for row in property_rows:
        row_id = row["property_assertion_id"]
        entity = entities.get(row.get("entity_id"))
        property_id = row["semantic_property_id"]
        definition = properties.get(property_id)
        if entity is None or definition is None or property_id not in effective.get(
            entity["most_specific_type_id"], []
        ):
            finding("PROPERTY_OWNER_INVALID", row_id,
                    "Property lacks an asserted entity with an approved effective property.",
                    property_id=property_id, entity_id=row.get("entity_id"))
            continue
        try:
            if row["value_type"] != definition[1]["value_type"]:
                raise ValueError("Declared value type differs.")
            value = decode_property_scalar(row.get("normalized_value_json"), row["value_type"])
        except (TypeError, ValueError, OverflowError):
            finding("PROPERTY_VALUE_INVALID", row_id,
                    "Asserted scalar is absent or incompatible with its declared type.",
                    property_id=property_id, entity_id=row.get("entity_id"))
            continue
        values[(entity["entity_id"], property_id)].append((value, row))
        if not _present(value):
            finding("PROPERTY_VALUE_EMPTY", row_id, "Asserted scalar is blank.",
                    severity="blocker" if definition[1].get("required") else "warning",
                    property_id=property_id, entity_id=entity["entity_id"])
    for (entity_id, property_id), observed in values.items():
        if len({row["normalized_value_json"] for _, row in observed}) > 1:
            finding("PROPERTY_VALUE_CONFLICT", entity_id,
                    "Multiple unequal asserted values cannot form one published scalar.",
                    property_id=property_id)

    mapping_owners: set[tuple[str, str]] = set()
    mapping_properties: set[tuple[str, str]] = set()
    mapping_relationships: set[str] = set()
    type_mappings: dict[str, Mapping[str, Any]] = {}
    if crosswalks is not None:
        for crosswalk in crosswalks:
            raw = crosswalk.model_dump(mode="json") if hasattr(crosswalk, "model_dump") else crosswalk
            type_mappings.update((m["canonical_semantic_type_id"], m) for m in raw["semantic_type_mappings"])
            mapping_owners.update(
                (m["owner_semantic_type_id"], m["canonical_property_id"])
                for m in raw["semantic_property_ownership_mappings"]
            )
            mapping_properties.update(
                (m["canonical_semantic_type_id"], p["canonical_property_id"])
                for m in raw["semantic_type_mappings"] for p in m["physical_property_bindings"]
            )
            mapping_relationships.update(
                m["canonical_semantic_relationship_id"] for m in raw["relationship_mappings"]
            )
    else:
        finding("MAPPING_NOT_ASSESSED", "publication", "No publication crosswalk supplied.", severity="warning")

    property_coverage = []
    for property_id, (owner_type, definition) in sorted(properties.items()):
        applicable = [e for e in entity_rows if property_id in effective.get(e["most_specific_type_id"], [])]
        populated = 0
        for entity in applicable:
            entity_id = entity["entity_id"]
            present = any(_present(v) for v, _ in values.get((entity_id, property_id), []))
            populated += int(present)
            if not present:
                finding(
                    "REQUIRED_PROPERTY_MISSING" if definition.get("required") else "OPTIONAL_PROPERTY_UNKNOWN",
                    entity_id, "No populated declared value; a mapping or display repair cannot supply this fact.",
                    severity="blocker" if definition.get("required") else "warning",
                    property_id=property_id, owner_type_id=owner_type,
                )
            if crosswalks is not None and (
                (owner_type, property_id) not in mapping_owners
                or (entity["most_specific_type_id"], property_id) not in mapping_properties
            ):
                finding("PROPERTY_MAPPING_MISSING", entity_id,
                        "Declared owner or physical property mapping is absent.",
                        property_id=property_id, value_present=present)
        property_coverage.append({
            "property_id": property_id, "owner_type_id": owner_type,
            "required": definition.get("required", False), "applicable_entities": len(applicable),
            "populated_entities": populated, "missing_entities": len(applicable) - populated,
            "null_fraction": (len(applicable) - populated) / len(applicable) if applicable else None,
            "owner_mapping_present": (owner_type, property_id) in mapping_owners if crosswalks is not None else None,
        })

    grounded_labels = 0
    template_assessments = []
    derived_displays = []
    for entity in entity_rows:
        entity_id, type_id = entity["entity_id"], entity["most_specific_type_id"]
        label = entity.get("label")
        label_evidence = entity.get("label_evidence_span_id")
        grounded = _present(label) and label_evidence in (entity.get("evidence_span_ids") or [])
        grounded_labels += int(bool(grounded))
        if not _present(label):
            finding("PRESENTATION_LABEL_MISSING", entity_id, "L4 has no nonempty presentation label.")
        elif not grounded:
            finding("LABEL_EVIDENCE_LINK_MISSING", entity_id, "Presentation label lacks an entity evidence link.")
        rule = approved.label_rules.get(type_id) if approved else None
        if rule is None:
            finding("SEMANTIC_LABEL_RULE_UNSPECIFIED", entity_id,
                    "No explicit rule establishes that this mention is an approved business name.",
                    severity="blocker" if approved else "warning", semantic_type_id=type_id)
            continue
        named = [
            (value, prop) for property_id in rule.property_ids
            for value, prop in values.get((entity_id, property_id), [])
            if _present(value) and _evidence_linked(prop)
        ]
        rendered_templates = [
            _render_display_template(entity_id, template, parts, values, label)
            for template, parts in templates.get(type_id, [])
        ]
        if rendered_templates:
            template_assessments.append({
                "entity_id": entity_id, "semantic_type_id": type_id,
                "templates": rendered_templates,
            })
        if rule.derive_display_from_properties:
            derived = _derive_instance_display(entity, rule, rendered_templates, values)
            derived_displays.append(derived)
            if derived["status"] != "derived":
                finding(
                    "GROUNDED_NAME_MISSING" if not named and not any(
                        t["rendered_label"] is not None for t in rendered_templates
                    ) else "PRESENTATION_LABEL_NAME_MISMATCH",
                    entity_id, "No safe derived instance display: " + derived["reason"],
                    property_ids=rule.property_ids,
                )
            continue
        if not named and not any(t["status"] != "unavailable" for t in rendered_templates) and not (
            rule.allow_evidence_mention and grounded
        ):
            finding("GROUNDED_NAME_MISSING", entity_id,
                    "No populated evidence-linked value satisfies the approved name rule.",
                    property_ids=rule.property_ids)
        elif not (rule.allow_evidence_mention and grounded) and not any(
            t["status"] == "matched" for t in rendered_templates
        ) and not any(
            isinstance(label, str) and isinstance(value, str)
            and _normalized_label(label) == _normalized_label(value)
            for value, _ in named
        ):
            finding("PRESENTATION_LABEL_NAME_MISMATCH", entity_id,
                    "Grounded name exists but the presentation label does not carry it.")

    endpoint_counts: Counter[tuple[str, str, str]] = Counter()
    relationship_counts = Counter(r["semantic_relationship_id"] for r in relationship_rows)
    for row in relationship_rows:
        relationship_id = row["semantic_relationship_id"]
        if relationship_id not in relationships:
            finding("RELATIONSHIP_TYPE_UNDECLARED", row["relationship_id"], "Relationship type is not declared.")
        for endpoint in ("source", "target"):
            entity_id = row[f"{endpoint}_entity_id"]
            if entity_id not in entities:
                finding("RELATIONSHIP_ENDPOINT_MISSING", row["relationship_id"],
                        "Relationship endpoint is not an asserted entity.", endpoint=endpoint, entity_id=entity_id)
            else:
                endpoint_counts[(relationship_id, endpoint, entity_id)] += 1
        if crosswalks is not None and relationship_id not in mapping_relationships:
            finding("RELATIONSHIP_MAPPING_MISSING", row["relationship_id"], "Relationship lacks a publication mapping.")
    for relationship_id in sorted(relationships):
        if not relationship_counts[relationship_id]:
            finding("DECLARED_RELATIONSHIP_UNOBSERVED", relationship_id,
                    "No asserted relationship of this declared type; no participation requirement is inferred.",
                    severity="warning")
    for rule in approved.endpoint_coverage if approved else []:
        for entity in entity_rows:
            if rule.semantic_type_id not in entity.get("asserted_type_ids", [entity["most_specific_type_id"]]):
                continue
            observed = endpoint_counts[(rule.relationship_type_id, rule.endpoint, entity["entity_id"])]
            if observed < rule.minimum_per_entity:
                finding("RELATIONSHIP_COVERAGE_MISSING", entity["entity_id"],
                        "Asserted endpoint participation is below the explicit approved minimum.",
                        relationship_type_id=rule.relationship_type_id, endpoint=rule.endpoint,
                        observed=observed, minimum=rule.minimum_per_entity)

    graph_coverage = []
    if graph_definition is not None:
        if crosswalks is None:
            raise ValueError("Graph presentation assessment requires canonical publication crosswalks.")
        try:
            columns_by_table = _graph_columns(graph_definition)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("BUSINESS_QUALITY_GRAPH_DEFINITION_INVALID") from exc
        for type_id, mapping in sorted(type_mappings.items()):
            columns = columns_by_table.get(mapping["physical_table_id"], set())
            applicable = [e for e in entity_rows if type_id in e.get("asserted_type_ids", [])]
            for entity in applicable:
                if "__label" not in columns:
                    finding("GRAPH_LABEL_MAPPING_MISSING", entity["entity_id"],
                            "Local Graph definition does not expose the L4 presentation label; this is not missing source evidence.",
                            severity="warning", semantic_type_id=type_id,
                            l4_label_present=_present(entity.get("label")))
                for prop in mapping["physical_property_bindings"]:
                    if prop["physical_column_id"] not in columns:
                        finding("GRAPH_PROPERTY_MAPPING_MISSING", entity["entity_id"],
                                "Local Graph definition omits a declared property column.",
                                severity="warning", semantic_type_id=type_id,
                                property_id=prop["canonical_property_id"],
                                value_present=any(_present(v) for v, _ in values.get(
                                    (entity["entity_id"], prop["canonical_property_id"]), []
                                )))
            graph_coverage.append({
                "semantic_type_id": type_id,
                "physical_table_id": mapping["physical_table_id"],
                "entity_count": len(applicable),
                "node_mapping_present": mapping["physical_table_id"] in columns_by_table,
                "label_mapping_present": "__label" in columns,
                "property_mapping_count": sum(
                    prop["physical_column_id"] in columns for prop in mapping["physical_property_bindings"]
                ),
            })

    findings.sort(key=canonical_json)
    counts = Counter(f["severity"] for f in findings)
    report = {
        "report_version": REPORT_VERSION,
        "mode": "strict" if approved else "baseline",
        "domain_contract_hash": domain_contract_hash,
        "policy": approved.model_dump(mode="json") if approved else None,
        "policy_hash": canonical_sha256(approved.model_dump(mode="json")) if approved else None,
        "input_hashes": {
            **input_hashes,
            **({"graph_definition_hash": canonical_sha256(graph_definition)} if graph_definition is not None else {}),
        },
        "crosswalk_hashes": sorted(canonical_sha256(
            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
        ) for c in crosswalks) if crosswalks is not None else [],
        "summary": {
            "status": "blocked" if counts["blocker"] else "pass",
            "entity_count": len(entity_rows), "property_assertion_count": len(property_rows),
            "relationship_count": len(relationship_rows), "grounded_label_count": grounded_labels,
            "blocker_count": counts["blocker"], "warning_count": counts["warning"],
            "counts_by_code": dict(sorted(Counter(f["code"] for f in findings).items())),
        },
        "property_coverage": property_coverage,
        "evidence_link_coverage": evidence_coverage,
        "presentation_coverage": {
            "l4_nonempty_labels": sum(_present(e.get("label")) for e in entity_rows),
            "l4_grounded_labels": grounded_labels,
            "graph_presentation": "local-mapping-assessed" if graph_definition is not None else "not-assessed",
            "graph_mapping_coverage": graph_coverage,
            "reason": "Canonical evidence links and planned property mappings do not establish live Graph display.",
        },
        "relationship_coverage": [
            {"relationship_type_id": key, "asserted_count": relationship_counts[key]}
            for key in sorted(relationships)
        ],
        "semantic_precision": {
            "status": "not-assessed",
            "reason": "Structural/content validity, nonnull required fields, and evidence links do not prove semantic precision or source entailment.",
        },
        "findings": findings,
        "limitations": [
            "Evidence links are assessed separately from display/mapping coverage; sealed L3/L4 validation establishes source grounding.",
            "A stable source identity does not prove business uniqueness or duplicate resolution.",
            "Passing proves only declared fields and explicit policy rules, not complete domain truth.",
            "This is a pre-publication assessment, not a live Fabric Graph readback.",
        ],
    }
    if approved and any(rule.display_templates for rule in approved.label_rules.values()):
        report["presentation_coverage"]["display_template_assessments"] = sorted(
            template_assessments, key=canonical_json,
        )
    if approved and any(rule.derive_display_from_properties for rule in approved.label_rules.values()):
        report["presentation_coverage"]["derived_instance_displays"] = sorted(
            derived_displays, key=canonical_json,
        )
    if approved and approved.requirement_paths:
        report["structural_requirement_path_readiness"] = assess_requirement_path_readiness(
            contract=contract, tables=tables, rules=approved.requirement_paths,
        )
    return {**report, "report_hash": canonical_sha256(report)}


def assess_business_quality(
    source: SealedL4ServingSource,
    *,
    policy: BusinessQualityPolicy | Mapping[str, Any] | None = None,
    crosswalks: Sequence[Any] | None = None,
    graph_definition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource

    if not isinstance(source, SealedL4ServingSource):
        raise ValueError("Business quality requires a sealed L4 source.")
    rows = {
        name: pq.read_table(source.resolve(name)).to_pylist()
        for name in (
            "semantic_publication_authority", "semantic_asserted_entities",
            "semantic_asserted_properties", "semantic_asserted_relationships",
        )
    }
    authority = rows["semantic_publication_authority"][0]
    return assess_business_quality_rows(
        contract=json.loads(authority["domain_contract_json"]),
        domain_contract_hash=authority["domain_contract_hash"],
        tables=rows,
        input_hashes={
            "l4_receipt_hash": source.receipt.receipt_hash,
            "l4_manifest_hash": source.manifest.manifest_hash,
            "l3_manifest_hash": source.input_manifest.manifest_hash,
            "projection_hash": source.projection.projection_hash,
        },
        policy=policy, crosswalks=crosswalks, graph_definition=graph_definition,
    )


def require_business_quality(
    source: SealedL4ServingSource,
    *,
    policy: BusinessQualityPolicy | Mapping[str, Any],
    crosswalks: Sequence[Any],
) -> dict[str, Any]:
    report = assess_business_quality(source, policy=policy, crosswalks=crosswalks)
    if report["summary"]["blocker_count"]:
        raise BusinessQualityError(report)
    return report
