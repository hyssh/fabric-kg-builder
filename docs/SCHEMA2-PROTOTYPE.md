# Testing the 0.2.6 corpus-first prototype

This local prototype keeps exploratory design separate from the existing strict
DomainContractV2 approval/extraction contract. It also retains the document
challenge and revision loop. It does not claim that the cloud release roadmap or
live technician-question acceptance is complete.

## Capability discovery

```bash
fabric-kg --version
fabric-kg domain discover --help
fabric-kg domain design-schema
fabric-kg domain question-context --help
fabric-kg domain window-schema
fabric-kg domain window-align --help
fabric-kg domain design --help
fabric-kg domain evaluate-design --help
fabric-kg domain compile-design --help
fabric-kg domain assessment-schema
fabric-kg domain assess --help
fabric-kg domain review-assessment --help
fabric-kg domain revise --help
fabric-kg domain analyze-layout --help
fabric-kg enrich --help
```

`assessment-schema` prints the actual versioned JSON schemas. Package version
alone is insufficient to identify a prototype build; record the source commit.

### Independent Graph native identifiers

Node and relationship **labels** come from the sealed domain's approved
presentation catalog, not internal L5 crosswalk labels or physical table names.
For example, `Part Number` becomes `Part_Number`; pipeline prefixes such as
`L5_Type_...` are not added. The shared readable-name allocator enforces
letter-first ASCII names, underscores, a 128-character bound, and deterministic
case-insensitive collision handling. Edge labels reserve node labels too.
Relationships expanded into several endpoint pairs use readable source and
target concept names to distinguish the pairs.

Aliases, physical tables/columns, keys and canonical IDs remain unchanged.
The Graph catalog and generated readback queries use the same native labels.
This does not automatically rename an already-deployed Graph, and an old sealed
publication plan must not be resumed with different labels.

The independent Graph compiler separates exposed property identifiers from
Lakehouse column names. Valid letter-first ASCII identifiers remain unchanged.
Other columns (including `__canonical_id`, `__label` and asserted-edge internal
columns) use the existing deterministic Graph alias sanitizer: sanitized stem
(up to 96 characters), `_`, then eight SHA-256 hex characters. The allocator
reserves existing valid column names first and resolves collisions with numeric
suffixes in sorted source-column order, across the complete Graph.

Only `properties[].name`, `primaryKeyProperties` and
`propertyMappings[].propertyName` use those transport names.
`propertyMappings[].sourceColumn`, edge endpoint source-key columns, Lakehouse
schemas, canonical identity **values**, semantic authority and topology remain
unchanged. Native integers use `INT`, not `INTEGER`. Scalar types are selected
through the source-column mapping, not by assuming exposed names equal columns.
Readback builds MATCH/RETURN/keyset expressions from native labels and mapped
properties and still compares every typed scalar and actual endpoint-pair
multiset, including duplicates. No evidence or schema-identity gate is relaxed.

Native validation rejects invalid contract-owned labels, duplicate aliases,
missing key mappings, conflicting types for a shared property name, unsupported
type tokens and published 1,000-item array limits before creation. This is
transport compatibility, not an ontology meaning change; it introduces no new
semantic limitation approval.

First-party references, checked **2026-09-12**:

- [Graph limitations](https://learn.microsoft.com/en-us/fabric/graph/limitations):
  identifiers cannot start with underscore. The catalog's 128-character naming
  recommendation is not documented as an enforced maximum.
- [Graph property types](https://learn.microsoft.com/en-us/fabric/graph/gql-graph-types#supported-property-types):
  `INT`/`INT64`, `STRING`, `BOOL`/`BOOLEAN`, `DOUBLE`/`FLOAT64`/`FLOAT`;
  shared property names must have consistent types.
- [Native definition structure](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/graph-model-definition)
  and [schema design](https://learn.microsoft.com/en-us/fabric/graph/design-graph-schema).

The complete available first-party JSON schemas (four Graph parts and three
transitive shared schemas), exact-byte SHA-256 hashes and source URLs are
captured in `tests/fixtures/fabric_graph_schema/`. The referenced
`graphIndex/common/identifiers/1.0.0/schema.json` returns **404** and is not
fabricated locally. Thus offline checks are not a claim of exhaustive live
service validation. The failed service response exposed only its top errors,
not all 106 diagnostics. `INTEGER` occurred twice in that failed definition;
its replacement uses documented native tokens rather than assuming those
unreported diagnostics.

**Existing sealed plans are incompatible with this compiler change.** Use the
public `app publish-structured --prototype-create-only --dry-run` path to create
a new plan/materialization and obtain approval for its exact hash before
deployment. Never rewrite an old
plan/journal, substitute corrected parts into its request, or blind-retry a
failed create. Preserve prior receipts and perform any authorized cleanup
separately. A native create/readback must still succeed before readiness or
business acceptance is claimed.

### Existing independent Graph labels-only repair

For an already-published, content-verified independent Graph, use a separately
reviewed repair rather than changing or resuming its original publication plan:
Pass `--naming-review COPILOT_NAMES.json` to use the exact same reviewed canonical
type names as the Ontology repair (see **Copilot semantic naming authority** below).

```bash
fabric-kg app repair-graph-labels \
  --workspace-id "$WORKSPACE_ID" --graph-id "$INDEPENDENT_GRAPH_ID" \
  --l4-run "$SEALED_L4_RUN" --l3-root "$SEALED_L3_ROOT" \
  --publication-plan "$ORIGINAL_PLAN" --prototype-journal "$ORIGINAL_JOURNAL" \
  --materialize "$ORIGINAL_MATERIALIZATION" \
  --state "$NEW_REPAIR_STATE" --dry-run
```

Planning performs live **reads** and writes a new local backup, approved catalog
mapping, full scalar/topology proof, exact label-field delta and hash-bound plan.
Review `plan.json` and `replacement.json` in the state directory. Apply by
repeating the same input arguments, replacing `--dry-run` with
`--live --approve-plan "$REPAIR_PLAN_HASH" --acknowledge-nontransactional`.
The hash is the new repair plan's hash, not the original publication approval.
Fabric has no atomic compare-and-swap guard here; the acknowledgement accepts
the residual concurrency risk despite fresh before/after drift checks.

The command changes only single labels in `graphType.json` on the original,
returned-ID-owned independent Graph. It preserves aliases, keys, properties,
endpoint mappings, source bindings, all other definition parts and data.
It does not create/delete items, refresh data, modify the managed Graph,
Ontology or Lakehouse, resume publication, or approve business facts.
Both planning and final verification compare all expected scalar values and
canonical endpoint-pair multisets, not merely node/edge totals.

The repair state retains an immutable update intent before the one permitted
update request. After an uncertain or failed response, inspect its evidence;
the same arguments with `--live`, the exact approval, acknowledgement and
`--resume` can only read/poll and verify, never repeat the update. A completed
`receipt.json` records `verified-labels-only`. An accepted definition without
successful runtime queries is **not** success; service loading requirements
remain a separate concern. Never overwrite the original plan, journal or
repair evidence.

### Independent Graph getDefinition defaults

The subsequent native create succeeded. Its service readback preserved all
Graph types, labels, properties, keys, source references/mappings and endpoints.
It added `edgeIdMapping: null` to edge bindings, `visualFormat: null` to styling,
and a schema-only `graphSettings.json`; layout coordinates and zoom were
serialized as floating-point numbers. These serialization differences caused
the full-definition gate to stop after the returned Graph ID was recorded.

Publication and reconciliation now share a narrowly scoped comparison:

- Only the exact `graphSettings/1.0.0` schema-only object is equivalent to an
  absent settings part. Additional fields, even null, or different versions fail.
- Only null `edgeIdMapping` on edge bindings and null top-level `visualFormat`
  are equivalent to absence, under their exact `1.0.0` part schemas. Empty
  arrays/objects, configured values, or dropped mappings still fail.
- Only `modelLayout` position/pan coordinates and `zoomLevel` treat numerically
  identical integer/float JSON representations as equivalent. Booleans are not
  numbers; changed coordinates still fail.
- Every other field, part, array order, key and type is compared without
  normalization. Unknown extras are not discarded.

The captured first-party schemas define `edgeIdMapping` as an optional string
array and layout values as numbers. Their styling schema requires an object
`visualFormat`, whereas the service accepts its absence and emits null; the
REST documentation's styling example also omits it. Therefore null handling is
an explicit, observed service-serialization compatibility rule, not a claim
that null conforms to the published schema. The service's
`graphSettings/1.0.0/schema.json` URL returns 404 (checked 2026-09-12); no
nonempty settings behavior is inferred from that missing schema.

Raw getDefinition artifacts and receipt hashes remain unchanged and exact.
This is a **readback-only repair**: compiler output, native templates, plan
semantics and source/table proofs do not change. No template replacement or
Graph recreation is necessary to address this drift. The runtime compiler
fingerprint nevertheless changes, so a blind old-plan resume remains blocked.
The existing `reconcile-prototype-create` command handles **unresolved creates
without returned IDs**, not this `identity-verified`, returned-ID-owned Graph.
Do not reclassify its ownership or edit its plan/journal to force that path.
Use the explicit returned-ID runtime-repair mode below before resuming retained
items with the repaired runtime.

### Reviewed runtime repair of a returned-ID-owned Graph

`app reconcile-prototype-create --returned-id-runtime-repair --kind graph`
is a separate policy from ambiguous-create reconciliation. It accepts only an
`identity-verified` Graph with equal `item_id` and `returned_item_id`, no
operator-reconciled ownership, and successful original HTTP 200/201/202 create
evidence. Metadata must match the exact workspace, type, name and run
description. Synchronous creates require the original response's returned ID;
HTTP 202 requires the recorded successful operation, matching response headers,
and fresh successful operation/result reads confirming that same ID.

Preview is read-only and non-authorizing. Acceptance requires the exact review
hash, actor and rationale, and repeats all live proofs. It adds only a
`runtime_repair` receipt to the existing journal: the original action, ownership
and plan bytes are unchanged. An existing runtime-repair receipt cannot be
replaced or chained through this mode.

The receipt binds the original plan bytes/hash, complete journal snapshot hash,
journal identity/baseline, entire selected create action, current compiler,
full native proof (including raw definition and its actual hashes), and
materialized artifact byte digests. Recompilation must preserve every plan
field except compiler hash and the derived plan hash, including templates,
tables, identities, scope, approvals, limits and ordering. Changes requiring
new templates are still rejected.

Resume checks immutable artifact digests before materialization can restore
missing files, revalidates the receipt and selected action, then repeats live
read-only native/identity/create-operation proofs before any publication
mutation. It permits newly appended readback evidence and the exact verified
definition hash, not altered original evidence. Expired/unreadable operation
results or changed metadata fail closed; neither IDs nor ownership are inferred.
The repair command itself never performs Fabric create/update/delete or Delta
writes. Publication subsequently runs the existing full scalar/topology gates.

For a retained Graph, the public command sequence is below.
Preview does not accept the repair. Review its output and replace
`REVIEW_HASH_FROM_PREVIEW` before running acceptance. The final live command is
the existing public acceptance/resume path, not another create plan.

```bash
ROOT=/path/to/publication-artifacts
PLAN="$ROOT/prototype-plan.json"
JOURNAL="$ROOT/prototype-journal.json"
MATERIALIZE="$ROOT/materialized"
L4=/path/to/sealed/l4/run
L3=/path/to/sealed/l3
REVIEW="$ROOT/returned-id-review.json"
# Set these to the exact original plan/journal values, not replacement IDs.
WORKSPACE_ID=WORKSPACE_UUID
GRAPH_ID=RETURNED_GRAPH_UUID
NAME_PREFIX=ORIGINAL_NAME_PREFIX
PLAN_HASH=ORIGINAL_PLAN_HASH

# Read-only review (repair dry-run).
.venv/bin/fabric-kg app reconcile-prototype-create \
  --returned-id-runtime-repair --kind graph \
  --item-id "$GRAPH_ID" \
  --plan "$PLAN" --prototype-journal "$JOURNAL" --materialize "$MATERIALIZE" \
  --l4-run "$L4" --l3-root "$L3" --review "$REVIEW"

# Explicit acceptance; reads Fabric again and appends the local receipt only.
.venv/bin/fabric-kg app reconcile-prototype-create \
  --returned-id-runtime-repair --kind graph \
  --item-id "$GRAPH_ID" \
  --plan "$PLAN" --prototype-journal "$JOURNAL" --materialize "$MATERIALIZE" \
  --l4-run "$L4" --l3-root "$L3" --review "$REVIEW" \
  --accept-review REVIEW_HASH_FROM_PREVIEW --actor operator \
  --rationale "Reviewed successful returned Graph create and exact readback-only runtime repair"

# Verify the original plan under its accepted runtime receipt.
.venv/bin/fabric-kg app publish-structured --prototype-create-only --dry-run \
  --l4-run "$L4" --l3-root "$L3" \
  --workspace-id "$WORKSPACE_ID" --name-prefix "$NAME_PREFIX" \
  --plan "$PLAN" --prototype-journal "$JOURNAL" --materialize "$MATERIALIZE" \
  --prototype-approve-limitation ontology.property-relationship-aliases-metadata-only \
  --prototype-readback-page-size 1000 --prototype-readback-total-rows 1000000

# Resume the same owned IDs; retain the original plan hash and approvals.
.venv/bin/fabric-kg app publish-structured --prototype-create-only --live \
  --l4-run "$L4" --l3-root "$L3" \
  --workspace-id "$WORKSPACE_ID" --name-prefix "$NAME_PREFIX" \
  --plan "$PLAN" --prototype-journal "$JOURNAL" --materialize "$MATERIALIZE" \
  --prototype-approve-limitation ontology.property-relationship-aliases-metadata-only \
  --prototype-readback-page-size 1000 --prototype-readback-total-rows 1000000 \
  --approve-live "$PLAN_HASH"
```

### Native managed companion Graphs and subsequent read-only proof

The companion adapter supports the native
`graphInstance/definition/dataSources/1.0.0` dialect separately from the
independent Graph's `graphIndex/definition/dataSources/1.1.0` item references.
Native `DeltaTable` sources must have exactly
`abfss://<workspace-guid>@onelake.pbidedicated.windows.net/<lakehouse-guid>/Tables/dbo/<compiled-table>`.
The exact host, scheme, canonical GUIDs, workspace, Lakehouse and compiled table
must match. Encoding, traversal, credentials, ports, query/fragment components,
other schemas/hosts, duplicate or unused sources and arbitrary source properties
are rejected. Source URIs and physical columns are never rewritten.

Native graph type/definition schemas, opaque numeric aliases, mapped properties,
canonical keys, relationship endpoints, null `edgeIdMapping`, Ontology styling
and schema-only settings are checked explicitly. The service `.platform` metadata
is checked against live metadata; its valid zero logical UUID is not treated as
an item ID or ownership evidence. Unknown fields/defaults fail closed. All mapped
scalars and the complete endpoint-pair **multiset**, including duplicate edges
and empty relationship types, are verified with the existing typed keyset queries.

An already accepted repair cannot be silently replaced to authorize another
runtime. Instead, `app verify-prototype-companion` produces a **separate**
hash-bound read-only plan and proof. It supports the retained partial prototype
whose independent Graph is already verified and whose original returned-ID
repair remains intact; Semantic Model publication is outside this path.

```bash
# Reuse the exact original PLAN/JOURNAL/MATERIALIZE/L4/L3 variables above.
# COMPANION_ID is explicit; the snapshot is a previously retrieved getDefinition response.
COMPANION_ID=MANAGED_COMPANION_UUID
SNAPSHOT="$ROOT/companion-native-readback.json"
VERIFICATION_PLAN="$ROOT/companion-verification-plan.json"
PROOF="$ROOT/companion-verification-proof.json"

# Local-only preview: compile all readback windows; no Fabric calls.
.venv/bin/fabric-kg app verify-prototype-companion \
  --plan "$PLAN" --prototype-journal "$JOURNAL" --materialize "$MATERIALIZE" \
  --l4-run "$L4" --l3-root "$L3" --companion-id "$COMPANION_ID" \
  --companion-definition "$SNAPSHOT" --verification-plan "$VERIFICATION_PLAN" \
  --proof "$PROOF"

# Review the new plan, then explicitly approve only these read operations.
.venv/bin/fabric-kg app verify-prototype-companion \
  --plan "$PLAN" --prototype-journal "$JOURNAL" --materialize "$MATERIALIZE" \
  --l4-run "$L4" --l3-root "$L3" --companion-id "$COMPANION_ID" \
  --companion-definition "$SNAPSHOT" --verification-plan "$VERIFICATION_PLAN" \
  --proof "$PROOF" --approve-readback VERIFICATION_PLAN_HASH_FROM_PREVIEW
```

The verification plan binds exact original plan/journal/snapshot bytes, the
complete previous receipt, artifact digests, current verifier/compiler, selected
IDs, expected query windows and unchanged original limits. Execution repeats
native definition/metadata/returned-create proofs and checks **both** independent
and companion Graph counts, scalars and endpoint multisets. Live definitions,
metadata and local inputs must remain unchanged through the final check.
Only GET, getDefinition and the planned executeQuery reads are permitted.
Raw responses and their actual hashes are retained in the separate proof,
including failures; use new output paths for a new review or retry.

Neither preview nor execution rewrites the original plan, journal, receipt,
ownership or managed Graph. Historical `last_error`, companion errors and the
original partial status remain untouched and are explicitly labeled historical
in the new proof; its fresh verification status is separate. This path does not
authorize publication/resume under a changed compiler, does not bypass the
existing Data Agent handoff gate, and performs no refresh/create/update/delete
or Delta writes. Expired operation provenance and any changed evidence fail
closed, without resource churn.

## Corpus first: discover, consolidate, then design

The normal sequence is `domain discover` -> `domain design --discovery` ->
`domain evaluate-design` -> `domain compile-design` -> existing `domain approve`
-> `enrich --discovery`. See the
[corpus-first contract](specs/SPEC-CORPUS-FIRST-PIPELINE.md).

Plan source discovery first; this inventories files and supplied OCR-cache page
coverage without extracting text, writing state or calling a model:

```bash
fabric-kg domain discover --input ./documents \
  --cache-dir .fkg/discovery --out .fkg/discovery/prepared.json
```

Discovery does not require an ontology or fabricated intake questions.
`--intake intake.json` is optional when business context is already available.
With cached OCR, supply `--ocr-cache` and `--ocr-identity`; a cache miss must
not trigger a hidden Document Intelligence request.

Exact chunk counts require source preparation. This explicit local preparation
writes a partial checkpoint but allows **zero** model calls:

```bash
fabric-kg --config fabric-kg.yaml domain discover --input ./documents \
  --cache-dir .fkg/discovery --out .fkg/discovery/prepared.json \
  --live --max-calls 0
```

Then allow bounded discovery and document/corpus synthesis. The following budget
is illustrative, not a promise that it covers every corpus:

```bash
fabric-kg --config fabric-kg.yaml domain discover --input ./documents \
  --cache-dir .fkg/discovery --resume .fkg/discovery/prepared.json \
  --out .fkg/discovery/run-1.json --live --max-calls 256 --concurrency 4
```

If the run is partial, retain it and resume into a fresh output path with an
appropriate budget. Matching successful work is reused. Failed source preparation
can recover without rereading successful sources. Preserve the same source,
extractor, model and influential business context; resume must not hide drift.
When intake is omitted during resume, the previous business context is retained.

If dense chunks repeatedly fail to return complete JSON, first inspect the
missing-only plan. An explicit retry can raise the output ceiling for **only**
prior chunks without received responses:

```bash
fabric-kg --config fabric-kg.yaml domain discover --input ./documents \
  --cache-dir .fkg/discovery --resume .fkg/discovery/run-1.json \
  --out .fkg/discovery/run-2.json \
  --retry-missing --retry-max-completion-tokens 16384
```

Add `--live` only after reviewing the plan and call/token budget. Successful
responses and received malformed envelopes are reused, not retargeted; the
global model configuration and summary output ceilings remain unchanged.
The ceiling can be explicitly increased up to 32,768, but does not guarantee a
valid response. Missing-response diagnostics retain partial provider output and
status privately; they are not repaired into valid JSON or printed to stdout.
Treat diagnostic/cache files as sensitive source artifacts.

Only a complete discovery result is used for the normal design path. Completion
means accounted source/chunk processing and consolidation, not perfect semantic
recall. Inspect candidate grounding quality and pending work as well as status.

An explicit prototype coverage acceptance can admit a partial run at an exact
99% or greater processed-chunk ratio. First inspect the local plan, then add
`--accept` to record the user's reviewed exception:

```bash
fabric-kg domain accept-discovery-partial --file .fkg/discovery/run-1.json \
  --min-chunk-coverage 0.99 --actor reviewer \
  --rationale "Prototype coverage accepted; retain remaining gaps for review" \
  --out .fkg/discovery/coverage-acceptance.json
```

This does not alter the partial run or approve ontology facts. Pass the resulting
artifact to `domain design --discovery-acceptance FILE` alongside its exact
`--discovery`. All documents remain represented in a bounded valid-summary
frontier, with missing processing, summary and grounding warnings. Initial
acceptance checks current sources. After ontology approval, replay checks the
actual supplied source mount and reconstructs the exact sealed acceptance;
relocating identical bytes does not authorize a changed corpus.

The threshold uses processed/no-candidates chunks, not candidate grounding or
semantic recall. Accepted gaps remain `pending_review` through serving and agent
context; permissions, evidence verification and final schema approval remain
separate gates. Default behavior stays strict without an acceptance artifact.

Grounding retains every original observation in a ledger. Exact primary-source
matches enter a verified subset; ambiguous, malformed or context-only observations
remain quarantined with stable IDs and reasons. A processed chunk with quarantine
is not an empty response or fully verified knowledge. After verifier changes,
exact-bound received responses can be revalidated without repeating model calls;
old request/prompt provenance and original caches remain immutable.

The seed may be a reference sketch or a domain YAML. Its complete parsed content
and hash are preserved as design context, not source evidence. The optional
`--description` supplies additional business/domain intent. Existing seed
approval does not transfer to a generated design.

Explicitly allow bounded generation, then evaluate the saved design locally:

```bash
fabric-kg --config fabric-kg.yaml domain design \
  --input ./documents --intake intake.json --seed-domain reference.yaml \
  --discovery .fkg/discovery/run-1.json \
  --out .fkg/design/draft.json --live --max-calls 2 \
  --proposal-trace-dir .fkg/design/private-traces
fabric-kg domain evaluate-design --file .fkg/design/draft.json \
  --out .fkg/design/evaluation.json
```

Continue with `run-1.json` only if its reported status is complete. Otherwise,
resume and substitute the actual completed run path in subsequent commands.
Do not rename or edit a partial run to make it appear complete.
Use repeatable `--discovery-node NODE_ID` for bounded document/chunk detail
alongside the corpus root. The earlier bounded-sample workflow now requires
explicit `--sample-only`, is labelled limited, and cannot be combined with
`--discovery`.

A draft can retain common types with no question assignments and concepts that
are not used by the current questions. Question gaps are not JSON/schema errors.
Inspect structural support, missing answer fields, unresolved semantic adequacy
and compiler limitations separately. This evaluation is not an answer oracle.

Generation currently makes one logical model call with no automatic repair loop;
`--max-calls` is a ceiling. Optional traces retain private requests and completed
responses, including invalid designs, for diagnosis. Treat them as sensitive
source artifacts and keep them outside version control. A new run can use the
original seed plus `--description` feedback; there is no automatic design-revision
controller. The description is saved separately without rewriting the intake.

After reviewing the actual report, supply its exact hash to compilation:

```bash
fabric-kg domain compile-design --file .fkg/design/draft.json \
  --input ./documents --evaluation .fkg/design/evaluation.json \
  --accept-evaluation-hash <exact-evaluation-hash> \
  --out-state .fkg/l1-026 --out-domain .fkg/l1-026/domain.yaml
```

Compilation is model-free and rechecks current sources. It cannot silently drop
types, invent question tags or alter criticality to fit the existing compiler.
An unsupported design remains saved for review; a compiler limitation does not
mean the ontology itself is invalid. Existing strict limits still apply at the
Schema-2 handoff. A successful compilation remains unapproved: use the emitted
project/run/proposal anchors with `domain approve` before extraction.

Do not pass the design JSON to `enrich`. Do not assume `init-domain --domain-file`
loads a seed in Schema-2: unsupported legacy seed options now fail explicitly
with guidance to use `domain design`.

### Approved reuse and current limits

After explicit approval, reuse the exact discovery bound into that L1 handoff:

```bash
fabric-kg enrich --input ./documents --domain-file .fkg/l1-026/domain.yaml \
  --l1-state .fkg/l1-026 --l2-state .fkg/l2-026 \
  --discovery .fkg/discovery/run-1.json --replay-only --dry-run
```

Remove `--dry-run` to execute local candidate mapping/replay. The default with
`--discovery` is no new model calls. Missing/unmapped work stays pending;
`--reextract-pending --max-reextract-calls N` explicitly permits targeted new
calls. Omission by a retry is not authority to delete an original observation.
Inspect reuse, pending and candidate-disposition counts rather than just exit
status. Existing L3 verification still applies.

A discovery-bound approved domain requires its matching discovery; forgetting it
must not launch a full second model pass. Legacy approved domains retain their
compatibility behavior. Source bytes and cache hashes are rechecked; prepared
units are not reparsed in normal design/compile/replay.
Use the exact discovery run sealed into approval, not merely the latest resume
filename: a successor run has its own hash even when it reuses every response.
Quarantined observations remain pending in replay, never silently discarded.

The sample-only compatibility route still has a bounded sampler; it is not
evidence of full-corpus understanding. Normal discovery processes all declared
eligible chunks, with budgets and unsupported content explicitly accounted for.

The intake still requires five to ten questions. Existing strict compilation
limits remain visible, including relationship-usage tags and retained-type/path
constraints. Draft creation can succeed while compilation remains blocked.
Do not describe this milestone as a universally compilable design workflow.

Name matching ignores case and separators, but is only a review aid. It does not
establish that differently named concepts are equivalent, or that a matching
name has the same meaning. Review the actual endpoints and property ownership:
model-written rationales can contradict the generated graph.

## Windowed common-schema alignment and snapshots

### Human-readable Fabric presentation

Canonical IDs are machine identities, not user-facing names. Native entity,
relationship and property names should be derived from the approved readable
catalog. Use the shared Ontology/Graph-safe rule: a leading ASCII letter followed
by letters, digits or underscores (maximum 128 characters). The Ontology
transport schema permits hyphens, but Graph node labels do not; transport
acceptance alone is not sufficient validation. A label such as
`Battery Screw` becomes `Battery_Screw`. Keep original human labels and canonical
IDs in the naming report; do not change canonical IDs or physical table names.
For example, `local_environmental_or_e-waste_laws_and_guidelines` must become
`local_environmental_or_e_waste_laws_and_guidelines`. Check collisions after
normalization, including collisions with pre-existing underscore names.

Fresh compilation follows the [Fabric Ontology semantic enrichment schema](https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/ontology-definition):
only entity types support native `synonyms`. Properties and relationships support
`description` and `customAttributes`, not `synonyms`. Their approved display label,
aliases and ASCII alias are retained losslessly as a JSON string in
`semanticEnrichment.customAttributes.fabric_kg_presentation`. This is custom
metadata, **not native synonym/search behavior**. Missing descriptions are emitted
as empty strings. Prototype plans require explicit approval of
`ontology.property-relationship-aliases-metadata-only`; entity synonyms are unchanged.
Definition readback still compares every non-platform decoded field exactly.

A publication stopped after Fabric discarded unsupported synonyms cannot resume
as successful. Retain its immutable plan, journal and artifacts. The
`reconcile-prototype-create` command only handles unresolved creates with no
returned item ID, and only compiler changes that preserve the entire semantic
plan, including native templates. It cannot reconcile this serialization change
or an already returned-ID-owned Ontology. Naming repair also cannot add custom
attributes or retrospectively amend the publication proof. Review a fresh
create-only dry run in new artifact paths, explicitly approve the metadata-only
limitation, and obtain approval/capacity for new items before any live publication.
Reusing the partial existing estate would require a separately designed and
approved migration; never hand-edit ownership, hashes or immutable definitions.

For a local-only review, use the same sealed source paths and fresh output paths:

```bash
fabric-kg app publish-structured --prototype-create-only --dry-run \
  --l4-run L4_RUN --l3-root L3_STATE --workspace-id WORKSPACE_ID \
  --name-prefix enrichment_review \
  --plan NEW_REVIEW_PLAN --materialize NEW_REVIEW_MATERIALIZATION \
  --prototype-journal NEW_REVIEW_JOURNAL
```

This preview is intentionally blocked on the metadata-only limitation. After
explicit approval, generate another **new** plan/materialization/journal with
`--prototype-approve-limitation ontology.property-relationship-aliases-metadata-only`
(and any other reviewed limitations). Do not add approvals to an existing
immutable plan. A fresh estate needs two available GraphModel slots, including
its automatic Ontology companion; retaining the partial estate consumes capacity.

For an existing Ontology, prefer a reviewed presentation-only `updateDefinition`
over deletion/recreation. Back up the complete current definition, retain every
part and binding, and permit only names, source-backed descriptions and an
existing label-property display selector to change. Identity properties,
numeric IDs, relation endpoints, data types, source scope, data and sensitivity
labels stay unchanged. Existing synonyms/custom attributes are preserved during
the live repair.

The API replaces the full definition and has no assumed transactional/CAS
guarantee. Re-read before updating, reject unexpected drift, persist the update
intent and operation reference, and verify the complete after-definition.
Never blindly repeat an uncertain POST or automatically roll back over another
editor's changes. Old publication snapshots remain immutable and must not be
mistaken for the current presentation after repair.

Renamed types/properties can change GQL names; retain the before/after map for
query updates. This repair does not consolidate entity types or turn types into
instances. Those are separate reviewed modelling changes, not cosmetic edits.

Use the explicit repair path, not the original create-only publisher:

```bash
fabric-kg app repair-ontology-names \
  --workspace-id WORKSPACE_ID --ontology-id EXISTING_ONTOLOGY_ID \
  --l4-run L4_RUN --l3-root L3_STATE \
  --publication-plan ORIGINAL_PLAN --prototype-journal ORIGINAL_JOURNAL \
  --materialize ORIGINAL_MATERIALIZATION --state NEW_REPAIR_DIRECTORY
```

This performs remote reads and creates a new local backup, naming map and plan.
It does not mutate Fabric. Inspect the complete change plan, then repeat the
same command with `--live --approve-plan HASH --acknowledge-nontransactional`.
An interrupted operation uses `--resume` for readback/LRO polling only, never
another update POST. Keep the original publication snapshot and the new repair
receipt separately; a successful naming repair does not retrospectively change
the original publication definition hash.

For a subsequent naming correction, use a **new** `--state` directory and pass
`--previous-repair-state` pointing to the completed repair. The CLI validates its
plan, backup, replacement, mapping, receipt and observed definition against the
same sealed publication authority, then requires the current Fabric definition
to match that predecessor. Historical hyphen-containing names are accepted only
when validating the previous repair; every new replacement uses Graph-safe names.
Do not reuse a completed plan or its readback-only resume to authorize a new update.

Readback records Fabric's observed normalization of an absent relationship
`semanticEnrichment.customAttributes` to an empty object separately. It never
permits nonempty attributes, changed synonyms or different presentation fields.
If a verifier fix is needed after the single update was dispatched,
`--resume --accept-verifier-update CURRENT_REPAIR_CODE_HASH` explicitly approves
readback-only verification with that code. It cannot authorize another update
POST; source/compiler/naming bindings and the original approved plan still
must match.

### Copilot semantic naming authority

Both `app repair-ontology-names` and `app repair-graph-labels` accept
`--naming-review FILE`. This is a separate, versioned operator authority produced
by a Copilot semantic review, **not** a rewritten approved Domain and **not**
an automatic model call made by the repair command. Review predicates,
direction and endpoint roles with Copilot before accepting the catalog:

```json
{
  "version": "copilot-semantic-names/1.0.0",
  "domain_contract_hash": "<exact sealed Domain contract SHA256>",
  "author": "GitHub Copilot semantic review",
  "entity_types": {
    "semantic-type:device_model": {
      "native_name": "DeviceModel",
      "display_name": "Device model",
      "reason": "Reviewed business noun for the existing model type."
    }
  },
  "relationship_types": {
    "relationship-type:example": {
      "native_name": "ReferencesDeviceModel",
      "display_name": "References device model",
      "verb": "References",
      "source_type_ids": ["semantic-type:device_model"],
      "target_type_ids": ["semantic-type:device_model"],
      "reason": "Example only: review the actual existing predicate and endpoints."
    }
  }
}
```

The example is a schema illustration, not a deployable partial catalog. The
file must contain **every** canonical entity and relationship ID from the sealed
Domain and publication crosswalk, with no extras (12 types / 38 typed pairs for
the current rollout). Endpoint ID sets and Domain hash must match exactly.
Independent Graph review requires one canonical relationship per typed pair;
multi-endpoint canonical relationships are rejected rather than inventing
disambiguating verbs. Entity native names are noun PascalCase. Relationship
native names and display labels must start with their declared verb:
`Has`, `Includes`, `Specifies`, `Applies`, `Replaces`, `References`, `Targets`, or
`Is`. Native names must match `[A-Za-z][A-Za-z0-9_]{0,127}` and be casefold-unique
across the catalog. Invalid names, duplicate JSON keys, opaque digest suffixes,
unknown fields and missing review reasons fail closed. Explicit business digits
such as `M365Connector` are allowed. There is no hash/counter fallback or inferred
verb selection in reviewed mode.

Add the **same** review file to each command shown above, using separate new
repair state directories and separate exact repair-hash approvals. Plan Ontology
first, apply only after approval, verify it, then separately plan/approve/verify
the **owned independent Graph**, never its managed companion. For a previously
repaired Ontology, retain and pass its complete `--previous-repair-state`.
Do not substitute a current repair into the original publication plan/journal.

Reviewed Ontology repair changes only native entity/relationship `name` values;
it does not rename properties, change descriptions/display-property selectors,
or add synonyms/custom attributes. Reviewed Graph repair changes actual
node/edge labels while preserving internal aliases, keys, endpoints, scalars and
all source binding bytes. Mapping reports include canonical ID, old/new names,
endpoint IDs for relationships, display label and Copilot reason. Display labels
remain review evidence, not unsupported relationship synonym fields.

The repair plan retains the original review bytes (base64), SHA256, parsed
payload, and naming implementation bytes/hash. Keep the external review file:
apply/resume require the same path and unchanged bytes, including whitespace.
Deleted, replaced, omitted or changed reviews block before any update; completed
Ontology donor chains revalidate each ancestor's own review, not the newest
catalog. Fresh final readback also rechecks local authority. All existing full
backups, exact new-hash approvals, allowlist diffs, one-update intent and
resume-without-repost safeguards still apply. Original publication readiness
remains superseded; a naming repair does not establish managed-Graph queryability.

**Fresh-publication gap:** `publish-structured` does not yet accept this naming
review hook or reject every collision requiring legacy digest disambiguation.
Default compilation/naming is intentionally unchanged so original publication
plans remain reproducible for ownership and proof checks. Do not claim that
fresh publication is Copilot-named or collision-proof. Until a separately
reviewed publisher integration exists, inspect public labels before provisioning
and use these explicitly approved same-item repair paths for existing items.

### Explicit current Ontology baseline review

`repair-ontology-names --current-definition-review FILE` is an optional alternative
to `--previous-repair-state`, not a fabricated prior repair receipt. Without it,
the original strict baseline checks are unchanged. Use it only for independently
reviewed pre-existing type names or relationship-local contextualization UUIDs:

```json
{
  "version": "ontology-current-definition-review/1.0.0",
  "policy": "ontology-current-type-names-contextualization-local-id-only-v1",
  "workspace_id": "<original workspace UUID>",
  "ontology_id": "<original returned/owned Ontology UUID>",
  "domain_contract_hash": "<sealed Domain SHA256>",
  "publication_plan_hash": "<original publication plan hash>",
  "original_definition_hash": "<decoded original definition content hash>",
  "current_definition_hash": "<decoded current definition content hash>",
  "current_definition_file": "captured-current-definition.json",
  "current_definition_file_sha256": "<SHA256 of the complete captured file bytes>",
  "actor": "<reviewing actor>",
  "rationale": "<explicit reason for accepting the pre-existing changes>"
}
```

All fields are required; unknown fields and duplicate JSON keys are rejected.
The captured file contains the complete `{"parts": [...]}` definition, not a
getDefinition response wrapper. Its path is absolute or relative to the review
file. Content hashes use the repair's `_content_hash`, including `.platform`;
the original/current equivalence check excludes only that separately validated
service envelope.

The validator permits only native type `name` changes and one-to-one local
contextualization ID/path substitutions **within the same relationship**, with
every other binding field identical. Path UUIDs must match payload IDs. Duplicate
or ambiguous bindings, changed part order, endpoints, properties, key relations,
cardinality, source tables/columns/schema/workspace/Lakehouse, and added/removed
semantic bindings fail closed. Original ownership, sealed source/provenance and
table proofs are still required. Fresh reads must match the explicit current
hash before the full ownership proof is run against that exact definition.

The plan retains review bytes, parsed policy, validator bytes/hash, complete
captured file bytes and the original-to-current diff separately from the update
diff. Apply/resume revalidate them; edits including whitespace block execution.
Replacement invariants compare the **current backup** to the new names, retaining
current binding and `.platform` bytes, never restoring old contextualization IDs.
Completed reviewed-baseline repairs may subsequently be supplied as
`--previous-repair-state`; their review and captured file must remain unchanged.
Existing receipts are not rewritten. The existing explicitly approved
readback-only verifier upgrade remains the sole repair-code-drift exception;
it cannot permit review/validator drift or another POST.

Ontology LRO polling accepts a matching `x-ms-operation-id` and trusted
`https://wabi-*-redirect.analysis.windows.net/v1/operations/<same UUID>` Location
(optionally `/result`). It always sends GETs to `api.fabric.microsoft.com`,
never credentials to the regional hostname. Non-HTTPS, credentials, ports,
queries, fragments, mismatched IDs and other origins are rejected.

### Integrated raw-text windows

`domain window-run` connects raw source chunks, the complete intake, evolving
working schemas and candidate extraction. Unlike `window-align`, it starts from
an empty schema or an explicit provisional reference and makes new bounded extraction calls. Existing discovery is
used only as a verified source cache, not as a substitute for those new results.

For a shared business vocabulary from window zero, first infer a reference from
the complete first cached document and original intake:

```bash
fabric-kg --config fabric-kg.yaml domain window-bootstrap \
  --discovery .fkg/discovery/run-1.json --intake intake.json \
  --out-state .fkg/concept-reference
```

This is a zero-call plan; add `--live` for one bounded model call. With `--intake`,
the model selects exact indexed source paragraphs instead of retyping quotations.
Separate typed entity, relationship and property arrays are compiled into the
provisional reference with unresolved identities. Inspect the reference and its
evidence; record any refinement in a separate copy, never edit sealed responses.
The inferred file is `.fkg/concept-reference/schema-reference.json`.

```bash
fabric-kg --config fabric-kg.yaml domain window-run \
  --discovery .fkg/discovery/run-1.json --intake intake.json \
  --seed-reference .fkg/concept-reference/schema-reference.json \
  --out-state .fkg/window-run --window-size 8 --concurrency 4 \
  --schema-policy reviewed-concepts \
  --max-calls 160 --max-repair-calls 80 --stop-after-document 1
```

Default is a read-only plan. Alternatively use `--prepared PREPARED.json` instead
of `--discovery`. Add `--live` to execute. Each chunk request produces raw
candidates and schema proposals together. Domain context, all example questions
and the frozen input schema accompany every extraction and repair request.
Only the coordinator evaluates proposals and commits the successor schema.
Omit `--seed-reference` for the empty-schema path. Reference contents are frozen
inside the run; all reference concepts remain provisional. Resume inherits the
saved reference when the flag is omitted and rejects a changed reference before
inference. Bootstrap resume requires repeating the original `--intake` option.

Fresh runs default to `reviewed-concepts`: reusable business types are separate
from source-labelled instances. New schema concepts require a separate, compact
model admission decision before working acceptance. Codes, dates and descriptions
belong in properties rather than value-specific entity types; named parts and
numbered instructions remain instances. Containment, applicability and sequence
use source-supported relationships, not invented subclass chains.

`--schema-policy concepts` selects the self-assessment-only policy;
`observed-terms` preserves legacy behavior. Omitting the option on resume inherits
the saved policy, never silently migrates an old schema. A policy change needs a
fresh state. Status reports include type/instance diagnostics, not semantic-recall
claims. See the [assessment, plan and experiments](specs/SPEC-WINDOW-SCHEMA-OPERATIONS.md#concept-first-assessment-and-bounded-comparison).

`--max-calls` includes repair and semantic-admission calls for this invocation;
`--max-repair-calls` is their run-wide ceiling (default 16). The example allows
78 extraction calls plus up to 80 reviews, not a guaranteed document size.
Review-budget exhaustion leaves unreviewed proposals unresolved; an empty accepted
initial schema is bootstrap-blocked. `--max-tokens` optionally caps
conservative token reservations, not measured provider token usage. Input,
completion and prepared-source chunk bounds are available through
`--max-request-chars`, `--max-completion-tokens` and `--max-chunk-chars`.
Existing discovery chunk coordinates are preserved.

For an evolving schema that outgrows the initial admission ceiling, explicitly
set `--request-char-budget N` on the invocation. This changes only the permitted
request size, not the saved config, model, prompts, source data or cached request
contents. New dispatch records retain the actual size and effective ceiling.
It does not increase the provider's token/context capacity, truncate the schema
or override an aggregate token budget.

Resume with the same input, intake and configuration plus `--resume --live`.
Invocation call budgets and the document stop can change without resetting the
schema. Use `--max-calls 0` for client-free cached continuation. Retrying an
uncertain dispatched request requires explicit `--retry-uncertain`; it is not a
guarantee of exactly-once remote execution.
Known invalid-JSON responses have separately retained private provider
diagnostics and require `--retry-invalid-response`. These two retry permissions
are not interchangeable.

```bash
fabric-kg domain window-run-status --state .fkg/window-run
fabric-kg domain window-run-history --state .fkg/window-run
fabric-kg domain window-run-schema --state .fkg/window-run
```

A stop after document one retains the entire input scope. If more documents
remain, the run is partial. Continue it instead of treating its completed prefix
as a complete corpus. The only partial-processing exception is an explicit
review at or above 99% of the complete declared chunk inventory:

```bash
fabric-kg domain accept-window-run-partial --window-run .fkg/window-run \
  --min-chunk-coverage 0.99 --actor reviewer \
  --rationale "Reviewed remaining processing gaps for this prototype" \
  --out .fkg/window-coverage.json
```

Default is a non-authorizing preview; `--accept` seals the reviewed coverage.
Incomplete source preparation cannot be waived because the chunk denominator
is unknown. For an accepted partial run, supply
`domain design --window-run-acceptance .fkg/window-coverage.json` alongside
`--window-run`. The exact acceptance and all gaps remain bound to the approved
domain and replay; original partial status and grounding quarantine are not
cleared. A first-document stop far below 99% cannot use this exception.

An operator may instead explicitly authorize a **limited committed-prefix
prototype**. This is a different scope decision, not a lowered coverage
threshold. Stop the active extraction process first, then use the same run
configuration with `--resume --live --max-calls 0 --max-windows 0` to seal
exactly the current committed prefix without consuming later cached responses.

```bash
fabric-kg domain accept-window-run-prefix --window-run .fkg/window-run \
  --actor reviewer --rationale "Deploy only the current committed prefix" \
  --out .fkg/prefix-scope.json
```

Inspect the preview before adding `--accept`. Pass this exact scope artifact to
`domain design --window-run-acceptance .fkg/prefix-scope.json`. The original
corpus remains partial; omitted chunks and in-flight responses remain excluded,
not observed-empty. Prefix boundaries apply to model evidence, L2 proposals,
L3 quote relocation/caches and publication evidence. Full SourceUnits remain
unchanged for provenance. Prefix-scoped Fabric descriptions and agent
instructions disclose the included/total counts and scope acceptance hash.

For a complete run, the design path consumes its actual context, prepared source
identity, final schema and candidate ledger:

```bash
fabric-kg --config fabric-kg.yaml domain design \
  --input ./documents --intake intake.json --window-run .fkg/window-run \
  --out .fkg/window-design.json --live
```

Use the existing `evaluate-design`, `compile-design` and `domain approve` steps.
If the model draft omitted source-supported working types or relationships, use
the model-free source-retention step before evaluation:

```bash
fabric-kg domain retain-window-schema --file .fkg/window-design.json \
  --out .fkg/window-design-retained.json
```

The default is a read-only plan; `--apply` writes a new unapproved derived draft.
It preserves the original model draft and records source/projection hashes.
Unsupported endpoint multiplicity, identity conflicts and definition conflicts
remain explicit findings, not silently narrowed or merged.
`--prefer-window-definitions --actor REVIEWER --rationale TEXT` explicitly
chooses exact source definitions over different model descriptions only where
names, scopes and identities otherwise match. Existing graph route targets may
be corrected with `--route-target QUESTION=TYPE`, and typed collection
requirements appended with `--completeness FILE`; both require actor/rationale
and cannot alter source facts, observed counts or authoritative SQL routing.
Evaluate the resulting draft afresh; the parent's evaluation cannot approve it.

If final design incorrectly promotes a descriptive field into a natural identity,
repeat `--source-scoped-type EXACT_NAME` for each reviewed type, with actor/rationale.
For example, `--source-scoped-type Model --source-scoped-type Procedure` clears
only those unapproved draft identity claims, leaving optional variant properties
intact. Each selection must uniquely match an exact working entity name whose
identity is unresolved; selected types must still be retainable with their exact
definitions and hierarchy. Affected descendants also require explicit selection.
The original identity claims remain in the hash-bound parent draft. This cannot
downgrade an approved contract, establish cross-document identity, or approve facts.

Compact ordered collections compile to an explicit C0 1.1 requirement for
ascending, unique, contiguous zero-based member positions. This is a schema
requirement, not evidence of observed order or completeness: cardinality remains
unknown, and source ordinals must not be renumbered or gaps filled to satisfy it.
Missing or incompatible ordering evidence cannot establish a complete collection.
Contracts compiled before `compact-to-schema2-1.8.0` must be freshly compiled and
approved to adopt this requirement; existing sealed artifacts are not upgraded.

An approved contract that already declares this policy needs **no schema change**
to replay partial observations. The L2 collection-partition successor preserves
one-based, gapped, duplicate or missing positions in an immutable
`l2.collection_deferral@1.0.0`, instead of trying to mint an invalid C0 collection.
It never renumbers members, fills gaps or changes an approved requirement. Bad
roles, references, conflicting member identities and incompatible approved
ordering policies still fail; they are not treated as partial ordering.

L3 rederives the complete proposal-or-deferral partition from the persisted
atomic observations. Deferred scopes receive an explicit unresolved,
readiness-blocked `l3.required_member_outcome@1.1.0`, and **no collection manifest**.
Atomic entities, relationships and properties still pass their ordinary evidence
and lifecycle validation; L4 may serve only those that independently assert.
The approved requirement and its question bindings remain in force. A successful
stage receipt means processing/accounting succeeded, **not** that a procedure is
complete or ready for execution.

Then explicitly review the working-to-approved mappings:

```bash
fabric-kg domain review-window-run-mapping \
  --window-run .fkg/window-run --target-domain .fkg/l1/domain.yaml \
  --actor reviewer --rationale "Reviewed final concept names and scopes" \
  --out .fkg/window-run-mapping.json
```

The default review is a non-authorizing preview; `--accept` explicitly seals it.
The approved domain and review bind run, prepared-source, context, final schema
and final mapping hashes. Use fresh L2 state for replay:

```bash
fabric-kg enrich --input ./documents --domain-file .fkg/l1/domain.yaml \
  --l1-state .fkg/l1 --l2-state .fkg/l2-window-run \
  --window-run .fkg/window-run --mapping-review .fkg/window-run-mapping.json \
  --replay-only --dry-run
```

Remove `--dry-run` to execute the reviewed zero-call replay. Continue with
unchanged `validate-evidence` and `project-serving`. Working schema acceptance
does not approve evidence, invent identities or assert graph edges.

Use a **new L2 state directory** when adopting the collection-partition successor,
even if a previous replay failed after writing atomic checkpoints. Its fingerprint
differs from the old policy. Within the new state, interrupted collection writes
and repeated replay reuse the exact atomic checkpoints without model calls.
Historical successful handoffs remain readable; historical invalid/omitted
collections are not retroactively accepted. Inspect `collection-deferrals/`, the
L3 run's `required-member-outcomes/`, and the CLI's blocked-scope count before
making any completeness claim.

Partition implementation `l2-collection-partition/1.0.1` validates C0 member
role syntax and prohibited sentinel roles before deciding whether observed order
can be deferred. This changes the replay fingerprint: use fresh L2 state after
upgrading from `1.0.0`, without recompiling or reapproving the unchanged domain.
The deferral carrier remains `1.0.0`; existing valid handoffs remain readable,
but malformed-role deferrals fail deterministic L3 revalidation.

### Align previously received candidates

Align saved discovery observations before repeating any raw extraction. The
current CLI requires a hash-verified, approved Schema-2 seed domain, used only
as a working reference; its approval does not approve later working changes.
Plan all windows without calls/writes:

```bash
fabric-kg domain window-align --discovery .fkg/discovery/run-1.json \
  --seed-domain .fkg/l1-026/domain.yaml --out-state .fkg/windows \
  --window-size 64 --concurrency 12 --proposal-mode deterministic --max-calls 0
```

Add `--live` to persist windows. Explicit `deterministic` mode uses no model and
records unique formatting normalization only; unknown synonyms remain pending.
Model mode uses the configured Foundry client and a bounded `--max-calls` budget
for alignment proposals, not another raw-source extraction:

```bash
fabric-kg --config fabric-kg.yaml domain window-align \
  --discovery .fkg/discovery/run-1.json --seed-domain .fkg/l1-026/domain.yaml \
  --out-state .fkg/model-windows --window-size 8 --concurrency 4 \
  --proposal-mode model --max-calls 32 --live
```

Every chunk in a window receives the same schema snapshot. The coordinator
evaluates proposals and commits one successor before the next window. Original
observations remain immutable, and final mapping uses the final snapshot.
An oversized context or exhausted budget remains partial instead of truncating
the schema or claiming all chunks were processed.

Use `--resume` with the same state and configuration to continue. Proposal mode
is bound to the manifest; switching deterministic/model modes requires a new
run. A complete deterministic run means complete processing, not complete
semantic mapping. Inspect the actual state and all transitions:

```bash
fabric-kg domain window-status --state .fkg/windows
fabric-kg domain window-history --state .fkg/windows
```

Snapshots/logs preserve before/after hashes, input chunk IDs, request/response
identity, additions/rejections and pending choices. They are the durable record,
not chat memory. `domain window-schema` exports the exact machine-readable
contracts without requiring model credentials.

Review the final mapping against the exact approved target domain:

```bash
fabric-kg domain review-window-mapping --state .fkg/windows \
  --discovery .fkg/discovery/run-1.json --target-domain .fkg/l1-026/domain.yaml \
  --actor reviewer --rationale "Reviewed compatible working-schema mappings" \
  --out .fkg/window-mapping-review.json
```

Default review is read-only. Add `--accept` only after inspecting the proposed
targets and unresolved concepts. The review binds the discovery bytes/hash,
target-domain hash and final window run/snapshot/mapping hashes; it does not
change the approved domain or authorize evidence.

```bash
fabric-kg enrich --input ./documents --domain-file .fkg/l1-026/domain.yaml \
  --l1-state .fkg/l1-026 --l2-state .fkg/l2-window-mapped \
  --discovery .fkg/discovery/run-1.json --window-state .fkg/windows \
  --mapping-review .fkg/window-mapping-review.json --replay-only --dry-run
```

`--window-mapping` is an alias of `--mapping-review`. Use fresh L2 state for a
changed mapping review. Values, source anchors and raw observations are not
rewritten; unmatched/provisional concepts remain pending and existing L3 checks
still apply.

For a later ontology design, `domain design --window-state DIR` carries the final
working schema as versioned reference context alongside the matching discovery.
It does not automatically approve new working concepts.

Detailed source text need not all become ontology properties. Follow
[Foundry/Search orchestration guidance](FOUNDRY-SEARCH-ORCHESTRATION.md) for using
core ontology keys to retrieve scoped original paragraphs/tables and for routing
analytics to SQL. Index expansion and runtime source adjudication are not
implemented by these window commands.

## Question routing and Lakehouse SQL context

Collect the expected answer and business background along with each question.
Use ontology/graph retrieval for concepts, relationships and procedural scope.
Analytical counts, numeric analysis and trends normally belong to Lakehouse SQL.
Do not create ontology quantity/metric properties solely to answer those
questions. Numeric source facts, identifiers and safety text remain intact.

An intake question can explicitly declare routing. The following is **one entry**
in `competency_questions`, not a complete intake:

```yaml
id: cq:q6
question: How many distinct parts are linked to this repair job?
business_critical: true
pending_requirements:
  - Confirm whether the requested count is distinct SKUs or physical pieces.
routing:
  version: "1.0.0"
  backend: lakehouse_sql
  operation: count
  rationale: Count the scoped part records in Lakehouse SQL.
  population: Parts linked to the selected repair job and device variant
  grain: Distinct governed part identity, not physical part occurrences
  filters:
    - Selected repair job, model and variant
  time_requirements: []
  source_requirements:
    - Approved part records and task-to-part associations
    - Complete scoped records and original source evidence
  physical_binding_state: unresolved
```

The two backends are `ontology_graph` and `lakehouse_sql`; operations are
`lookup`, `count`, `aggregate` and `trend`. Missing population/grain/time/source
details remain review requirements, not fabricated physical bindings. Explicit
intake routing is authoritative; the model can propose routing for unclassified
questions. A SKU lookup is not SQL analysis merely because the SKU contains digits.

Inspect the context before and after approval:

```bash
fabric-kg domain question-context --file .fkg/design/draft.json
fabric-kg domain question-context --file .fkg/l1-026/domain.yaml
fabric-kg domain question-context --l4-run <sealed-l4-run> --l3-root <l3-state>
```

These commands write JSON to stdout only. They include the source/domain hash,
canonical routing context/hash, original question criticality, business
background, unrouted question IDs and unresolved requirements. No model or SQL
is called. Redirect stdout only when a separate local export is desired.

SQL-directed questions remain in the domain but do not need a fabricated
ontology answer path or quantity property. Their Graph coverage remains false;
SQL readiness is separately unverified. All-SQL designs can be saved and
exported, but the strict ontology compilation path reports that no ontology
handoff is needed rather than fabricating graph definitions.

Per-question `pending_requirements` also survive compilation and approval,
including unresolved design notes. They remain separate from `routing` and do
not become facts or execution permission. Snapshots use version `1.1.0` when
these notes exist, otherwise the unchanged `1.0.0` representation.
Additional user descriptions remain labeled in approved business context without
modifying the original intake.

Approved context is included in extraction request identity and retained through
the existing sealed domain in serving. For the prompt-agent deployment path,
supply that same approved domain using `app deploy-agent --domain-contract`.
Use `--dry-run` for local planning; omitting it can deploy and is not authorized
by a context inspection request. The static `app compile-l6` command is not a
replacement for this per-domain runtime handoff.

Routing is **not** a SQL executor, verified table/column mapping or computed
answer. The current L6 guard protects exact registered SQL-question wording
before Graph/Search; it is not a general paraphrase classifier. Unregistered
paraphrases and mixed requests still need intent resolution and source readiness.
Never substitute a graph-only answer when a registered SQL route is unresolved.

## Foundry project inference

The CLI can explicitly use a Foundry project Responses endpoint instead of the
account-level Chat Completions endpoint:

```yaml
foundry:
  endpoint: ${FOUNDRY_PROJECT_ENDPOINT}
  inference_api: project_responses
  chat_deployment: gpt-4.1
  request_timeout_seconds: 300
```

Use an existing deployment available to that project. This transport requires
the existing `agent` optional dependency (`azure-ai-projects>=2.3`), authenticates
with AzureCliCredential, uses `store: false`, and never falls back to API keys or
another endpoint. It supports JSON generation, not embeddings. The default
`chat_completions` transport retains its existing configuration.

Model connectivity is separate from a valid ontology proposal. `init-domain`
still rejects generated candidates that violate evidence, ordering or endpoint
contracts. HTTP provider failures now retain a bounded, redacted status/detail
in the CLI failure audit.

## 1. Strict compatibility proposals and document assessment

The older `init-domain --input ... --intake ... --non-interactive` path prepares a
strict Schema-2 draft, not the exploratory draft above. Live L1 generation uses
the configured model and requires a budget.
`--candidates` supplies an explicitly offline fixture instead. The draft is not
approved merely because the command exits successfully.

Assessment defaults to planning, with no model calls or output-file writes:

```bash
fabric-kg domain assess --file domain.yaml --input ./documents
```

Inspect `planned_windows` and `file_dispositions`. Only then run a bounded
assessment:

```bash
fabric-kg --config fabric-kg.yaml domain assess \
  --file domain.yaml --input ./documents \
  --live --max-calls 4 --max-output-tokens 1600 \
  --out .fkg/assessments/first.json
```

`--responses` is an offline JSON object mapping exact planned window IDs to
`{"findings": [...]}`. Do not label fixture execution as model validation.

Reports are create-only. Use a new `--out` and `--checkpoint` pointing to an
earlier report to continue matching inputs. Responses are also cached beside the
report, by exact request/model fingerprint. Cached responses are revalidated.

`complete` means all represented text windows were assessed with no excluded or
failed file dispositions. It does not prove semantic recall. Deferred windows,
unsupported modalities and unlabelled source facts remain explicit limitations.

## 2. Review and produce a separate revision

Create a decisions array with each report finding's **actual** ID, a disposition
(`accepted`, `rejected`, or `deferred`) and nonempty rationale. Decide every
finding exactly once; do not invent IDs or auto-accept merely to unblock a run.

```bash
fabric-kg domain review-assessment \
  --file domain.yaml --assessment .fkg/assessments/first.json \
  --decisions decisions.json --actor reviewer \
  --out .fkg/reviews/first.json --revision-out .fkg/reviews/request.json
```

Reviewing findings does not approve the ontology. `domain revise` defaults to
planning; `--live --max-calls 2` permits bounded model generation, while
`--candidates` supplies an offline L1 proposal fixture.

```bash
fabric-kg domain revise \
  --parent-state .fkg/l1 --input ./documents \
  --assessment .fkg/assessments/first.json \
  --review .fkg/reviews/first.json --request .fkg/reviews/request.json \
  --out-state .fkg/revisions/second
```

The command re-reads the corpus, verifies report windows against actual sources,
supplies the parent definitions, and mints fresh design evidence for accepted
locations. It cannot silently remove IDs, change identity/hierarchy/property
types or add required properties to existing types. Such breaking changes need
a separately authorized workflow.

The child stays a blocked draft. The parent is unchanged. Inspect the child and
its recorded change set, then use `domain approve` with the exact project ID,
run ID and proposal hash. Never substitute placeholders or hand-edit receipts.

## 3. Consume explicit approved state

Pass both the approved domain and its corresponding L1 state:

```bash
fabric-kg enrich --input ./documents \
  --domain-file .fkg/revisions/second/domain.yaml \
  --l1-state .fkg/revisions/second --l2-state .fkg/runs/second/l2 \
  --dry-run
```

Without `--dry-run`, enrichment may call the model. It checks the current corpus
against the approved manifest. L2 output must not overlap sources or L1 state;
`--force` cannot delete an explicitly supplied state root.

Continue with `validate-evidence` and `project-serving`, supplying the matching
`--l1-state`, `--l2-state`, `--l3-state` and `--domain` arguments from their help.
`project-serving` prints its actual immutable run root; that, not merely the L4
state parent directory, is the input to `app publish-structured --l4-run`.

Property values now retain owner identity, canonical scalar JSON and evidence
through L2/L3/L4 into typed L5a columns. Only literal source-supported values are
asserted; unsupported normalization transformations remain non-asserting.

## 4. Optional raw OCR replay

```bash
fabric-kg domain analyze-layout \
  --input document.pdf --endpoint "$AZURE_DOCINTEL_ENDPOINT" \
  --cache-dir .fkg/ocr --max-pages 1
```

Default mode is a plan. Explicit `--live` permits one analysis POST using the
current Azure CLI identity, never API-key fallback. Over-page/byte-limit inputs
are rejected before submission. A matching cache makes no new POST. Ambiguous
failures retain a reservation; do not remove it to retry blindly.

The cache preserves lossless raw response JSON, Unicode codepoints, tables and
geometry. Its 1.1 format rejects legacy 1.0 with explicit guidance rather than
silently normalizing or reanalyzing it.

Save the returned nonsecret `extractor_identity` as JSON, then supply
`domain assess --ocr-cache .fkg/ocr --ocr-identity identity.json`.
For OCR-backed revisions, also pass `domain revise --ocr-cache .fkg/ocr`.
Do not claim cached OCR text is perfect interpretation of the original image.

## 5. Offline acceptance and live boundaries

### Schema-2 prototype Data Agent handoff

After the create-only structured publication has verified its owned Ontology,
bound tables and companion graph, inspect:

```bash
fabric-kg app publish-prototype-agent --help
```

This separate path consumes the actual prototype plan/journal and sealed L4/L3
authority. It does not create or accept a fabricated legacy H3 projection receipt.
Plan first:

```bash
fabric-kg app publish-prototype-agent \
  --prototype-journal <publication-journal> --prototype-plan <publication-plan> \
  --materialize <publication-materialized-root> \
  --l4-run <sealed-l4-run> --l3-root <l3-state> \
  --workspace-id <approved-workspace-id> --name-prefix <approved-prefix> \
  --out-state <separate-agent-state>
```

After inspecting the actual plan, live creation requires the same arguments plus
`--live --approve-live <exact-agent-plan-hash> --acknowledge-preview`.
This creates one new **draft** Data Agent with the actual owned Ontology and
same-Lakehouse SQL source. It does not adopt, update, publish or delete an
existing agent. A ready SQL endpoint binding is required.

#### Reviewed publication runtime repair

An accepted `returned-id-owned-exact-runtime-repair-v1` receipt in the original
publication journal is supported without another CLI switch. Offline planning
recompiles the exact semantic publication plan and validates the original plan
bytes, current publication compiler, accepted review, successful original Graph
create evidence and immutable artifact digests using the reconciliation validators.
It makes no remote calls. Operator-reconciled creates, unbound compiler drift,
unsupported repair kinds and missing/tampered receipts remain rejected.

Only repaired-source agent plans add `publication_runtime_repair`, binding the
complete receipt hash, review hash, original/current compiler identities,
candidate plan hash, original plan bytes and immutable artifact authority.
Existing unrepaired plan fields are unchanged; changed agent code still requires
a fresh agent plan and approval, not a legacy compiler-identity alias.
Live execution revalidates these bindings and obtains fresh exact returned-ID,
create-LRO (when applicable) and native-definition proof before any agent create.
The original publication plan, journal and repair receipt are never rewritten.

This compatibility does **not** bypass Ontology companion readiness, source
ownership, Delta/readback validation or the independent create budget. A
`GraphNotQueryable` companion still blocks the handoff. A valid local repair
receipt is not evidence of Data Agent question accuracy or complete source scope.
`--runtime-context-review` below reviews instructions only; it cannot authorize
a publication runtime repair.

#### Explicit runtime organization-context review

Fabric global instructions have a **15,000-character hard limit**, including
safety instructions, routing, scope notices and exported context. Oversize plans
fail offline; the publisher never truncates context or raises this limit.
When the approved organization context contains obsolete compiler-editing
directions, an operator may explicitly review a replacement using
`--runtime-context-review FILE`:

```json
{
  "version": "schema2-runtime-context-review/1.0.0",
  "domain_contract_hash": "<exact approved sealed DomainContract hash>",
  "actor": "<reviewer identity>",
  "rationale": "<why this replacement preserves business meaning and scope>",
  "organization_context": "<complete reviewed runtime organization context>"
}
```

These fields are required; each must be a nonempty string. The only optional
field is `"routing_encoding": "columns-v1"` (described below). Duplicate
keys, unknown versions and mismatched/unapproved contract hashes are rejected.
The file is bounded to 128,000 UTF-8 bytes and replacement text to 15,000
characters; the **complete global instruction** must still fit 15,000 characters.
No model summarizes or edits the text. The reviewer must retain true business
context and scope, rather than interpreting this feature as permission to remove
constraints to make a plan fit.

Only the value of `business_context.organization_context` in the global runtime view changes.
Question routing, pending requirements, users, decisions, problem, scope,
source notices and all IDs remain unchanged. The original full context remains
in `plan.json.question_context`, the original handoff and Lakehouse source
metadata `schema2_question_context`. The retained `export_hash` identifies that
original source export, not the reviewed runtime view; global instructions
explicitly disclose this distinction.

When replacing organization text alone does not fit, the reviewer may explicitly
add `"routing_encoding": "columns-v1"` to the same JSON. This lossless encoding
stores homogeneous question keys once in `columns`, homogeneous routing keys
once in `routing_columns`, and questions as value rows. Zip each question row
with `columns`; its `routing` value is a row zipped with `routing_columns`.
Every value, nested list/object, null, pending constraint, question ID and array
order is preserved. The original routing `context_hash` identifies the expanded
content, not its encoded representation. A short decoding explanation is
included in global instructions. Nonhomogeneous keys and reserved encoding-key
collisions fail closed rather than dropping data. Source metadata and the plan's
full `question_context` retain the original object representation.

Omitting `routing_encoding` preserves object encoding; no automatic compression
or fallback occurs. Adding/removing/changing this choice changes the review and
plan identity. Even explicitly encoded instructions must pass the same complete
15,000-character check.

The optional offline sizing regression accepts the approved Surface demo contract
via `FKG_TEST_APPROVED_DOMAIN_CONTRACT=/path/to/domain.yaml` when running
`pytest tests/unit/test_schema2_prototype_agent.py -k actual_approved_contract_columns_instruction_size -s`.
It uses 64-character source/projection hash placeholders only in memory, never
as deployment facts or saved publication artifacts. It checks the complete
instruction, expanded routing equality and unchanged source metadata.

The plan and CLI output disclose the review content, resolved file path,
canonical review hash and exact-byte file SHA-256. Use the **same file and flag**
for live execution and resume: changing its text, actor, rationale, path or even
formatting, removing the flag, or editing the plan requires a new state directory
and explicit plan approval. Exact native definition readback checks the reviewed
instructions and the preserved full source metadata. No other content is
automatically reduced if the result remains too long.

Without this option the native definition and plan fields are unchanged. This
backward-compatible publisher revision preserves its predecessor's no-review
compiler identity through a revision-hash guard; subsequent code changes still
invalidate that compatibility alias. Review-enabled plans bind the actual new
compiler fingerprint and never share legacy approval identity.

Lakehouse SQL endpoint provisioning may finish after initial Lakehouse creation.
If the saved metadata is not ready, resume the exact approved
`app publish-structured --prototype-create-only --live` plan/journal. Its owned
item readback refreshes metadata without creating replacements. Plan the agent
only after that readback succeeds; its plan binds the updated publication journal.
Do not edit SQL endpoint fields in the journal by hand.

Optional `--search-source FILE` requires a real captured native Search source
configuration; an endpoint name alone is not evidence of a working source.
Without it, do not promise original-text retrieval from an index. Local evidence
validation does not establish that the Data Agent can access those quotations.

Definition/source readback is not end-user acceptance. The user must have
appropriate access to the new draft and sources, and actual questions must
exercise ontology lookup, SQL counts and source citations. Published-stage
availability, asking-user permissions and runtime SQL answers remain unverified
until explicitly tested. Keep partial items and recorded operation IDs on error;
do not create a replacement blindly.

The existing pytest runner contains a complete fake-provider CLI exercise:

```bash
python -m pytest tests/unit/test_schema2_prototype_cli.py \
  tests/unit/test_domain_assessment.py tests/unit/test_domain_revision.py \
  tests/unit/test_layout_cache_cmd.py -q --no-cov -p no:cacheprovider
```

The CLI exercise reaches approved L1, L2, L3, L4 and typed L5a materialization
with a non-null source-grounded scalar. This is not a live Fabric deployment.

Do not use `densify` as a schema-2 step or route around missing authority through
legacy output files. Concrete schema-2 live Fabric publication remains
capability-gated. Heterogeneous endpoint key signatures, full cross-release
migration, and production-wide recovery remain separate work.
In particular, current generated physical crosswalk IDs are not yet a proven
stable-ID upgrade mechanism. Do not infer safe deployed schema evolution from
a successful local draft revision.

Model/DI access denial is a blocker requiring the resource owner to confirm
data-plane permissions/access policy. Stop rather than retry with keys, other
identities or another resource. Successful management-plane listing is not
proof that inference is authorized.
