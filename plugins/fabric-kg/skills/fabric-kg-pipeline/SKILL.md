---
name: fabric-kg-pipeline
description: Use the installed fabric-kg CLI to design from business intent, seed YAML and documents, evaluate question gaps separately, and compile reviewed designs into evidence-governed extraction. Separately authorize model spending, approval and deployment.
---

## Purpose

Propose the ontology the business needs, then let documents challenge it.
Drive the installed CLI; do not implement another pipeline in chat or hand-edit
receipts, hashes, manifests or asserted data to make a command pass.

## Confirm capabilities and intent first

Run `fabric-kg --version`, then help for the exact commands below. Prototype
commands may differ between builds with the same version number. If a command
is unavailable, report that incompatibility; do not silently use legacy commands.

Classify intent as design-only, prepare, assess, review/revise, extract, deploy,
or update. A design-only conversation does not authorize creating project files,
model calls, loading data or deployment. Ask only for missing high-impact inputs.

Collect roles, decisions, domain, competency questions, expected answers, source
paths and access/retention requirements. Do not default to a sample taxonomy.

Classify question intent while collecting that context. Use ontology/graph for
entities, relationships, applicability and procedural retrieval; numerical,
count and trend analysis should be routed to Lakehouse SQL. A SKU, model number
or numeric safety threshold in a question does not by itself make it analytical.
Preserve both needs in mixed requests or require review instead of silently
discarding one part.

Keep the question ID, text, criticality, routing rationale, population/scope,
grain, filters, time context and required source information as execution context.
Keep unresolved decisions in per-question `pending_requirements`; do not drop
them after evaluation or mark them solved merely because compilation succeeds.
Do not add quantity/metric/trend ontology types or properties merely to satisfy
an SQL-directed question. Preserve ordinary source facts and quotations.
Distinct SKU counts and physical occurrences are not interchangeable, and
truncated chat results are not a valid counting population.

Inspect `domain question-context --help` and export the actual retained context
when handing work to another pipeline stage or agent. A route to Lakehouse SQL
is a plan, not verified table/column bindings, SQL execution or answer readiness.
If the configured consumer cannot execute that route, report the capability
gap rather than substitute a graph query or invent a count.

## Corpus-first workflow (0.2.6)

Collect intent first, then use `domain discover` to prepare the complete corpus
and collect open candidates before `domain design`. Consolidate results per
document and across the corpus, then evaluate and compile the discovery-bound
design before the existing approval/extraction workflow. Read each command's
help for its exact options. Live discovery/generation require explicit model
authorization; evaluation and compilation are not deployment.

Inspect actual file/chunk coverage, failed/unsupported/deferred work and resume
bindings. A partial discovery is not full-corpus understanding. Do not quietly
substitute a small sample, truncate the corpus or label an empty failed response
as successful discovery. The old bounded design route is explicit `--sample-only`
compatibility mode, not the normal full-corpus path.

Use prepared parsing/OCR and grounded candidate responses again after approval
through `enrich --discovery`. Do not automatically send every chunk to the model
a second time. Reuse only unambiguously compatible observations; expose unmapped
or missing work and authorize only the necessary targeted re-extraction.
Discovery is not approval or final semantic evidence: existing L3 verification
still applies. Never edit caches, IDs or receipts to raise the reuse rate.

Pass an existing YAML design through the dedicated seed input. Preserve the full
design intent, not only its description. A user sketch is reference material;
its example identifiers and safety statements are not source evidence. An
approved seed does not automatically approve the generated design.

Keep common concepts and useful independent types even when they do not support
a current example question. Question references may be empty or contain multiple
IDs. Do not invent tags, remove questions or downgrade criticality to pass a gate.
Assess ontology answer fields, applicability and ordering separately from SQL
source requirements, calculation grain and time scope, not just graph connectivity.

A design draft is not an approved Schema-2 domain. Gaps and compiler limitations
must remain visible in the separate evaluation. If compilation is blocked,
retain the draft and explain the specific unsupported capability; do not shorten
its meaning to fit a hop limit or add fake completeness evidence. Never pass a
design draft directly to `enrich` or describe structural support as live answer
correctness.

The older `init-domain` path is a strict compatibility workflow, not the preferred
way to explore an incomplete design. Do not assume its legacy seed/profile
options work in Schema-2; follow the installed CLI's explicit capability checks.

## Supported schema-2 local workflow

| Operation | Command | Boundary |
|---|---|---|
| Discover corpus | `fabric-kg domain discover --help` | Full source/chunk accounting and cached unapproved observations; explicit partial state |
| Explore design | `fabric-kg domain design --help` | Discovery-bound, seed-aware unapproved design; explicit sample-only compatibility |
| Evaluate design | `fabric-kg domain evaluate-design --help` | Separate structural question report; not factual answer acceptance |
| Inspect question context | `fabric-kg domain question-context --help` | Read-only routing and data-requirement handoff; not a query executor |
| Compile design | `fabric-kg domain compile-design --help` | Local strict Schema-2 handoff; does not approve or deploy |
| Strict proposal (compatibility) | `fabric-kg init-domain --input ... --intake ... --non-interactive` | Model calls; strict Schema-2 draft requiring approval. `--dry-run` inventories only |
| Plan document assessment | `fabric-kg domain assess --file ... --input ...` | Default mode: no model calls or output-file writes |
| Execute assessment | `fabric-kg domain assess --file ... --input ... --live --max-calls ... --out ...` | Explicit model authorization; inspect complete/partial coverage and findings |
| Offline assessment | Same command with `--responses ...` instead of `--live` | Fixture/replay mode is not live model validation |
| Record decisions | `fabric-kg domain review-assessment --file ... --assessment ... --decisions ... --actor ... --out ... --revision-out ...` | Every finding gets accepted/rejected/deferred plus rationale; does not approve an ontology |
| Regenerate draft | `fabric-kg domain revise --parent-state ... --input ... --assessment ... --review ... --request ... --out-state ... --live --max-calls ...` | New, separate unapproved L1 state; parent remains unchanged. No execution mode means dry-run |
| Approve exact draft | `fabric-kg domain approve --file ... --state-dir ... --approved-by ... --project-id ... --run-id ... --proposal-hash ...` | Use actual anchors from the draft; obtain explicit user approval |
| Reuse/extract | `fabric-kg enrich --input ... --domain-file ... --l1-state ... --l2-state ... --discovery ...` | Consumes exact approved L1; reuse candidates and scope additional work; inspect help and dry-run before calls |
| Verify evidence | `fabric-kg validate-evidence --l1-state ... --l2-state ... --state ... --domain ...` | Local L3; inspect unresolved/unsupported outcomes, not just exit status |
| Materialize serving | `fabric-kg project-serving --l1-state ... --l2-state ... --l3-state ... --state ... --domain ...` | Local L4 asserted/audit tables |

Read each command's help before constructing its arguments. Pass a revision's
separate `--l1-state` and choose a fresh `--l2-state`; `--domain-file` alone does
not select state. Keep L1/domain/source paths separate from L2 output.
Do not copy or rewrite sealed artifacts to hide a mismatch. Explicit state roots
cannot be deleted with `--force`; retain prior runs and use a new output root.

Use `domain assessment-schema` for machine-readable assessment/review contracts.
For OCR, `domain analyze-layout` plans by default; `--live` permits one capped
analysis POST using Azure CLI credentials. It preserves the complete raw response.
`domain assess --ocr-cache ... --ocr-identity ...` reads exact cached responses
without DI calls. A cached-layout read is not proof OCR interpreted every fact
correctly, and incomplete/unsupported OCR provenance must remain visible.

## Review, cost and replay

- `complete` assessment means window/file accounting, not perfect semantic recall.
- Keep deferred windows, unsupported images, unread documents and remaining
  questions visible. Do not approve because no findings were returned.
- Distinguish OCR/extraction failure, source conflict, scope differences and
  ontology omission. Silence is not a negative fact.
- Review recommendations are not authority to change a deployed contract.
- Specify a model-call limit, input bound and output-token bound. Obtain a spend
  budget; request limits are not exact billing measurements.
- Retain response cache artifacts; use `--response-cache` or an exact-input
  `--checkpoint` for assessment replay. Never overwrite an earlier report.
- A 401/403 is not transient: stop, show the permission category, and do not list
  keys, switch identities or retry a different resource to evade the denial.
- Stop on failure. Resume only when recorded input/contract/model identities
  match; a previous success status alone is insufficient.

## Deployment is separate and capability-gated

`fabric-kg app publish-structured --help` describes the schema-2 L5a planning
surface. Default transactional live publication remains capability NO-GO.
An explicit create-only prototype path, where available, is not transactional
deployment or recovery. Inspect its exact plan, owned-item journal and readback
requirements before separately authorizing any live call. Do not remove guards,
create fake receipts or describe a dry-run as deployment. Fabric workspace
authority is separate from Azure resource groups.

Existing semantic-bundle deployment commands remain compatibility paths; they
are not an automatic continuation from L4. Obtain the exact supported target,
resource ownership, readback/recovery plan and explicit deployment approval.
Do not replace shared resources to work around an integration gap.

Postdeployment fixes are versioned proposals. Preserve the previous release and
require approval for changed meaning, identity keys, withdrawals and live writes.
Do not claim estate-wide rollback from Search alias rollback alone.

## Legacy and examples

`set-domain` is deprecated. `densify` consumes legacy canonical JSON and explicit
schema-1 rules; it is not mandatory or a schema-2 step. The `surface-repro` skill
is an optional domain-specific historical example, never the default workflow.

Never print or commit credentials. Keep resource-specific configuration and
customer evidence outside tracked source. Cite actual command outputs and
artifact paths, distinguishing offline, model-tested and deployed results.
