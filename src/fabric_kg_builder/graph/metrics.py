"""GRP-014 (revised): Quality metrics and gold evaluation contracts.

Fix #13:
- DomainEvaluationContract with gold entities/relationships/claims + domain_name
- EvaluationMetrics with entity/relationship precision/recall/f1, groundedness, coherence
- evaluate_against_gold() function with threshold enforcement
- Three-domain: supply-chain, legal/contracts, facilities
- Keep existing QualityMetrics / compute_quality_metrics
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Existing QualityMetrics (kept for backward compat)
# ---------------------------------------------------------------------------

@dataclass
class QualityMetrics:
    total_entities: int = 0
    total_relationships: int = 0
    total_claims: int = 0
    avg_confidence: float = 0.0
    entities_without_descriptions: int = 0
    relationships_without_evidence: int = 0
    claims_with_invalid_status: int = 0
    threshold_failures: list[str] = field(default_factory=list)
    passed: bool = True


def compute_quality_metrics(
    entities: list,
    relationships: list,
    claims: list,
    *,
    min_avg_confidence: float = 0.5,
    max_missing_descriptions: float = 0.3,
) -> QualityMetrics:
    m = QualityMetrics(
        total_entities=len(entities),
        total_relationships=len(relationships),
        total_claims=len(claims),
    )
    if entities:
        m.avg_confidence = sum(
            getattr(e, "confidence", 1.0) or 1.0 for e in entities
        ) / len(entities)
        m.entities_without_descriptions = sum(
            1 for e in entities if not getattr(e, "description", None)
        )
    if claims:
        valid = {"asserted", "retracted", "disputed"}
        m.claims_with_invalid_status = sum(
            1 for c in claims if getattr(c, "status", "asserted") not in valid
        )
    failures: list[str] = []
    if m.avg_confidence < min_avg_confidence:
        failures.append(
            f"avg_confidence {m.avg_confidence:.2f} < threshold {min_avg_confidence}"
        )
    if entities and (m.entities_without_descriptions / len(entities)) > max_missing_descriptions:
        failures.append(
            f"missing descriptions {m.entities_without_descriptions}/{len(entities)} "
            f"> {max_missing_descriptions:.0%}"
        )
    m.threshold_failures = failures
    m.passed = not failures
    return m


# ---------------------------------------------------------------------------
# Gold evaluation contracts (Fix #13)
# ---------------------------------------------------------------------------

@dataclass
class GoldEntity:
    display_name: str
    entity_type: str
    description: str = ""


@dataclass
class GoldRelationship:
    source_name: str
    target_name: str
    relationship_type: str


@dataclass
class GoldClaim:
    predicate: str
    status: str = "asserted"


@dataclass
class DomainEvaluationContract:
    domain_name: str
    gold_entities: list[GoldEntity]
    gold_relationships: list[GoldRelationship]
    gold_claims: list[GoldClaim] = field(default_factory=list)
    description: str = ""
    # minimum acceptable metrics
    min_entity_f1: float = 0.7
    min_relationship_f1: float = 0.5
    min_groundedness: float = 0.6
    min_coherence: float = 0.5


@dataclass
class EvaluationMetrics:
    domain_name: str
    entity_precision: float = 0.0
    entity_recall: float = 0.0
    entity_f1: float = 0.0
    relationship_precision: float = 0.0
    relationship_recall: float = 0.0
    relationship_f1: float = 0.0
    groundedness_score: float = 0.0  # fraction of predicted entities with non-empty descriptions
    coherence_score: float = 0.0     # fraction of relationships referencing valid entity names
    claims_precision: float = 0.0
    claims_recall: float = 0.0
    threshold_failures: list[str] = field(default_factory=list)
    passed: bool = True


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def evaluate_against_gold(
    contract: DomainEvaluationContract,
    predicted_entities: list,
    predicted_relationships: list,
    predicted_claims: list | None = None,
) -> EvaluationMetrics:
    """Compare predicted outputs against gold contract for a domain."""
    gold_enames = {_normalize_name(g.display_name) for g in contract.gold_entities}
    pred_enames = {_normalize_name(getattr(e, "display_name", "")) for e in predicted_entities}

    tp_e = len(gold_enames & pred_enames)
    entity_precision = tp_e / len(pred_enames) if pred_enames else 0.0
    entity_recall = tp_e / len(gold_enames) if gold_enames else 0.0
    entity_f1 = _f1(entity_precision, entity_recall)

    # Relationship matching (source_type, relationship_type, target_type by name)
    gold_rels = {
        (_normalize_name(r.source_name), r.relationship_type, _normalize_name(r.target_name))
        for r in contract.gold_relationships
    }
    pred_rels: set[tuple[str, str, str]] = set()
    for r in predicted_relationships:
        # Find entity names from IDs if possible
        src = _normalize_name(str(getattr(r, "source_entity_id", "")))
        tgt = _normalize_name(str(getattr(r, "target_entity_id", "")))
        rtype = getattr(r, "relationship_type", "")
        pred_rels.add((src, rtype, tgt))

    tp_r = len(gold_rels & pred_rels)
    rel_precision = tp_r / len(pred_rels) if pred_rels else 0.0
    rel_recall = tp_r / len(gold_rels) if gold_rels else 0.0
    rel_f1 = _f1(rel_precision, rel_recall)

    # Groundedness: fraction of predicted entities with non-empty descriptions
    groundedness = 0.0
    if predicted_entities:
        with_desc = sum(1 for e in predicted_entities if getattr(e, "description", None))
        groundedness = with_desc / len(predicted_entities)

    # Coherence: fraction of relationships whose source/target IDs appear in entity list
    coherence = 0.0
    if predicted_relationships:
        entity_id_set = {getattr(e, "entity_id", "") for e in predicted_entities}
        coherent = sum(
            1 for r in predicted_relationships
            if getattr(r, "source_entity_id", "") in entity_id_set
            and getattr(r, "target_entity_id", "") in entity_id_set
        )
        coherence = coherent / len(predicted_relationships)

    # Claims (optional)
    claims_precision = 0.0
    claims_recall = 0.0
    if predicted_claims is not None and contract.gold_claims:
        gold_cpred = {_normalize_name(g.predicate) for g in contract.gold_claims}
        pred_cpred = {_normalize_name(getattr(c, "predicate", "")) for c in predicted_claims}
        tp_c = len(gold_cpred & pred_cpred)
        claims_precision = tp_c / len(pred_cpred) if pred_cpred else 0.0
        claims_recall = tp_c / len(gold_cpred) if gold_cpred else 0.0

    failures: list[str] = []
    if entity_f1 < contract.min_entity_f1:
        failures.append(f"entity_f1={entity_f1:.2f} < threshold {contract.min_entity_f1}")
    if rel_f1 < contract.min_relationship_f1:
        failures.append(f"relationship_f1={rel_f1:.2f} < threshold {contract.min_relationship_f1}")
    if groundedness < contract.min_groundedness:
        failures.append(f"groundedness={groundedness:.2f} < threshold {contract.min_groundedness}")
    if coherence < contract.min_coherence:
        failures.append(f"coherence={coherence:.2f} < threshold {contract.min_coherence}")

    return EvaluationMetrics(
        domain_name=contract.domain_name,
        entity_precision=entity_precision,
        entity_recall=entity_recall,
        entity_f1=entity_f1,
        relationship_precision=rel_precision,
        relationship_recall=rel_recall,
        relationship_f1=rel_f1,
        groundedness_score=groundedness,
        coherence_score=coherence,
        claims_precision=claims_precision,
        claims_recall=claims_recall,
        threshold_failures=failures,
        passed=not failures,
    )


# ---------------------------------------------------------------------------
# Three canonical domain fixtures (supply_chain, legal, facilities)
# ---------------------------------------------------------------------------

SUPPLY_CHAIN_CONTRACT = DomainEvaluationContract(
    domain_name="supply_chain",
    description="Automotive supply chain entities and relationships",
    gold_entities=[
        GoldEntity("Acme Corp", "org", "Tier-1 supplier of precision parts"),
        GoldEntity("BrakeXcel", "product", "High-performance brake system"),
        GoldEntity("Detroit Plant", "location", "Primary assembly facility"),
        GoldEntity("FastFreight Inc", "org", "Logistics carrier"),
        GoldEntity("PO-2024-001", "document", "Purchase order for brake systems"),
        GoldEntity("Incoming Inspection", "process", "Quality gate on received goods"),
        GoldEntity("Steel Grade A36", "product", "Structural steel for frames"),
        GoldEntity("Harbor Port", "location", "Import port for raw materials"),
        GoldEntity("Quality Control", "process", "End-of-line testing"),
        GoldEntity("Compliance Report", "document", "Regulatory compliance document"),
        GoldEntity("Widget Alpha", "product", "Core manufactured product"),
        GoldEntity("Supplier B", "org", "Secondary raw material supplier"),
    ],
    gold_relationships=[
        GoldRelationship("Acme Corp", "supplies", "BrakeXcel"),
        GoldRelationship("BrakeXcel", "ships_to", "Detroit Plant"),
        GoldRelationship("FastFreight Inc", "carries", "BrakeXcel"),
        GoldRelationship("Detroit Plant", "processes", "Incoming Inspection"),
    ],
    gold_claims=[
        GoldClaim("BrakeXcel meets ISO 9001 certification"),
        GoldClaim("Acme Corp is approved vendor"),
    ],
    min_entity_f1=0.5,
    min_relationship_f1=0.3,
    min_groundedness=0.7,
    min_coherence=0.5,
)

LEGAL_CONTRACT = DomainEvaluationContract(
    domain_name="legal_contracts",
    description="Contract and obligation extraction from legal text",
    gold_entities=[
        GoldEntity("Alpha Services LLC", "org", "Service provider party"),
        GoldEntity("Beta Corp", "org", "Client party"),
        GoldEntity("Master Services Agreement", "document", "Primary contract"),
        GoldEntity("Payment Obligation", "concept", "Financial obligation"),
        GoldEntity("Liability Cap", "concept", "Maximum liability clause"),
        GoldEntity("Notice Period", "concept", "Required notice before termination"),
        GoldEntity("Confidentiality Clause", "document", "Non-disclosure obligation"),
        GoldEntity("Arbitration", "process", "Dispute resolution mechanism"),
        GoldEntity("Governing Law", "concept", "Jurisdiction for disputes"),
        GoldEntity("Effective Date", "concept", "Contract commencement date"),
        GoldEntity("Appendix A", "document", "Scope of work appendix"),
        GoldEntity("Performance Standard", "concept", "Service level obligation"),
    ],
    gold_relationships=[
        GoldRelationship("Alpha Services LLC", "party_to", "Master Services Agreement"),
        GoldRelationship("Beta Corp", "party_to", "Master Services Agreement"),
        GoldRelationship("Payment Obligation", "defined_in", "Master Services Agreement"),
        GoldRelationship("Arbitration", "resolves", "Liability Cap"),
    ],
    gold_claims=[
        GoldClaim("Alpha Services LLC shall deliver within 30 days"),
        GoldClaim("Liability Cap is 2x annual contract value"),
    ],
    min_entity_f1=0.5,
    min_relationship_f1=0.3,
    min_groundedness=0.7,
    min_coherence=0.5,
)

FACILITIES_CONTRACT = DomainEvaluationContract(
    domain_name="facilities",
    description="Facilities management and technical drawing entities",
    gold_entities=[
        GoldEntity("HVAC-Unit-A", "equipment", "Air handling unit"),
        GoldEntity("Zone 1A", "location", "North wing zone"),
        GoldEntity("Cooling Tower CT-1", "equipment", "Rooftop cooling tower"),
        GoldEntity("Maintenance Plan MP-2024", "document", "Annual maintenance schedule"),
        GoldEntity("Boiler Room", "location", "Basement boiler facility"),
        GoldEntity("Sprinkler System", "equipment", "Fire suppression system"),
        GoldEntity("Emergency Exit E-1", "location", "East wing exit"),
        GoldEntity("Elevator E2", "equipment", "Main service elevator"),
        GoldEntity("Work Order WO-1042", "document", "Repair work order"),
        GoldEntity("Safety Inspection", "process", "Annual safety audit"),
        GoldEntity("Air Handler AHU-3", "equipment", "Secondary air handler"),
        GoldEntity("Room 204", "location", "Conference room"),
    ],
    gold_relationships=[
        GoldRelationship("HVAC-Unit-A", "serves", "Zone 1A"),
        GoldRelationship("Cooling Tower CT-1", "connects_to", "HVAC-Unit-A"),
        GoldRelationship("Maintenance Plan MP-2024", "covers", "HVAC-Unit-A"),
        GoldRelationship("Sprinkler System", "located_at", "Zone 1A"),
    ],
    gold_claims=[
        GoldClaim("HVAC-Unit-A last serviced 2024-01"),
        GoldClaim("Cooling Tower CT-1 requires annual inspection"),
    ],
    min_entity_f1=0.5,
    min_relationship_f1=0.3,
    min_groundedness=0.7,
    min_coherence=0.5,
)
