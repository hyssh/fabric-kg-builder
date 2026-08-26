"""Single registry and version-negotiation authority for C0.Core."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

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
from .evidence import EvidenceSpan, EvidenceSpanV1_1, SourceUnit
from .extraction import (
    ExtractionCandidateBatch,
    RequiredMemberManifest,
    RequiredMemberManifestV1_1,
    RequiredMemberSetProposal,
    RequiredMemberSetProposalV1_1,
)
from .identity import StandaloneCanonicalIdentityEnvelope
from .lifecycle import CandidateAccountingDisposition, CandidateLifecycleRecord
from .projection import AuditProjection, SemanticServingProjection
from .publication import (
    AccessPolicy,
    GovernedAssetReference,
    ProjectionEquivalence,
    PublicationCrosswalk,
    PublicationCrosswalkV1_1,
)
from .receipts import ArtifactManifest, StageReceipt
from .resources import StageResourceMetrics
from .runtime import (
    AgenticRetrievalCoverageReceipt,
    AgenticRetrievalRequestContext,
    CitationPresentation,
    OntologyScopeEnvelope,
    QueryBudget,
    ResolvedOntologyScope,
    ResolvedRetrievalScope,
    SearchCitationEnvelope,
)

REGISTERED_CONTRACTS: dict[str, type[ContractModel]] = {
    "c0.identity": StandaloneCanonicalIdentityEnvelope,
    "c0.source_unit": SourceUnit,
    "c0.evidence_span": EvidenceSpan,
    "c0.extraction_candidate_batch": ExtractionCandidateBatch,
    "c0.required_member_set_proposal": RequiredMemberSetProposal,
    "c0.required_member_manifest": RequiredMemberManifest,
    "c0.candidate_lifecycle_record": CandidateLifecycleRecord,
    "c0.candidate_accounting_disposition": CandidateAccountingDisposition,
    "c0.canonical_entity_assertion": CanonicalEntityAssertion,
    "c0.canonical_relationship_assertion": CanonicalRelationshipAssertion,
    "c0.canonical_property_assertion": CanonicalPropertyAssertion,
    "c0.audit_projection": AuditProjection,
    "c0.semantic_serving_projection": SemanticServingProjection,
    "c0.publication_crosswalk": PublicationCrosswalk,
    "c0.projection_equivalence": ProjectionEquivalence,
    "c0.governed_asset_reference": GovernedAssetReference,
    "c0.access_policy": AccessPolicy,
    "c0.query_budget": QueryBudget,
    "c0.ontology_scope_envelope": OntologyScopeEnvelope,
    "c0.resolved_ontology_scope": ResolvedOntologyScope,
    "c0.resolved_retrieval_scope": ResolvedRetrievalScope,
    "c0.agentic_retrieval_request_context": AgenticRetrievalRequestContext,
    "c0.agentic_retrieval_coverage_receipt": AgenticRetrievalCoverageReceipt,
    "c0.search_citation_envelope": SearchCitationEnvelope,
    "c0.citation_presentation": CitationPresentation,
    "c0.artifact_manifest": ArtifactManifest,
    "c0.stage_receipt": StageReceipt,
    "c0.stage_resource_metrics": StageResourceMetrics,
}
SUPPORTED_VERSIONS: dict[str, tuple[str, ...]] = {
    kind: (CONTRACT_VERSION,) for kind in REGISTERED_CONTRACTS
}
SUPPORTED_VERSIONS["c0.evidence_span"] = ("1.0.0", "1.1.0")
SUPPORTED_VERSIONS["c0.required_member_set_proposal"] = ("1.0.0", "1.1.0")
SUPPORTED_VERSIONS["c0.required_member_manifest"] = ("1.0.0", "1.1.0")
SUPPORTED_VERSIONS["c0.publication_crosswalk"] = ("1.0.0", "1.1.0")

REGISTERED_CONTRACT_VERSIONS: dict[tuple[str, str], type[ContractModel]] = {
    (kind, CONTRACT_VERSION): model for kind, model in REGISTERED_CONTRACTS.items()
}
REGISTERED_CONTRACT_VERSIONS[("c0.evidence_span", "1.1.0")] = EvidenceSpanV1_1
REGISTERED_CONTRACT_VERSIONS[
    ("c0.required_member_set_proposal", "1.1.0")
] = RequiredMemberSetProposalV1_1
REGISTERED_CONTRACT_VERSIONS[
    ("c0.required_member_manifest", "1.1.0")
] = RequiredMemberManifestV1_1
REGISTERED_CONTRACT_VERSIONS[
    ("c0.publication_crosswalk", "1.1.0")
] = PublicationCrosswalkV1_1


def negotiate_contract(kind: str, version: str) -> type[ContractModel]:
    if kind not in REGISTERED_CONTRACTS:
        raise UnknownContractKindError(f"unregistered contract kind: {kind}")
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
    return REGISTERED_CONTRACT_VERSIONS[(kind, version)]


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


def schema_catalog() -> dict[tuple[str, str], dict[str, Any]]:
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    for (kind, version), model in sorted(REGISTERED_CONTRACT_VERSIONS.items()):
        schema = model.model_json_schema()
        if kind == "c0.identity":
            identity_schema = schema
        else:
            identity_schema = next(
                definition
                for name, definition in schema["$defs"].items()
                if name
                in {
                    "CanonicalIdentityEnvelope",
                    "EvidenceIdentityV1_1",
                    "RequiredMemberManifestIdentityV1_1",
                    "RequiredMemberSetProposalIdentityV1_1",
                    "PublicationCrosswalkIdentityV1_1",
                }
            )
        identity_schema["properties"]["contract_kind"] = {
            "const": kind,
            "title": "Contract Kind",
            "type": "string",
        }
        identity_schema["properties"]["contract_version"] = {
            "const": version,
            "default": version,
            "title": "Contract Version",
            "type": "string",
        }
        catalog[(kind, version)] = {
            "contract_kind": kind,
            "contract_version": version,
            "schema": schema,
        }
    return catalog


def write_registered_schemas(output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for (kind, version), entry in schema_catalog().items():
        filename = f"{kind.replace('.', '-')}-{version}.schema.json"
        content = canonical_json(entry)
        (output_dir / filename).write_text(content + "\n", encoding="utf-8")
        hashes[f"{kind}@{version}"] = canonical_sha256(entry)
        if version == CONTRACT_VERSION:
            hashes[kind] = hashes[f"{kind}@{version}"]
    index = {
        "registry_version": "1.5.0",
        "schemas": [
            {
                "contract_kind": kind,
                "contract_version": version,
                "schema_hash": hashes[f"{kind}@{version}"],
                "path": f"{kind.replace('.', '-')}-{version}.schema.json",
            }
            for kind, version in sorted(REGISTERED_CONTRACT_VERSIONS)
        ],
    }
    (output_dir / "registry.json").write_text(
        canonical_json(index) + "\n", encoding="utf-8"
    )
    return hashes
