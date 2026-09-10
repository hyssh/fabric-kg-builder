# Windowed working-schema operations

Status, 2026-09-09: cached-candidate alignment/replay is implemented. The
standalone first-document bootstrap is experimental. The integrated raw-text
window loop below is PLANNED, not implemented.

## Goal and scope

Replace independent naming across discovery chunks with a common, evolving
working vocabulary and mapping registry. Carry its identity through the last
chunk, persist each batch transition locally, and reuse existing observations.

The existing change implements CLI state, alignment and handoff. Detailed source
retrieval, multi-span answer adjudication and Search index construction remain
Foundry Agent orchestration guidance, not new engines in this release.

An evolving working schema is not a published ontology. An accepted working
change is not an asserted source fact or final mapping/domain approval.

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

Planning requested 2026-09-09. This section supersedes any implication that
`window-align` already performs progressive extraction from raw document chunks.
No implementation or model execution is authorized by this planning document.

### Current capability and missing connections

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

### Proposed CLI contract (not available yet)

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
Finalize flag names with CLI contract tests before documenting them as supported.

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
