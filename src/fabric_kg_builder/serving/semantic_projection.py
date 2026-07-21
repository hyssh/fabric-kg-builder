"""Deterministic maintenance semantic serving projection.

The canonical extraction tables remain immutable, domain-neutral observations.
This module creates a separate serving projection for Fabric Graph Model and
Ontology use.  It normalizes unstable extractor labels and relationship verbs,
keeps source lineage unchanged, and only creates a factual claim when both
endpoints occur together in an existing evidence span.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from fabric_kg_builder.model.ids import make_id

_TYPE_MAP = {
    "facility": "Facility",
    "building": "Facility",
    "location": "Location",
    "area": "Location",
    "equipment_asset": "Equipment",
    "equipmentasset": "Equipment",
    "equipment_component": "Equipment",
    "hvac_system": "Equipment",
    "maintenance_record": "MaintenanceAction",
    "diagnostic_event": "MaintenanceEvent",
    "project": "Project",
    "person": "Person",
    "contractor": "Contractor",
    "organization": "Organization",
    "company": "Organization",
    "manufacturer": "Organization",
    "technical_document": "EvidenceDocument",
    "technicaldocument": "EvidenceDocument",
    "warranty": "Warranty",
    "contact_role": "PersonRole",
    "role": "PersonRole",
}
_VERB_MAP = {
    "has_equipment": "contains",
    "has_component": "contains",
    "has_subsystem": "contains",
    "has_document": "documented_by",
    "documented_in": "documented_by",
    "documents": "documents",
    "documents_equipment": "documents",
    "referenced_in": "cites",
    "referenced_in_document": "cites",
    "located_at": "located_at",
    "located_in": "located_at",
    "installed_at": "installed_at",
    "installed_in": "installed_at",
    "serviced_by": "serviced_by",
    "has_maintenance_record": "has_maintenance_action",
    "part_of": "part_of",
    "component_of": "part_of",
    "is_component_of": "part_of",
    "part_of_system": "part_of",
    "manufactured_by": "manufactured_by",
    "has_manufacturer": "manufactured_by",
    "serves": "serves",
    "connected_to": "connected_to",
    "used_in": "used_in",
    "has_warranty": "covered_by",
    "has_contact_role": "has_contact",
    "has_role": "has_role",
    "works_for": "works_for",
    "references": "cites",
    "supplies": "supplied_by",
    "furnished": "supplied_by",
    "has_technical_document": "documented_by",
    "contains": "contains",
    "includes": "contains",
    "contains_equipment": "contains",
    "includes_equipment": "contains",
    "includes_sheet": "contains",
    "includes_section": "contains",
    "includes_sensor": "contains",
    "has_location": "located_at",
    "has_record_drawing": "documented_by",
    "references_standard": "cites",
    "references_document": "cites",
    "created_by": "created_by",
    "prepared_by": "prepared_by",
    "performed_by": "performed_by",
    "performed_test": "performed_by",
    "controls": "controls",
    "controlled_by": "controlled_by",
    "monitors": "monitors",
    "served_by": "serviced_by",
    "service_provider": "serviced_by",
    "provides_service_for": "services",
    "complies_with": "complies_with",
    "subject_to_warranty": "covered_by",
    "covered_by_warranty": "covered_by",
    "scheduled_for_removal": "scheduled_for_removal",
    "scheduled_to_remain": "scheduled_to_remain",
    "documents_procedure_for": "applies_to",
    "manufacturing_location_of": "manufactured_at",
    "maintenance_record_for": "applies_to",
    "applies_to_system": "applies_to",
    "applies_to_equipment": "applies_to",
    "scheduled_maintenance_for": "applies_to",
}
_ORGANIZATION_SUFFIX_RE = re.compile(
    r"\b(?:inc(?:orporated)?|llc|l\.l\.c\.|ltd|limited|corp(?:oration)?|"
    r"company|co\.)\b",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"\b(planned|complete(?:d)?|final|active|pending|approved|installed|removed|"
    r"demolished|operational|failed)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b((?:19|20)\d{2}(?:[-/]\d{1,2}[-/]\d{1,2})?)\b")


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _semantic_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Return runner-owned semantic metadata embedded in canonical rows."""
    value = row.get("properties_json")
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def canonical_entity_type(raw_type: object, display_name: object = "") -> str:
    """Map spelling variants to stable maintenance nouns."""
    slug = _slug(raw_type)
    # A legal organization name is stronger evidence than an extraction label
    # such as "maintenance record". Preserve the original type on the row.
    if _ORGANIZATION_SUFFIX_RE.search(str(display_name or "")):
        return "Organization"
    if slug in _TYPE_MAP:
        return _TYPE_MAP[slug]
    return "EvidenceItem"


def canonical_verb(
    raw_verb: object,
    source_type: str = "",
    target_type: str = "",
) -> str:
    """Map extractor-specific relationship labels to controlled graph verbs."""
    slug = _slug(raw_verb)
    direct = _VERB_MAP.get(slug)
    if direct:
        return direct

    if "manufacturer" in slug:
        return "manufactures" if source_type == "Organization" else "manufactured_by"
    if slug.startswith(("reference", "cites")):
        return "cites"
    if slug.startswith("part_of") or slug.endswith("_of_system"):
        return "part_of"
    if slug.startswith("serves"):
        return "serves"
    if slug.startswith("requires"):
        return "requires"
    if slug.startswith("connect"):
        return "connected_to"
    if slug.startswith("install"):
        return "installed_at"
    if slug.startswith(("perform_maintenance", "performed_maintenance", "maintenance_for")):
        return "services" if source_type == "Organization" else "applies_to"
    if slug.startswith("document"):
        return "documents" if source_type == "EvidenceDocument" else "documented_by"
    if slug.startswith(("has_", "contains_", "includes_")):
        target_verbs = {
            "Location": "located_at",
            "Facility": "located_at",
            "Equipment": "contains",
            "MaintenanceAction": "has_maintenance_action",
            "EvidenceDocument": "documented_by",
            "Warranty": "covered_by",
            "Person": "has_contact",
            "PersonRole": "has_contact",
            "Project": "has_project",
        }
        return target_verbs.get(target_type, "contains")
    return "related_to"


def _evidence_citation(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, Search-addressable evidence locator."""
    return {
        "evidence_id": evidence.get("evidence_id"),
        "chunk_id": evidence.get("chunk_id"),
        "document_element_id": evidence.get("document_element_id"),
        "source_file_id": evidence.get("source_file_id"),
        "page_number": evidence.get("page_number"),
        "section_path": evidence.get("section_path"),
        "blob_url": evidence.get("blob_url"),
    }


def _supported_entity_evidence(
    entity: dict[str, Any], evidence_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Find exact-name evidence spans from the entity's source file.

    This deliberately errs on the conservative side.  A source-file match
    alone is lineage, not textual support, so it is insufficient for a claim.
    """
    evidence_by_id = {
        str(row.get("evidence_id")): row
        for row in evidence_rows
        if row.get("evidence_id")
    }
    explicit = [
        evidence_by_id[evidence_id]
        for evidence_id in entity.get("evidence_ids") or []
        if evidence_id in evidence_by_id
    ]
    if explicit:
        return explicit
    name = str(entity.get("display_name") or "").strip().lower()
    source_file_id = entity.get("source_file_id")
    if not name or not source_file_id:
        return []
    return [
        evidence for evidence in evidence_rows
        if evidence.get("source_file_id") == source_file_id
        and name in str(evidence.get("text") or "").lower()
    ][:20]


def _typed_fields(
    text: str, supported: bool, is_maintenance_event: bool
) -> tuple[str | None, str | None, str | None]:
    """Extract compact maintenance fields only when an evidence span supports it."""
    if not supported:
        return None, None, None
    status_match = _STATUS_RE.search(text)
    date_match = _DATE_RE.search(text)
    status = status_match.group(1).lower() if status_match else None
    event_date = date_match.group(1).replace("/", "-") if date_match else None
    return (text[:500] or None) if is_maintenance_event else None, status, event_date


def _claim_valid_from(text: str) -> tuple[datetime | None, str | None]:
    """Return a conservative UTC temporal value and its source precision."""
    match = _DATE_RE.search(text)
    if not match:
        return None, None
    token = match.group(1).replace("/", "-")
    try:
        if len(token) == 4:
            return datetime(int(token), 1, 1, tzinfo=timezone.utc), "year"
        return datetime.fromisoformat(token).replace(tzinfo=timezone.utc), "day"
    except ValueError:
        return None, None


def build_semantic_projection(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build deterministic serving entities, edges, claims, and evidence links.

    IDs for entities and relationships are deliberately preserved.  Claim IDs
    are deterministic from a supported relationship and evidence ID, allowing
    incremental reruns without duplicate assertions.
    """
    evidence_by_id = {
        str(row["evidence_id"]): row for row in evidence if row.get("evidence_id")
    }
    entities_by_id = {
        str(row["entity_id"]): row for row in entities if row.get("entity_id")
    }
    supported_by_entity = {
        entity_id: _supported_entity_evidence(entity, evidence)
        for entity_id, entity in entities_by_id.items()
    }

    semantic_entities: list[dict[str, Any]] = []
    for entity_id, entity in entities_by_id.items():
        semantic_metadata = _semantic_metadata(entity)
        if (
            semantic_metadata.get("semantic_contract_hash")
            and semantic_metadata.get("semantic_lane") != "authoritative"
        ):
            continue
        supporting = supported_by_entity[entity_id]
        support_text = " ".join(str(row.get("text") or "") for row in supporting)
        authoritative = (
            semantic_metadata.get("semantic_lane") == "authoritative"
            and bool(semantic_metadata.get("semantic_type_id"))
        )
        semantic_type = (
            str(entity.get("entity_type") or "")
            if authoritative
            else "__discovery__"
            + (_slug(entity.get("entity_type")) or "entity")
            if semantic_metadata.get("semantic_contract_hash")
            else canonical_entity_type(
                entity.get("entity_type"), entity.get("display_name")
            )
        )
        action, status, event_date = _typed_fields(
            support_text,
            bool(supporting),
            semantic_type in {"MaintenanceAction", "MaintenanceEvent"},
        )
        aliases = list(dict.fromkeys(
            [str(alias) for alias in (entity.get("aliases") or []) if alias]
            + [str(entity.get("display_name") or "")]
        ))
        citations = [_evidence_citation(row) for row in supporting]
        semantic_entities.append({
            **entity,
            "entity_type": semantic_type,
            "original_entity_type": (
                semantic_metadata.get("original_type")
                or entity.get("entity_type")
            ),
            "semantic_contract_hash": semantic_metadata.get(
                "semantic_contract_hash"
            ),
            "semantic_type_id": semantic_metadata.get("semantic_type_id"),
            "semantic_lane": semantic_metadata.get("semantic_lane"),
            "review_status": semantic_metadata.get("review_status"),
            "aliases_json": json.dumps(aliases, ensure_ascii=False),
            "action": action,
            "status": status,
            "event_date": event_date,
            "evidence_ids_json": json.dumps(
                [row["evidence_id"] for row in supporting], ensure_ascii=False
            ),
            "citation_json": json.dumps(citations, ensure_ascii=False),
        })
    semantic_type_by_id = {
        str(row["entity_id"]): str(row["entity_type"])
        for row in semantic_entities
    }

    semantic_relationships: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    claim_evidence: list[dict[str, Any]] = []
    for relationship in relationships:
        semantic_metadata = _semantic_metadata(relationship)
        source = entities_by_id.get(str(relationship.get("source_entity_id") or ""))
        target = entities_by_id.get(str(relationship.get("target_entity_id") or ""))
        if not source or not target:
            continue
        if (
            str(source["entity_id"]) not in semantic_type_by_id
            or str(target["entity_id"]) not in semantic_type_by_id
        ):
            continue
        source_type = semantic_type_by_id[str(source["entity_id"])]
        target_type = semantic_type_by_id[str(target["entity_id"])]
        authoritative = (
            semantic_metadata.get("semantic_lane") == "authoritative"
            and bool(semantic_metadata.get("semantic_relationship_id"))
        )
        processing_status = semantic_metadata.get("processing_status")
        if (
            semantic_metadata.get("semantic_contract_hash")
            and (
                not authoritative
                or processing_status in {
                    "discovery",
                    "unresolved",
                    "rejected",
                }
            )
        ):
            continue
        if authoritative:
            verb = str(relationship.get("relationship_type") or "")
        elif semantic_metadata.get("semantic_contract_hash"):
            verb = "discovery__" + (
                _slug(relationship.get("relationship_type")) or "related_to"
            )
        else:
            verb = canonical_verb(
                relationship.get("relationship_type"),
                source_type,
                target_type,
            )
            if (
                verb == "related_to"
                and source_type == "Equipment"
                and target_type == "MaintenanceAction"
            ):
                verb = "has_maintenance_action"
        explicit_ids = list(relationship.get("evidence_ids") or [])
        if relationship.get("evidence_id"):
            explicit_ids.insert(0, relationship["evidence_id"])
        explicit_rows = [
            evidence_by_id[evidence_id]
            for evidence_id in dict.fromkeys(explicit_ids)
            if evidence_id in evidence_by_id
        ]
        # A relationship is supported only by an evidence span that names both
        # endpoints.  This prevents source-file co-occurrence from becoming fact.
        candidates = explicit_rows or supported_by_entity.get(
            str(source["entity_id"]), []
        )
        target_name = str(target.get("display_name") or "").lower()
        supporting = [
            row for row in candidates
            if row and target_name
            and target_name in str(row.get("text") or "").lower()
        ][:1]
        traceable = supporting or explicit_rows[:1]
        if not traceable:
            continue
        evidence_id = traceable[0]["evidence_id"]
        citation_rows = explicit_rows or supporting
        citations = [_evidence_citation(row) for row in citation_rows]
        evidence_text = str(supporting[0].get("text") or "") if supporting else ""
        valid_from, temporal_precision = _claim_valid_from(evidence_text)
        assertion_status = (
            (
                semantic_metadata.get("assertion_status")
                or "asserted"
            )
            if supporting
            else "unverified"
        )
        semantic = {
            **relationship,
            "relationship_type": verb,
            "original_relationship_type": (
                semantic_metadata.get("original_relationship_type")
                or relationship.get("relationship_type")
            ),
            "semantic_contract_hash": semantic_metadata.get(
                "semantic_contract_hash"
            ),
            "semantic_relationship_id": semantic_metadata.get(
                "semantic_relationship_id"
            ),
            "semantic_lane": semantic_metadata.get("semantic_lane"),
            "review_status": semantic_metadata.get("review_status"),
            "evidence_id": evidence_id,
            "evidence_ids_json": json.dumps(
                [row["evidence_id"] for row in citation_rows],
                ensure_ascii=False,
            ),
            "citation_json": json.dumps(citations, ensure_ascii=False),
            "assertion_status": assertion_status,
            "event_date": valid_from.date().isoformat() if valid_from else None,
        }
        semantic_relationships.append(semantic)
        if not supporting or assertion_status in {"unresolved", "rejected"}:
            continue

        evidence_row = supporting[0]
        claim_id = make_id(
            "claim",
            f"{relationship.get('relationship_id')}:{verb}:{evidence_id}",
        )
        observed_at = relationship.get("created_at") or datetime.now(timezone.utc)
        claims.append({
            "claim_id": claim_id,
            "subject_entity_id": relationship["source_entity_id"],
            "predicate": verb,
            "object_entity_id": relationship["target_entity_id"],
            "value_json": json.dumps(
                {"temporal_precision": temporal_precision}
            ) if temporal_precision else None,
            "status": assertion_status,
            "confidence": relationship.get("confidence"),
            "valid_from": valid_from,
            "valid_to": None,
            "observed_at": observed_at,
            "summary": (
                f"{source.get('display_name')} {verb.replace('_', ' ')} "
                f"{target.get('display_name')}"
            ),
            "review_state": "not_reviewed",
            "project_id": relationship.get("project_id", ""),
            "asset_id": relationship.get("asset_id", ""),
            "asset_version_id": relationship.get("asset_version_id", ""),
            "run_id": relationship.get("run_id", ""),
            "parent_record_id": relationship.get("relationship_id"),
            "source_locator_json": json.dumps(_evidence_citation(evidence_row)),
            "schema_version": relationship.get("schema_version", ""),
            "domain_hash": relationship.get("domain_hash"),
        })
        claim_evidence.append({
            "claim_id": claim_id,
            "evidence_id": evidence_id,
            "occurrence_id": relationship.get("relationship_id"),
            "support_type": "supports",
            "confidence": relationship.get("confidence"),
        })

    return {
        "semantic_entities": semantic_entities,
        "semantic_relationships": semantic_relationships,
        "claims": claims,
        "claim_evidence": claim_evidence,
    }
