# Corpus-first discovery and candidate reuse

Status: implementation specification, 2026-09-09.

## Goal and order

Use the full declared corpus to inform ontology design, without automatically
paying for a second complete extraction pass after approval:

1. Collect business intent, seed references and question execution context.
2. Prepare all eligible source files once: verified parsing or exact cached OCR,
   normalized source units, bounded chunks and source/document context.
3. Discover open entity, relationship and property candidates from every chunk.
4. Consolidate chunk results per document, then across the corpus.
5. Design the ontology and Graph/SQL question routing from that complete
   discovery context; evaluate and explicitly approve a compatible design.
6. Reuse prepared sources and candidates in approved L2 mapping and L3 evidence
   validation. Reprocess only explicitly identified gaps when authorized.
7. Continue to serving/publication under existing approval and readiness rules.

These are ordered phases, not permission to skip review. Discovery candidates,
summaries and quote checks are not asserted ontology facts or live SQL bindings.
The multi-ontology roadmap remains deferred.

## Reuse existing primitives

Reuse `SourceCorpusManifest`, verified source snapshots, `SourceUnit`, exact DI
response caches, bounded work-unit/splitting infrastructure and
`RawCandidateResponse`. Its observed labels are suitable for open pre-approval
observations; mapping into an approved vocabulary happens later.

Source preparation must not forge an approved `L2Inputs`, receipt or domain.
Factor domain-neutral source work from the existing materializer/reader where
needed. Source-unit identity derives from source version, locator, text and
position; preserve those fields when binding prepared units to approved authority.
Never mutate the original discovery artifact to look approved.

## Preparation and coverage

Every corpus file and every planned chunk needs an explicit disposition.
At minimum distinguish processed candidates, processed-empty responses, failed,
unsupported and deferred work. A response containing no candidates is not proof
that the source has no relevant information.

Keep chunk-processing coverage separate from candidate grounding quality.
An invalid or ambiguous quote must not discard unrelated valid candidates from
the same model response. Retain original raw candidates, a verified subset and
per-candidate quarantine reasons/identities. A nonempty response whose candidates
are all quarantined is not an honestly empty response.

If a response contains a genuine `candidates` array plus unexpected root fields,
the array may be grounded without discarding the original envelope. Quarantine
the extra fields separately with field names, raw-response and extra-field
hashes; never merge them into an invented candidate. Array-candidate accounting
and envelope anomalies are distinct. An empty array with quarantined extras is
not a clean no-candidates outcome. Reuse must preserve these anomalies as pending
even when the grounded array is usable.

The normal workflow has no per-kind quota that a first file can consume.
Bounded text windows and request budgets control individual calls, not silent
exclusion of later files. Preserve document/page/section location, parent context
and neighboring/continuation references without turning every heading into an
executable step.

Record coverage scope. Processing every supported text chunk is not semantic
recall or proof of visual interpretation. Unsupported modalities and unread
content remain visible.

An exhausted call/token/time budget produces a persisted partial run, not a
complete discovery. The design command must not silently treat a partial run as
full-corpus understanding. Resume or explicitly use the labelled sample-only
compatibility workflow instead.

## Discovery persistence and resume

Persist raw responses and validated observation records per chunk using
create-only, atomic writes. Bind source/chunk content and coordinates, parser/OCR
identity, prompt version, model identity and every supplied context that affects
the model request. Domain approval is not needed to read sources or propose
observations, but no semantic approval is minted.

Resume must reuse exactly matching completed work without repeating its model
calls. Changed source, extractor, prompt, model or influential context must not
masquerade as a cache hit. Retain failures and malformed responses for diagnosis.
Never fill failed work with an empty success result.

Validate quotes/references against actual prepared source text before using them
as design support. Source offsets, labels, values and relationships remain
proposals until their appropriate evidence/semantic checks pass. Unknown
references and fabricated quotes must not become design authority.

Accept an exact in-range supplied span before considering unique-quote relocation.
Ambiguous or context-only quotes are not assigned an arbitrary primary-source
location. Secondary context is for interpretation, not evidence for another
page. Summaries use only grounded observations and explicit quality diagnostics.

When a verification fix can recover an existing exact-bound raw response,
resume may revalidate it without another model call. Preserve its original
request/prompt/model provenance, write new derived verification artifacts rather
than overwriting the old ones, and account for remaining quarantined observations.

## Document and corpus consolidation

Document synthesis must see the relevant chunk results and their order/context,
not only a sample selected from the first page. Corpus synthesis must account for
every included document.

Use bounded fan-in consolidation or another explicit bounded representation.
Do not place the entire corpus in one prompt or silently truncate the input.
Each summary references its children and their hashes/source observations, so
coverage can be audited and details retrieved from the retained candidates.

Persist malformed/over-budget summary responses with the actual request and
child identities before reporting failure. They do not contribute to completed
coverage. A bounded resume may retry only the missing summary; do not invent
child-ID coverage or truncate a failed summary to manufacture success.

Keep configuration/revision differences, conflicts, subprocedure references and
unresolved conditions visible. Do not merge cross-document entities merely
because their labels look similar. Summaries assist design; they do not replace
raw source evidence or prove all source facts were extracted.

## Discovery-bound design

The normal public design workflow consumes a complete discovery result and
binds it to the new draft alongside business intent, the full seed and question
routing context. Preserve model/source/summary provenance in the draft and
compiled handoff.

Retain the previous bounded-sample route only as an explicit `--sample-only`
compatibility mode, clearly labelled. Existing approved contracts remain usable;
new normal designs must not implicitly return to eight first-file excerpts.

SQL-directed requirements stay in execution context. Do not manufacture metric
ontology types or SQL bindings merely to make discovered numerical data fit.

## Approved mapping and targeted additional work

After approval, reuse raw observations through existing closed-vocabulary mapping
and candidate accounting. Map labels/identity/property references only when the
approved meaning is unambiguous. Preserve original local references and record
any mechanical chunk scoping needed to prevent local-ID collisions.

Unknown labels, missing identity witnesses, ambiguous mappings and unmapped
properties remain explicit review/re-extraction needs. Do not infer new facts
or silently create vocabulary to raise the reuse rate.

Prepared text may be reused after current source/hash validation. Rebind only
the authority envelope when appropriate; keep stable source identities and
locators. Discovery quote checks are not reused as approved extraction evidence:
the existing L3 verifier must still verify/mint evidence under approved authority.

The replay-only path makes no model calls for compatible observations. If new
model work is needed, scope it to missing/unmapped chunks and require explicit
authorization. Report reused, remapped, unresolved and newly processed counts.
No automatic full second pass and no guarantee that every future schema can
reuse every prior observation.

## Public workflow and acceptance

The CLI must expose planning, bounded live discovery, resume, discovery-aware
design and approved reuse. Read-only planning lists all files and exact cached
page coverage where available; chunk counts remain explicitly unknown until
preparation. Explicit zero-model-call preparation may persist the full chunk
inventory and a partial run before any model work is authorized.

Acceptance cases:

- Multiple documents contribute all eligible chunks; one file cannot consume a
  corpus-wide sample quota.
- Exact cached OCR is reused with complete page coverage and no hidden DI calls.
- Budget exhaustion/failure is partial and blocks a claim of full discovery.
- Resume makes zero calls for matching completed chunks.
- Document/corpus consolidation has auditable coverage and bounded inputs.
- Design binds the discovery result and no longer defaults to sample-only input.
- Approved reuse preserves source evidence and makes zero additional model calls
  for compatible observations.
- A single missing/unmapped chunk does not trigger a complete corpus re-run.
- Source/cache/response tampering, local-ID collisions and stale inputs are
  rejected or explicitly unresolved, not repaired with fabricated data.
- SQL routing, pending requirements and user background survive the new phases.
- Existing approved-domain, identity, evidence and publication guards remain.

Use existing test tools only. Real acceptance uses public CLI commands, not
scripts importing project functions or injecting replacement candidates.
