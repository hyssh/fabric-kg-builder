# L5a Structured Publication Parity

L5a supersedes only the structured publication and materialization behavior
previously explored on frozen PR #35 (`hyssh-semantic-materialization-deploy`).
It does not merge or reactivate that branch. The successor implementation is
rebuilt on the merged C0.Publish contracts and the receipt-anchored
`SealedL4ServingSource`.

## Preserved behavior

- compile physical tables before any target mutation;
- submit persisted definition files rather than transient definitions;
- verify target existence, supported state, table data, and persisted
  definitions after update;
- fail closed on partial publication, conditionally clean up resources created
  by the failed attempt, and conditionally restore updated resources;
- invalidate reuse when semantic authority or materialized artifacts change;
- preserve schema-1 behavior by exposing L5a only through its explicit
  schema-2 stage entry point.

## Successor changes

- L4 is the only schema-2 data source; semantic/raw aliases and L3 bypasses are
  rejected;
- C0.Publish `PublicationCrosswalk`, `ProjectionEquivalence`,
  `GovernedAssetReference`, and `AccessPolicy` are persisted without changing
  their contracts or registry;
- exact `RequiredMemberManifestV1_1` and anchored L3 manifest authority is
  carried into every target proof;
- Ontology hierarchy is explicitly flattened and Graph relationships are
  limited to crosswalk-approved identities and endpoints;
- all four L5a targets use a bounded batched lifecycle and deterministic
  read-back evidence;
- publication, cleanup, and restore use inspected-state compare-and-swap plus a
  per-attempt ownership token so concurrent changes are not overwritten;
- stable canonical IDs use reserved physical identity columns; mapped property
  columns remain schema-only until sealed owner/value authority exists;
- Search indexes, Graph query execution, Data Agent activation, synthesis, and
  live Fabric validation remain later-layer work.

## Issue #29 coverage

L5a closes the lifecycle/materialization failure class from issue #29:
compilation cannot claim publishability while target definitions are missing or
unmaterialized, and a successful update response cannot substitute for
persisted resource and definition read-back. The exact L4 seal, crosswalk,
policy, governed assets, target IDs, and code version key checkpoint reuse, so a
semantic-authority change cannot reuse stale structured publication.

Issue #29 endpoint adoption and enrichment checkpoint diagnostics are outside
L5a. The earlier `required_for_asserted` mismatch is superseded for this path by
the asserted-only L4 source: L5a never materializes unresolved relationships.
Live Fabric capability and Surface validation are intentionally deferred to L7.
