# Windowed working-schema operations

Status, 2026-09-11: cached-candidate alignment/replay is implemented. The
standalone first-document bootstrap is experimental. The integrated raw-text
coordinator and approval/replay bridge are implemented locally. Bounded live
execution covered the first document and continuation into the second;
full-corpus semantic acceptance is not complete. Fresh integrated runs now default
to core-business concept proposals. The first same-source experiment below
completed, exposed residual issues, and prompted a versioned refinement;
full semantic acceptance is not claimed.

## Goal and scope

Replace independent naming across discovery chunks with a common, evolving
working vocabulary and mapping registry. Carry its identity through the last
chunk, persist each batch transition locally, and reuse existing observations.

The existing change implements CLI state, alignment and handoff. Detailed source
retrieval, multi-span answer adjudication and Search index construction remain
Foundry Agent orchestration guidance, not new engines in this release.

An evolving working schema is not a published ontology. An accepted working
change is not an asserted source fact or final mapping/domain approval.

## Concept-first assessment and bounded comparison

### Observed-terms baseline

The 2026-09-11 read-only assessment examined the approved prefix contract and the
38 committed windows covering 302 chunks, not the entire extraction file tree.
Local evidence is under
`/Users/hyssh/.copilot/session-state/b7936870-1338-44f3-beea-5ed110ae4791/files/surface-prototype-20260908`.
Paths in this subsection are relative to that artifact root.

| Artifact/scope | Chunks | Entity concepts | Property concepts | Relationship concepts |
|---|---:|---:|---:|---:|
| `integrated-windows-v11/windows/000009.json`, first complete document | 78 | 64 | 2 | 2 |
| `integrated-windows-v11-final-schema.json`, older exported snapshot | 86 | 71 | 4 | 2 |
| `integrated-windows-v11/windows/000037.json`, actual paused prefix | 302 | 266 | 54 | 13 |

All three snapshots have zero parent-type links. The approved
`paused-prefix-l1/domain.yaml` contains 270 entity types and 20 relationship types,
also without parent-type links. Of its entity types, 260 have retained `ws_`
identifiers. Approval and retention are not evidence of suitable abstraction.
The `*-final-schema.json` export is stale relative to the paused prefix; do not
compare its 86 chunks against a new 78-chunk run as though the scopes match.

Preserved proposals promote names such as `M1234550 Tape`, `Surface Laptop Studio`
and `Procedure - Installation (Enclosure)` into root entity types. Antenna, fan
and USB-C connector also become individual component-specific types rather than
observations classified under a reusable part concept. The tape example is
preserved in
`integrated-windows-v11/responses/24fefedd77f0eafc3fa25ac301a73a5f2e24980b91e8507e488e108cd4366896.json`:
candidate index 5 uses `M1234550 Tape` for both label and observed type, and proposes
`Component.m1234550_tape` because it observed that tape as a component for SSD.
This is a class/instance decision, not a display-name cleanup.

The old request asks for new concepts to use their observed names. Combined with
label-as-type generation, that produces lexical classes. The prefix contains
607 of 2,969 raw entity candidates whose label and observed type are identical;
this is a diagnostic, not a count of proven modeling errors. Generic names alone
are insufficient too: 689 mapped `component`/`Component` observations resolve to
`component.display_tdm`, whose definition is specifically a display requiring
calibration/authentication, including observations labeled Battery, Feet and
Motherboard. Inspect definition fit, not just successful name matching.

For comparison accounting, the first-document run has 920 raw candidates:
729 entities, 149 properties and 42 relationships. Its final mapping has 409
mapped, 366 pending and 145 quarantined records. The 302-chunk prefix has 3,631
raw candidates: 2,969 entities, 515 properties and 147 relationships; 1,741 mapped,
1,284 pending and 606 quarantined. These are observation records, not distinct
real-world objects or proof of semantic recall.

### Current policy and its limits

Fresh `domain window-run` invocations default to `--schema-policy reviewed-concepts`
(prompt v1.5). Explicit `--schema-policy concepts` selects the v1.3 self-assessment
policy; `--schema-policy observed-terms` retains the legacy
v1.1 behavior. An implicit policy on resume inherits the recorded run's policy,
including v1.1/v1.2/v1.3/v1.4; it must not reinterpret responses under
the new policy. Changing policy requires a fresh state rather than migrating a
recorded run in place.

Concept-first generation uses a general class name in candidate `observed_type`
while preserving the source label and anchors. The explicit observed-type/name
matching guard remains; do not bypass it with approximate matching or rewrite
old evidence. New proposals provide an abstraction level, rationale and reuse
assessment. Admission checks reject declarations of instances as types and
instance names as type aliases. A parent-type proposal needs an explicit IS-A
rationale. Model-to-SKU-to-Part and Country-to-City-to-Town containment or
association structures belong in supported relationships, not fabricated
subclass chains. Genuine subclassing remains distinct from those relationships.

V1.3 additionally requires an explicit representation (`entity_type`,
`relationship_type` or `property`) matching the proposed kind. Declared scalar
values, specialized instances and retrieval-only details cannot enter the schema.
An entity type needs a rationale for independent identity/lifecycle or business
role. A proposal that declares an already-fitting existing concept is rejected:
classification must use that existing type during original extraction, not
create another class. These explanations do not authorize entity identity keys.

V1.4 adds enforced independent semantic admission, because two experiments showed
that stronger instructions and self-declared rationales alone do not reliably
reject value classes. Original extraction proposals cannot admit their own new
concepts. They receive a review-required diagnostic and enter the existing
bounded repair/review channel. A separate model call critiques each proposal
against the source, business context and frozen schema, then either returns an
explicit indexed replacement or vetoes it into pending evidence. Every admitted
replacement still passes the structural and grounding checks. A missing review
budget cannot promote an unreviewed type; an entirely unaccepted initial schema
remains bootstrap-blocked.

This is a separate model invocation/task, not a different model or a guarantee
of independent statistical errors. The review remains provisional; it is not
human approval or fact validation. Reuse the immutable request/response,
supersession, budget and resume machinery rather than overwrite extracted data.
`--max-repair-calls` includes these semantic calls (default 16), and they also
consume `--max-calls`. They run per affected chunk response, not one mandatory
second call for every chunk; known vocabulary without new proposals needs none.

V1.5 makes that admission contract deliberately small and separately prompted:
`decisions: [{proposal_index, verdict: admit|veto, reason}]`. Every failed proposal
index must appear exactly once. The schema supports provider-enforced structured
output without the open dictionaries needed by the extraction envelope. The
critic does not regenerate candidates or definitions. An admitted index selects
the original proposal and records the review reason; a veto leaves it pending.
Both original and critic responses remain sealed. The code rejects missing,
duplicate or unknown indexes and revalidates every selected original proposal.
The manifest binds this critic prompt and schema as well as the extractor.

At every window the operation is:

1. Carry the original domain brief/questions, complete current schema and pending
   decisions into each bounded primary-text request.
2. Classify source mentions under reusable business roles first; preserve specific
   names, item codes and instruction wording in instance labels and exact evidence.
3. Extract explicit actions/associations with local endpoint candidates and their
   own primary-source quotations. Normalized class/predicate names need not occur
   literally in the quote. Do not infer missing applicability or ordering links.
4. Evaluate abstraction declarations, obtain the required independent semantic
   admission, and check quotes, endpoint/owner types and additive changes at the
   shared barrier. Retain rejected observations and original
   responses; repairs cannot rewrite their labels, values or evidence.
5. Commit the next schema and remapping records, then reuse that schema in the next
   window. Final design receives the modelling policy as well as the schema and
   detailed evidence; domain, mapping and fact approval remain separate.

The ontology/type view should show `Part`; an instance graph can still show many
individual parts with labels such as fan or antenna. Reducing schema types does
not mean deleting useful instances or merging all parts into one object.
`window-run-status` reports type counts, grounded/mapped candidate counts,
class/instance-name collisions and bounded instance-label examples per class.
These are descriptive diagnostics, not an automatic semantic acceptance score.

These guards validate structure and model self-assessment, **not semantic proof**.
A model can label an instance as a reusable concept or supply a plausible but
incorrect rationale. Grounded quotations do not prove the abstraction, identity,
endpoint meaning or relation direction is correct. Human-reviewed semantic
fixtures and the existing evidence/approval gates remain necessary.

The target is a domain-general conceptual vocabulary, not a hard-coded Surface
allowlist or a target count of types. Model, SKU, Part, Procedure, Step, Symptom,
Department and geographic concepts illustrate reusable roles; only source-backed
roles and relations should appear in a particular run. Do not invent departments,
places, steps or edges just to complete those example patterns.

### First-document experiments

Use the existing cached discovery and intake, an empty initial schema and a new
output state. Do not seed from the approved 270-type contract or resume the old
observed-terms state. The first source is
`src:0699b2ff3442e3e6c1ca00db9cc30475`,
`Surface Laptop 7th Edition for Business English Service Guide.pdf` (78 chunks).
Its cached Document Intelligence artifact is
`ocr-corpus/fc6a9e15284211562214161a9cc8a18f912c44072abb98a720a603484c055852.json`;
`ocr-cli-logs/10.stdout` records the 78-page `prebuilt-layout` extraction.
Cached prepared source units are in
`corpus-first-full-cache/sources/ae3be9fd738b48c2ae43c4a6f6d97105fb087935dca96d9d13470aa06c8f53b0.json`.
No new DI extraction is needed for this comparison.

From the artifact root, plan the bounded run through the public CLI:

```bash
fabric-kg --config fabric-kg.yaml domain window-run \
  --discovery corpus-first-full-discovery-run4-c12.json \
  --intake intake-sql-routing-pending.json \
  --out-state concept-windows-v15-first-document \
  --schema-policy reviewed-concepts --window-size 8 --concurrency 8 \
  --max-request-chars 160000 --max-completion-tokens 32768 \
  --max-calls 160 --max-repair-calls 80 --stop-after-document 1
```

Use an unused state path; add `--live` only for the authorized bounded inference.
Compare with `integrated-windows-v11-run3.log` and window `000009.json`, snapshot
10, rather than the older 86-chunk export or 302-chunk approved contract.
Historical configuration uses `gpt-4.1`, 8,000-character chunks and the same
window/request/completion limits. The completed historical first document used
86 total model calls, including eight repairs. V1.2/v1.3 retain the eight-repair
ceiling with a 100-call cap. Reviewed runs allow up to 80 admission/repair calls and 160
total calls, explicitly trading additional inference for a separate critique.
All experiments stop at the same document boundary and reuse DI extraction.

The first concept-first pass is retained separately in
`concept-windows-v12-first-document`, with `concept-windows-v12-live.log` and
`concept-windows-v12-status.json`. It completed all 78 chunks with 86 calls:
21 entity types versus 64, but only two relationship types (revision metadata).
Its final mapping counts were 773 mapped, 23 pending and 159 quarantined.
The generic Component definition no longer meant only a display, and named parts,
steps and symptoms were preserved under broader classes. Nevertheless, Battery
remained a component-specific class, and document number/date/change values became
entities. This pass was not accepted as sufficient simply because counts improved.

V1.3 retains the v1.2 prompt and response contracts for readback/resume rather than
rewriting that experiment. It adds the representation/identity-role checks above,
prefers owned scalar values over metadata nodes, distinguishes instances from
functional specializations, and explicitly prompts for source-stated typed
relationships. It reiterates that table cells must not be concatenated into
fabricated quotes. The new comparison uses the same cached chunks in a fresh state.

The v1.3 self-assessed run completed 78 chunks with 16 entity types, five property
types and four relationship types. It introduced business predicates (`acts_on`,
`requires_tool`, `has_step`) but still admitted scalar wrapper classes and
conflated Procedure with Step in a relationship scope. Stronger generation
instructions did not establish semantic acceptance.

The v1.4 full-proposal critic experiment was stopped and frozen at 24 chunks using
`--resume --max-calls 0 --max-windows 0`; the freeze made zero model calls. Its
first 16 chunks exposed omitted replacement indexes, invalid `action: pending`
proposals and incorrect critiques of legitimate general types/properties. It is
retained in `concept-windows-v14-first-document` and
`concept-windows-v14-frozen.json`, not used as a completed 78-chunk comparison.
Its small schema was partly a fail-closed protocol result, not proof of better
modelling. Unfinished dispatches are not silently retried. This finding motivated
the separate compact critic and kind-aware instructions in v1.5.

The unseeded v1.5 run completed the same 78 chunks, with identical prepared,
source-cache, intake and model hashes and unchanged configuration except the
prompt version. `concept-window-comparison.json` records those comparisons.

| Run | Entity types | Property types | Relationship types | Calls (including reviews) |
|---|---:|---:|---:|---:|
| Legacy v1.1 | 64 | 2 | 2 | 86 |
| Concept-first v1.2 | 21 | 1 | 2 | 86 |
| Self-assessed v1.3 | 16 | 5 | 4 | 86 |
| Compact-critic v1.5, empty seed | 18 | 1 | 5 | 133 |

V1.5 recorded 79 admit and 173 veto decisions with zero indexed-review protocol
errors. Its 637 grounded entity, 102 property and 40 relationship candidates
yielded 717 mapped, 62 pending and 289 quarantined records. It preserved broader
Part/ServicePart/Procedure/ProcedureStep roles, but still admitted Battery,
BatteryCover and Fastener specializations. This is evidence of improvement and
remaining errors, not a declaration that all concepts are suitably abstract.

### Source-derived reference before window zero

The experiments expose a cold-start design problem as well as a prompt problem:
parallel page workers share an *empty* schema initially, so early pages can
establish specialized or inconsistent roles before a broader role is available.
A critic seeing that same empty snapshot cannot reliably resolve the global
concept design. Add a full-first-document reference step before extraction:

1. Infer a reusable working reference from the complete first cached document
   plus the original intake, including all questions. Keep exact quotation
   examples and uncertainty; infer concepts, not asserted instances.
2. Inspect the reference for model names masquerading as types, values as entities,
   procedure/step confusion and unsupported links. It is not a domain approval.
3. Supply its `DesignReference` to `window-run --seed-reference`. Bind the complete
   reference content/hash, not a mutable external path. All seed concepts are
   provisional and their identities unresolved.
4. Every first-window worker sees that same version-zero reference. Later windows
   reuse and evolve it through the existing grounded, independently reviewed
   proposal path. A specific fan or battery can now reuse an already-present
   Part role rather than becoming an early root class.
5. Resume inherits the exact recorded reference; a different reference requires
   a new run. Existing unseeded manifest/request/report bytes remain unchanged.

This is a generated domain reference, not a source-specific type allowlist in
code. Seeded and empty-seed experiments must be reported separately: the seeded
run deliberately has an additional whole-document modelling call and a different
initial vocabulary. Subsequent document inference remains paused until requested.

The reference path is implemented as `window-bootstrap --intake ...` followed by
`window-run --seed-reference ...`. Intake bootstrap uses separate provider-strict
entity/relationship/property arrays and source paragraph selectors. Runtime code
sets unresolved identities and converts into the existing `DesignReference`;
the model cannot put relationship endpoints on a property or choose an unknown
scalar datatype. Lossless indexed spans preserve all original text and offsets.
Selected IDs resolve only to supplied source paragraphs, which are then checked
by the existing exact-source validator. They support proposed vocabulary, not
asserted relationships. Legacy no-intake bootstrap behavior is unchanged.

Three rejected bootstrap attempts remain preserved: the first two could not
faithfully retype source whitespace/table quotations; the third used invalid
property shapes. No invalid quotation or schema was accepted. The compact,
pointer-grounded run `concept-first-document-reference-v4` completed in one call
over all 78 chunks/122,746 source characters, producing eight entity types, eight
relationship types and four properties.

`concept-reference-reviewed.json` is a separately reviewed copy. Its only semantic
change broadens the inferred Part definition from orderable service parts to
physical components/subcomponents, so a connector is not falsely asserted to be
an independently orderable SKU. `concept-reference-review.json` records that
assistant review and the original reference file hash. This is not an unchanged
LLM output or human/domain approval. Part Number/SKU retains the source catalogue
identifier meaning; no device-configuration SKU relationship is manufactured.
The original inferred response, reference and evidence are unchanged.

### Completed guided first-document result

`concept-windows-guided-first-document` completed all 78 original chunks in 10
windows. Every window started from or retained the shared high-level vocabulary.
No component-specific entity type was added. Final entity types are Model, Part,
Part Number, Procedure, Step, Tool, Consumable and Safety Rule.

| Metric, same 78 cached chunks | Legacy empty-seed run | Guided concept run |
|---|---:|---:|
| Entity types | 64 | 8 |
| Relationship types | 2 | 8 |
| Property types | 2 | 4 |
| Mapped entity candidates | 404 | 724 |
| Mapped relationship candidates | 2 | 75 |
| Grounded relationship candidates | 19 | 125 |
| Mapped property candidates | 3 | 8 |

The guided result has 155 distinct mapped Part labels, 107 Step labels, 53 Procedure
labels, 36 Tool labels, 14 Part Number labels and five Model labels. These are
labels, not resolved real-world identities. Antenna and named fasteners remain
instances of Part, not separate classes. There are zero class/instance-name
collisions in the diagnostic. The reference exposes typed links including
Procedure -> includes step -> Step and Step -> requires tool -> Tool; observed
relationship candidates remain distinct from those schema definitions.

The 75 mapped relationship observations populate five predicates: includes step
(24), requires tool (28), requires consumable (11), invokes safety rule (9), and
replaces part (3). For example, page 23 connects Feet Replacement to its removal
Step and that Step to Nylon Spudger, using the original removal instruction as
evidence. Fan/Fan Assembly and Antenna map to Part on their servicing pages.
USB-C connectors occur in a mapped motherboard-installation Step quotation but
were not extracted as standalone Part entities; power-button mentions likewise
do not establish a separately extracted Part. Do not claim mappings that are absent.

The successful workflow used one whole-document bootstrap call plus 102 window
calls (78 extraction and 24 admission/repair calls), with zero new DI extraction.
The bootstrap and earlier experiments are separately retained, including failed
attempts. Initialization intentionally differs from the baseline: a reviewed
source-derived reference is present rather than an empty seed. Source chunks,
prepared/source-cache hashes, original intake, model hash and window limits match.
This is not a claim of a prompt-only controlled experiment.

The result is still working-only: 151 observations are pending and 230 quarantined;
only eight of 98 grounded property candidates mapped. Source quotation grounding
is not complete semantic verification, identity resolution or L3 fact approval.
For example, the Consumable labels still include a reusable bucket, and symptom
coverage and part-SKU applicability need semantic review. The quantity schema
permits Step owners while 88 grounded quantity observations use Part owners and
remain pending; do not broaden ownership merely to inflate mapping counts.
There are no mapped model-applicability or has-part-number relationships yet,
and 41 grounded acts-on-part observations are pending. Fewer classes do not
establish complete business-question coverage. No ontology migration, new Fabric
deployment, deletion, full-corpus inference or Data Agent publication occurred.

Evidence:

- `concept-guided-live.log`, `concept-guided-status.json`, `concept-guided-schema.json`
- `concept-window-comparison.json` and `concept-reference-review.json`
- Run hash: `493e3b701d265b8cb9707e313d08fb0e0b7ad52d564e867ce019032800d75ff3`
- Schema hash: `4f1c21a4d32ac738631d559e18d229a827e84306264752b32d416d88b3f700fb`
- Seed hash: `1841fa214bf888e9cd7208588d9732e8ae6dc5e21f581447422a15ed2a79034b`

`concept-guided-resume.json` records a zero-call resume with the same run and
schema hashes, omitting `--seed-reference` to exercise inherited frozen content.
`concept-reference-v4-resume.json` records zero-call, zero-write bootstrap replay.
The original 302-chunk S38 run and its deployed 270-type ontology remain unchanged.

Acceptance requires:

1. Identical 78 chunk identities, source/text hashes and intake, with model and
   configuration differences recorded. Budget exhaustion or fewer chunks is a
   partial experiment, not a whole-document comparison.
2. Zero class/instance leakage and zero definition-incompatible mappings in the
   explicitly reviewed fixture; preserve item/model/procedure names and evidence
   as observations rather than class names or instance aliases.
3. Reusable definitions and justified identity treatment for retained concepts;
   audit proposed IS-A links separately from containment and association.
4. Account for every baseline verified observation as a supported mapping,
   retained unresolved observation or justified exclusion. Report changed
   extraction output and mapped/pending/quarantined counts without dropping
   evidence to improve scores.
5. Review relevant part, tool, procedure/step and symptom examples for precision
   and missed evidence. Preserve quantities, order, conditions and source
   attribution. Neither fewer classes nor more mapped records proves success.
6. Ground relation endpoints and direction; do not impose a positive relation
   quota or fabricate missing Model/SKU/Part or geographic relationships.

This assessment and local comparison authorize no migration, domain approval,
mapping approval, replay into approved outputs or publication. Existing runs and
the approved paused-prefix artifacts remain unchanged.

## Ordered operation

### Current standalone first-document bootstrap

The initial document window starts with an empty working schema, not an approved
domain used as a vocabulary constraint. Infer a working reference from all
cached source-text chunks of that document. The saved discovery artifact is a
source-cache container here: prior model candidates, summaries, other documents,
and the previously approved ontology are not inference input.

Choose the first eligible document in the recorded corpus inventory unless an
exact source-file ID is supplied. Keep its complete chunk inventory, offsets,
page locators and text hashes. Every chunk uses input version 0; commit one
version-1 snapshot only after the response passes structural and source-quote
checks. An oversized document must be deferred explicitly, never silently
sampled to fit the request.

The result describes concepts and possible typed relationships, not asserted
instances. Every inferred concept retains a source quotation. Identity policies
remain unresolved, and all newly inferred concepts are provisional. Preserve
the exact model request and original response before interpreting the result.
This bootstrap does not approve a domain, assert graph edges or deploy anything.

### Current cached-candidate alignment

1. Load the exact discovery artifact and an explicit seed schema/reference.
2. Create a stable working schema with canonical concept IDs, definitions,
   aliases, owner/endpoint constraints and unresolved choices.
3. Plan deterministic windows over the declared chunk inventory.
4. Freeze schema version N for every worker in the current window.
5. Map existing observations and collect unknown expressions, conflicts and
   proposed additions. Preserve raw source and grounding context.
6. Evaluate combined proposals at a single window boundary; reject structurally
   invalid changes and retain ambiguous/breaking changes as pending.
7. Atomically record the decision, version N+1 and the completed window.
8. Apply N+1 to the next window. Reconcile affected earlier observations from
   cached data rather than repeating raw extraction.
9. Review the final mapping against its target domain before approved replay.

No worker updates the shared schema independently. Parallelism operates within
a frozen version; the version transition occurs only after batch collection.

## Persisted state

The exact public schemas are exported by the CLI. Their responsibilities are:

| Record | Required meaning |
|---|---|
| Run identity | Discovery/source/seed hashes, model and prompt versions, window policy and budgets |
| Schema snapshot | Version, parent snapshot hash, stable definitions/aliases/mappings, pending choices |
| Window input | Every chunk ID, input schema version/hash, bounded actual model context |
| Model proposal | Original request/response identity and candidate changes; no authority promotion |
| Evaluation | Accepted working changes, rejected/ambiguous choices and reasons |
| Window commit | Before/after schema hashes, completed chunk IDs and result/accounting hashes |
| Final mapping review | Exact final snapshot, target domain and discovery hashes, reviewer and rationale |

Keep all raw discovery candidates, original labels and evidence anchors immutable.
Schema IDs must not change merely because an alias or display name changes.
Do not automatically merge entities or silently redefine identity, direction,
property type or applicability.

Each stored transition must be sufficient to explain which source observations
caused a change. A narrative summary alone is not the authoritative state.
Final replay must use the final reviewed mappings, not stale per-window aliases.

## Bounded model context

The complete working schema remains local. Each request receives the relevant
definitions/mappings plus a consistent catalog and necessary pending context.
Bind the actual context to the request hash.

Do not silently truncate definitions or claim a whole window was handled if only
some observations were presented. Oversized requests require a visible bounded
plan/defer state or further sub-batching that preserves the same frozen version.
Completion counters must distinguish processing coverage, mapping coverage and
grounding quality.

Existing Foundry configuration supplies inference. Copilot can coordinate and
review through the public CLI; do not assume an undocumented Copilot inference
API or require two model calls for every observation. Additional review should
focus on uncertain/high-impact changes.

## Mapping and evidence boundaries

Formatting-equivalent names may be proposed under an explicit unique-match
policy. Synonyms need a recorded mapping decision; relationship mappings must
consider direction and source/target types. Property mappings are owner-specific.
Do not map a logical component to an orderable SKU solely because names resemble
one another.

Unknown source concepts may extend the working schema; they must not be forced
into the approved target domain. Until a new domain is reviewed and approved,
new concepts remain pending for approved extraction.

Final reviewed mappings can change classification/reference vocabulary but not
invent property values, widen source quotes, repair identities from labels,
promote quarantined observations, or assert an unsupported relation. L3 evidence
and identity verification still applies.

The window receives document/page/section and original owner/value/row context
where already available. It must not attribute neighboring source text to a
different primary chunk. Failure to structure an optional detail does not by
itself invalidate the conceptual ontology: preserve the detail for retrieval
and retain its unresolved status rather than fabricate a structured fact.

## Resume and inspection

Plan mode makes no model calls or state writes. Live runs use bounded calls and
concurrency; resource exhaustion leaves a partial run with an exact cursor.
Completed windows replay without model calls. Interrupted work does not advance
the committed schema/cursor, and received responses remain available for matching
resume without unsafe duplicate calls.

The completed run's authoritative bytes/hash do not change on resume. Per-call
model/reuse counters are separate invocation diagnostics, so inspecting or
resuming completed work cannot invalidate an accepted mapping review.

Validate source, seed, model, prompt and window-policy bindings before any new
dispatch. Reject tampered snapshots, reordered/missing log links, stale mapping
reviews and mismatched target domains. Create-only atomic files prevent partial
cache entries from becoming authority.

Provide read-only status/history inspection: current schema, completed/planned
windows and chunks, last committed cursor, pending changes and schema diffs.
Do not infer active execution from a task timer or a stale `in_progress` label.

## Ontology, Search and SQL responsibility split

- Ontology/Graph holds core concepts, meaningful relations, procedure structure,
  applicability and retrieval keys. Not every paragraph becomes a graph property.
- Search provides detailed original text and context. Document/version/section
  references and canonical or governed lookup keys guide retrieval.
- Foundry Agent orchestrates scoped Graph retrieval, Search expansion and
  evidence-aware answers. A retrieved hit is not automatically a verified fact.
- Lakehouse SQL handles routed numerical/count/trend questions using their
  preserved population/grain/filter/time/source requirements.
- Source-index coverage, parent/row retrieval, permissions and revision filters
  are prerequisites documented in the helper. This CLI change does not build
  a new index or guarantee those runtime capabilities.
- No Search answer is silently written back into a published ontology.

## Integration plan: close the raw-text window loop

Planning and implementation were authorized 2026-09-09. This section supersedes any implication that
`window-align` already performs progressive extraction from raw document chunks.
Implementation and bounded inference do not authorize cloud publication.

### Gap assessment recorded before integration

| Surface | Available now | Missing for the integrated loop |
|---|---|---|
| `domain discover` | Immutable prepared sources, raw observations, grounding and accounting | A changing working schema shared with extraction requests |
| `domain window-bootstrap` | Empty-schema inference from one complete cached document, raw response retention | Domain/questions input, smaller chunk windows, candidate output and continuation |
| `domain window-align` | Frozen batches, working-schema evaluation, history, final remapping | Raw-text extraction and bootstrap integration; CLI currently requires an approved seed domain |
| `domain design --window-state` | Final alignment schema as a reference | Authority-preserving input from the new integrated run |
| `enrich --mapping-review` | Approved-domain replay without another model pass | Consumption of the integrated run's candidate ledger and exact final review |

The first live bootstrap consumed 78 cached DI chunks from the first recorded
document, without domain/questions or an approved seed. It proposed seven entity
types, five relationship types and one property. A relationship with an empty
target list prevented acceptance. That raw draft is not a committed schema.

### 1. One coordinator, two distinct authority levels

Add a raw-window coordinator, rather than a shell script chaining commands that
have incompatible input contracts:

```text
cached OCR / prepared sources + intake
    -> persist context C and complete chunk plan
    -> freeze working schema S0 (empty unless an explicit reference is supplied)
    -> read current window with C + Sn
    -> collect raw candidates + proposed schema changes
    -> preserve responses; ground candidates; evaluate schema changes
    -> commit window ledger + Sn+1 + exact cursor
    -> locally reconcile affected cached candidates
    -> next window, retaining C and Sn+1 across document boundaries
    -> final working schema + complete observation ledger
    -> design/evaluate/compile + explicit domain/mapping approval
    -> approved L2 replay -> L3 evidence validation -> L4
```

Before approval, extraction produces working observations with local references,
not approved C0/L2 identities or asserted graph instances. Do not weaken
`ClosedVocabulary`, mint fake approval receipts or mutate a sealed L1 contract
to accommodate an evolving working schema.

The first window is not required to encompass the entire first document. Use
bounded contiguous chunk windows, with an explicit option to stop after one
document for inspection. Completing a document does not reset the schema.

### 2. Persist and resend the right context

Introduce a hashed `RunContext` holding the original domain/business brief,
every example question and its ID, routing/criticality, pending requirements,
background and source constraints. Reuse existing intake types and
`question_routing_context`; the routing helper alone is insufficient because it
intentionally omits questions without explicit routing.

Every extraction, schema-review and repair request carries the same context
version/hash and the actual context contents. Carry inferred domain hypotheses
as working state separately from the user's immutable brief. Common concepts
need not be forced to answer an example question. Preserve the common/domain
layer distinction through design handoff.

Each request includes the full current working schema and pending vocabulary
decisions, its current primary chunks, and explicitly identified relevant
paragraph/table-row/section context. Do not repeatedly send the entire history
of raw documents. Neighboring context retains its own source identity and cannot
silently become evidence for a primary chunk.

Pending vocabulary context must be a versioned, deduplicated conflict catalog,
not concatenated historical raw proposals and diagnostics. Keep the complete
ledger locally and bind the catalog to its hash and counts; preserve distinct
owner/endpoint scopes and make any representative examples explicit. Reconstruct
the catalog from that ledger during validation. This is not permission to
truncate the working schema, current source text or original user context.

Bound both input and output size before dispatch. Prefer smaller planned windows
over truncating source text or the shared schema. If mandatory context/schema
alone exceeds the budget, stop with a visible context-limit state. A later
retrieval-assisted schema catalog is outside this first integration.

### 3. Extract observations and propose changes in one pass

Use a typed working response envelope with two independent channels:

| Channel | Meaning |
|---|---|
| Candidates | Raw entity, relationship and property observations, local references, exact source anchors and available identity values |
| Schema proposals | New concepts, aliases and scoped relationships/properties, each with source support and a reason |

Reuse `RawCandidateResponse`, `WorkingConcept`, `DesignReference` and the existing
grounding/evaluation rules through versioned adapters. Request identity binds the
input schema and chunk inventory; do not rely on a model correctly echoing every
input ID to establish coverage.

At S0, allow open observations and initial concept proposals together. After
accepting S1, map those retained observations locally. Subsequent windows use Sn
for known concepts and explicitly propose unknown concepts rather than forcing
them into the nearest approved type. This avoids routinely reading every chunk
once for schema design and again for extraction.

One logical extraction request per bounded work unit is the default, not a
promise of one call per entire document. Additional semantic review or repair
calls are separately bounded and reported; do not mandate two calls for every
chunk. Budget selection must account for candidates as well as schema output.

### 4. Evaluate at a single batch barrier

All parallel workers read the same Sn. Only the coordinator can commit Sn+1.
Reuse entity-first dependency resolution, owner/endpoint scope checks and
final-window remapping already implemented in `window_schema.py`.

Deterministic checks cover endpoint existence/type/direction, property owners,
identifier stability, alias ambiguity, cycles, duplicate definitions and source
quotation validity. Changes to identity rules, direction, existing definitions
or overlapping scopes require explicit review rather than additive acceptance.
A bounded LLM reviewer can propose semantic corrections; its answer is still
unapproved input to those checks.

The live failure becomes a regression: a relationship cannot have an empty
target. A literal identifier can be proposed as an owned property, or as a
relationship to a justified entity type; the coordinator must not invent that
choice merely to make validation pass. Distinguish a subprocedure relation from
an atomic step relation instead of silently treating their names as equivalent.

Persist the raw response before validation. Quarantine an invalid change and
its dependency closure while retaining independent valid candidates/proposals.
Return structured diagnostics and, when the run's repair budget permits, send
only the failed proposal, required source context and errors for correction.
Retain both attempts and their linkage; never overwrite the original response.

An unparseable whole response or unresolved transport failure blocks that work
unit. A parsed response with rejected proposals may commit a fully accounted
window with explicit pending items. Such a commit is processing progress, not a
claim that all observations were semantically mapped. If no initial schema can
be accepted, report bootstrap-blocked instead of claiming successful inference.

### 5. Durable progress, reconciliation and resume

Reuse atomic cache writes and hash-linked window commits. Persist context,
prepared-source identity, input/output schemas, requests, raw responses,
candidate ledgers, evaluation decisions, repair attempts and cursors. Store
extraction-time and final mapping versions separately.

Every committed window has a transition record, even when no vocabulary change
was accepted. Label no-op changes explicitly. New aliases can remap earlier
cached observations without re-extraction; a genuinely missing fact remains
missing unless a separately authorized source request retrieves it.

Request cache keys include source slices, context/schema hashes, model,
parameters and prompt version. Resume rejects drift, reuses received responses,
and preserves stable completed-run/review hashes. Dispatch interrupted without
a durable response is an uncertain request, not permission to claim exactly-once
remote execution; require explicit retry authorization for that case.

Track document/chunk processing coverage, mapped/pending/quarantined candidates,
accepted/rejected schema changes, model/repair calls and token usage where
available. Keep the existing explicit partial-coverage acceptance policy:
accepting at least 99% processing never clears semantic or evidence gaps.

### 6. Final design and approved extraction bridge

Seal a new integrated-run artifact containing the prepared-source binding,
original context, observation ledger, coverage, final working schema and history.
Do not fabricate a completed legacy discovery summary to satisfy existing gates.

Extend design/compile approval inputs and replay adapters to consume this run
explicitly. A provisional concept is not eligible for asserted output merely
because it exists in Sn. Final domain and mapping reviews bind the actual
integrated run, target domain and final schema hashes. Reuse candidates into a
fresh approved L2 state, followed by unchanged L3 and L4.

Keep ontology focused on core structure, relationships and retrieval keys.
Search detail expansion and SQL execution remain separate responsibilities;
neither is implemented or claimed by this coordinator.

### CLI contract

```bash
# Read-only plan over an existing prepared source artifact.
fabric-kg domain window-run --prepared PREPARED.json --intake intake.json \
  --out-state .fkg/window-run --window-size 8 --concurrency 4 \
  --max-calls 32 --max-repair-calls 2 --stop-after-document 1

# Same command with --live executes; --resume continues the exact recorded run.
# An existing --discovery FILE may alternatively supply its verified prepared
# sources, without treating its old candidates or summaries as fresh extraction.
```

Expose compatible read-only status/history/schema inspection for both run kinds.
The one-document stop is an invocation budget, not a new schema reset or changed
corpus scope; resume can proceed to document two. Changing intake or model/prompt
configuration requires a new explicitly identified run, not silent resume drift.
The implemented flags and approval commands are documented in
[the prototype guide](../SCHEMA2-PROTOTYPE.md#integrated-raw-text-windows).

### Implementation sequence and ownership

| Phase | Deliverable and likely code surface | Exit criterion |
|---|---|---|
| 1. Contracts and context | Versioned run/context/response types near `domain/window_schema.py`; intake/routing reuse | Empty-seed and later-window requests carry all domain/questions and exact source/schema bindings |
| 2. Source-window extraction | New coordinator near `domain/discovery.py`; reuse `prepare_discovery_corpus`, chunking and raw candidate grounding | Real cached text produces both candidate and schema channels without an approved domain or prior extraction dependency |
| 3. Evaluation and repairs | Reuse `window_schema.py` barrier logic; refactor experimental `schema_bootstrap.py` onto it | Invalid SKU edge is diagnosed; valid independent work survives; repairs are bounded and archived |
| 4. CLI and durability | `cli/domain_window_cmd.py`, command registration and coordinator caches | Plan/live/resume/one-document stop, atomic commit and last-chunk inspection work through public CLI |
| 5. Approval/replay bridge | `domain_design_cmd.py`, design/compile bindings, `discovery_reuse.py`, `window_mapping.py` | Final reviewed schema and cached observations reach genuine approved L2/L3/L4 without a second full model pass |
| 6. Demonstration and guidance | Existing test runner, public CLI artifacts, prototype/helper documentation | First document, second-document continuity, then full-corpus run show actual schema and relationship outcomes with limitations |

Phases 1-4 establish the working loop; phase 5 is necessary before calling the
pipeline integrated. Do not report the whole feature complete after only adding
another bootstrap or alignment command. Stay in the current worktree; no new
branches, project sessions, pushes, PR changes or cloud publication are part of
this plan.

### Acceptance for the missing loop

1. Start without a preapproved schema. Show first-window schema proposals and
   candidate observations with exact source provenance.
2. Every model request, including repairs and the last chunk, contains the same
   domain/questions context and its bound input schema. No question is silently
   omitted because it lacks a route or current graph path.
3. All workers in a window use Sn; later windows and the next document use its
   committed successor. No hidden reset or stale-schema extraction.
4. Exercise the actual empty-target failure, inverse predicates, ambiguous
   aliases, property/relationship distinctions and independent valid siblings.
5. Reject fabricated quotations, identity values, targets and source attribution;
   keep optional retrieval details pending without inventing graph facts.
6. Interrupt before dispatch, after response persistence and before/after commit.
   Resume preserves accepted authority and does not repeat durable model work.
7. Show all 78 chunks of the first document accounted for, its schema history,
   extracted candidates and unresolved issues; do not reuse the failed draft as
   an accepted seed.
8. Demonstrate at least two sequential windows and then a second document, with
   a new concept/alias carried forward and earlier candidates remapped.
9. After explicit approval, produce source-supported relationships in real L4
   output, not just nonempty schema edges or injected offline fixtures. Report
   mapped, evidence-rejected and pending counts separately; a positive count
   alone does not establish complete business-question coverage.
10. Run the full declared corpus only after the smaller handoffs work. Report
    processing, semantic quality and publication as separate milestones.

## Existing alignment acceptance

1. All workers in one window use one frozen snapshot; the next window uses the
   committed successor, including at the last declared chunk.
2. A newly accepted alias is available to later windows and final remapping of
   earlier cached observations, without overwriting their originals.
3. Unknown/ambiguous/new concepts remain explicit; no forced synonym or identity
   merge and no fabricated facts.
4. Local snapshots and before/after logs reproduce each version transition.
5. Interrupted/budget-limited runs resume without repeating committed work.
6. Final mappings require explicit review and exact discovery/target bindings
   before approved candidate replay; absent mappings preserve legacy behavior.
7. Real L2/L3/L4 tests demonstrate valid mapped relationships/properties while
   invalid evidence remains rejected.
8. Public CLI inspection demonstrates actual coverage and change history.
9. Existing SQL routing, pending requirements and accepted coverage limitations
   survive unchanged.

Use the existing test runner; live acceptance invokes public CLI commands over
saved discovery artifacts, not project functions in temporary scripts or
hand-edited replacement candidates.
