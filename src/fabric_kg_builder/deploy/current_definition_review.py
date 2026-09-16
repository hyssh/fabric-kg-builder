"""Explicit review of pre-existing native names and local contextualization IDs."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr

from fabric_kg_builder.contracts.base import canonical_sha256

POLICY = "ontology-current-type-names-contextualization-local-id-only-v1"


class CurrentDefinitionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["ontology-current-definition-review/1.0.0"]
    policy: Literal["ontology-current-type-names-contextualization-local-id-only-v1"]
    workspace_id: StrictStr
    ontology_id: StrictStr
    domain_contract_hash: StrictStr
    publication_plan_hash: StrictStr
    original_definition_hash: StrictStr
    current_definition_hash: StrictStr
    current_definition_file: StrictStr
    current_definition_file_sha256: StrictStr
    actor: StrictStr
    rationale: StrictStr


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate current definition/review JSON key: {key}")
        result[key] = value
    return result


def _payloads(definition):
    from fabric_kg_builder.deploy.schema2_ontology_presentation import _decode

    _decode(definition)
    return {
        part["path"]: json.loads(
            base64.b64decode(part["payload"], validate=True).decode("utf-8-sig"),
            object_pairs_hook=_unique,
        )
        for part in definition["parts"] if part["path"] != ".platform"
    }


def validate_equivalence(original: dict, current: dict) -> dict:
    """Pair bindings uniquely within one relationship, never erase semantic drift."""
    from fabric_kg_builder.deploy.schema2_ontology_presentation import _diff

    old, new = _payloads(original), _payloads(current)
    pattern = r"RelationshipTypes/([^/]+)/Contextualizations/([^/]+)\.json"

    def contextualizations(payloads):
        groups = {}
        for path, payload in payloads.items():
            match = re.fullmatch(pattern, path)
            if not match:
                if "/Contextualizations/" in path:
                    raise ValueError("Invalid contextualization path")
                continue
            relationship, local_id = match.groups()
            if (
                not isinstance(payload, dict)
                or payload.get("id") != local_id
                or str(uuid.UUID(local_id)) != local_id
            ):
                raise ValueError("Contextualization path UUID must match payload id")
            value = copy.deepcopy(payload)
            del value["id"]
            key = (relationship, canonical_sha256(value))
            if key in groups:
                raise ValueError("Duplicate/ambiguous semantic contextualization binding")
            groups[key] = (path, value)
        return groups

    before_bindings, after_bindings = contextualizations(old), contextualizations(new)
    if set(before_bindings) != set(after_bindings):
        raise ValueError("Current definition changed semantic contextualization bindings")
    substitutions = {}
    for key, (old_path, value) in before_bindings.items():
        new_path, new_value = after_bindings[key]
        if canonical_sha256(value) != canonical_sha256(new_value):
            raise ValueError("Current contextualization binding payload changed")
        substitutions[old_path] = new_path
    if {substitutions.get(path, path) for path in old} != set(new):
        raise ValueError("Current definition changed native part scope")
    if [substitutions.get(path, path) for path in old] != list(new):
        raise ValueError("Current definition changed native part order")
    changes = []
    for path, payload in old.items():
        new_path = substitutions.get(path, path)
        actual = new[new_path]
        expected = copy.deepcopy(payload)
        if path in substitutions:
            expected["id"] = actual["id"]
        elif re.fullmatch(r"(EntityTypes|RelationshipTypes)/[^/]+/definition\.json", path):
            if (
                not isinstance(actual.get("name"), str) or not actual["name"].strip()
                or not isinstance(expected.get("name"), str)
            ):
                raise ValueError("Current type name must remain a nonempty string")
            expected["name"] = actual["name"]
        if canonical_sha256(expected) != canonical_sha256(actual):
            raise ValueError(f"Current definition changed protected content: {path}")
        if path != new_path:
            changes.append({"before_part": path, "after_part": new_path, "local_id_only": True})
        changes.extend({"part": path, **change} for change in _diff(payload, actual))
    return {"policy": POLICY, "original_to_current_diff": changes}


def load_review(path: Path, *, original: dict, plan: dict, ontology_id: str, domain_hash: str):
    from fabric_kg_builder.deploy.schema2_ontology_presentation import _content_hash
    from fabric_kg_builder.deploy.schema2_prototype_reconcile import _platform_envelope

    raw = path.read_bytes()
    payload = json.loads(raw, object_pairs_hook=_unique)
    review = CurrentDefinitionReview.model_validate(payload)
    if not review.actor.strip() or not review.rationale.strip():
        raise ValueError("Current definition review requires actor and rationale")
    for field in (
        "domain_contract_hash", "publication_plan_hash", "original_definition_hash",
        "current_definition_hash", "current_definition_file_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", getattr(review, field)):
            raise ValueError(f"Invalid current definition review hash: {field}")
    if (
        review.workspace_id != plan["workspace_id"] or review.ontology_id != ontology_id
        or review.domain_contract_hash != domain_hash
        or review.publication_plan_hash != plan["plan_hash"]
        or review.original_definition_hash != _content_hash(original)
    ):
        raise ValueError("Current definition review original plan/domain/item identity mismatch")
    definition_path = Path(review.current_definition_file)
    if not definition_path.is_absolute():
        definition_path = path.parent / definition_path
    definition_raw = definition_path.read_bytes()
    if hashlib.sha256(definition_raw).hexdigest() != review.current_definition_file_sha256:
        raise ValueError("Reviewed current definition file bytes changed")
    current = json.loads(definition_raw, object_pairs_hook=_unique)
    if _content_hash(current) != review.current_definition_hash:
        raise ValueError("Reviewed current definition content hash mismatch")
    platform = _platform_envelope(current, plan, "ontology")
    original_platform = _platform_envelope(original, plan, "ontology")
    if original_platform is not None and platform != original_platform:
        raise ValueError("Current definition changed original platform envelope")
    comparison = validate_equivalence(original, current)
    code = Path(__file__).read_bytes()
    evidence = {
        "path": str(path.resolve()), "bytes_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_base64": base64.b64encode(raw).decode("ascii"), "payload": payload,
        "code_sha256": hashlib.sha256(code).hexdigest(),
        "code_base64": base64.b64encode(code).decode("ascii"),
        "definition_path": str(definition_path.resolve()),
        "definition_raw_base64": base64.b64encode(definition_raw).decode("ascii"),
        **comparison,
    }
    return current, evidence
