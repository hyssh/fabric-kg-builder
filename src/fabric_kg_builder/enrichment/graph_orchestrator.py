"""Graph extraction orchestrator (Fix #3).

Offline pipeline: chunks → SubgraphOccurrences → merge → summarize → claims → hierarchy
→ GraphExtractionResult. Injectable extractor/summarizer/claim_extractor.
No network calls; all LLM calls behind injected protocols.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from fabric_kg_builder.graph.claims import (
    ClaimExtractorProtocol,
    DeterministicClaimExtractor,
)
from fabric_kg_builder.graph.community import build_community_hierarchy, INSUFFICIENT_HIERARCHY_EVIDENCE
from fabric_kg_builder.graph.occurrence import EntityOccurrence, RelationshipOccurrence, SubgraphOccurrence
from fabric_kg_builder.graph.persistence import (
    GraphExtractionResult,
    MergedEntityRecord,
    MergedRelationshipRecord,
)
from fabric_kg_builder.graph.relationship import MergedRelationship, merge_relationship_occurrences
from fabric_kg_builder.graph.resolution import ResolutionResult as _ResResult, resolve_candidates
from fabric_kg_builder.graph.summarizer import (
    DeterministicSummarizer,
    SummarizerProtocol,
    consolidate_description_typed,
)
from fabric_kg_builder.model.schemas import ClaimEvidenceRow, EntityRow, RelationshipRow


@runtime_checkable
class SubgraphExtractorProtocol(Protocol):
    def extract(self, text: str, text_unit_id: str, domain_hash: Optional[str]) -> SubgraphOccurrence: ...


class DummyExtractor:
    """No-op extractor that produces empty subgraphs (for testing/offline use)."""
    def extract(self, text: str, text_unit_id: str, domain_hash: Optional[str] = None) -> SubgraphOccurrence:
        from fabric_kg_builder.model.ids import make_id
        oid = make_id("subgraph", f"{text_unit_id}:{domain_hash or ''}")
        return SubgraphOccurrence(
            occurrence_id=oid,
            text_unit_id=text_unit_id,
            domain_hash=domain_hash,
            entity_occurrences=[],
            relationship_occurrences=[],
        )


@dataclass
class GraphOrchestrator:
    """Coordinates offline graph extraction pipeline."""
    extractor: SubgraphExtractorProtocol = field(default_factory=DummyExtractor)
    summarizer: SummarizerProtocol = field(default_factory=DeterministicSummarizer)
    claim_extractor: Optional[ClaimExtractorProtocol] = None

    def run(
        self,
        chunks: list[dict],  # each: {"text_unit_id": str, "text": str}
        *,
        source_id: str = "unknown",
        domain_hash: Optional[str] = None,
        seed: int = 42,
        run_id: str = "",
    ) -> GraphExtractionResult:
        """Run end-to-end pipeline on a list of text chunks."""
        all_entity_occs: list[EntityOccurrence] = []
        all_rel_occs: list[RelationshipOccurrence] = []

        # Phase 1: Extract subgraph occurrences from each chunk
        for chunk in chunks:
            text_unit_id = chunk["text_unit_id"]
            text = chunk.get("text", "")
            subgraph = self.extractor.extract(text, text_unit_id, domain_hash)
            all_entity_occs.extend(subgraph.entity_occurrences)
            all_rel_occs.extend(subgraph.relationship_occurrences)

        # Phase 2: Block + resolve entities
        # Build simple EntityRow proxies from occurrences for blocking
        entity_proxies = self._occurrences_to_proxy_entities(all_entity_occs)
        decisions = resolve_candidates(entity_proxies)

        # Build canonical_id map (SAME → keep lower ID)
        from fabric_kg_builder.graph.resolution import ResolutionDecision
        canonical_map: dict[str, str] = {}
        for d in decisions:
            if d.decision == ResolutionDecision.SAME:
                keep = min(d.entity_a_id, d.entity_b_id)
                drop = max(d.entity_a_id, d.entity_b_id)
                canonical_map[drop] = keep

        def _canonical(eid: str) -> str:
            while canonical_map.get(eid, eid) != eid:
                eid = canonical_map[eid]
            return eid

        # Phase 3: Build merged entity records
        merged_entity_data: dict[str, dict] = {}
        proxy_map = {e.entity_id: e for e in entity_proxies}
        for occ in all_entity_occs:
            canonical_id = _canonical(occ.local_id)
            if canonical_id not in merged_entity_data:
                proxy = proxy_map.get(occ.local_id)
                merged_entity_data[canonical_id] = {
                    "entity_id": canonical_id,
                    "display_name": occ.display_name,
                    "entity_type": occ.entity_type,
                    "occurrence_ids": [],
                    "descriptions": [],
                    "all_evidence_ids": [],
                    "aliases": list(occ.aliases),
                }
            entry = merged_entity_data[canonical_id]
            entry["occurrence_ids"].append(occ.local_id)
            if occ.description and occ.description not in entry["descriptions"]:
                entry["descriptions"].append(occ.description)
            for eid in occ.evidence_ids:
                if eid not in entry["all_evidence_ids"]:
                    entry["all_evidence_ids"].append(eid)
            for alias in occ.aliases:
                if alias not in entry["aliases"]:
                    entry["aliases"].append(alias)

        # Consolidate descriptions
        merged_entities: list[MergedEntityRecord] = []
        for canonical_id, data in merged_entity_data.items():
            typed_result = consolidate_description_typed(
                data["descriptions"],
                summarizer=self.summarizer,
                occurrence_ids=data["occurrence_ids"],
                evidence_ids=data["all_evidence_ids"],
            )
            merged_entities.append(MergedEntityRecord(
                entity_id=canonical_id,
                display_name=data["display_name"],
                entity_type=data["entity_type"],
                description=typed_result.summary,
                occurrence_ids=data["occurrence_ids"],
                descriptions=data["descriptions"],
                all_evidence_ids=data["all_evidence_ids"],
                aliases=data["aliases"],
            ))

        # Phase 4: Merge relationships (canonical entity IDs)
        rel_groups: dict[tuple[str, str, str], list[RelationshipOccurrence]] = {}
        for rel_occ in all_rel_occs:
            src_can = _canonical(rel_occ.source_local_id)
            tgt_can = _canonical(rel_occ.target_local_id)
            key = (src_can, rel_occ.relationship_type, tgt_can)
            rel_groups.setdefault(key, []).append(rel_occ)

        merged_relationships: list[MergedRelationshipRecord] = []
        for (src, rel_type, tgt), occs in rel_groups.items():
            merged_list = merge_relationship_occurrences(occs)
            merged_rel = merged_list[0] if merged_list else None
            if not merged_rel:
                continue
            merged_relationships.append(MergedRelationshipRecord(
                relationship_id=f"rel:{src}:{rel_type}:{tgt}",
                source_entity_id=src,
                target_entity_id=tgt,
                relationship_type=rel_type,
                description=merged_rel.primary_description,
                occurrence_ids=merged_rel.occurrence_ids,
                descriptions=merged_rel.descriptions,
                all_evidence_ids=merged_rel.all_evidence_ids,
            ))

        # Phase 5: Extract evidence-backed claims for each entity occurrence.
        claims: list[dict] = []
        claim_evidence: list[dict] = []
        chunk_text = {
            str(chunk["text_unit_id"]): str(chunk.get("text", ""))
            for chunk in chunks
        }
        claim_extractor = self.claim_extractor or DeterministicClaimExtractor()
        seen_claim_ids: set[str] = set()
        seen_evidence_links: set[tuple[str, str, Optional[str]]] = set()

        for occ in all_entity_occs:
            if not occ.evidence_ids:
                continue
            text = occ.span.text if occ.span is not None else chunk_text.get(
                occ.text_unit_id, ""
            )
            if not text.strip():
                continue
            subject_entity_id = _canonical(occ.local_id)
            result = claim_extractor.extract(
                text,
                subject_entity_id,
                evidence_id=occ.evidence_ids[0],
                occurrence_id=occ.local_id,
                domain_hash=domain_hash,
                run_id=run_id,
            )
            for claim in result.claims:
                if claim.claim_id in seen_claim_ids:
                    continue
                seen_claim_ids.add(claim.claim_id)
                claims.append(claim.model_dump(mode="json"))
            links = list(result.evidence_links)
            for evidence_id in occ.evidence_ids[1:]:
                for claim in result.claims:
                    links.append(
                        ClaimEvidenceRow(
                            claim_id=claim.claim_id,
                            evidence_id=evidence_id,
                            occurrence_id=occ.local_id,
                            support_type="supports",
                            confidence=claim.confidence,
                        )
                    )
            for link in links:
                key = (link.claim_id, link.evidence_id, link.occurrence_id)
                if key in seen_evidence_links:
                    continue
                seen_evidence_links.add(key)
                claim_evidence.append(link.model_dump(mode="json"))

        claim_contradictions = self._find_claim_contradictions(claims)

        # Phase 6: Build hierarchy
        entity_rows = self._merged_to_entity_rows(merged_entities)
        rel_rows = self._merged_to_rel_rows(merged_relationships)
        hierarchy_result = build_community_hierarchy(
            entity_rows, rel_rows, seed=seed, domain_hash=domain_hash, run_id=run_id
        )

        if hasattr(hierarchy_result, "method") and hierarchy_result.method == INSUFFICIENT_HIERARCHY_EVIDENCE:
            clusters_dict: list[dict] = []
            memberships_dict: list[dict] = []
        else:
            clusters_dict = [c.model_dump() for c in hierarchy_result.clusters]
            memberships_dict = [m.model_dump() for m in hierarchy_result.memberships]

        return GraphExtractionResult(
            source_id=source_id,
            domain_hash=domain_hash,
            entity_occurrences=all_entity_occs,
            relationship_occurrences=all_rel_occs,
            merged_entities=merged_entities,
            merged_relationships=merged_relationships,
            claims=claims,
            claim_evidence=claim_evidence,
            claim_contradictions=claim_contradictions,
            hierarchy_clusters=clusters_dict,
            hierarchy_memberships=memberships_dict,
        )

    @staticmethod
    def _find_claim_contradictions(
        claims: list[dict],
    ) -> list[tuple[str, str]]:
        grouped: dict[tuple[str, str], list[dict]] = {}
        for claim in claims:
            key = (
                str(claim.get("subject_entity_id", "")),
                str(claim.get("predicate", "")),
            )
            grouped.setdefault(key, []).append(claim)

        pairs: set[tuple[str, str]] = set()
        for group in grouped.values():
            asserted = [
                claim for claim in group if claim.get("status") == "asserted"
            ]
            negative = [
                claim
                for claim in group
                if claim.get("status") in {"retracted", "disputed"}
            ]
            for left in asserted:
                for right in negative:
                    pairs.add((str(left["claim_id"]), str(right["claim_id"])))
        return sorted(pairs)

    def _occurrences_to_proxy_entities(self, occs: list[EntityOccurrence]) -> list[EntityRow]:
        from datetime import datetime, timezone
        from fabric_kg_builder.model.schemas import EntityRow
        from fabric_kg_builder.model.ids import normalize_canonical_key, content_hash as _ch
        rows: list[EntityRow] = []
        seen: set[str] = set()
        now = datetime.now(timezone.utc)
        for occ in occs:
            if occ.local_id in seen:
                continue
            seen.add(occ.local_id)
            ck = normalize_canonical_key(occ.entity_type, occ.display_name)
            ch = _ch(f"{occ.entity_type}:{occ.display_name}:{occ.local_id}")
            rows.append(EntityRow(
                entity_id=occ.local_id,
                entity_type=occ.entity_type,
                display_name=occ.display_name,
                canonical_key=ck,
                description=occ.description,
                aliases=occ.aliases if occ.aliases else None,
                confidence=occ.confidence,
                content_hash=ch,
                created_at=now,
                updated_at=now,
            ))
        return rows

    def _merged_to_entity_rows(self, merged: list[MergedEntityRecord]) -> list[EntityRow]:
        from datetime import datetime, timezone
        from fabric_kg_builder.model.schemas import EntityRow
        from fabric_kg_builder.model.ids import normalize_canonical_key, content_hash as _ch
        rows: list[EntityRow] = []
        now = datetime.now(timezone.utc)
        for m in merged:
            ck = normalize_canonical_key(m.entity_type, m.display_name)
            ch = _ch(f"{m.entity_type}:{m.display_name}:{m.entity_id}")
            rows.append(EntityRow(
                entity_id=m.entity_id,
                entity_type=m.entity_type,
                display_name=m.display_name,
                canonical_key=ck,
                description=m.description,
                aliases=m.aliases if m.aliases else None,
                confidence=1.0,
                content_hash=ch,
                created_at=now,
                updated_at=now,
            ))
        return rows

    def _merged_to_rel_rows(self, merged: list[MergedRelationshipRecord]) -> list[RelationshipRow]:
        from datetime import datetime, timezone
        from fabric_kg_builder.model.schemas import RelationshipRow
        from fabric_kg_builder.model.ids import content_hash as _ch
        rows: list[RelationshipRow] = []
        now = datetime.now(timezone.utc)
        for r in merged:
            ch = _ch(f"{r.relationship_type}:{r.source_entity_id}:{r.target_entity_id}")
            rows.append(RelationshipRow(
                relationship_id=r.relationship_id,
                source_entity_id=r.source_entity_id,
                target_entity_id=r.target_entity_id,
                relationship_type=r.relationship_type,
                confidence=1.0,
                content_hash=ch,
                created_at=now,
            ))
        return rows
