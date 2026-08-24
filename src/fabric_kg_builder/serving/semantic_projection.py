"""Deterministic maintenance semantic serving projection.

The canonical extraction tables remain immutable, domain-neutral observations.
This module creates a separate serving projection for Fabric Graph Model and
Ontology use.  It normalizes unstable extractor labels and relationship verbs,
keeps source lineage unchanged, and only creates a factual claim when both
endpoints occur together in an existing evidence span.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.model.ids import make_id
from fabric_kg_builder.semantic.canonical_hash import (
    canonical_hash as _schema2_hash,
    canonical_json as _schema2_canonical_json,
    canonical_row_hash as _schema2_row_hash,
    canonical_table_hash as _schema2_table_hash,
    canonicalize as _schema2_normalize,
)

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


def _build_schema1_semantic_projection(
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


_SCHEMA2_LIFECYCLE_RANK = {
    "asserted": 4,
    "unresolved": 3,
    "rejected": 2,
    "discovery": 1,
}
_SCHEMA2_TERMINALS = (
    "asserted",
    "unresolved",
    "rejected",
    "discovery",
    "deduplicated",
    "endpoint_unresolved",
    "endpoint_unpublished",
)
_LEGACY_UNVERIFIED_STATE = "LEGACY_UNVERIFIED_STATE"


@dataclass(frozen=True)
class SemanticProjectionResult(Mapping[str, Any]):
    """Typed schema-2 projection with dict-compatible legacy indexing."""

    semantic_entities: list[dict[str, Any]]
    semantic_relationships: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    claim_evidence: list[dict[str, Any]]
    audit_relationships: list[dict[str, Any]]
    receipt: dict[str, Any]

    def _mapping(self) -> dict[str, Any]:
        return {
            "semantic_entities": self.semantic_entities,
            "semantic_relationships": self.semantic_relationships,
            "claims": self.claims,
            "claim_evidence": self.claim_evidence,
            "audit_relationships": self.audit_relationships,
            "raw_audit_relationships": self.audit_relationships,
            "relationship_audit": self.audit_relationships,
            "receipt": self.receipt,
            "projection_receipt": self.receipt,
        }

    def __getitem__(self, key: str) -> Any:
        return self._mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())

    def as_dict(self) -> dict[str, Any]:
        """Return a shallow dict adapter for serialization-oriented callers."""
        return self._mapping()


def _schema2_json_metadata(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _schema2_has_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_schema2_has_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_schema2_has_nonfinite(item) for item in value)
    return False


def _schema2_field(
    row: Mapping[str, Any],
    metadata: Mapping[str, Any],
    name: str,
    *aliases: str,
) -> Any:
    for key in (name, *aliases):
        value = row.get(key)
        if value is not None:
            return value
    for key in (name, *aliases):
        value = metadata.get(key)
        if value is not None:
            return value
    return None


def _schema2_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        value = [value]
    return sorted({
        str(item).strip()
        for item in value
        if item is not None and str(item).strip()
    })


def _schema2_reasons(row: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[str]:
    return _schema2_strings(
        _schema2_field(
            row,
            metadata,
            "reason_codes",
            "rejection_reasons",
            "audit_reason_codes",
            "audit_reasons",
        )
    )


def _schema2_state(
    row: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str, str | None, list[str]]:
    raw = _schema2_field(
        row,
        metadata,
        "assertion_state",
        "assertion_status",
        "processing_status",
    )
    raw_state = str(raw or "").strip().casefold()
    lane = str(
        _schema2_field(row, metadata, "semantic_lane") or ""
    ).strip().casefold()
    processing = str(
        _schema2_field(row, metadata, "processing_status") or ""
    ).strip().casefold()
    reasons = _schema2_reasons(row, metadata)
    if raw_state == "unverified":
        return "unresolved", "unverified", sorted(
            set(reasons) | {_LEGACY_UNVERIFIED_STATE}
        )
    if lane == "discovery" or processing == "discovery":
        return "discovery", raw_state or None, reasons
    if raw_state in _SCHEMA2_LIFECYCLE_RANK:
        return raw_state, raw_state, reasons
    if processing == "accepted":
        return "asserted", raw_state or None, reasons
    if processing in {"unresolved", "rejected"}:
        return processing, raw_state or None, reasons
    return "unresolved", raw_state or None, sorted(
        set(reasons) | {"UNKNOWN_LIFECYCLE_STATE"}
    )


def _schema2_evidence_ids(
    row: Mapping[str, Any], metadata: Mapping[str, Any]
) -> list[str]:
    values: list[Any] = []
    for key in (
        "evidence_id",
        "verified_evidence_id",
        "evidence_ids",
        "evidence_id_hints",
    ):
        value = _schema2_field(row, metadata, key)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(value)
        elif value:
            values.append(value)
    return _schema2_strings(values)


def _schema2_verified_evidence(
    row: Mapping[str, Any],
    metadata: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    return [
        evidence_id
        for evidence_id in _schema2_evidence_ids(row, metadata)
        if evidence_id in evidence_by_id
        and evidence_by_id[evidence_id].get("runner_verified") is True
    ]


def _schema2_merge_set_field(
    rows: list[Mapping[str, Any]],
    field: str,
    *aliases: str,
) -> list[str]:
    values: list[Any] = []
    for row in rows:
        metadata = _schema2_json_metadata(row.get("properties_json")) or {}
        value = _schema2_field(row, metadata, field, *aliases)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(value)
        elif value:
            values.append(value)
    return _schema2_strings(values)


def _schema2_entity_approval(
    row: Mapping[str, Any], contract: DomainContractV2
) -> bool:
    approval = _schema2_json_metadata(row.get("proposal_approval_json")) or {}
    required_keys = (
        "proposal_hash",
        "source_profile_hash",
        "prompt_version",
        "model_version",
    )
    optional_keys = (
        "prompt_hash",
        "model_hash",
    )
    for key in (*required_keys, *optional_keys):
        row_value = row.get(key)
        if row_value is not None:
            approval[key] = row_value
    expected = {
        key: getattr(contract.approval, key)
        for key in (*required_keys, *optional_keys)
    }
    required_complete = all(
        expected[key]
        and approval.get(key) == expected[key]
        for key in required_keys
    )
    optional_consistent = all(
        key not in approval or approval[key] == expected[key]
        for key in optional_keys
    )
    return required_complete and optional_consistent


def _schema2_type_path(
    actual: str,
    allowed: list[str],
    *,
    parent_by_id: Mapping[str, str | None],
    allow_subtypes: bool,
) -> list[str] | None:
    if actual in allowed:
        return [actual]
    if not allow_subtypes:
        return None
    path = [actual]
    seen = {actual}
    cursor = parent_by_id.get(actual)
    while cursor is not None and cursor not in seen:
        path.append(cursor)
        if cursor in allowed:
            return path
        seen.add(cursor)
        cursor = parent_by_id.get(cursor)
    return None


def _schema2_entity_validation(
    row: Mapping[str, Any],
    *,
    contract: DomainContractV2,
    active_hash: str,
    entity_definitions: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    endpoint_evidence: set[str] | None = None,
) -> dict[str, Any]:
    raw_properties = row.get("properties_json")
    metadata = _schema2_json_metadata(raw_properties)
    values = metadata or {}
    raw_state = str(
        _schema2_field(row, values, "assertion_state", "assertion_status") or ""
    ).casefold()
    assertion_state = "unresolved" if raw_state == "unverified" else raw_state
    lane = str(_schema2_field(row, values, "semantic_lane") or "").casefold()
    review_status = str(
        _schema2_field(row, values, "review_status") or ""
    ).casefold()
    type_id = str(_schema2_field(row, values, "semantic_type_id") or "")
    definition = entity_definitions.get(type_id)
    contract_hash = str(
        _schema2_field(row, values, "semantic_contract_hash") or ""
    )
    reasons: list[str] = []
    entity_id = str(row.get("entity_id") or "")
    if not entity_id.strip():
        reasons.append("ENTITY_ID_INVALID")
    if not str(row.get("display_name") or "").strip():
        reasons.append("ENTITY_DISPLAY_NAME_INVALID")
    if not str(row.get("canonical_key") or "").strip():
        reasons.append("ENTITY_CANONICAL_KEY_INVALID")
    if raw_properties not in (None, "") and metadata is None:
        reasons.append("ENTITY_PROPERTIES_NOT_OBJECT")
    if assertion_state != "asserted":
        reasons.append("ENTITY_NOT_ASSERTED")
    if lane != "authoritative":
        reasons.append("ENTITY_NOT_AUTHORITATIVE")
    if review_status != "approved":
        reasons.append("ENTITY_TYPE_NOT_APPROVED")
    if definition is None:
        reasons.append("ENTITY_TYPE_UNAPPROVED")
    elif str(row.get("entity_type") or "") not in {
        definition.id,
        definition.name,
    }:
        reasons.append("ENTITY_TYPE_MISMATCH")
    if contract_hash != active_hash:
        reasons.append("STALE_CONTRACT_HASH")

    explicit_evidence_ids = _schema2_evidence_ids(row, values)
    source_file_id = str(row.get("source_file_id") or "")
    dangling_evidence_ids = sorted(
        evidence_id
        for evidence_id in explicit_evidence_ids
        if evidence_id not in evidence_by_id
    )
    unverified_evidence_ids = sorted(
        evidence_id
        for evidence_id in explicit_evidence_ids
        if evidence_id in evidence_by_id
        and evidence_by_id[evidence_id].get("runner_verified") is not True
    )
    source_mismatched_evidence_ids = sorted(
        evidence_id
        for evidence_id in explicit_evidence_ids
        if source_file_id
        and evidence_id in evidence_by_id
        and str(evidence_by_id[evidence_id].get("source_file_id") or "")
        != source_file_id
    )
    directly_verified = {
        evidence_id
        for evidence_id in explicit_evidence_ids
        if evidence_id in evidence_by_id
        and evidence_by_id[evidence_id].get("runner_verified") is True
        and (
            not source_file_id
            or str(evidence_by_id[evidence_id].get("source_file_id") or "")
            == source_file_id
        )
    }
    verified_endpoint_evidence = {
        evidence_id
        for evidence_id in (endpoint_evidence or set())
        if evidence_id in evidence_by_id
        and evidence_by_id[evidence_id].get("runner_verified") is True
        and (
            not source_file_id
            or str(evidence_by_id[evidence_id].get("source_file_id") or "")
            == source_file_id
        )
    }
    verified = sorted(directly_verified | verified_endpoint_evidence)
    if dangling_evidence_ids:
        reasons.append("ENTITY_EVIDENCE_DANGLING")
    if unverified_evidence_ids:
        reasons.append("ENTITY_EVIDENCE_UNVERIFIED")
    if source_mismatched_evidence_ids:
        reasons.append("ENTITY_EVIDENCE_SOURCE_MISMATCH")
    business_approved = bool(
        definition
        and definition.business_defined
        and _schema2_entity_approval(row, contract)
    )
    if not verified and not business_approved:
        if not explicit_evidence_ids:
            reasons.append("ENTITY_EVIDENCE_MISSING")
        elif not (
            dangling_evidence_ids
            or unverified_evidence_ids
            or source_mismatched_evidence_ids
        ):
            reasons.append("ENTITY_EVIDENCE_UNVERIFIED")
    return {
        "assertion_state": assertion_state,
        "lane": lane,
        "review_status": review_status,
        "type_id": type_id,
        "definition": definition,
        "contract_hash": contract_hash,
        "verified_evidence_ids": verified,
        "dangling_evidence_ids": dangling_evidence_ids,
        "unverified_evidence_ids": unverified_evidence_ids,
        "source_mismatched_evidence_ids": source_mismatched_evidence_ids,
        "business_approved": business_approved,
        "reasons": sorted(set(reasons)),
    }


def _schema2_relationship_occurrence_validation(
    item: Mapping[str, Any],
    *,
    contract: DomainContractV2,
    active_hash: str,
    relationship_definitions: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    entity_winners: Mapping[str, Mapping[str, Any]],
    entity_type_by_id: Mapping[str, str],
    published_entity_ids: set[str],
    parent_by_id: Mapping[str, str | None],
) -> dict[str, Any]:
    row = item["row"]
    metadata = item["metadata"]
    state = item["state"]
    relationship_id = item["relationship_id"]
    source_id = str(row.get("source_entity_id") or "")
    target_id = str(row.get("target_entity_id") or "")
    endpoint_unresolved = (
        not source_id
        or not target_id
        or source_id.startswith("unresolved-endpoint:")
        or target_id.startswith("unresolved-endpoint:")
        or source_id not in entity_winners
        or target_id not in entity_winners
    )
    endpoint_unpublished = (
        not endpoint_unresolved
        and (
            source_id not in published_entity_ids
            or target_id not in published_entity_ids
        )
    )
    definition_id = str(
        _schema2_field(row, metadata, "semantic_relationship_id") or ""
    )
    definition = relationship_definitions.get(definition_id)
    primary_evidence_id = str(
        _schema2_field(row, metadata, "evidence_id", "verified_evidence_id") or ""
    )
    verified_ids = _schema2_verified_evidence(row, metadata, evidence_by_id)
    reasons: list[str] = []
    if state == "asserted":
        if not relationship_id:
            reasons.append("RELATIONSHIP_ID_INVALID")
        if (
            str(_schema2_field(row, metadata, "semantic_contract_hash") or "")
            != active_hash
        ):
            reasons.append("STALE_CONTRACT_HASH")
        if definition is None:
            reasons.append("UNAPPROVED_RELATION")
        elif str(row.get("relationship_type") or "") != definition.predicate:
            reasons.append("UNAPPROVED_PREDICATE")
        if (
            str(_schema2_field(row, metadata, "validation_authority") or "")
            != "schema2"
        ):
            reasons.append("INVALID_VALIDATION_AUTHORITY")
        if not primary_evidence_id:
            reasons.append("EVIDENCE_MISSING")
        elif primary_evidence_id not in evidence_by_id:
            reasons.append("EVIDENCE_DANGLING")
        elif evidence_by_id[primary_evidence_id].get("runner_verified") is not True:
            reasons.append("EVIDENCE_NOT_RUNNER_VERIFIED")
        verified_hint = str(
            _schema2_field(row, metadata, "verified_evidence_id") or ""
        )
        if verified_hint and verified_hint != primary_evidence_id:
            reasons.append("EVIDENCE_FK_MISMATCH")
        if endpoint_unresolved:
            reasons.append("ENDPOINT_UNRESOLVED")
        elif endpoint_unpublished:
            reasons.append("ENDPOINT_UNPUBLISHED")

        if definition is not None and not endpoint_unresolved:
            source_row = entity_winners[source_id]
            target_row = entity_winners[target_id]
            source_metadata = (
                _schema2_json_metadata(source_row.get("properties_json")) or {}
            )
            target_metadata = (
                _schema2_json_metadata(target_row.get("properties_json")) or {}
            )
            source_type = str(
                _schema2_field(row, metadata, "resolved_source_type_id")
                or entity_type_by_id.get(source_id)
                or _schema2_field(
                    source_row, source_metadata, "semantic_type_id"
                )
                or ""
            )
            target_type = str(
                _schema2_field(row, metadata, "resolved_target_type_id")
                or entity_type_by_id.get(target_id)
                or _schema2_field(
                    target_row, target_metadata, "semantic_type_id"
                )
                or ""
            )
            source_actual = str(
                _schema2_field(
                    source_row, source_metadata, "semantic_type_id"
                )
                or source_type
            )
            target_actual = str(
                _schema2_field(
                    target_row, target_metadata, "semantic_type_id"
                )
                or target_type
            )
            allow_subtypes = (
                definition.endpoint_policy == "allow_subtypes"
                and contract.extraction_policy.allow_subtype_endpoints
            )
            source_path = _schema2_type_path(
                source_actual,
                definition.source_types,
                parent_by_id=parent_by_id,
                allow_subtypes=allow_subtypes,
            )
            target_path = _schema2_type_path(
                target_actual,
                definition.target_types,
                parent_by_id=parent_by_id,
                allow_subtypes=allow_subtypes,
            )
            recorded_source_path = _schema2_field(
                row, metadata, "source_inheritance_path"
            )
            recorded_target_path = _schema2_field(
                row, metadata, "target_inheritance_path"
            )
            if (
                source_path is None
                or target_path is None
                or source_type not in definition.source_types
                or target_type not in definition.target_types
                or (
                    recorded_source_path
                    and list(recorded_source_path) != source_path
                )
                or (
                    recorded_target_path
                    and list(recorded_target_path) != target_path
                )
            ):
                reasons.append("INCOMPATIBLE_ENDPOINT")
        direction = str(
            _schema2_field(row, metadata, "direction") or ""
        ).casefold()
        if direction not in {"forward", "source_to_target"}:
            reasons.append("INCOMPATIBLE_DIRECTION")
    return {
        "source_id": source_id,
        "target_id": target_id,
        "endpoint_unresolved": endpoint_unresolved,
        "endpoint_unpublished": endpoint_unpublished,
        "definition": definition,
        "primary_evidence_id": primary_evidence_id,
        "verified_evidence_ids": verified_ids,
        "generated_reasons": sorted(set(reasons)),
    }


def _schema2_relationship_gate(reason: str) -> str:
    if reason.startswith("EVIDENCE_"):
        return "SEM-102"
    if reason in {
        "ENDPOINT_UNRESOLVED",
        "ENDPOINT_UNPUBLISHED",
        "INCOMPATIBLE_ENDPOINT",
    }:
        return "SEM-103"
    return "SEM-101"


def _schema2_projection(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    contract: DomainContractV2,
) -> SemanticProjectionResult:
    computed_hash = compute_contract_hash(contract)
    active_hash = contract.approval.contract_hash or computed_hash
    contract_violations: list[str] = []
    if contract.approval.status != "approved":
        contract_violations.append("CONTRACT_NOT_APPROVED")
    if contract.approval.contract_hash != computed_hash:
        contract_violations.append("ACTIVE_CONTRACT_HASH_INVALID")

    finite_violations = sorted(
        table
        for table, rows in (
            ("entities", entities),
            ("relationships", relationships),
            ("evidence", evidence),
        )
        if _schema2_has_nonfinite(rows)
    )

    evidence_occurrences = sorted(
        (
            (_schema2_row_hash(row), row)
            for row in evidence
        ),
        key=lambda item: (
            item[0],
            _schema2_canonical_json(item[1]),
        ),
    )
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for _, row in evidence_occurrences:
        evidence_id = str(row.get("evidence_id") or "")
        if not evidence_id:
            continue
        existing = evidence_by_id.get(evidence_id)
        if existing is None or (
            row.get("runner_verified") is True,
            _schema2_row_hash(row),
        ) > (
            existing.get("runner_verified") is True,
            _schema2_row_hash(existing),
        ):
            evidence_by_id[evidence_id] = row

    entity_definitions = {
        definition.id: definition
        for definition in contract.candidate_model.entity_types
    }
    parent_by_id = {
        definition.id: definition.parent
        for definition in contract.candidate_model.entity_types
    }
    relationship_definitions = {
        definition.id: definition
        for definition in contract.candidate_model.relationship_types
    }
    entity_occurrences: list[dict[str, Any]] = []
    for row in entities:
        metadata = _schema2_json_metadata(row.get("properties_json")) or {}
        raw_state = str(
            _schema2_field(
                row, metadata, "assertion_state", "assertion_status"
            )
            or ""
        ).casefold()
        state = "unresolved" if raw_state == "unverified" else raw_state
        lane = str(
            _schema2_field(row, metadata, "semantic_lane") or ""
        ).casefold()
        review = str(
            _schema2_field(row, metadata, "review_status") or ""
        ).casefold()
        type_id = str(
            _schema2_field(row, metadata, "semantic_type_id") or ""
        )
        contract_hash = str(
            _schema2_field(row, metadata, "semantic_contract_hash") or ""
        )
        evidence_ids = _schema2_evidence_ids(row, metadata)
        definition = entity_definitions.get(type_id)
        approval = (
            _schema2_json_metadata(row.get("proposal_approval_json")) or {}
        )
        for approval_key in (
            "proposal_hash",
            "source_profile_hash",
            "prompt_hash",
            "prompt_version",
            "model_version",
            "model_hash",
        ):
            if row.get(approval_key) is not None:
                approval[approval_key] = row[approval_key]
        authority = {
            "assertion_state": state,
            "semantic_lane": lane,
            "review_status": review,
            "semantic_type_id": type_id,
            "semantic_contract_hash": contract_hash,
            "canonical_key": str(row.get("canonical_key") or ""),
            "business_defined": bool(
                definition and definition.business_defined
            ),
            "business_defined_approval": (
                approval
                if definition is not None and definition.business_defined
                else None
            ),
        }
        direct_verified = bool(
            _schema2_verified_evidence(row, metadata, evidence_by_id)
        )
        business_approved = bool(
            definition
            and definition.business_defined
            and _schema2_entity_approval(row, contract)
        )
        entity_occurrences.append({
            "row": row,
            "metadata": metadata,
            "entity_id": str(row.get("entity_id") or ""),
            "row_hash": _schema2_row_hash(row),
            "evidence_ids": evidence_ids,
            "authority": authority,
            "authority_hash": _schema2_hash(authority),
            "selection_key": (
                -int(state == "asserted"),
                -int(lane == "authoritative"),
                -int(review == "approved"),
                -int(definition is not None),
                -int(contract_hash == active_hash),
                -int(direct_verified or business_approved),
                _schema2_row_hash(row),
            ),
        })
    entity_occurrences.sort(
        key=lambda item: (
            item["entity_id"],
            item["row_hash"],
            _schema2_canonical_json(item["row"]),
        )
    )
    entity_ordinal_by_key: Counter[tuple[str, str]] = Counter()
    for item in entity_occurrences:
        ordinal_key = (item["entity_id"], item["row_hash"])
        item["ordinal"] = entity_ordinal_by_key[ordinal_key]
        entity_ordinal_by_key[ordinal_key] += 1
        item["occurrence_key"] = (
            f"{item['entity_id'] or '<missing>'}:"
            f"{item['row_hash']}:{item['ordinal']}"
        )

    entity_groups: dict[str, list[dict[str, Any]]] = {}
    for item in entity_occurrences:
        group_key = item["entity_id"] or item["occurrence_key"]
        entity_groups.setdefault(group_key, []).append(item)

    entity_winners: dict[str, dict[str, Any]] = {}
    entity_winner_item_by_id: dict[str, dict[str, Any]] = {}
    entity_conflict_violations: list[str] = []
    entity_winner_key_by_occurrence: dict[str, str] = {}
    for group_key, items in sorted(entity_groups.items()):
        ordered = sorted(items, key=lambda item: item["selection_key"])
        winner_item = ordered[0]
        entity_id = winner_item["entity_id"]
        rows = [item["row"] for item in items]
        merged = dict(winner_item["row"])
        aliases = _schema2_merge_set_field(rows, "aliases", "search_aliases")
        evidence_ids = sorted({
            evidence_id
            for item in items
            for evidence_id in item["evidence_ids"]
        })
        reasons = _schema2_merge_set_field(
            rows, "audit_reason_codes", "audit_reasons", "reason_codes"
        )
        if aliases:
            merged["aliases"] = aliases
        if evidence_ids:
            merged["evidence_ids"] = evidence_ids
        if reasons:
            merged["audit_reason_codes"] = reasons
        if entity_id:
            entity_winners[entity_id] = merged
            entity_winner_item_by_id[entity_id] = winner_item
        for item in items:
            entity_winner_key_by_occurrence[item["occurrence_key"]] = (
                winner_item["occurrence_key"]
            )
            item["merged_evidence_ids"] = evidence_ids
        authority_hashes = {item["authority_hash"] for item in items}
        if len(authority_hashes) > 1:
            for item in items:
                item["authority_conflict"] = True
            occurrence_keys = ",".join(
                sorted(item["occurrence_key"] for item in items)
            )
            entity_conflict_violations.append(
                f"{group_key}:ENTITY_AUTHORITY_CONFLICT:{occurrence_keys}"
            )

    endpoint_evidence_by_entity: dict[str, set[str]] = {}
    for relationship in relationships:
        metadata = (
            _schema2_json_metadata(relationship.get("properties_json")) or {}
        )
        state, _, _ = _schema2_state(relationship, metadata)
        definition_id = str(
            _schema2_field(
                relationship, metadata, "semantic_relationship_id"
            )
            or ""
        )
        definition = relationship_definitions.get(definition_id)
        if (
            state != "asserted"
            or definition is None
            or relationship.get("relationship_type") != definition.predicate
            or _schema2_field(
                relationship, metadata, "semantic_contract_hash"
            )
            != active_hash
            or _schema2_field(
                relationship, metadata, "validation_authority"
            )
            != "schema2"
        ):
            continue
        verified = _schema2_verified_evidence(
            relationship, metadata, evidence_by_id
        )
        for endpoint_key in ("source_entity_id", "target_entity_id"):
            endpoint_id = str(relationship.get(endpoint_key) or "")
            if endpoint_id and verified:
                endpoint_evidence_by_entity.setdefault(
                    endpoint_id, set()
                ).update(verified)

    candidate_entities: list[dict[str, Any]] = []
    entity_hard_violations: list[str] = list(entity_conflict_violations)
    entity_reconciliation: list[dict[str, Any]] = []
    entity_type_by_id: dict[str, str] = {}
    for item in entity_occurrences:
        validation = _schema2_entity_validation(
            item["row"],
            contract=contract,
            active_hash=active_hash,
            entity_definitions=entity_definitions,
            evidence_by_id=evidence_by_id,
            endpoint_evidence=endpoint_evidence_by_entity.get(
                item["entity_id"], set()
            ),
        )
        item["validation"] = validation
        if validation["assertion_state"] == "asserted" and validation["reasons"]:
            entity_hard_violations.extend(
                f"{item['occurrence_key']}:{reason}"
                for reason in validation["reasons"]
            )

    verified_evidence_by_entity: dict[str, list[str]] = {}
    for entity_id, items in sorted(entity_groups.items()):
        verified_evidence_by_entity[entity_id] = sorted({
            evidence_id
            for item in items
            for evidence_id in item["validation"]["verified_evidence_ids"]
        })

    for item in entity_occurrences:
        validation = item["validation"]
        selected = (
            entity_winner_key_by_occurrence[item["occurrence_key"]]
            == item["occurrence_key"]
        )
        reasons = set(validation["reasons"])
        if item.get("authority_conflict"):
            reasons.add("ENTITY_AUTHORITY_CONFLICT")
        if not selected:
            reasons.add("ENTITY_OCCURRENCE_DEDUPLICATED")
        entity_reconciliation.append({
            "occurrence_key": item["occurrence_key"],
            "entity_id": item["entity_id"],
            "canonical_row_hash": item["row_hash"],
            "authority_hash": item["authority_hash"],
            "evidence_ids": item["evidence_ids"],
            "merged_evidence_ids": item["merged_evidence_ids"],
            "verified_evidence_ids": validation["verified_evidence_ids"],
            "merged_verified_evidence_ids": verified_evidence_by_entity.get(
                item["entity_id"], []
            ),
            "source_mismatched_evidence_ids": validation[
                "source_mismatched_evidence_ids"
            ],
            "winner_occurrence_key": entity_winner_key_by_occurrence[
                item["occurrence_key"]
            ],
            "selected": selected,
            "bucket": "selected" if selected else "deduplicated",
            "reason_codes": sorted(reasons),
        })
    entity_reconciliation.sort(key=lambda row: row["occurrence_key"])

    for entity_id, row in sorted(entity_winners.items()):
        validation = entity_winner_item_by_id[entity_id]["validation"]
        if validation["reasons"]:
            continue
        definition = validation["definition"]
        verified = verified_evidence_by_entity.get(entity_id, [])
        supporting = [evidence_by_id[item] for item in verified]
        aliases = _schema2_strings(
            list(row.get("aliases") or [])
            + [str(row.get("display_name") or "")]
        )
        projected = {
            **row,
            "entity_type": definition.name,
            "assertion_state": "asserted",
            "semantic_type_id": definition.id,
            "semantic_lane": "authoritative",
            "review_status": "approved",
            "semantic_contract_hash": active_hash,
            "aliases": aliases,
            "aliases_json": _schema2_canonical_json(aliases),
            "evidence_ids": verified,
            "evidence_ids_json": _schema2_canonical_json(verified),
            "citation_json": _schema2_canonical_json(
                [_evidence_citation(item) for item in supporting]
            ),
        }
        candidate_entities.append(projected)
        entity_type_by_id[entity_id] = definition.id

    candidate_entities.sort(key=lambda row: str(row.get("entity_id") or ""))
    published_entity_ids = {
        str(row["entity_id"]) for row in candidate_entities
    }

    occurrence_rows: list[dict[str, Any]] = []
    for row in relationships:
        metadata = _schema2_json_metadata(row.get("properties_json")) or {}
        state, original_state, reasons = _schema2_state(row, metadata)
        occurrence_rows.append({
            "row": row,
            "metadata": metadata,
            "row_hash": _schema2_row_hash(row),
            "relationship_id": str(row.get("relationship_id") or ""),
            "state": state,
            "original_state": original_state,
            "reasons": reasons,
            "verified": bool(
                _schema2_verified_evidence(row, metadata, evidence_by_id)
            ),
        })

    occurrence_rows.sort(
        key=lambda item: (
            item["relationship_id"],
            item["row_hash"],
            _schema2_canonical_json(item["row"]),
        )
    )
    ordinal_by_key: Counter[tuple[str, str]] = Counter()
    for item in occurrence_rows:
        key = (item["relationship_id"], item["row_hash"])
        item["ordinal"] = ordinal_by_key[key]
        ordinal_by_key[key] += 1
        item["occurrence_key"] = (
            f"{item['relationship_id'] or '<missing>'}:"
            f"{item['row_hash']}:{item['ordinal']}"
        )

    relationship_groups: dict[str, list[dict[str, Any]]] = {}
    for item in occurrence_rows:
        group_id = item["relationship_id"]
        relationship_groups.setdefault(group_id, []).append(item)

    winners: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    losers: list[dict[str, Any]] = []
    for _, group in sorted(relationship_groups.items()):
        ordered = sorted(
            group,
            key=lambda item: (
                -_SCHEMA2_LIFECYCLE_RANK[item["state"]],
                -int(item["verified"]),
                item["row_hash"],
                item["ordinal"],
            ),
        )
        winners.append((ordered[0], ordered))
        losers.extend(ordered[1:])

    audit_relationships: list[dict[str, Any]] = []
    candidate_relationships: list[dict[str, Any]] = []
    candidate_claims: list[dict[str, Any]] = []
    candidate_claim_evidence: list[dict[str, Any]] = []
    relationship_gate_violations: dict[str, list[str]] = {
        "SEM-101": [],
        "SEM-102": [],
        "SEM-103": [],
    }
    reconciliation: list[dict[str, Any]] = []
    terminal_by_occurrence: dict[str, str] = {
        item["occurrence_key"]: "deduplicated" for item in losers
    }
    winner_hash_by_occurrence: dict[str, str] = {}
    for item in occurrence_rows:
        validation = _schema2_relationship_occurrence_validation(
            item,
            contract=contract,
            active_hash=active_hash,
            relationship_definitions=relationship_definitions,
            evidence_by_id=evidence_by_id,
            entity_winners=entity_winners,
            entity_type_by_id=entity_type_by_id,
            published_entity_ids=published_entity_ids,
            parent_by_id=parent_by_id,
        )
        item.update(validation)
        item["reasons"] = sorted(
            set(item["reasons"]) | set(validation["generated_reasons"])
        )
        if item["state"] == "asserted":
            for reason in validation["generated_reasons"]:
                gate = _schema2_relationship_gate(reason)
                relationship_gate_violations[gate].append(
                    f"{item['occurrence_key']}:{reason}"
                )

    for winner, group in winners:
        row = winner["row"]
        metadata = winner["metadata"]
        state = winner["state"]
        relationship_id = winner["relationship_id"]
        source_id = winner["source_id"]
        target_id = winner["target_id"]
        endpoint_unresolved = winner["endpoint_unresolved"]
        endpoint_unpublished = winner["endpoint_unpublished"]
        lane = str(
            _schema2_field(row, metadata, "semantic_lane") or ""
        ).casefold()
        if state == "discovery" or lane == "discovery":
            terminal = "discovery"
        elif endpoint_unresolved:
            terminal = "endpoint_unresolved"
        elif state == "unresolved":
            terminal = "unresolved"
        elif state == "rejected":
            terminal = "rejected"
        elif endpoint_unpublished:
            terminal = "endpoint_unpublished"
        else:
            terminal = "asserted"
        terminal_by_occurrence[winner["occurrence_key"]] = terminal
        for item in group:
            winner_hash_by_occurrence[item["occurrence_key"]] = winner["row_hash"]

        merged_evidence = sorted({
            item_id
            for item in group
            for item_id in _schema2_evidence_ids(
                item["row"], item["metadata"]
            )
        })
        merged_source_spans = sorted({
            item_id
            for item in group
            for item_id in _schema2_strings(
                _schema2_field(
                    item["row"],
                    item["metadata"],
                    "source_span_ids",
                )
            )
        })
        merged_reasons = sorted({
            reason
            for item in group
            for reason in item["reasons"]
        })
        if terminal == "endpoint_unresolved":
            merged_reasons = sorted(
                set(merged_reasons) | {"ENDPOINT_UNRESOLVED"}
            )
        elif terminal == "endpoint_unpublished":
            merged_reasons = sorted(
                set(merged_reasons) | {"ENDPOINT_UNPUBLISHED"}
            )

        audit = {
            **row,
            "assertion_state": state,
            "assertion_status": state,
            "original_assertion_state": winner["original_state"],
            "reason_codes": merged_reasons,
            "evidence_ids": merged_evidence,
            "source_span_ids": merged_source_spans,
            "terminal_bucket": terminal,
            "canonical_row_hash": winner["row_hash"],
            "deduplicated_occurrence_count": len(group) - 1,
        }

        asserted_reasons = winner["generated_reasons"]
        definition = winner["definition"]
        primary_evidence_id = winner["primary_evidence_id"]
        verified_ids = [
            item
            for item in merged_evidence
            if item in evidence_by_id
            and evidence_by_id[item].get("runner_verified") is True
        ]
        if asserted_reasons:
            audit["reason_codes"] = sorted(
                set(audit["reason_codes"]) | set(asserted_reasons)
            )
        audit_relationships.append(audit)

        valid_asserted = (
            terminal == "asserted"
            and state == "asserted"
            and not asserted_reasons
            and definition is not None
            and primary_evidence_id in verified_ids
        )
        if not valid_asserted:
            continue

        evidence_row = evidence_by_id[primary_evidence_id]
        citations = [
            _evidence_citation(evidence_by_id[item])
            for item in verified_ids
        ]
        semantic = {
            **audit,
            "relationship_type": definition.predicate,
            "semantic_relationship_id": definition.id,
            "semantic_contract_hash": active_hash,
            "semantic_lane": "authoritative",
            "review_status": "approved",
            "evidence_id": primary_evidence_id,
            "evidence_ids_json": _schema2_canonical_json(verified_ids),
            "citation_json": _schema2_canonical_json(citations),
        }
        candidate_relationships.append(semantic)
        claim_id = make_id(
            "claim",
            f"{relationship_id}:{definition.predicate}:{primary_evidence_id}",
        )
        candidate_claims.append({
            "claim_id": claim_id,
            "subject_entity_id": source_id,
            "predicate": definition.predicate,
            "object_entity_id": target_id,
            "value_json": None,
            "status": "asserted",
            "confidence": row.get("confidence"),
            "valid_from": row.get("valid_from"),
            "valid_to": row.get("valid_to"),
            "observed_at": row.get("created_at"),
            "summary": (
                f"{entity_winners[source_id].get('display_name')} "
                f"{definition.predicate.replace('_', ' ')} "
                f"{entity_winners[target_id].get('display_name')}"
            ),
            "review_state": "not_reviewed",
            "project_id": row.get("project_id", ""),
            "asset_id": row.get("asset_id", ""),
            "asset_version_id": row.get("asset_version_id", ""),
            "run_id": row.get("run_id", ""),
            "parent_record_id": relationship_id,
            "source_locator_json": _schema2_canonical_json(
                _evidence_citation(evidence_row)
            ),
            "schema_version": row.get("schema_version", "2.0"),
            "domain_hash": active_hash,
        })
        candidate_claim_evidence.extend(
            {
                "claim_id": claim_id,
                "evidence_id": evidence_id,
                "occurrence_id": relationship_id,
                "support_type": "supports",
                "confidence": row.get("confidence"),
            }
            for evidence_id in verified_ids
        )

    audit_relationships.sort(
        key=lambda row: (
            str(row.get("relationship_id") or ""),
            str(row.get("canonical_row_hash") or ""),
        )
    )
    candidate_relationships.sort(
        key=lambda row: str(row.get("relationship_id") or "")
    )
    candidate_claims.sort(key=lambda row: str(row.get("claim_id") or ""))
    candidate_claim_evidence.sort(
        key=lambda row: (
            str(row.get("claim_id") or ""),
            str(row.get("evidence_id") or ""),
        )
    )

    winner_keys = {
        winner["occurrence_key"] for winner, _ in winners
    }
    for item in occurrence_rows:
        reasons = set(item["reasons"])
        bucket = terminal_by_occurrence[item["occurrence_key"]]
        if bucket == "deduplicated":
            reasons.add("DEDUPLICATED_OCCURRENCE")
        elif bucket == "endpoint_unresolved":
            reasons.add("ENDPOINT_UNRESOLVED")
        elif bucket == "endpoint_unpublished":
            reasons.add("ENDPOINT_UNPUBLISHED")
        reconciliation.append({
            "occurrence_key": item["occurrence_key"],
            "relationship_id": item["relationship_id"],
            "canonical_row_hash": item["row_hash"],
            "winner_canonical_row_hash": winner_hash_by_occurrence[
                item["occurrence_key"]
            ],
            "normalized_lifecycle_state": item["state"],
            "original_lifecycle_state": item["original_state"],
            "bucket": bucket,
            "winner": item["occurrence_key"] in winner_keys,
            "reason_codes": sorted(reasons),
        })
    reconciliation.sort(key=lambda row: row["occurrence_key"])

    terminal_counts = {
        terminal: 0 for terminal in _SCHEMA2_TERMINALS
    }
    for record in reconciliation:
        terminal_counts[record["bucket"]] += 1
    lifecycle_counts = {
        lifecycle: 0 for lifecycle in _SCHEMA2_LIFECYCLE_RANK
    }
    for item in occurrence_rows:
        lifecycle_counts[item["state"]] += 1
    reason_counts = dict(sorted(Counter(
        reason
        for record in reconciliation
        for reason in record["reason_codes"]
    ).items()))

    reconciliation_violations: list[str] = []
    if len(reconciliation) != len(relationships):
        reconciliation_violations.append("RECONCILIATION_RECORD_COUNT_MISMATCH")
    if sum(terminal_counts.values()) != len(relationships):
        reconciliation_violations.append("TERMINAL_COUNT_MISMATCH")
    if any(
        record["bucket"] not in _SCHEMA2_TERMINALS
        for record in reconciliation
    ):
        reconciliation_violations.append("UNKNOWN_TERMINAL_BUCKET")

    serving_violations: list[str] = []
    serving_evidence_violations: list[str] = []
    serving_state_violations: list[str] = []
    for row in candidate_relationships:
        if row.get("assertion_state") != "asserted":
            serving_state_violations.append(
                f"{row.get('relationship_id')}:SERVING_STATE_NOT_ASSERTED"
            )
        if (
            row.get("source_entity_id") not in published_entity_ids
            or row.get("target_entity_id") not in published_entity_ids
        ):
            serving_violations.append(
                f"{row.get('relationship_id')}:SERVING_ENDPOINT_MISSING"
            )
        if row.get("evidence_id") not in evidence_by_id:
            serving_evidence_violations.append(
                f"{row.get('relationship_id')}:SERVING_EVIDENCE_MISSING"
            )

    sem100_violations = sorted(
        set(contract_violations + entity_hard_violations)
    )
    sem101_violations = sorted(set(
        relationship_gate_violations["SEM-101"]
        + serving_state_violations
    ))
    sem102_violations = sorted(set(
        relationship_gate_violations["SEM-102"]
        + serving_evidence_violations
    ))
    sem103_violations = sorted(set(
        relationship_gate_violations["SEM-103"]
        + serving_violations
    ))
    sem104_violations = sorted(set(
        reconciliation_violations
        + [
            f"{table}:NON_FINITE_CANONICAL_VALUE"
            for table in finite_violations
        ]
    ))
    invariant_results = [
        {
            "id": "SEM-100",
            "gate": "SEM-100",
            "passed": not sem100_violations,
            "violations": sem100_violations,
            "details": sem100_violations,
        },
        {
            "id": "SEM-101",
            "gate": "SEM-101",
            "passed": not sem101_violations,
            "violations": sem101_violations,
            "details": sem101_violations,
        },
        {
            "id": "SEM-102",
            "gate": "SEM-102",
            "passed": not sem102_violations,
            "violations": sem102_violations,
            "details": sem102_violations,
        },
        {
            "id": "SEM-103",
            "gate": "SEM-103",
            "passed": not sem103_violations,
            "violations": sem103_violations,
            "details": sem103_violations,
        },
        {
            "id": "SEM-104",
            "gate": "SEM-104",
            "passed": not sem104_violations,
            "violations": sem104_violations,
            "details": sem104_violations,
        },
    ]
    failed = any(not item["passed"] for item in invariant_results)

    final_entities = [] if failed else candidate_entities
    final_relationships = [] if failed else candidate_relationships
    final_claims = [] if failed else candidate_claims
    final_claim_evidence = [] if failed else candidate_claim_evidence

    aggregate_hashes = {
        "input_entities": _schema2_table_hash(entities, "entity_id"),
        "input_relationships": _schema2_table_hash(
            relationships, "relationship_id"
        ),
        "input_evidence": _schema2_table_hash(evidence, "evidence_id"),
        "audit_relationships": _schema2_table_hash(
            audit_relationships, "relationship_id"
        ),
        "candidate_semantic_entities": _schema2_table_hash(
            candidate_entities, "entity_id"
        ),
        "candidate_semantic_relationships": _schema2_table_hash(
            candidate_relationships, "relationship_id"
        ),
        "candidate_claims": _schema2_table_hash(
            candidate_claims, "claim_id"
        ),
        "candidate_claim_evidence": _schema2_table_hash(
            candidate_claim_evidence, "claim_id", "evidence_id"
        ),
        "semantic_entities": _schema2_table_hash(
            final_entities, "entity_id"
        ),
        "semantic_relationships": _schema2_table_hash(
            final_relationships, "relationship_id"
        ),
        "claims": _schema2_table_hash(final_claims, "claim_id"),
        "claim_evidence": _schema2_table_hash(
            final_claim_evidence, "claim_id", "evidence_id"
        ),
    }
    receipt = {
        "receipt_schema_version": "1.0",
        "schema": "2.0",
        "schema_version": "2.0",
        "schema_mode": "schema2",
        "mode": "strict",
        "status": "failed" if failed else "succeeded",
        "active_contract_hash": active_hash,
        "active_hash": active_hash,
        "input_candidate_count": len(relationships),
        "input_counts": {
            "entity_occurrences": len(entities),
            "relationship_occurrences": len(relationships),
            "evidence_occurrences": len(evidence),
        },
        "lifecycle_counts": lifecycle_counts,
        "terminal_counts": terminal_counts,
        "reason_counts": reason_counts,
        "dedup_counts": {
            "relationship_groups": len(winners),
            "deduplicated_occurrences": len(losers),
        },
        "entity_reconciliation_counts": {
            "input_occurrences": len(entity_occurrences),
            "entity_groups": len(entity_groups),
            "selected_occurrences": sum(
                record["selected"] for record in entity_reconciliation
            ),
            "deduplicated_occurrences": sum(
                not record["selected"] for record in entity_reconciliation
            ),
            "authority_conflicts": len(entity_conflict_violations),
        },
        "entity_reason_counts": dict(sorted(Counter(
            reason
            for record in entity_reconciliation
            for reason in record["reason_codes"]
        ).items())),
        "entity_reconciliation_policy": [
            "asserted",
            "authoritative",
            "approved_review",
            "approved_type",
            "active_contract_hash",
            "verified_evidence_or_complete_business_approval",
            "canonical_row_hash",
        ],
        "endpoint_counts": {
            "unresolved": terminal_counts["endpoint_unresolved"],
            "unpublished": terminal_counts["endpoint_unpublished"],
            "published": terminal_counts["asserted"],
        },
        "serving_counts": {
            "semantic_entities": len(final_entities),
            "semantic_relationships": len(final_relationships),
            "claims": len(final_claims),
            "claim_evidence": len(final_claim_evidence),
        },
        "candidate_serving_counts": {
            "semantic_entities": len(candidate_entities),
            "semantic_relationships": len(candidate_relationships),
            "claims": len(candidate_claims),
            "claim_evidence": len(candidate_claim_evidence),
        },
        "reconciliation": reconciliation,
        "reconciliation_records": reconciliation,
        "occurrence_reconciliation": reconciliation,
        "entity_reconciliation_records": entity_reconciliation,
        "canonical_row_hashes": {
            "entities": sorted(_schema2_row_hash(row) for row in entities),
            "relationships": [
                {
                    "occurrence_key": item["occurrence_key"],
                    "hash": item["row_hash"],
                }
                for item in occurrence_rows
            ],
            "evidence": sorted(_schema2_row_hash(row) for row in evidence),
            "audit_relationships": [
                {
                    "relationship_id": row.get("relationship_id"),
                    "hash": row.get("canonical_row_hash"),
                }
                for row in audit_relationships
            ],
        },
        "aggregate_table_hashes": aggregate_hashes,
        "invariants": [
            {
                "gate": item["gate"],
                "passed": item["passed"],
                "details": item["details"],
            }
            for item in invariant_results
        ],
        "invariant_results": {
            item["id"]: {
                "passed": item["passed"],
                "violations": item["violations"],
            }
            for item in invariant_results
        },
    }
    return SemanticProjectionResult(
        semantic_entities=final_entities,
        semantic_relationships=final_relationships,
        claims=final_claims,
        claim_evidence=final_claim_evidence,
        audit_relationships=audit_relationships,
        receipt=receipt,
    )


def build_semantic_projection(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    schema2_contract: DomainContractV2 | None = None,
) -> dict[str, list[dict[str, Any]]] | SemanticProjectionResult:
    """Build the compatible schema-1 or authoritative schema-2 projection.

    Passing ``schema2_contract`` opts into strict schema-2 lifecycle projection.
    Omitting it preserves the original schema-1 behavior and return shape.
    """
    if schema2_contract is None:
        return _build_schema1_semantic_projection(
            entities, relationships, evidence
        )
    if not isinstance(schema2_contract, DomainContractV2):
        raise TypeError("schema2_contract must be a DomainContractV2")
    return _schema2_projection(
        entities, relationships, evidence, schema2_contract
    )
