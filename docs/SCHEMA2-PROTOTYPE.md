# Testing the 0.2.6 design-first prototype

This local prototype keeps exploratory design separate from the existing strict
DomainContractV2 approval/extraction contract. It also retains the document
challenge and revision loop. It does not claim that the cloud release roadmap or
live technician-question acceptance is complete.

## Capability discovery

```bash
fabric-kg --version
fabric-kg domain design-schema
fabric-kg domain question-context --help
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

## Design first: seed YAML, intent, then question evaluation

The 0.2.6 sequence is `domain design` -> `domain evaluate-design` ->
`domain compile-design` -> existing `domain approve` -> `enrich`.
The [design-first specification](specs/SPEC-0.2.6-DESIGN-FIRST.md) defines the
authority boundaries and acceptance cases.

Plan first; this does not write output or call a model:

```bash
fabric-kg domain design --input ./documents --intake intake.json \
  --seed-domain reference.yaml --out .fkg/design/draft.json
```

The seed may be a reference sketch or a domain YAML. Its complete parsed content
and hash are preserved as design context, not source evidence. The optional
`--description` supplies additional business/domain intent. Existing seed
approval does not transfer to a generated design.

Explicitly allow bounded generation, then evaluate the saved design locally:

```bash
fabric-kg --config fabric-kg.yaml domain design \
  --input ./documents --intake intake.json --seed-domain reference.yaml \
  --out .fkg/design/draft.json --live --max-calls 2 \
  --proposal-trace-dir .fkg/design/private-traces
fabric-kg domain evaluate-design --file .fkg/design/draft.json \
  --out .fkg/design/evaluation.json
```

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

### Current design-first limits

The existing sampler can exhaust a sample-kind quota on a single file; a corpus
inventory is not evidence that every document influenced the proposal. Inspect
the draft's actual `samples.source_units`, not just `corpus_entries`. Design
generation currently uses the native bounded sampler, not the full OCR cache
used by later assessment/extraction commands.

The intake still requires five to ten questions. Existing strict compilation
limits remain visible, including relationship-usage tags and retained-type/path
constraints. Draft creation can succeed while compilation remains blocked.
Do not describe this milestone as a universally compilable design workflow.

Name matching ignores case and separators, but is only a review aid. It does not
establish that differently named concepts are equivalent, or that a matching
name has the same meaning. Review the actual endpoints and property ownership:
model-written rationales can contradict the generated graph.

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
