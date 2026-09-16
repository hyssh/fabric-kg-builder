"""Deterministic presentation names; canonical identities never become labels."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# Ontology transport allows hyphens, but Graph node labels do not.
NATIVE_NAME_PATTERN = r"[A-Za-z][A-Za-z0-9_]{0,127}"
_DEFINITION_PATH = re.compile(r"(EntityTypes|RelationshipTypes)/([0-9]+)/definition\.json")
_BASE_ID = "1000000"
PRESENTATION_ATTRIBUTE = "fabric_kg_presentation"
METADATA_ONLY_ALIASES_LIMITATION = "ontology.property-relationship-aliases-metadata-only"


def readable_catalog_from_domain(domain: Any) -> dict[str, Any]:
    """Extract approved labels only, including inherited property declarations.

    Callers must verify the domain's sealed publication authority before using
    this catalog to change a deployed definition.
    """
    if hasattr(domain, "model_dump"):
        domain = domain.model_dump(mode="json")
    model = domain["candidate_model"]
    catalog: dict[str, Any] = {
        "version": "1.0",
        "entity_types": {},
        "properties": {},
        "relationship_types": {},
    }

    def add(kind: str, key: str, item: Mapping[str, Any]) -> None:
        metadata = {
            field: copy.deepcopy(item[field])
            for field in ("display_name", "description", "aliases", "ascii_alias")
            if field in item
        }
        if key in catalog[kind] and catalog[kind][key] != metadata:
            raise ValueError(f"Conflicting approved presentation metadata: {key}")
        catalog[kind][key] = metadata

    for entity in model["entity_types"]:
        add("entity_types", entity["type_id"], entity)
        for prop in entity.get("declared_properties", ()):
            add("properties", prop["property_id"], prop)
    for rel in model["relationship_types"]:
        add("relationship_types", rel["relationship_type_id"], rel)
    return catalog


def _label_name(metadata: Mapping[str, Any], *, prefix: str) -> str:
    label = metadata.get("display_name")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("Approved display_name is required; canonical IDs are not labels")
    candidates = [label]
    if metadata.get("ascii_alias"):
        candidates.append(metadata["ascii_alias"])
    candidates.extend(metadata.get("aliases", ()))
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, str) or (index and not candidate.isascii()):
            continue
        normalized = re.sub(r"[^A-Za-z0-9_]+", "_", candidate).strip("_")
        if not re.search(r"[A-Za-z0-9]", normalized):
            continue
        if not re.match(r"[A-Za-z]", normalized):
            normalized = prefix + "_" + normalized
        if re.fullmatch(r"ws[_-][0-9a-f]{32,}", normalized, re.IGNORECASE):
            raise ValueError("Opaque ws identifiers cannot be approved presentation labels")
        return normalized[:128]
    raise ValueError(f"Label {label!r} requires an explicitly approved ASCII alias")


def allocate_readable_names(
    metadata_by_id: Mapping[str, Mapping[str, Any]],
    *,
    prefix: str = "Type",
    reserved: Sequence[str] = (),
) -> dict[str, str]:
    """Allocate casefold-unique bounded names, independently of input ordering."""
    names = {
        key: _label_name(value, prefix=prefix)
        for key, value in sorted(metadata_by_id.items())
    }
    reserved_keys = {value.casefold() for value in reserved}
    counts = Counter(value.casefold() for value in names.values())
    conflicted = {
        key for key, value in names.items()
        if counts[value.casefold()] > 1 or value.casefold() in reserved_keys
    }
    used = reserved_keys | {
        value.casefold() for key, value in names.items() if key not in conflicted
    }
    for key in sorted(conflicted):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        for length in range(8, 65, 4):
            candidate = names[key][:127 - length] + "_" + digest[:length]
            if candidate.casefold() not in used:
                names[key] = candidate
                used.add(candidate.casefold())
                break
        else:
            raise ValueError(f"Cannot allocate a unique readable name for {key!r}")
    return names


def _decode(part: Mapping[str, Any]) -> dict[str, Any]:
    if part.get("payloadType") != "InlineBase64":
        raise ValueError(f"Presentation repair requires InlineBase64 parts: {part['path']}")
    return json.loads(base64.b64decode(part["payload"], validate=True))


def _indexed_parts(parts: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result = {part["path"]: part for part in parts}
    if len(result) != len(parts):
        raise ValueError("Duplicate native part paths")
    return result


def _enrich(
    payload: dict[str, Any], metadata: Mapping[str, Any], *, preserve_live_metadata: bool,
    entity_synonyms: bool = True,
) -> None:
    enrichment = copy.deepcopy(payload.get("semanticEnrichment", {}))
    if not preserve_live_metadata:
        if entity_synonyms:
            synonyms = list(enrichment.get("synonyms", []))
            for label in [metadata["display_name"], *metadata.get("aliases", ())]:
                if label not in synonyms:
                    synonyms.append(label)
            enrichment["synonyms"] = synonyms
        else:
            # Fabric only supports native synonyms on entity types. Preserve
            # approved labels as custom metadata, not as synonym functionality.
            if "synonyms" in enrichment:
                raise ValueError("Unsupported native property/relationship synonyms require explicit migration")
            presentation = json.dumps({
                key: metadata[key] for key in ("display_name", "aliases", "ascii_alias")
                if key in metadata
            }, sort_keys=True, ensure_ascii=False)
            attributes = enrichment.setdefault("customAttributes", {})
            if PRESENTATION_ATTRIBUTE in attributes and attributes[PRESENTATION_ATTRIBUTE] != presentation:
                raise ValueError("Conflicting native presentation custom attribute")
            attributes[PRESENTATION_ATTRIBUTE] = presentation
            enrichment.setdefault("description", "")
    if metadata.get("description"):
        enrichment["description"] = metadata["description"]
    if enrichment != payload.get("semanticEnrichment", {}):
        payload["semanticEnrichment"] = enrichment


@dataclass(frozen=True)
class OntologyPresentationRepair:
    parts: tuple[dict[str, Any], ...]
    mapping_report: tuple[dict[str, Any], ...]


def validate_presentation_only(
    before_parts: Sequence[Mapping[str, Any]],
    after_parts: Sequence[Mapping[str, Any]],
    *,
    preserve_live_metadata: bool = True,
) -> None:
    """Reject any change outside type/property presentation fields.

    Binding/contextualization and all other parts must be byte-for-byte equal,
    including their envelope metadata. Definition identity, order, endpoints,
    inheritance, namespace, and arbitrary scope fields must be unchanged.
    """
    before = _indexed_parts(before_parts)
    after = _indexed_parts(after_parts)
    if list(before) != list(after):
        raise ValueError("Presentation repair changed native paths or part ordering")
    type_names: dict[str, set[str]] = {"EntityTypes": set(), "RelationshipTypes": set()}

    def strip_presentation(payload: dict[str, Any]) -> None:
        payload.pop("name", None)
        enrichment = payload.pop("semanticEnrichment", {})
        retained = {
            key: value for key, value in enrichment.items()
            if key != "description" and (preserve_live_metadata or key != "synonyms")
        }
        if not preserve_live_metadata and isinstance(retained.get("customAttributes"), dict):
            retained["customAttributes"].pop(PRESENTATION_ATTRIBUTE, None)
            if not retained["customAttributes"]:
                retained.pop("customAttributes")
        if retained:
            payload["semanticEnrichment"] = retained

    def protect_existing_metadata(old: dict[str, Any], new: dict[str, Any]) -> None:
        attributes = old.get("semanticEnrichment", {}).get("customAttributes", {})
        if PRESENTATION_ATTRIBUTE in attributes and (
            new.get("semanticEnrichment", {}).get("customAttributes", {}).get(PRESENTATION_ATTRIBUTE)
            != attributes[PRESENTATION_ATTRIBUTE]
        ):
            raise ValueError("Presentation repair changed existing custom presentation metadata")

    for path, old_part in before.items():
        new_part = after[path]
        match = _DEFINITION_PATH.fullmatch(path)
        if not match:
            if old_part != new_part:
                raise ValueError(f"Presentation repair changed a binding/non-definition part: {path}")
            continue
        if {k: v for k, v in old_part.items() if k != "payload"} != {
            k: v for k, v in new_part.items() if k != "payload"
        }:
            raise ValueError(f"Presentation repair changed a part envelope: {path}")
        old, new = _decode(old_part), _decode(new_part)
        protect_existing_metadata(old, new)
        name = new.get("name", "")
        if not re.fullmatch(NATIVE_NAME_PATTERN, name) or name.casefold() in type_names[match[1]]:
            raise ValueError(f"Invalid or colliding native presentation name: {path}")
        type_names[match[1]].add(name.casefold())
        if match[1] == "EntityTypes":
            for old_prop, new_prop in zip(old.get("properties", []), new.get("properties", [])):
                protect_existing_metadata(old_prop, new_prop)
            properties = new.get("properties", [])
            property_names = [prop.get("name", "") for prop in properties]
            if any(not re.fullmatch(NATIVE_NAME_PATTERN, name) for name in property_names) or len(
                {name.casefold() for name in property_names}
            ) != len(property_names):
                raise ValueError(f"Invalid or colliding native property name: {path}")
            if new.get("displayNamePropertyId") not in {prop["id"] for prop in properties}:
                raise ValueError(f"Display label references a nonexistent property: {path}")
        for payload in (old, new):
            strip_presentation(payload)
            if match[1] == "EntityTypes":
                payload.pop("displayNamePropertyId", None)
                for prop in payload.get("properties", []):
                    strip_presentation(prop)
        if old != new:
            raise ValueError(f"Presentation repair changed identities/bindings/schema: {path}")


def repair_ontology_presentation(
    current_parts: Sequence[Mapping[str, Any]],
    *,
    l5a_ontology: Mapping[str, Any],
    catalog: Mapping[str, Any],
    preserve_live_metadata: bool = True,
) -> OntologyPresentationRepair:
    """Produce a complete presentation-only replacement of CURRENT native parts.

    Never compile replacement bindings: preserve every original byte. The L5
    crosswalk maps existing numeric IDs to approved catalog labels.
    Existing-item repairs preserve synonyms and custom attributes. Only fresh
    compilation may add entity synonyms. Property/relationship labels and aliases
    are retained in a namespaced custom attribute, not native synonyms. The repair
    mapping always records the exact original approved display label.
    """
    parts = _indexed_parts(current_parts)
    entities = {
        str(item["id"]): item for item in l5a_ontology["entity_types"]
    }
    relationships = {
        str(item["id"]): item for item in l5a_ontology["relationship_types"]
    }
    if len(entities) != len(l5a_ontology["entity_types"]) or len(relationships) != len(
        l5a_ontology["relationship_types"]
    ):
        raise ValueError("Duplicate L5 ontology numeric IDs")
    native_ids = {"EntityTypes": set(), "RelationshipTypes": set()}
    for path in parts:
        match = _DEFINITION_PATH.fullmatch(path)
        if match:
            if str(_decode(parts[path]).get("id")) != match[2]:
                raise ValueError(f"Definition ID differs from its path: {path}")
            native_ids[match[1]].add(match[2])
    if native_ids["EntityTypes"] - {_BASE_ID} != set(entities) - {_BASE_ID} or (
        not set(entities).issubset(native_ids["EntityTypes"])
        or native_ids["RelationshipTypes"] != set(relationships)
    ):
        raise ValueError("Current native types do not exactly match the authoritative L5 crosswalk")

    def metadata(kind: str, key: str) -> Mapping[str, Any]:
        try:
            value = catalog[kind][key]
        except KeyError as exc:
            raise ValueError(f"Approved readable catalog missing {kind}: {key}") from exc
        _label_name(value, prefix="Type")
        return value

    type_meta = {
        key: metadata("entity_types", item["canonical_semantic_type_id"])
        for key, item in entities.items()
    }
    rel_meta = {
        key: metadata("relationship_types", item["canonical_semantic_relationship_id"])
        for key, item in relationships.items()
    }
    # Disambiguation hashes use stable canonical IDs, not presentation order.
    type_names = allocate_readable_names({
        entities[key]["canonical_semantic_type_id"]: value for key, value in type_meta.items()
    }, reserved=["surface_entity"] if (
        _BASE_ID in native_ids["EntityTypes"] and _BASE_ID not in entities
    ) else [])
    rel_names = allocate_readable_names({
        relationships[key]["canonical_semantic_relationship_id"]: value
        for key, value in rel_meta.items()
    }, prefix="Relationship")
    result = []
    report: list[dict[str, Any]] = []
    for original in current_parts:
        path = original["path"]
        match = _DEFINITION_PATH.fullmatch(path)
        if not match:
            result.append(copy.deepcopy(dict(original)))
            continue
        payload = _decode(original)
        type_id = match[2]
        previous = copy.deepcopy(payload)
        if match[1] == "EntityTypes":
            entity = entities.get(type_id)
            bound_labels: set[str] = set()
            for binding_path, binding_part in parts.items():
                if binding_path.startswith(f"EntityTypes/{type_id}/DataBindings/"):
                    configuration = _decode(binding_part)["dataBindingConfiguration"]
                    expected_table = entity["physical_table_id"] if entity else l5a_ontology.get(
                        "instance_presentation", {}
                    ).get("base_entity_table", "l4_semantic_asserted_entities")
                    if configuration["sourceTableProperties"]["sourceTableName"] != expected_table:
                        raise ValueError(f"Current binding differs from the L5 table: {binding_path}")
                    expected_label = "__label" if entity else "label"
                    for binding in configuration.get("propertyBindings", ()):
                        if binding["sourceColumnName"] == expected_label:
                            bound_labels.add(str(binding["targetPropertyId"]))
            if entity is not None:
                canonical = entity["canonical_semantic_type_id"]
                payload["name"] = type_names[canonical]
                _enrich(payload, type_meta[type_id], preserve_live_metadata=preserve_live_metadata)
                property_meta = {
                    prop["canonical_property_id"]: metadata("properties", prop["canonical_property_id"])
                    for prop in entity.get("properties", ())
                }
                l5_properties = {str(prop["id"]): prop for prop in entity.get("properties", ())}
                native_properties = {str(prop["id"]): prop for prop in payload["properties"]}
                property_names = allocate_readable_names(
                    property_meta, prefix="Property", reserved=[
                        prop["name"] for prop_id, prop in native_properties.items()
                        if prop_id not in l5_properties
                    ],
                )
                if len(l5_properties) != len(entity.get("properties", ())) or (
                    len(native_properties) != len(payload["properties"])
                ) or not set(
                    l5_properties
                ).issubset(native_properties):
                    raise ValueError(f"Current properties differ from the L5 crosswalk: {path}")
                for prop_id, prop in native_properties.items():
                    if prop_id not in l5_properties and (
                        prop_id not in payload["entityIdParts"]
                        and prop_id not in bound_labels
                        and prop.get("name") != "label"
                    ):
                        raise ValueError(f"Unmapped native property requires approved metadata: {path}/{prop_id}")
                for prop_id, l5_prop in l5_properties.items():
                    prop = native_properties[prop_id]
                    old_name = prop["name"]
                    key = l5_prop["canonical_property_id"]
                    prop["name"] = property_names[key]
                    _enrich(
                        prop, property_meta[key], preserve_live_metadata=preserve_live_metadata,
                        entity_synonyms=False,
                    )
                    report.append({
                        "kind": "property", "owner_id": type_id, "id": prop_id,
                        "canonical_id": key, "old_name": old_name,
                        "new_name": prop["name"], "display_name": property_meta[key]["display_name"],
                    })
            else:
                canonical = None
            property_ids = {str(prop["id"]) for prop in payload["properties"]}
            bound_labels &= property_ids - {str(value) for value in payload["entityIdParts"]}
            if len(bound_labels) > 1:
                raise ValueError(f"Ambiguous bound label property: {path}")
            if bound_labels:
                payload["displayNamePropertyId"] = next(iter(bound_labels))
        else:
            relationship = relationships[type_id]
            canonical = relationship["canonical_semantic_relationship_id"]
            payload["name"] = rel_names[canonical]
            _enrich(
                payload, rel_meta[type_id], preserve_live_metadata=preserve_live_metadata,
                entity_synonyms=False,
            )
        report.append({
            "kind": "entity_type" if match[1] == "EntityTypes" else "relationship_type",
            "id": type_id, "canonical_id": canonical,
            "old_name": previous["name"], "new_name": payload["name"],
            "display_name": (
                rel_meta[type_id]["display_name"] if match[1] == "RelationshipTypes"
                else type_meta[type_id]["display_name"] if type_id in type_meta else payload["name"]
            ),
            **({
                "old_display_name_property_id": previous.get("displayNamePropertyId"),
                "new_display_name_property_id": payload.get("displayNamePropertyId"),
                "entity_id_parts": payload["entityIdParts"],
            } if match[1] == "EntityTypes" else {}),
        })
        replacement = copy.deepcopy(dict(original))
        if payload != previous:
            replacement["payload"] = base64.b64encode(
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
        result.append(replacement)
    validate_presentation_only(current_parts, result, preserve_live_metadata=preserve_live_metadata)
    return OntologyPresentationRepair(tuple(result), tuple(report))
