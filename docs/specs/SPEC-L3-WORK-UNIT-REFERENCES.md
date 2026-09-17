# L3 work-unit-local references

Model-local references (`e1`, `e13`, etc.) belong to a response/work unit, not
the entire SourceUnit. L3 indexes them by:

`(source_unit_id, work_unit_id, casefold(local_reference))`

Properties and both relationship endpoints must resolve the exact proposed
canonical entity ID in the current work unit. A missing current-unit reference
has no source-wide fallback. Duplicate/case-insensitive conflicting references
within one work unit remain unresolved. Same-leaf asserted-entity requirements,
classification, direction, identity, scalar, source/hash, approved range and
occurrence checks are unchanged. Completeness consumes validated relationship
results; it does not independently resolve local references.

All accepted proposal carrier versions persist `work_unit_id`; no version
justifies inferring it from a source-wide reference. L3 verifies a nonempty leaf
has one source/work-unit pair and recomputes its sealed candidate-batch ID from
that pair and the batch candidate-version IDs. Empty leaves carry no references.
Mixed or mismatched proposal scope metadata is invalid.

Shared context hashes include the triple-scoped index and entity work-unit
dependencies. The verifier binding `work-unit-local-reference/1.0.0` invalidates
earlier input/leaf cache addresses; historical artifacts are not rewritten.

## Separate frozen L2 identity limitation

Reference scope is **not** a canonical identity migration. The frozen L2
stable-source identity fallback uses SourceUnit plus local ID, not work unit.
Thus the same type/identity policy and local ID reused across work units can
produce the same canonical entity ID for distinct occurrences. L3 does not
re-key or merge these identities as part of the reference fix.

L3 now rejects proven collisions using `IDENTITY_POLICY_VIOLATION`. Detection
requires the approved identity policy and persisted inputs to reproduce the
canonical ID as `derived_source_identity`. Reuse of that source-local fallback
ID across work units of one SourceUnit is not an authorized merge, even when
labels match. Within one work unit/type, differing persisted entity payloads
(including the raw payload hash) are ambiguous; exact payload duplicates are
not. All affected entity candidates retain their evidence and audit lifecycle,
but neither they nor their dependent properties/relationships can assert.

The guard does **not** ban canonical IDs shared across work units generally.
Recomputed business-key identities are excluded. This carrier does not persist
an independent native/global stable-source witness: IDs that cannot reproduce
from its available inputs keep the existing opaque/unavailable witness outcome.
L3 does not invent native provenance or reinterpret a business key as a local
fallback. Qualified references yielding distinct recomputed IDs remain valid;
the detector does not trust a magic prefix to exempt an actual collision.

Identity-policy/witness/payload dependencies enter the source-local shared
context hash. `source-local-identity-collision/1.0.0` is a new verifier binding,
so cached pre-defense assertions are unreachable in a fresh validation run.

For a synthetic device-maintenance example, two work units in one source reuse
service-part reference `sp1` for distinct parts `DEMO-101` and `DEMO-202`.
If the frozen source-local fallback gives them the same canonical identity,
these colliding unqualified identities are non-asserting under the L3 defense. A
separately approved, zero-model handoff projection can namespace references and
recompute IDs through the unchanged core; old input/output artifacts remain
immutable.

The regression suite documents the frozen L2 behavior explicitly. Correcting
it requires a separately approved identity/version migration, not a hidden L3
label match, entity merge, or source-wide fallback.
