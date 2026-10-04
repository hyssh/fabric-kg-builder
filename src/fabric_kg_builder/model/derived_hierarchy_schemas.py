"""PyArrow schemas for the W3 serving/CLI artifact layer of the derived
(Schema-2 "2.0-derived-leiden") community hierarchy.

Scope note
----------
These schemas are the **on-disk serialization format** that the offline CLI
writes after invoking W2's ``build_derived_community_hierarchy[_safe]``
core. They are intentionally distinct from, and must never duplicate, W2's
in-memory hierarchy core/provider/contracts (``community_contracts.py``,
``community_hierarchy_v2.py``). W2 owns the Python dataclasses/Protocol
(``DerivedClusterRow``, ``DerivedMembershipRow``, ``DerivedHierarchyOutcome``,
``HierarchyStatus``, ``CommunityProvider``); this module only owns how a
*realized* outcome gets persisted as a new, independently versioned derived
artifact.

Round 4 correction (direct read of W2's real source, supersedes all prior
rounds below): W3 obtained read access to a checksum-verified snapshot of
W2's actual module bodies (not just a relayed field manifest) and corrected
every remaining divergence found:

- Cluster rows carry W2's full, real 17-field ``DerivedClusterRow`` shape,
  including ``hierarchy_version``, ``is_singleton_isolate``,
  ``is_oversized``, ``provider_id``, ``provider_version``, ``policy_id``,
  ``domain_hash`` (nullable), ``run_id``, ``execution_fingerprint`` (always
  populated per-row by the time an outcome reaches a caller), and
  ``relationship_contributed_evidence_ids`` (terminal-only *subset* of
  ``evidence_ids`` -- not a separately-propagated/derived table).
- ``evidence_ids`` is W2's full per-cluster evidence union (ancestor-
  inclusive on terminals); ``relationship_contributed_evidence_ids`` is a
  strict subset of it, always empty on non-terminal clusters. Neither is
  recomputed here -- both are carried through verbatim.
- ``native_depth`` is **nullable** (``Optional[int]`` on the real
  dataclass): a legitimate singleton/insufficient terminal has no native
  depth and carries ``None``, never a substituted ``0``.
- There is **no separate W2 relationship-evidence table/list**. The
  previously-planned ``derived_relationship_evidence`` schema was a W3
  invention with no counterpart in the real outcome shape and has been
  **removed**; relationship-sourced evidence is already fully represented
  via each terminal cluster's own ``relationship_contributed_evidence_ids``.
- The inter-community diagnostics table remains, but is now a **direct
  pass-through** of W2's real, non-nullable
  ``InterCommunityRelationshipDiagnostic`` (``relationship_id``,
  ``relationship_type``, ``source_entity_id``, ``target_entity_id``,
  ``source_cluster_id``, ``target_cluster_id``, ``reason`` -- all required
  strings, never W3-derived from sealed relationship rows).
- Membership rows mirror the real 4-field ``DerivedMembershipRow``
  (``cluster_id``, ``entity_id``, ``evidence_ids``, ``primary_membership``)
  plus W3-added outcome-level provenance columns (see below) since the real
  dataclass carries no run/provider identity of its own.
- W2's execution-identity hash functions
  (``compute_community_identity`` / ``compute_evidence_binding_hash`` /
  ``compute_execution_fingerprint``) were confirmed, by reading their
  bodies directly, to each return a bare, unprefixed 64-hex-character
  lowercase SHA-256 content digest (not a truncated, namespace-prefixed
  ``make_id(...)`` identifier). This is a breaking shape change from an
  earlier round's ``"dfingerprint:<32 hex chars>"`` id-shaped output.
  Every column here that carries one of those values is still typed as a
  plain string at the Arrow-schema level (schemas here do not themselves
  enforce shape), but callers constructing a real outcome/receipt should
  expect and validate the real bare-64-hex-lowercase-SHA-256 shape.

None of these tables reuse the legacy ``CLUSTERS_SCHEMA`` /
``CLUSTER_MEMBERSHIPS_SCHEMA`` arrow schemas (``hierarchy_version = "1.0"``),
per explicit instruction that W2 owns those fields and W3 must not lock a
competing schema against them.
"""

from __future__ import annotations

import pyarrow as pa

_STR = pa.string()
_INT32 = pa.int32()
_INT64 = pa.int64()
_BOOL = pa.bool_()
_LIST_STR = pa.list_(pa.string())

#: Version string for the derived hierarchy artifact *format itself*
#: (distinct from W2's hierarchy-core version string, e.g.
#: ``"2.0-derived-leiden"``, which is carried through verbatim as a column
#: value below -- never redefined or reinterpreted here).
DERIVED_HIERARCHY_ARTIFACT_CONTRACT_VERSION = "1.0.0"

#: Fallback hierarchy-core version string used only when an outcome itself
#: does not carry one (should not occur in practice: W2's
#: ``DerivedHierarchyOutcome.hierarchy_version`` always defaults to its own
#: real version constant). Kept as a last-resort default, never silently
#: substituted when the outcome does supply a value.
DERIVED_HIERARCHY_CORE_VERSION_FALLBACK = "2.0-derived-leiden"

CONTRACT_KIND_DERIVED_COMMUNITY_CLUSTERS = "schema2.derived_community_clusters"
CONTRACT_KIND_DERIVED_COMMUNITY_MEMBERSHIPS = "schema2.derived_community_memberships"
CONTRACT_KIND_DERIVED_INTER_COMMUNITY_DIAGNOSTICS = (
    "schema2.derived_inter_community_relationship_diagnostics"
)

#: W3-added run-level provenance, appended to every table so each parquet
#: file is independently joinable/auditable without the receipt. None of
#: these are W2 dataclass fields in their own right for the membership and
#: diagnostics tables; for the clusters table most are already-native
#: per-row fields and are *not* duplicated here (see
#: ``DERIVED_COMMUNITY_CLUSTERS_SCHEMA`` below).
_PROVENANCE_FIELDS = [
    pa.field("run_id", _STR, nullable=False),
    pa.field("config_id", _STR, nullable=False),
    pa.field("policy_id", _STR, nullable=False),
    pa.field("seed", _INT64, nullable=False),
    pa.field("hierarchy_version", _STR, nullable=False),
    pa.field("row_hash", _STR, nullable=False),
]

#: One row per derived community node (cluster) in the hierarchy. Field
#: names/nullability mirror W2's real ``DerivedClusterRow`` dataclass
#: exactly; ``config_id``, ``seed``, and ``row_hash`` are the only W3
#: additions (every other provenance field -- ``run_id``,
#: ``execution_fingerprint``, ``provider_id``, ``provider_version``,
#: ``policy_id``, ``hierarchy_version`` -- is already native per-row).
DERIVED_COMMUNITY_CLUSTERS_SCHEMA = pa.schema([
    pa.field("cluster_id", _STR, nullable=False),
    pa.field("hierarchy_version", _STR, nullable=False),
    pa.field("parent_cluster_id", _STR, nullable=True),
    pa.field("structural_height", _INT32, nullable=False),
    pa.field("native_depth", _INT32, nullable=True),
    pa.field("is_terminal", _BOOL, nullable=False),
    pa.field("is_singleton_isolate", _BOOL, nullable=False),
    pa.field("is_oversized", _BOOL, nullable=False),
    pa.field("member_entity_ids", _LIST_STR, nullable=False),
    pa.field("evidence_ids", _LIST_STR, nullable=False),
    pa.field("relationship_contributed_evidence_ids", _LIST_STR, nullable=False),
    pa.field("provider_id", _STR, nullable=False),
    pa.field("provider_version", _STR, nullable=False),
    pa.field("policy_id", _STR, nullable=False),
    pa.field("domain_hash", _STR, nullable=True),
    pa.field("run_id", _STR, nullable=False),
    pa.field("execution_fingerprint", _STR, nullable=False),
    pa.field("config_id", _STR, nullable=False),
    pa.field("seed", _INT64, nullable=False),
    pa.field("row_hash", _STR, nullable=False),
])

#: One row per (entity, terminal-cluster) membership. W2's real
#: ``DerivedMembershipRow`` is only 4 fields (``cluster_id``, ``entity_id``,
#: ``evidence_ids``, ``primary_membership``); it carries no run/provider
#: identity of its own, so the outcome-level provenance fields are added
#: here (W3-added, not native to the row).
DERIVED_COMMUNITY_MEMBERSHIPS_SCHEMA = pa.schema([
    pa.field("cluster_id", _STR, nullable=False),
    pa.field("entity_id", _STR, nullable=False),
    pa.field("evidence_ids", _LIST_STR, nullable=False),
    pa.field("primary_membership", _BOOL, nullable=False),
    *_PROVENANCE_FIELDS,
])

#: One row per inter-community (or otherwise diagnosably-routed) relationship
#: surfaced by W2's real ``inter_community_relationship_diagnostics`` --
#: a direct pass-through, never W3-derived from sealed relationship rows.
#: Source direction is preserved verbatim (``source_*`` / ``target_*`` are
#: never swapped or normalized). All six W2 fields are non-nullable per the
#: real ``InterCommunityRelationshipDiagnostic`` dataclass.
DERIVED_INTER_COMMUNITY_DIAGNOSTICS_SCHEMA = pa.schema([
    pa.field("relationship_id", _STR, nullable=False),
    pa.field("relationship_type", _STR, nullable=False),
    pa.field("source_entity_id", _STR, nullable=False),
    pa.field("target_entity_id", _STR, nullable=False),
    pa.field("source_cluster_id", _STR, nullable=False),
    pa.field("target_cluster_id", _STR, nullable=False),
    pa.field("reason", _STR, nullable=False),
    *_PROVENANCE_FIELDS,
])

DERIVED_HIERARCHY_TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "derived_community_clusters": DERIVED_COMMUNITY_CLUSTERS_SCHEMA,
    "derived_community_memberships": DERIVED_COMMUNITY_MEMBERSHIPS_SCHEMA,
    "derived_inter_community_relationship_diagnostics": (
        DERIVED_INTER_COMMUNITY_DIAGNOSTICS_SCHEMA
    ),
}

DERIVED_HIERARCHY_CONTRACT_KINDS: dict[str, str] = {
    "derived_community_clusters": CONTRACT_KIND_DERIVED_COMMUNITY_CLUSTERS,
    "derived_community_memberships": CONTRACT_KIND_DERIVED_COMMUNITY_MEMBERSHIPS,
    "derived_inter_community_relationship_diagnostics": (
        CONTRACT_KIND_DERIVED_INTER_COMMUNITY_DIAGNOSTICS
    ),
}
