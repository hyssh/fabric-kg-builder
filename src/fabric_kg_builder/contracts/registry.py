"""Single registry and version-negotiation authority for C0.Core."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .assertions import (
    CanonicalEntityAssertion,
    CanonicalPropertyAssertion,
    CanonicalRelationshipAssertion,
)
from .base import (
    CONTRACT_VERSION,
    ContractModel,
    UnknownContractKindError,
    UnknownContractMajorError,
    canonical_json,
    canonical_sha256,
    contract_major,
)
from .evidence import EvidenceSpan, SourceUnit
from .identity import StandaloneCanonicalIdentityEnvelope
from .lifecycle import CandidateAccountingDisposition, CandidateLifecycleRecord
from .projection import AuditProjection, SemanticServingProjection
from .receipts import ArtifactManifest, StageReceipt
from .resources import StageResourceMetrics

REGISTERED_CONTRACTS: dict[str, type[ContractModel]] = {
    "c0.identity": StandaloneCanonicalIdentityEnvelope,
    "c0.source_unit": SourceUnit,
    "c0.evidence_span": EvidenceSpan,
    "c0.candidate_lifecycle_record": CandidateLifecycleRecord,
    "c0.candidate_accounting_disposition": CandidateAccountingDisposition,
    "c0.canonical_entity_assertion": CanonicalEntityAssertion,
    "c0.canonical_relationship_assertion": CanonicalRelationshipAssertion,
    "c0.canonical_property_assertion": CanonicalPropertyAssertion,
    "c0.audit_projection": AuditProjection,
    "c0.semantic_serving_projection": SemanticServingProjection,
    "c0.artifact_manifest": ArtifactManifest,
    "c0.stage_receipt": StageReceipt,
    "c0.stage_resource_metrics": StageResourceMetrics,
}
SUPPORTED_VERSIONS: dict[str, tuple[str, ...]] = {
    kind: (CONTRACT_VERSION,) for kind in REGISTERED_CONTRACTS
}


def negotiate_contract(kind: str, version: str) -> type[ContractModel]:
    try:
        model = REGISTERED_CONTRACTS[kind]
    except KeyError as exc:
        raise UnknownContractKindError(f"unregistered contract kind: {kind}") from exc
    supported = SUPPORTED_VERSIONS[kind]
    requested_major = contract_major(version)
    supported_majors = {contract_major(item) for item in supported}
    if requested_major not in supported_majors:
        raise UnknownContractMajorError(
            f"{kind} major {requested_major} is unsupported; supported versions: {supported}"
        )
    if version not in supported:
        raise ValueError(
            f"{kind} version {version} is not registered; supported versions: {supported}"
        )
    return model


def parse_contract(payload: str | bytes | dict[str, Any]) -> ContractModel:
    if isinstance(payload, bytes):
        raw = json.loads(payload.decode("utf-8"))
    elif isinstance(payload, str):
        loaded = yaml.safe_load(payload)
        if not isinstance(loaded, dict):
            raise ValueError("contract payload must be an object")
        raw = loaded
    else:
        raw = payload
    identity = raw.get("identity") if isinstance(raw.get("identity"), dict) else raw
    kind = identity.get("contract_kind")
    version = identity.get("contract_version")
    if not isinstance(kind, str) or not isinstance(version, str):
        raise ValueError("contract_kind and contract_version are required")
    model = negotiate_contract(kind, version)
    return model.model_validate_json(canonical_json(raw))


def schema_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for kind, model in sorted(REGISTERED_CONTRACTS.items()):
        schema = model.model_json_schema()
        if kind == "c0.identity":
            identity_schema = schema
        else:
            identity_schema = schema["$defs"]["CanonicalIdentityEnvelope"]
        identity_schema["properties"]["contract_kind"] = {
            "const": kind,
            "title": "Contract Kind",
            "type": "string",
        }
        identity_schema["properties"]["contract_version"] = {
            "const": CONTRACT_VERSION,
            "default": CONTRACT_VERSION,
            "title": "Contract Version",
            "type": "string",
        }
        catalog[kind] = {
            "contract_kind": kind,
            "contract_version": CONTRACT_VERSION,
            "schema": schema,
        }
    return catalog


def write_registered_schemas(output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for kind, entry in schema_catalog().items():
        filename = f"{kind.replace('.', '-')}-{CONTRACT_VERSION}.schema.json"
        content = canonical_json(entry)
        (output_dir / filename).write_text(content + "\n", encoding="utf-8")
        hashes[kind] = canonical_sha256(entry)
    index = {
        "registry_version": CONTRACT_VERSION,
        "schemas": [
            {
                "contract_kind": kind,
                "contract_version": CONTRACT_VERSION,
                "schema_hash": hashes[kind],
                "path": f"{kind.replace('.', '-')}-{CONTRACT_VERSION}.schema.json",
            }
            for kind in sorted(hashes)
        ],
    }
    (output_dir / "registry.json").write_text(
        canonical_json(index) + "\n", encoding="utf-8"
    )
    return hashes
