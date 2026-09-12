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
