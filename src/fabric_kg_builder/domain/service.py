"""Load, save, hash, and convert domain contracts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from fabric_kg_builder.enrichment.domain import DomainBrief, load_domain_brief

from .models import (
    DOMAIN_SCHEMA_VERSION,
    AmbiguousTerm,
    ApprovalMetadata,
    BusinessSection,
    CandidateModelSection,
    CanonicalTerm,
    ConstraintsSection,
    DomainContract,
    DomainSection,
    ExamplesSection,
    NegativeExample,
    PositiveExample,
    ProblemSection,
    TerminologySection,
)


class DomainContractError(Exception):
    """Base exception for contract operations."""


class DomainContractParseError(DomainContractError):
    """Raised when YAML cannot be parsed safely."""


class DomainContractValidationError(DomainContractError):
    """Raised when the contract does not match the schema."""


class DomainContractCompatibilityError(DomainContractError):
    """Raised when legacy compatibility handling is required."""


def utc_now_text() -> str:
    """Return an RFC3339 UTC timestamp."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_domain_contract() -> DomainContract:
    """Return a schema-valid draft contract scaffold."""
    return DomainContract(
        schema_version=DOMAIN_SCHEMA_VERSION,
        domain=DomainSection(
            name="TODO: replace with the primary domain name",
            description="TODO: describe the domain, its information assets, and the decisions this graph supports.",
            subdomains=["TODO: add one or more subdomains"],
        ),
        business=BusinessSection(
            organization_context="TODO: describe the organization or operating context for this domain.",
            users=["TODO: list primary users or personas"],
            decisions=["TODO: list one or more supported decisions"],
        ),
        problem=ProblemSection(
            statement="TODO: state the business problem or knowledge gap that the graph must address.",
            desired_outcomes=[
                "TODO: describe the measurable outcome the graph should enable."
            ],
            in_scope=["TODO: list in-scope concepts"],
            out_of_scope=["TODO: list out-of-scope concepts"],
        ),
        competency_questions=[
            "TODO: what question should the graph answer for a domain expert?"
        ],
        terminology=TerminologySection(
            canonical_terms=[
                CanonicalTerm(
                    term="TODO: add a canonical term",
                    definition="TODO: define the term precisely.",
                    synonyms=["TODO: add synonyms if relevant"],
                )
            ],
            ambiguous_terms=[
                AmbiguousTerm(
                    term="TODO: add an ambiguous term",
                    meanings=["TODO: explain possible meanings"],
                )
            ],
        ),
        candidate_model=CandidateModelSection(
            entity_categories=["TODO: add entity categories"],
            relationship_categories=["TODO: add relationship categories"],
        ),
        constraints=ConstraintsSection(
            temporal=["TODO: capture temporal constraints or assumptions"],
            regulatory=["TODO: capture regulatory requirements if any"],
            privacy=["TODO: capture privacy constraints if any"],
            safety=["TODO: capture safety constraints if any"],
        ),
        examples=ExamplesSection(
            positive=[
                PositiveExample(
                    text="TODO: add a representative positive example.",
                    expected=["TODO: list the expected extracted facts."],
                )
            ],
            negative=[
                NegativeExample(
                    text="TODO: add a representative negative example.",
                    reason="TODO: explain why it should not produce a graph fact.",
                )
            ],
        ),
        approval=ApprovalMetadata(status="draft"),
    )


def _derive_name_from_text(text: str) -> str:
    """Derive a human-readable name from legacy text."""
    cleaned = re.sub(r"\s+", " ", text).strip(" .")
    if not cleaned:
        return "Legacy Domain Contract"
    words = cleaned.split()
    candidate = " ".join(words[:5]).strip()
    return candidate[:80]


def render_domain_contract_yaml(contract: DomainContract) -> str:
    """Serialize a contract to stable UTF-8 YAML."""
    return yaml.safe_dump(
        contract.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _hash_payload(contract: DomainContract) -> dict:
    """Return the canonical hash payload excluding approval metadata."""
    payload = contract.model_dump(mode="json")
    payload.pop("approval", None)
    return payload


def render_hashable_contract_yaml(contract: DomainContract) -> str:
    """Serialize the hash payload to canonical YAML."""
    return yaml.safe_dump(
        _hash_payload(contract),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def compute_contract_hash(contract: DomainContract) -> str:
    """Return the SHA-256 hash of canonical YAML bytes."""
    canonical_yaml = render_hashable_contract_yaml(contract).encode("utf-8")
    return hashlib.sha256(canonical_yaml).hexdigest()


def load_domain_contract(path: Path | str) -> DomainContract:
    """Parse a YAML contract from disk with stable syntax and schema errors."""
    contract_path = Path(path)
    try:
        raw_text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DomainContractError(
            f"Could not read domain contract '{contract_path}': {exc}"
        ) from exc

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.MarkedYAMLError as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        column = getattr(getattr(exc, "problem_mark", None), "column", None)
        if line is not None and column is not None:
            raise DomainContractParseError(
                f"YAML syntax error in '{contract_path}' at line {line + 1}, column {column + 1}: {exc.problem or exc}"
            ) from exc
        raise DomainContractParseError(
            f"YAML syntax error in '{contract_path}': {exc}"
        ) from exc

    if loaded is None:
        raise DomainContractValidationError(
            f"Domain contract '{contract_path}' is empty."
        )
    if not isinstance(loaded, dict):
        raise DomainContractValidationError(
            f"Domain contract '{contract_path}' must be a YAML mapping."
        )

    try:
        return DomainContract.model_validate(loaded)
    except ValidationError as exc:
        raise DomainContractValidationError(
            f"Domain contract '{contract_path}' failed schema validation: {exc}"
        ) from exc


def save_domain_contract(contract: DomainContract, path: Path | str) -> None:
    """Write a contract to disk as stable YAML."""
    contract_path = Path(path)
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(render_domain_contract_yaml(contract), encoding="utf-8")


def save_json_document(data: dict, path: Path | str) -> None:
    """Persist a JSON document with UTF-8 encoding."""
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_domain_review_file(path: Path | str) -> dict:
    """Load raw review JSON from disk."""
    review_path = Path(path)
    try:
        return json.loads(review_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DomainContractError(
            f"Could not read domain review '{review_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DomainContractValidationError(
            f"Domain review '{review_path}' is not valid JSON: {exc}"
        ) from exc


def review_path_for_contract(path: Path | str) -> Path:
    """Return the default review sidecar path for a contract."""
    return Path(path).with_suffix(".review.json")


def proposal_path_for_contract(path: Path | str) -> Path:
    """Return the default proposal YAML sidecar path for a contract."""
    return Path(path).with_suffix(".review.proposed.yaml")


def convert_legacy_brief_to_contract(brief: DomainBrief) -> DomainContract:
    """Convert the legacy JSON brief into a draft v1 domain contract."""
    users = ["TODO: identify the primary users supported by this graph."]
    decisions = ["TODO: describe a key decision this graph should support."]
    desired_outcomes = [
        "TODO: capture the desired outcomes that success looks like for this domain."
    ]
    questions = brief.competency_questions or [
        "TODO: add competency questions for the converted legacy contract."
    ]
    notes = [
        "Converted from legacy domain.json.",
        "Review and complete the missing v1 business, scope, constraints, and example details before approval.",
    ]
    return DomainContract(
        schema_version=DOMAIN_SCHEMA_VERSION,
        domain=DomainSection(
            name=_derive_name_from_text(brief.business_domain or brief.domain_brief),
            description=brief.domain_brief,
            subdomains=[brief.business_domain] if brief.business_domain else [],
        ),
        business=BusinessSection(
            organization_context="TODO: add organization or business context for the converted legacy domain.",
            users=users,
            decisions=decisions,
        ),
        problem=ProblemSection(
            statement=brief.domain_brief,
            desired_outcomes=desired_outcomes,
            in_scope=[
                item.lower().replace("_", " ")
                for item in brief.key_entity_types
            ],
            out_of_scope=[],
        ),
        competency_questions=questions,
        terminology=TerminologySection(
            canonical_terms=[
                CanonicalTerm(
                    term=item.lower().replace("_", " "),
                    definition="TODO: add a precise definition for the converted legacy term.",
                    synonyms=[],
                )
                for item in brief.key_entity_types[:5]
            ],
            ambiguous_terms=[],
        ),
        candidate_model=CandidateModelSection(
            entity_categories=brief.key_entity_types,
            relationship_categories=brief.key_relationship_types,
        ),
        constraints=ConstraintsSection(
            temporal=[],
            regulatory=[],
            privacy=["TODO: add privacy constraints for the converted legacy contract."],
            safety=[
                "TODO: add safety or action constraints for the converted legacy contract."
            ],
        ),
        examples=ExamplesSection(
            positive=[
                PositiveExample(
                    text=brief.source_domain_text or brief.domain_brief,
                    expected=[
                        "TODO: add the expected grounded facts for this converted example."
                    ],
                )
            ],
            negative=[
                NegativeExample(
                    text="TODO: add a negative example for the converted legacy contract.",
                    reason="TODO: explain why the example should not produce a graph fact.",
                )
            ],
        ),
        approval=ApprovalMetadata(status="needs_review", notes=notes),
    )


def load_legacy_domain_brief(path: Path | str) -> DomainBrief:
    """Load the legacy JSON brief format."""
    try:
        return load_domain_brief(path)
    except (FileNotFoundError, ValidationError, json.JSONDecodeError) as exc:
        raise DomainContractCompatibilityError(
            f"Legacy domain brief '{path}' could not be loaded: {exc}"
        ) from exc


def domain_contract_to_legacy_brief(contract: DomainContract) -> DomainBrief:
    """Adapt the v1 contract into the legacy brief used by enrichment prompts."""
    constraint_items = (
        contract.constraints.temporal
        + contract.constraints.regulatory
        + contract.constraints.privacy
        + contract.constraints.safety
    )
    summary_parts = [
        f"{contract.domain.name}: {contract.domain.description}",
        f"Organization context: {contract.business.organization_context}",
        f"Problem: {contract.problem.statement}",
    ]
    if contract.problem.desired_outcomes:
        summary_parts.append(
            "Desired outcomes: " + "; ".join(contract.problem.desired_outcomes)
        )
    return DomainBrief(
        domain_brief=" ".join(summary_parts),
        industry=contract.domain.name,
        business_domain=contract.domain.subdomains[0]
        if contract.domain.subdomains
        else contract.domain.name,
        key_entity_types=contract.candidate_model.entity_categories,
        key_relationship_types=contract.candidate_model.relationship_categories,
        extraction_constraints=constraint_items,
        competency_questions=contract.competency_questions,
        source_domain_text=contract.domain.description,
    )
