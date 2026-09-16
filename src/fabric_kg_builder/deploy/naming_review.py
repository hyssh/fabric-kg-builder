"""Separate operator-approved Copilot naming authority; never rewrites Domain."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictStr

from fabric_kg_builder.deploy.ontology_names import NATIVE_NAME_PATTERN


class ReviewedName(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    native_name: StrictStr
    display_name: StrictStr
    reason: StrictStr


class ReviewedRelationship(ReviewedName):
    verb: Literal["Has", "Includes", "Specifies", "Applies", "Replaces", "References", "Targets", "Is"]
    source_type_ids: list[StrictStr]
    target_type_ids: list[StrictStr]


class NamingReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["copilot-semantic-names/1.0.0"]
    domain_contract_hash: StrictStr
    author: StrictStr
    entity_types: dict[StrictStr, ReviewedName]
    relationship_types: dict[StrictStr, ReviewedRelationship]


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate naming review JSON key: {key}")
        result[key] = value
    return result


def load_review(path: Path, domain: Any, domain_hash: str, ontology: dict) -> tuple[NamingReview, dict]:
    """Bind exact bytes, typed payload and implementation to sealed semantics."""
    raw = path.read_bytes()
    payload = json.loads(raw, object_pairs_hook=_unique_json)
    review = NamingReview.model_validate(payload)
    if review.domain_contract_hash != domain_hash:
        raise ValueError("Naming review domain_contract_hash differs from sealed Domain")
    if not review.author.strip() or "copilot" not in review.author.casefold():
        raise ValueError("Naming review author must identify the Copilot semantic reviewer")
    if hasattr(domain, "model_dump"):
        domain = domain.model_dump(mode="json")
    model = domain["candidate_model"]
    entities = {item["type_id"]: item for item in model["entity_types"]}
    relationships = {item["relationship_type_id"]: item for item in model["relationship_types"]}
    if set(review.entity_types) != set(entities) or set(review.relationship_types) != set(relationships):
        raise ValueError("Naming review requires the complete exact canonical entity/relationship ID sets")
    used = {"surface_entity"}
    for kind in ("entity_types", "relationship_types"):
        for key, item in getattr(review, kind).items():
            name = item.native_name
            if (
                not re.fullmatch(NATIVE_NAME_PATTERN, name)
                or name.casefold() in used
                or not item.display_name.strip() or not item.reason.strip()
                or re.search(r"_[0-9a-fA-F]{8,64}$", name)
            ):
                raise ValueError(f"Invalid/colliding reviewed name or missing review reason: {key}")
            used.add(name.casefold())
            if kind == "entity_types":
                if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
                    raise ValueError(f"Reviewed entity name must be noun PascalCase: {key}")
            else:
                assert isinstance(item, ReviewedRelationship)
                if not re.match(rf"^{item.verb}(?:$|[A-Z0-9_])", name) or not re.match(
                    rf"^{item.verb}(?:$|[\sA-Z0-9_])", item.display_name,
                ):
                    raise ValueError(f"Reviewed relationship name/display_name must be verb-first: {key}")
                for field in ("source_type_ids", "target_type_ids"):
                    actual = getattr(item, field)
                    if len(set(actual)) != len(actual) or set(actual) != set(relationships[key][field]):
                        raise ValueError(f"Reviewed relationship endpoints differ from sealed Domain: {key}/{field}")
    crosswalk_entities = {item["canonical_semantic_type_id"] for item in ontology["entity_types"]}
    crosswalk_relationships = {
        item["canonical_semantic_relationship_id"]: item for item in ontology["relationship_types"]
    }
    if crosswalk_entities != set(entities) or set(crosswalk_relationships) != set(relationships):
        raise ValueError("Naming review canonical IDs differ from publication crosswalk")
    for key, item in crosswalk_relationships.items():
        for side in ("source", "target"):
            if set(item[f"allowed_{side}_semantic_type_ids"]) != set(
                getattr(review.relationship_types[key], f"{side}_type_ids")
            ):
                raise ValueError(f"Naming review endpoints differ from publication crosswalk: {key}")
    code = Path(__file__).read_bytes()
    return review, {
        "path": str(path.resolve()), "bytes_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_base64": base64.b64encode(raw).decode("ascii"), "payload": payload,
        "code_sha256": hashlib.sha256(code).hexdigest(),
        "code_base64": base64.b64encode(code).decode("ascii"),
    }


def render_ontology(definition: dict, ontology: dict, review: NamingReview) -> tuple[dict, list]:
    """Change only actual native type names; preserve all other values and bytes."""
    result = copy.deepcopy(definition)
    index = {
        f"{native}/{item['id']}/definition.json": (key, getattr(review, kind)[key])
        for native, kind, canonical_field in (
            ("EntityTypes", "entity_types", "canonical_semantic_type_id"),
            ("RelationshipTypes", "relationship_types", "canonical_semantic_relationship_id"),
        )
        for item in ontology[kind]
        for key in [item[canonical_field]]
    }
    seen, mapping = set(), []
    for part in result["parts"]:
        path = part["path"]
        if path not in index:
            continue
        if path in seen or part.get("payloadType") != "InlineBase64":
            raise ValueError("Duplicate/unsupported reviewed ontology definition")
        seen.add(path)
        key, item = index[path]
        payload = json.loads(base64.b64decode(part["payload"], validate=True))
        if str(payload["id"]) != path.split("/")[1]:
            raise ValueError("Reviewed ontology crosswalk/native ID mismatch")
        old = payload["name"]
        payload["name"] = item.native_name
        mapping.append({
            "kind": "relationship_type" if isinstance(item, ReviewedRelationship) else "entity_type",
            "id": str(payload["id"]), "canonical_id": key, "old_name": old,
            "new_name": item.native_name, "display_name": item.display_name, "reason": item.reason,
            **({
                "verb": item.verb, "source_type_ids": item.source_type_ids,
                "target_type_ids": item.target_type_ids,
            } if isinstance(item, ReviewedRelationship) else {}),
        })
        if old != item.native_name:
            part["payload"] = base64.b64encode(json.dumps(
                payload, indent=2, ensure_ascii=False,
            ).encode()).decode("ascii")
    if seen != set(index):
        raise ValueError("Reviewed ontology native type set is incomplete")
    return result, mapping
