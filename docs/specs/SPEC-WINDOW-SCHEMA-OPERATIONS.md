# Windowed working-schema operations

Update, 2026-09-13 (normalization): fresh runs select version 1.3.0, adding
the explicit "BUSINESS CONCEPT MODELING AND SCHEMA NORMALIZATION" instruction.
Each complete document must compare meanings, consolidate equivalent entity types
and predicates, and introduce meaningful higher abstractions where justified.
Retain business-relevant subtypes, scopes and roles; retarget dependent endpoints,
owners and parents explicitly and explain prior-to-result mappings. This uses
the existing atomic add/update/delete/alias contract, not automatic merging.
The full block is `NORMALIZATION_POLICY` in `document_schema_evolution.py`.
Version 1.2.0 prompts, response contracts and retained runs remain unchanged;
evaluating the new prompt requires a fresh run from document one.

Update, 2026-09-13: version 1.2.0 enables audited additions, corrections and
deletions in provisional whole-document discovery. Local mutation and downstream
regressions exercise the new contract and preserve historical replay. Provider
throttling can interrupt a run; a processed prefix does not establish full-corpus
acceptance, final schema approval or deployment readiness.

Status, 2026-09-12: cached-candidate alignment/replay is implemented. The
standalone first-document bootstrap is experimental. The integrated raw-text
coordinator and approval/replay bridge are implemented locally. Fresh integrated
CLI runs default to complete-document, generalized schema discovery. Local
implementation does not establish full-corpus semantic acceptance.

## Goal and scope

Replace independent naming across documents with a common, evolving, reusable
type schema. Give each complete document the prior schema, persist its schema
transition, review/freeze the accumulated schema once, then perform bounded
instance extraction across all authorized documents and construct lineage.
Legacy chunked observation discovery and replay remain explicitly available.

Sequential GPT-5.4 generalization is the chosen mechanism to reduce
document-specific bias across an authorized document collection. Lack of a guarantee
of perfect recall is not a reason to prohibit corrections. Prefer the smallest
sufficient reusable vocabulary, retaining useful domain distinctions rather than
forcing all types into a generic Thing or all predicates into relates_to.

The existing change implements CLI state, alignment and handoff. Detailed source
retrieval, multi-span answer adjudication and Search index construction remain
Foundry Agent orchestration guidance, not new engines in this release.

An evolving working schema is not a published ontology. An accepted working
change is not an asserted source fact or final mapping/domain approval.

## Default whole-document schema discovery

Fresh `domain window-run` invocations default to `--discovery-mode whole-document`
and select `whole-document-schema/1.3.0`, not the legacy chunked observation prompt. It uses
the **same** immutable manifest, request/response ledger, document barriers,
working-schema history, design/projection review, and L1 approval bridge.
Select `--discovery-mode chunked` explicitly for legacy observation discovery.
Old manifests and serialized config hashes are unchanged: `RunConfig` retains
its legacy defaults, and resume inherits the recorded mode and prompt, including
`whole-document-schema/1.0.0`, `1.1.0` and `1.2.0`. A chunked run cannot be resumed as a whole-document
run, nor can an old prompt be silently upgraded in place.

1. Supply prepared sources (or a discovery cache, whose old candidates are
   ignored), complete intake/domain/questions, and optional `--seed-reference`.
   Every request contains all intake questions, the entire current schema, all
   cached text sections of exactly one document, and unresolved earlier decisions.
2. Propose reusable entity, directional relationship and owner-scoped scalar
   property **definitions**, not exhaustive instances. `add_concept`,
   `add_alias`, `update_concept`, and `delete_concept` reconcile the same working schema. Each
   definition has one or two exact, short source witnesses. Generalize from the
   document's evidence into types reusable across documents: model/SKU/part names,
   serial numbers and document titles are instance values, not new type names or
   type aliases. Reuse/update existing concepts before adding genuinely distinct
   types; preserve justified distinctions and leave unsupported merges/splits
   pending rather than collapsing everything into a generic type. Version 1.1.0
   proposals declare `layer` (`common`/`domain`), `generalization_reason`, and
   `scope_change` (`additive`/`broadening`/`narrowing`/`incompatible`); narrowing
   and incompatible changes require explicit correction rationale in 1.2.0
   (see below), otherwise they are rejected for review. A confirming document
   may propose no changes: schema growth is not required in every window. Unknown owners,
   endpoints, ambiguous updates and non-provisional identity policies are rejected;
   unresolved merge/split decisions remain pending for review.
3. Documents run serially, one logical schema request and one schema barrier per document.
   Transient transport failures may require separately budgeted physical attempts.
   `--window-size` and `--concurrency` do not split or batch documents in this mode.
   `--stop-after-document` and `--max-windows` retain their pause/resume controls.
   The schema evolves across documents without resetting after each one.
   Review/freeze the accumulated schema once through existing `domain design
   --window-run`, optional schema projection/corrections, `evaluate-design`,
   `compile-design`, and `approve`; discovery never grants approval.
4. After L1 approval, use `enrich --reextract-approved --window-run ...` to revisit
   **every authorized cached chunk, including earlier documents**, using the
   frozen schema. See the two-pass commands below. A deliberately accepted prefix
   remains limited to its exact committed chunks; it never authorizes later text.
   Build instance/evidence lineage only after this freeze and approved extraction,
   never from schema proposals treated as extracted facts. Do not alternate
   per-document schema discovery with authoritative instance extraction.

### Auditable additions, updates and deletions (1.2.0)

This implements the [current PRD amendment](../PRD-0.2.4-COPILOT-DOMAIN-DESIGN.md#current-workflow-amendment-auditable-whole-document-evolution-2026-09-13).
The first document evolves the provisional seed; the second compares with that
result; the third compares with the second result. Every request includes the
entire current schema, questions, prior transition history and unresolved context.
Prior history is not silently truncated; the existing complete-request token and
character admission gates still apply before every dispatch.

`delete_concept` specifies an existing `concept_id`, with no `concept` or `alias`.
`update_concept` supplies the complete replacement with the same ID and kind.
Both affect only working definitions. Every deletion requires `change_basis`
(`correction`, `duplicate`, `instance_as_type`, `superseded`) and nonempty
`prior_schema_impact`. Updates that remove/change earlier scope, aliases, parent,
layer, scalar type or identity context require these fields too. The prompt
requires reconciliation of earlier document evidence, dependent definitions and
questions. Mere absence from the latest document is not a valid reason.
Source-grounded rationale does not mechanically prove semantic safety; final
human review remains necessary.

All changes in a document are atomic: any invalid proposal or dangling
endpoint/owner/parent rejects the entire batch. Dependents must be explicitly
retargeted, corrected or deleted in that batch. There is no cascade deletion.
Repeated mutations of one ID and reuse of a retired ID are rejected. Existing
entity/relationship provisional identity and scalar-property shape rules remain.
Historical 1.0.0/1.1.0 requests, decisions, responses and replay remain unchanged.

Each committed `working_context.annotations.schema_transition` contains:

- Document reference and source-file ID, response hash and checkpoint/hash chain.
- `schema_revision_before` / `schema_revision_after`, status and `changed`.
- Each attempted action, target ID/kind, reason, generalization reason, change
  basis, prior-schema impact and full proposed mutation.
- Actual definition and layer before/after; addition starts at null, deletion
  ends at null. Rejected attempts retain the prior actual definition and record
  the proposed change separately.
- Exact source quotation, section/page locator, source-unit/text hash and offsets.
  Invalid witness attempts are retained as proposals, not verified evidence.

The existing snapshot `version` is still the immutable document-checkpoint
counter. `schema_revision` advances only when definitions or accepted layers
change. A confirming or rejected document leaves that revision unchanged while
retaining its checkpoint. The optional seed is revision 0; the first actual
change produces revision 1. The final design context carries this history too.
Use the public read-only controls:

```bash
fabric-kg domain window-run-status --state build/windows
fabric-kg domain window-run-history --state build/windows --changes-only
```

The history option is for new 1.2.0 runs; old runs require the original full
history view and are never retroactively assigned new audit records.
For example, adding `Product Model`, retargeting an existing relationship and
deleting an accidental named-model class can yield revision 2 in one batch.
A third document confirming that schema retains revision 2. This reduces
independent naming but cannot prove full coverage or eliminate document-order
bias. Review all authorized documents and questions before L1 approval.
Approved re-extraction and instance lineage are unchanged; business-quality
publication policy remains optional, as described below.

### Historical 1.1.0 generalization safeguards

The 1.1.0 prompt includes formatted few-shot examples distinguishing a reusable
type from a named model instance, then showing a later document reusing that type
without expanding the schema. Accepted common/domain classifications accompany
the current schema as `schema_layers` in subsequent document requests.

In addition to exact-witness and schema validation, the 1.1.0 runtime blocks automatic:

- Declared `narrowing` or `incompatible` changes.
- Loss of existing owners, relationship endpoints, aliases, or subtype scope.
- A `common` to `domain` layer change.
- Incompatible scalar-type changes; `integer` to `number` is the permitted widening.
- Any parent change, including parent removal, or changes to relationship context.

These guards preserve specific structural invariants; they do **not** establish
semantic equivalence or prove that a model-proposed generalization is correct.
Reusable meaning remains model-proposed and human-reviewed before the accumulated
schema is frozen. A declared `broadening` is not a substitute for that review.
Recorded `whole-document-schema/1.0.0` runs retain their legacy prompt and response
contract on replay/resume; the new metadata and safeguards belong to version 1.1.0,
not a silent migration of historical runs.

Example operator-supplied `model-capability.json` (replace all deployment facts
and limits with reviewed values for the actual deployment):

```json
{
  "deployment": "your-gpt54-deployment",
  "model_name": "gpt-5.4",
  "model_version": "2026-03-05",
  "deployment_sku": "GlobalStandard",
  "endpoint": "https://your-resource.openai.azure.com",
  "context_tokens": 128000,
  "max_input_tokens": 128000,
  "max_output_tokens": 32768,
  "source": "operator-reviewed deployment capability evidence"
}
```

The example is **not** a live capability attestation or a universal model
capacity promise. Profiles fail closed for unknown model/version combinations
and inconsistent limits. The profile, transport and prompt are sealed in the
new mode's configuration. Live Foundry endpoint/deployment and transport must
match. TPM quota is not a context-window limit.

Supported profiles include GPT-4.1, GPT-4.1-mini and GPT-4.1-nano version
`2025-04-14`, and GPT-5.4 version `2026-03-05`, subject to the helper's supported
deployment SKU and limit checks. Default input/context limits remain 128,000;
higher declarations require reviewed deployment-specific evidence. For an alias,
set the Foundry configuration's `chat_model` to the profile's actual model name:
deployment aliases alone do not identify a model family.

### Generation token allowances

New whole-document CLI runs and `domain design` now default to **32768 output
tokens**, including reasoning. Fresh GPT-5.4 approved extraction also defaults
to 32768, including deployment aliases with `chat_model: gpt-5.4`. Explicit
generation ceilings can be raised to **128000**; a smaller reviewed capability
profile or total-context limit still wins. No explicit request is silently reduced.
Generic GPT-5.4 JSON calls use 32768 when no output allowance is supplied.

The Microsoft GPT-5.4 model/version table documents **1050000 total context,
922000 maximum input, and 128000 maximum output** for the supported model.
Use those values only with a reviewed compatible deployment/SKU profile; the
128000-context example above is conservative, not the model's universal ceiling.
Input, framing, safety headroom and reserved output must fit together. TPM/RPM
remain separate deployment-throughput limits: increasing an output reservation
can trigger more throttling, not eliminate it.

Historical core constructor defaults and old sealed requests are preserved.
Window resume inherits its recorded output budget; approved extraction inherits
omitted output/context budgets and enforces code/authority compatibility.
An incompatible or exhausted donor requires explicit fresh-state continuation,
not editing its ledger. Legacy chunked discovery keeps its 4096 default but
now accepts an explicit `--max-completion-tokens` up to 128000; missing-response
retry ceilings support the same maximum. Assessment CLI runs default to 32768
with checkpoint inheritance, while legacy core defaults remain unchanged.

Larger limits reduce truncation risk but do not guarantee completeness. Incomplete
JSON remains invalid; source text is never cut to force a successful-looking
response. Private response diagnostics retain the failed output and provider
finish reason rather than treating partial data as approved facts.

GPT-5.4 uses `reasoning_effort: medium` for chat completions, or
`reasoning: {"effort": "medium"}` for project Responses, and omits temperature
and seed. GPT-4.1 keeps its existing deterministic transport settings. The same
generation-parameter helper supplies actual dispatch and preflight accounting;
offline document preflight also binds `chat_model` to the profile's model name.

The Foundry configuration's `request_timeout_seconds` controls the per-request
timeout (default: 120 seconds; maximum: 1800 seconds). Long GPT-5.4 whole-document
requests may require an explicitly larger, bounded timeout; increasing it does
not guarantee completion. The timeout is part of the sealed execution identity:
changing it requires a new run state, not resuming or overwriting the old state.
Retain the failed run and its request ledger when retrying with a different timeout.

For GPT-5.4, response schemas containing open-ended object maps select JSON-object
mode before dispatch rather than sending an unsupported strict JSON schema.
The full response schema remains in the instructions and local response validation
is unchanged. Legacy model request construction is retained for immutable donor
reconstruction. Provider API errors are persisted alongside physical request
records before an attempted fallback is blocked; retries require explicit action.

New live configuration loaded through `load_config` and the recommended
`fabric-kg.yaml`/`.env.example` select GPT-5.4 unless an explicit deployment
setting wins. Existing GPT-4 settings and historical request/donor identities are
not retargeted. The low-level `FoundryConfig` constructor retains its legacy
default for compatibility; it is not the recommended live configuration entrypoint.
`chat_model` has no inferred default: for a custom deployment alias, explicitly
set `AZURE_AI_CHAT_MODEL` to the verified underlying model (and update or unset
it when changing deployments). No capability profile is generated from these defaults.

```bash
fabric-kg --config fabric-kg.yaml domain window-run \
  --prepared build/prepared.json --intake intake.json \
  --discovery-mode whole-document --model-capabilities model-capability.json \
  --model-transport chat_completions \
  --out-state build/document-schema \
  --max-request-chars 2000000 --max-completion-tokens 8192 \
  --max-calls 2 --dry-run
```

The call budget is illustrative for two documents. Replace `--dry-run` with
`--live` only when authorized. For project Responses deployments use
`--model-transport project_responses` and the matching project endpoint/profile.
On resume use the original immutable size/config options and `--resume`;
mode, prompt version, profile contents, transport and seed are inherited when omitted.
Changing the profile or transport requires a fresh state, not an edited manifest.

Dry-run counts each complete document with the initial schema. Before **each**
live dispatch, the complete accumulated-schema request is tokenized again,
including transport instructions, response-schema copies and output reservation.
The actual SDK keyword arguments are checked again before a physical attempt
is reserved or sent, including any wrapper-added instructions and schema copies.
Tokenizer accounting uses the complete serialized SDK envelope with explicit
framing/safety reserves; it is an estimate, not provider-authoritative usage.
The character ceiling is an additional admission guard, not a truncation rule.
Overflow fails closed: no truncation, summarization, cross-document batching, or
silent fallback to chunked mode. Use a reviewed adequate profile/deployment or
start an explicitly chunked run. A growing schema can make a later document exceed
the limit even when its initial-schema dry-run fitted.

Whole-document inference reuses the authenticated Foundry SDK with SDK retries
disabled and `max_attempts=1`. Each durable coordinator dispatch permits at most
one physical request. Wrapper-level transport retries and strict-format fallbacks
are disabled at this boundary; the coordinator owns any additional attempt.
Retrying does not silently downgrade the format.
Every authorized retry receives a new durable dispatch reservation and consumes
the invocation's call and token budgets; insufficient remaining budget sends
no request. Cumulative run accounting retains earlier failed attempts as well.
The actual request hash/accounting are recorded under `physical-requests/`.

The whole-document CLI defaults to `--max-transport-retries 3` (up to four attempts
per logical document request) and `--max-transport-retry-wait-seconds 900` (total
sleep allowance per document, excluding the separately bounded request timeouts).
Set retries to zero to disable automatic retries. These are invocation controls,
not sealed schema/model settings; changing them does not invalidate historical
requests. The Python `RunBudget` defaults to zero retries for legacy callers;
explicit chunked mode retains its existing behavior.

Only observed transient API failures (429, 408, 5xx, connection errors and timeouts)
are automatically retried within the active invocation. Honor `retry-after-ms`,
`x-ms-retry-after-ms`, and `Retry-After` seconds/HTTP dates, adding positive jitter.
Without a usable provider hint, throttling waits start at 60 seconds; other
transient failures start at one second. Exponential fallback caps at 300 seconds,
but provider-requested waits are never shortened to that cap. If the remaining
sleep allowance cannot honor a delay, stop with
`transport_retry_wait_budget_exhausted` instead of retrying early.

Every attempt, including a failed or throttled one, consumes `--max-calls` and
`--max-tokens`. For example, a two-document invocation with three retries per
document needs `--max-calls 8` to allow all eight attempts; a smaller call budget
still wins. `physical-errors/` preserves provider errors and cooldowns; `retries/`
records the delay and failed-dispatch linkage. Retries resend the same request,
not a shortened document or a modified schema.

Authentication/configuration errors and invalid JSON/model responses are not
transport-retried. A previously uncertain dispatch (including a process crash
during a reserved retry wait) still requires `--resume --retry-uncertain`; invalid
responses retain their separate `--retry-invalid-response` gate. Timeout retries
can repeat remotely completed work, so no attempt is refunded or treated as
exactly-once execution.

TPM is a possible cause of throttling, not a diagnosis for timeout/connection
errors. Azure admission estimates include prompt size and the output-token
reservation, not only billed tokens. Backoff cannot fix a single request that
exceeds deployment quota or persistent capacity problems. Inspect deployment
limits and response metadata rather than silently reducing the approved request.
See [Azure OpenAI rate-limit guidance](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits).

Before accepting any schema response, the provider's `response.model` must exactly
match the profile's model name and version. A missing or different model is a
received-invalid response, with diagnostics and no schema commit; after correcting
the deployment, resumption requires `--retry-invalid-response`. Successful response
model identity is persisted and rechecked on replay. This detects retargeted aliases;
it does not turn the operator-supplied context profile into a live capacity attestation.

Prompt references are compact `s1`, `s2`, etc., with page/section hints when
available. The request ledger's `source_references` maps them to full immutable
source-unit IDs, text hashes and locators; verbose source metadata is not copied
into the model prompt. Witnesses are stored as `schema-witness:` supports in
schema history, not instance evidence. Existing chunk envelopes contain empty
candidate arrays solely to retain the exact extraction scope: **they do not
claim that the source contains no instances**. Whole-document authority is explicit
in the config and design context. Zero-call candidate replay is rejected for
this mode; instance lifecycle records and lineage start only in approved fresh L2
extraction. No extra service, deployment or lineage process is introduced.

Scope: “complete document” means every text SourceUnit in the verified prepared
cache. It does not attest OCR completeness, hidden image content, semantic recall,
or approval of model-proposed definitions. Schema conflicts are reviewed through
the existing approval workflow; this mode does not run chunk-level candidate
repair/admission calls.

## Placement of instance-data quality

Window discovery answers **which business concepts and relationships are useful**.
It does not guarantee that every property required by a later design was extracted.
Keep the following boundaries explicit:

| Boundary | Responsibility |
|---|---|
| Window discovery -> L1 design/approval | Reconcile concept definitions, property owners, essential fields and contextual identity. Freeze one reviewed contract. Adding a property to the design does not populate it. |
| Approved L1 -> L2 extraction | An explicit bounded re-extraction uses the accepted cached source units and the final closed vocabulary. Extract names, full content, scalar values and relationships separately, each with source evidence. No new OCR or automatic expansion of an accepted prefix. |
| L2 -> L3 validation | Ground proposed names independently from their supporting quotations. Validate field ownership, values, identities and endpoints. Source presence alone is not semantic entailment. |
| L3 -> L4 serving | Preserve validated names and full evidence independently. Never replace a name with a supporting paragraph or use a quotation-length limit to decide whether a name exists. Retain unresolved observations outside the asserted serving data. |
| L4 -> L5/publication | Assess required-field population, labels, evidence and governed relationship coverage before remote writes. Bind strict publication policy and its report to the exact inputs. A structurally deployable graph is not necessarily useful or complete. |
| Evaluation | Compare immutable runs over the same cached scope and reviewed contract. Model selection follows measured field/identity/relationship quality, not schema validity or node count alone. |

The default discovery-replay path remains distinct from explicit re-extraction.
Replay can align existing observations but cannot create values for newly declared
fields. A new contract or extraction prompt requires new run authority; do not
rewrite the original discovery, approval, source spans or deployment evidence.

Approved window-bound designs may represent one polymorphic relationship using
several endpoint-specific types with the same display name. As with retained
schema projections, disjoint endpoint scopes require canonical relationship IDs
in extraction; ambiguous human labels are excluded and listed explicitly in the
prompt. Overlapping scopes, case-only collisions and labels colliding with
reserved IDs still fail closed. This does not merge relations or guess an alias.

### Data lineage starts after schema freeze

Schema discovery history and instance-data lineage are separate. Continue recording
window schema changes with `domain window-run-history` before approval. Instance
lineage starts automatically with L2 extraction **after** the exact L1 approval
chain has passed validation. There is no extra tracing daemon or opt-in flag:
candidate lifecycle records, source manifests and contract hashes are the trace.

Read those artifacts through the public command:

```bash
fabric-kg lineage trace CANDIDATE_ID \
  --l1-state build/l1 --domain approved-domain.yaml \
  --l2-state build/reextracted-l2 --format json
```

Draft, changed, missing or stale approval is rejected before reading data records.
To include validated lifecycle state, evidence IDs and serving values, add
`--l4-run build/serving-l4/runs/EXACT_RUN_HASH --l3-root build/validated-l3`.
The L4 and L3 inputs must belong to the exact L2 handoff, not merely a matching
schema. Canonical entity, relationship and property assertion IDs can be traced
as well as candidate IDs. Reads do not run validation, make model calls, write a
registry or mutate historical runs. L2 anchors remain explicitly unverified
proposals; L4 evidence links and mechanical acceptance do not attest semantic
accuracy. The older registry-backed trace remains a separate compatibility path.

The exact previous and current approved-extraction system prompts, their dynamic
context and schema layers, and prompt-version compatibility rules are documented
in [Approved extraction prompt](SPEC-APPROVED-EXTRACTION-PROMPT.md).

The separate `domain design` conversion accepts `--max-completion-tokens`
(new CLI default 32768, maximum 128000) for large retained schemas. Reasoning consumes part
of this provider output allowance. A nondefault value is sealed in the design
inputs/request and checked on reload; historical default requests are unchanged.
This does not bypass evaluation or automatically regenerate malformed output.
With opt-in `--proposal-trace-dir`, failed JSON responses now retain private raw
output, finish/incomplete reasons and usage diagnostics for review before a new
attempt. Do not treat a truncated draft as an approved contract or a replay cache.

Keep policy generic in the pipeline and scenario-specific in reviewed runtime
design input. Product names and industry catalogs are not repository allowlists.
For example, a count attached to a part-use occurrence must not be reassigned to
a step just because an old schema allowed only Step-owned `quantity`. Conditional
values must remain unresolved unless their scope is supported. Concise titles,
complete instructions/warnings and exact supporting quotes are different data.

Publication reports must distinguish mechanical checks from human-reviewed
semantic accuracy and answer completeness. Passing non-null and evidence-link
checks is necessary, not proof that a phrase is the right product or that all
steps were extracted. Model evaluation must include those semantic cases.

### Two-pass contract and public commands

Pass 1 carries the domain hypothesis, example questions, previous working schema
and unresolved decisions through each document. Its extracted observations are
provisional design evidence. Reconcile definitions, ownership, identity and
document-order effects before approving the final schema. The approved schema
already defines the conceptual ontology; Lakehouse, Fabric Ontology and Graph
must use that same contract rather than redesign it independently.

Pass 2 revisits **all cached source units in the approved scope** under the frozen
schema. Earlier documents also need this extraction: their provisional values
cannot supply fields introduced later. A genuine schema gap creates a new
reviewed version and an explicitly scoped rerun, never an in-place schema change.
An accepted one-document prefix is not acceptance of the whole corpus.

For an approved window prefix, use a fresh L2 state and inspect the plan first:

```bash
fabric-kg enrich --reextract-approved \
  --input source-assets --domain-file approved-domain.yaml --l1-state build/l1 \
  --window-run build/windows --l2-state build/reextracted-l2 \
  --max-calls 10 --max-physical-calls 20 --max-output-tokens 16384 --dry-run
```

The budgets above are a synthetic example for ten chunks, not a corpus-size default.
Remove `--dry-run` only for authorized model execution. No OCR or original PDF
read is required. Retries consume the physical budget and overflow splits consume
logical calls. `--resume` requires identical sealed inputs, configuration, code
and budgets; it does not reset allowances. Default replay remains available.

If a run exhausts its sealed allowance, explicit donor continuation can reuse
verified raw responses in a **fresh** child state without modifying the donor:

```bash
fabric-kg enrich --reextract-approved \
  --reuse-approved-run build/reextracted-l2 \
  --input source-assets --domain-file approved-domain.yaml --l1-state build/l1 \
  --window-run build/windows --l2-state build/reextracted-continuation-l2 \
  --max-calls 4 --max-physical-calls 8 --max-output-tokens 16384 --dry-run
```

This synthetic example assumes six reusable responses and four remaining units. Inspect the
actual plan instead of inferring those counts from file count alone. Child
budgets are **additional** allowances; the plan separately reports prior spending
and cumulative lineage ceilings, including failed attempts. Reused raw responses
are processed again, not imported as asserted facts. Donors must be inactive,
sealed and compatible with the exact approved source, contract, request semantics
and model configuration. Changed semantics require fresh extraction, not cache
reuse. Resume the child with identical options, including `--reuse-approved-run`,
plus `--resume`. Use the completed child's L2 path in all downstream commands.

```bash
fabric-kg validate-evidence --domain approved-domain.yaml --l1-state build/l1 \
  --l2-state build/reextracted-l2 --state build/validated-l3
fabric-kg project-serving --domain approved-domain.yaml --l1-state build/l1 \
  --l2-state build/reextracted-l2 --l3-state build/validated-l3 --state build/serving-l4
fabric-kg assess-business-quality --l4-run build/serving-l4/runs/EXACT_RUN_HASH \
  --l3-root build/validated-l3 --quality-policy reviewed-quality-policy.json \
  --output build/reports/business-quality.json
```

Assessment writes a new report; exit 5 means blockers, not a missing report.
Omitting `--quality-policy` permits historical diagnostics but does not enforce
a reviewed business-name rule. A strict policy binds `domain_contract_hash`,
per-type `label_rules` (`property_ids`, `allow_evidence_mention`, `display_templates`) and any approved
`endpoint_coverage` requirements. Name properties and labels must be independently
grounded on the same entity and agree after NFC/whitespace normalization; their
supporting quotation spans need not have identical IDs. Full instruction or rule
text remains separate from its short label.

#### Reviewed display templates and structural path readiness

Default display matching remains strict equality to a grounded name property
(NFC/whitespace normalized), or an explicitly approved evidence-mention rule.
An optional `display_templates` list adds reviewed alternatives without generating
or updating labels, source assertions, required facts, or entity identities.
For this synthetic device-maintenance contract, the effective property IDs are
`property:service_part.part_number` and `property:service_part.name`, not `domain.*`.
Add this rule separately for `semantic-type:service_part` and
`semantic-type:fastener` (the latter inherits those properties), retaining all
other reviewed type rules:

```json
{
  "property_ids": ["property:service_part.name", "property:service_part.part_number"],
  "allow_evidence_mention": false,
  "display_templates": [
    "{property:service_part.part_number} {property:service_part.name}"
  ]
}
```

Each placeholder must exactly identify a declared effective property for that
type. Unknown properties, empty/literal-only templates, malformed braces,
format specifiers, conversions, and attribute/index expressions fail closed.
Literal separators/text are part of the reviewed policy. There is no Python
format-string evaluation or missing-value substitution. Every referenced value
must be a populated, unambiguous, correctly typed, evidence-linked scalar asserted
on that same entity. Independent property evidence spans are allowed; the entity
label still needs its own linked evidence. Missing/non-scalar values never produce
a partial display string. Boolean/numeric scalars use JSON spelling (`false`, `0`);
dates/timestamps use ISO format (timestamps normalized to UTC). Templates are
alternatives, not required-property waivers.

`presentation_coverage.display_template_assessments` records each entity/template,
rendered candidate (or null), match status, unavailable property IDs, assertion IDs,
and evidence-span IDs. Nonempty feature configuration is included in the normalized
`policy_hash`, and its assessment is included in `report_hash`. Empty
`display_templates` and `requirement_paths` fields are omitted during serialization
to preserve historical version `1.0.0` policy hashes. Corresponding report sections
are absent unless that feature is configured, preserving historical report shape
and hashes when neither feature is enabled. After opting in, review and recompute
the changed policy hash and publication plan; never edit a frozen plan to bypass
recomputation.

**Derived instance displays (separate opt-in).** Add
`"derive_display_from_properties": true` to an explicitly reviewed `label_rules`
entry to publish a separate instance display from existing asserted naming
properties. For ServicePart/Fastener use the rule above with this flag; for Tool
use `property_ids: ["property:tool.name"]`. The flag defaults to false and is
omitted when false, preserving historical normalized policy/report hashes.

Derivation chooses the first fully grounded supported template, then the first
supported property in `property_ids` order. It never fills missing template
placeholders. Synthetic device-maintenance examples:

| Original L4 mention | Existing asserted values | Derived instance display |
|---|---|---|
| `DEMO-101 Screws` | SKU `DEMO-101`, no name | `DEMO-101` |
| `DEMO-202 Foam x 1 (Shield Foam #2)` | SKU `DEMO-202`, name `Foam` | `DEMO-202 Foam` |
| `Inspection strap` | name `Inspection strap (demo accessory)` | Full asserted name |

This is not a mismatch waiver: original-mention evidence and unambiguous,
same-owner, evidence-linked string properties are required. Accepted syntax is
equality to an asserted naming value, a reviewed display prefix followed only by
quantity/parenthetical qualifiers, a whole-token SKU with an agreeing asserted
name when present, or a full asserted name whose parenthetical-free base equals
the mention. Case changes, synonyms and inferred names are not supported.
`DEMO-303 SSD` versus SKU `DEMO-303`/name `RAM`, `1` versus name `Tool`, identifier prefixes
such as `DEMO-30` versus `DEMO-303`, ambiguous names, and missing evidence remain blocked.
An asserted Name contradiction cannot be hidden by a SKU-only rule.
Required properties and missing approved names still block publication, even when
`allow_evidence_mention` is true. SKU-only output does not create a Name assertion.

`presentation_coverage.derived_instance_displays` records original mention and
label evidence, derived display, selected template/properties, guard properties,
property assertion IDs and evidence-span IDs (or a blocked reason). This section
exists only when derivation is configured. The L4 label, semantic properties,
identities, provenance and carried `l4_*` tables remain unchanged.

The shared publication compiler applies the reviewed display to every typed
membership's `__label` **before** snapshots, governed-asset hashes, plans and
materialization. Native Ontology and independent Graph bind that same column.
For widened Ontology endpoints, a separately hashed
`presentation_semantic_entities` table supplies `entity_id`, derived `label` and
`original_mention`; the original asserted-entity table is never rewritten.
Native companion binding/ownership and scalar readback checks follow that
presentation table. Display-query citations identify the asserted property
lineage as `derived-instance-display`, not as a verbatim source mention.

Callers constructing governed assets directly must pass the same `quality_policy`
to `compile_governed_assets`/`build_l5a_governed_assets` and
`compile_l5a_publication`. Prototype compilation does this automatically.
The final quality report, physical values and bindings are bound into derived
asset authority; stale/raw-label asset hashes fail rather than being adopted.
Changing a derived display requires a new policy-bound plan and materialization,
not a type-label repair or an edit to old sealed artifacts. Data Agent business
accuracy and complete source scope remain unassessed.

To separately measure the observed
Procedure → RepairItemRequirement → **one distinct allowed Item** path, add this
top-level `requirement_paths` value to the same version `1.0.0` quality policy:

```json
[
  {
    "procedure_type_id": "semantic-type:procedure",
    "requirement_type_id": "semantic-type:repair_item_requirement",
    "procedure_relationship_type_id": "relationship-type:procedure_has_requirement__procedure__repair_item_requirement",
    "item_relationship_type_ids": [
      "relationship-type:requirement_specifies_item__repair_item_requirement__consumable",
      "relationship-type:requirement_specifies_item__repair_item_requirement__fastener",
      "relationship-type:requirement_specifies_item__repair_item_requirement__service_part",
      "relationship-type:requirement_specifies_item__repair_item_requirement__tool"
    ]
  }
]
```

These IDs are checked against the supplied L4 domain contract. Allowed item types
and predicate IDs come from its relationship definitions, including endpoint
subtype policy; they are not guessed from labels. The existing assessment CLI
includes `structural_requirement_path_readiness` when these rules are configured. It reports
per-requirement owning procedures, distinct item IDs, supporting relationship IDs,
missing halves, multiple-item ambiguity, invalid edges, and procedures without a
ready path. Duplicate assertions to the same item do not count as multiple items;
the four item relationships are a **union of alternatives**, not four mandatory
edges. Empty requirement populations are `unobserved`, not vacuous success.

Readiness is **informational-only**, separate from the business-quality blocker
count/exit code and from explicit `endpoint_coverage` enforcement. It concerns only
the supplied asserted L4 population. Neither a ready path nor a passing quality
gate establishes question-answer accuracy, quantity correctness, applicability,
business uniqueness, or complete source scope. Those remain separate acceptance
and source-scope checks; missing links are not proof that a source has no such fact.

For publication, pass the same `--quality-policy` to `fabric-kg app
publish-structured`. The dry-run binds the recomputed report, policy and sealed
inputs. APPLY recomputes the gate before writes; editing the report or dropping
the option cannot downgrade an already bound plan. This gate does not replace
Graph runtime readback or source-cited question acceptance.
Plan-based query, agent, reconciliation and graph-presentation operations also
recompile using the bound quality policy and require the same recomputed report;
they must not silently revert to a policy-free projection.

Detailed Search content should retain original quotations and link to the
same canonical entity/relationship identities. Search publication and the final
Ontology/Graph-to-Search Markdown explanation remain separate delivery steps;
these extraction and quality commands do not deploy either automatically.

## Concept-first assessment and bounded comparison

### Observed-terms baseline

Compare only identical authorized source scopes and committed checkpoints.
An exported snapshot may be stale relative to a paused run; verify its chunk
inventory before comparing counts. Keep actual run artifacts and their hashes
in access-controlled storage, not in repository examples.

In a synthetic device-maintenance fixture, `DEMO-101 Tape`, `Aster 7` and
`Enclosure Installation` are instance labels, not automatically reusable root
types. Promoting their names into types is a class/instance modeling decision,
not a display-name cleanup. Conversely, a generic `Component` label can still
map to an overly narrow definition; inspect meaning as well as name matching.

Report entity/property/relationship concept counts, parent-type links,
class/instance-name collisions, and mapped/pending/quarantined observations.
Approval, retention and lower type counts do not prove suitable abstraction.
Observation counts are not distinct real-world objects or semantic recall.

### Explicit chunked policy and its limits

Fresh `domain window-run --discovery-mode chunked` invocations default to `--schema-policy reviewed-concepts`
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

V1.4 adds enforced independent semantic admission: stronger instructions and
self-declared rationales alone do not reliably reject value classes.
Original extraction proposals cannot admit their own new
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

The target is a domain-general conceptual vocabulary, not a hard-coded product
allowlist or a target count of types. Model, SKU, Part, Procedure, Step, Symptom,
Department and geographic concepts illustrate reusable roles; only source-backed
roles and relations should appear in a particular run. Do not invent departments,
places, steps or edges just to complete those example patterns.

### First-document experiments

Use verified cached discovery and intake, an empty initial schema and a new
output state. Do not seed from an approved baseline contract or resume an old
observed-terms state for a fresh comparison. Use an explicitly synthetic input
such as `device-maintenance-guide.pdf`; keep private corpus names, IDs and
source hashes outside this guide. No new DI extraction is needed when the
authorized cache is complete.

From `/path/to/run`, plan the bounded run through the public CLI:

```bash
fabric-kg --config fabric-kg.yaml domain window-run \
  --discovery discovery.json \
  --discovery-mode chunked \
  --intake intake.json \
  --out-state build/concept-first-document \
  --schema-policy reviewed-concepts --window-size 8 --concurrency 8 \
  --max-request-chars 160000 --max-completion-tokens 32768 \
  --max-calls 20 --max-repair-calls 10 --stop-after-document 1
```

The budgets are synthetic illustrations, not an allocation for a real corpus.
Use an unused state path; add `--live` only for authorized bounded inference.
Compare the same document boundary, prepared/source-cache and intake identities,
recording model, prompt, seed and configuration differences separately.

Historical prompt/response contracts remain available for exact readback and
resume. New representation/identity-role checks prefer owned scalar values over
metadata nodes, distinguish instances from functional specializations, and
require source-stated typed relationships. Never concatenate table cells into
fabricated quotes.

An interrupted run can be frozen with `--resume --max-calls 0 --max-windows 0`;
unfinished dispatches are not silently retried. Budget exhaustion, invalid review
indexes or fail-closed protocol errors can reduce schema size without improving
modeling. Report these separately from semantic outcomes and compare completed
scopes only. Neither a compact critic nor increased mapping counts establishes
semantic acceptance.

### Source-derived reference before window zero

An empty seed introduces a cold-start design problem as well as a prompt problem:
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
initial vocabulary. Subsequent document inference requires explicit authorization.

The reference path is implemented as `window-bootstrap --intake ...` followed by
`window-run --seed-reference ...`. Intake bootstrap uses separate provider-strict
entity/relationship/property arrays and source paragraph selectors. Runtime code
sets unresolved identities and converts into the existing `DesignReference`;
the model cannot put relationship endpoints on a property or choose an unknown
scalar datatype. Lossless indexed spans preserve all original text and offsets.
Selected IDs resolve only to supplied source paragraphs, which are then checked
by the existing exact-source validator. They support proposed vocabulary, not
asserted relationships. Legacy no-intake bootstrap behavior is unchanged.

Retain rejected bootstrap responses and diagnostics without accepting invalid
quotations or property shapes. Review any broadened Part definition explicitly:
physical components are not necessarily independently orderable SKUs. A reviewed
reference copy must record its rationale and original hash; it is neither
unchanged model output nor final domain approval. Keep original evidence intact.

### Guided first-document acceptance

Use a synthetic device-maintenance fixture to compare an empty seed with a
reviewed shared vocabulary such as Model, Part, Procedure, Step and Tool.
Named fasteners remain instances, while Procedure → includes step → Step and
Step → requires tool → Tool remain schema definitions until grounded observations
support them. Labels are not resolved identities, and mentions inside quotations
do not establish separately extracted entities.

Record bootstrap, extraction, admission/repair and failed calls separately.
A seeded comparison is not a prompt-only controlled experiment. Preserve pending
and quarantined observations, including owner mismatches and unsupported
applicability links; do not broaden ownership solely to inflate mapping counts.
Test exact zero-call resume and bootstrap replay against retained private hashes.
Neither those checks nor fewer classes establishes semantic or Data Agent
acceptance. External source readiness can still block Data Agent deployment.

Acceptance requires:

1. Identical authorized chunk identities, source/text hashes and intake, with model and
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

## Consistency architecture and optional industry specifications

Recommendation recorded 2026-09-12: keep one governed conceptual contract across
stages. An industry specification can improve terminology and acceptance criteria,
but cannot replace source-grounded inference or make facts true. Packs should be
optional; a customer must still be able to work from documents and intake alone.

### Stage placement

| Stage | Responsibility | Consistency boundary |
|---|---|---|
| Intake, before OCR | Capture business questions, industry hints, jurisdiction and scenario constraints; optionally select a reviewed pack. | Preserve the original intent; inferred domain is a separate hypothesis, not permission to filter unexpected source content. |
| After OCR, before window zero | Infer reusable concepts and typed relationships from cached source text plus intake and optional references. Review class/instance boundaries and identity choices. | Freeze a provisional reference. For heterogeneous corpora, a reviewed representative bootstrap is preferable to assuming the first document covers every business role. |
| Each extraction window | Classify source mentions against that reference; preserve detailed labels, values, local endpoints and exact evidence. Propose new concepts separately. | Every parallel worker uses the same schema version; an unknown concept remains pending rather than being forced into a nearby class. |
| Window barrier | Review additions and conflicts, check owner/endpoint scopes, and commit one successor snapshot. | Only the coordinator changes the working schema; affected cached observations are reconciled under the recorded mapping decisions. |
| Final design and approval | Add question-answer requirements, review semantic gaps and explicitly approve the contract and mappings. | Do not independently regenerate concept meaning or identity. Any difference from the working contract needs an explicit, reproducible correction. |
| Enrichment, validation and publication | Replay retained observations, resolve identities, validate facts, project serving tables, and publish Ontology/Graph and Lakehouse artifacts. | These stages consume the approved contract; they cannot silently invent types, widen evidence, merge labels into identities or promote quarantined candidates. |
| Retrieval | Use Graph for governed structure, Search for quoted detail, and SQL for numerical questions. | Bind all sources to compatible contract/publication versions and scope; retrieved text does not automatically update ontology facts. |

Strong consistency here means reproducible contracts and explicit state transitions,
not a guarantee of semantic correctness or a distributed transaction across Fabric
items. Retain separate statuses for source grounding, classification review, identity
resolution, fact assertion, publication integrity and business-question acceptance.
The bucket-as-Consumable error demonstrates why exact quotation alone is insufficient.

The publication exercise also exposed a final-design consistency gap: an LLM
reintroduced optional `variant` fields as identities for Model and Procedure.
Those fields cannot identify a model or procedure on their own. Repeated generation
also produced malformed answer bindings and conflicting intake routing. Preserve
these rejected proposals; the solution is explicit reviewed correction and eventually
deterministic contract promotion, not relaxing validation or retrying until a draft
merely compiles. Optional answer fields describe requirements and may remain empty;
their existence does not prove that a question can be answered.

### Optional governed packs, not a hardcoded industry taxonomy

Use a separately versioned, organization-governed specification repository for
reusable packs once local composition is implemented. Keep synthetic, redistributable
fixtures in the repository; never upload customer documents or extracted evidence.
Do not embed a particular external ontology or its URL as the runtime default.

| Layer | Contents |
|---|---|
| Common | Reusable modeling conventions and cross-domain business roles. |
| Industry/domain | Definitions, typed properties and relationships, scoped terminology and exclusions. |
| Scenario/use case | Competency questions, answer shapes, applicability, routing and acceptance fixtures; not necessarily subclasses. |
| Project overlay | Selected concepts, local extensions, exclusions, approved mappings and source identity choices. |

Composition must reject conflicting definitions of the same canonical ID, incompatible
property owners, direction changes and identity collisions. Never use silent
last-file-wins merging. An alias or close terminology match is not instance identity.
Unsupported pack concepts remain reference vocabulary; documents still supply the
evidence for instances and relationships.

A pack manifest should record namespace, immutable version/digest, locked dependencies,
contract/compiler compatibility, publisher, provenance, license and attribution,
jurisdiction, definitions, identity guidance, and positive/negative semantic fixtures.
Treat licensing permission, publisher authenticity and semantic approval separately.
Unknown or incompatible licenses need review before incorporation or redistribution.

The future CLI should separate inspection/acquisition from activation/composition:
explicit trusted repositories or local imports, digest and publisher verification,
content-addressed cache, offline replay, and a project lockfile. Do not fetch `latest`
during extraction. Packs are untrusted declarative data: reject executable hooks,
unapproved remote imports and unsafe archive paths. A remote download is convenience,
not a runtime dependency or authority to change a running schema.

### Delivery order and acceptance

The shared reference, frozen windows, admission review, snapshots, approved mappings
and replay bindings are implemented. A governed pack downloader/composer and a complete
semantic acceptance suite are not implemented.

1. Strengthen contract promotion and semantic fixtures first: Tool versus Consumable,
   Procedure versus Step, quantity ownership, Part Number versus device SKU,
   applicability direction, and supported symptom coverage.
2. Add local pack manifests, deterministic composition, provenance and compatibility
   checks, adapting the resolved result into the existing reference/design interfaces.
3. Add optional repository distribution with pinned versions and offline operation.
4. Evaluate representative document families before claiming industry-wide coverage.

Lock the effective intake, references/packs, source/OCR/chunk hashes, model and prompt
versions, schema snapshots, mapping decisions and compiler/validator versions.
Schema additions require reviewed successors; merges, splits, identity changes,
property-owner changes and relationship reversals require migration impact review.
Preserve raw observations and reproject compatible cached evidence before considering
new inference. Never alter historical receipts to make a changed schema appear
compatible. For structural Fabric replacements, publish a separate version and retain
the prior items until an explicit migration/cutover is authorized and supported.

Standards informing the design (not a claim that Fabric natively executes these):
[OWL 2](https://www.w3.org/TR/owl2-overview/) distinguishes classes and individuals;
[SHACL](https://www.w3.org/TR/shacl/) separates constraints from data;
[SKOS mappings](https://www.w3.org/TR/skos-reference/#mapping) distinguish exact,
close and hierarchical matches; [PROV-O](https://www.w3.org/TR/prov-o/#description)
provides a model for derivation and attribution. The existing JSON contracts can
implement these principles without requiring RDF conversion.

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
# Read-only complete-document plan; profile values must be reviewed for this deployment.
fabric-kg domain window-run --prepared PREPARED.json --intake intake.json \
  --model-capabilities model-capability.json \
  --out-state .fkg/window-run --max-calls 32 --stop-after-document 1

# Same command with --live executes; --resume continues the exact recorded run.
# An existing --discovery FILE may alternatively supply its verified prepared
# sources, without treating its old candidates or summaries as fresh extraction.
# --discovery-mode chunked explicitly selects legacy chunked observation discovery.
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
