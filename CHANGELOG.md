# Changelog

## 0.2.7 (local development)

Install from source. This version label does not imply a published PyPI release.

### Added

- **Native Hierarchical Leiden:** optional community derivation from sealed,
  validated L4 using `graspologic-native==1.2.5` through the `leiden` extra
  on Python 3.10–3.13. There is no fallback algorithm.
- **Offline `derive-hierarchy`:** writes three Parquet tables, a manifest, and
  an immutable receipt in a new output root without model or cloud calls.
  Original sealed inputs are never modified.
- **Hierarchy publication:** `app publish-structured --hierarchy-state`
  publishes `graph_communities` and `graph_community_members` after checking
  graph, evidence, and execution bindings. Unbound empty hierarchies are rejected.
- **Optional Ontology grouping:** `--ontology-inheritance` projects communities
  as abstract `group_<dominant type>` bases. This is display grouping, not proof
  of semantic inheritance.
- **Offline Ontology v2 conversion:** `app convert-ontology-v2` converts emitted
  v1 parts to TMDL while preserving inheritance and bindings. Publication
  remains v1 by default; this is not v2 Data Agent support.

### Changed

- **Ontology-only structured publication:** the create-only prototype creates
  Lakehouse + Ontology, not a standalone GraphModel. The platform-managed
  Ontology companion graph is verified before handoff. Existing journals remain
  readable; explicitly configured legacy graph capabilities remain separate.
- **Ontology-only Data Agent:** the prototype Agent uses Ontology v1 as its sole
  structured source. Its GQL instructions use entity/relationship type labels,
  `FILTER` rather than `WHERE`, and backtick-quoted `label`. Lakehouse SQL and
  AI Search are not automatically attached.
- **Extraction context:** default L2 extraction receives bounded ancestor
  headings, with governing cached-layout headings carried across pages.
  Headings guide interpretation, not evidence; quotations must still come from
  the source leaf. Prompt identity and cache bindings account for the context.
- **Relationship-type range:** the configured recommendation can vary within
  compiler capacity instead of being fixed at 8–20. The default remains 8–20,
  and the hard compiler capacity is unchanged.
- **Property variants:** narrowly scoped case/whitespace and trailing-qualifier
  string variants coalesce deterministically with an explicit warning.
  Other single-valued disagreements still fail closed.
- **Grounded label fallback:** opt-in `--required-label-fallback` can fill a
  missing required non-key string from an evidence-backed entity label.
  It never supplies missing keys and is recorded in publication identity.
- **Native date publication:** Arrow date columns bind to Ontology `DateTime`;
  graph readback must preserve the same calendar day at midnight UTC or as a
  bare ISO date. Other time components are rejected.

### Fixed

- Relationship direction and context survive serialization through the
  fail-closed identity witness in proposed-candidate contract 1.3.0.
  Historical 1.0–1.2 records retain their compatibility behavior.
- Hierarchy derivation rejects duplicate IDs and unknown endpoints, preserves
  all components and isolates, and validates exactly-once terminal membership.
  Derived identity binds graph, evidence, configuration, and seed with SHA256.
  Self-loops are excluded from the partition projection with diagnostics;
  original directed, typed facts remain intact.
- Evidence validation rejects endpoints that resolve only in another leaf and
  duplicate local IDs used for distinct payloads. Changed validator identity
  invalidates the corresponding L3 cache, not sealed L2 artifacts.
- Discovery resume no longer reuses stale prepared-source cache entries.
- Approved re-extraction preserves differently typed member fragments using
  `(semantic_id, approved_semantic_id)` instead of losing earlier fragments
  when the final leaf wins.

### Limits and approval boundaries

- Leiden reports explicit `complete`, `empty`, `insufficient`,
  `native_unavailable`, and `failed` states. `--max-cluster-size` is a split
  trigger, not a hard cap; oversized terminal communities are retained and
  flagged. Python 3.14 is not supported by the native provider.
- Structural completion does not recover missing relationships, establish
  procedure order, prove semantic inheritance, or demonstrate better answers.
  A/B answer-quality evaluation is deferred.
- Approved re-extraction, donor continuation, and discovery reuse retain their
  historical prompt/replay identities and do not receive the new heading context.
- L4 relationship validation retains `identity_recomputed=False`.
- Cloud publication requires separate approval of the exact dry-run plan.
  The create-only prototype is nontransactional, retains partial resources,
  and has no automatic rollback. A draft Agent is not answer-quality acceptance.
