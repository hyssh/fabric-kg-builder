# fabric-kg 0.2.4 Release Proof

## Candidate

- Version: `0.2.4`
- Base: `bfb9f2b24ff820174267932bf1dd3171788077a0`
- Acceptance runtime: installed `fabric-kg` wheel in an external Python 3.12 virtual environment
- Top-level command inventory: 38

## Scope

The release candidate adds `fabric-kg app deploy-l7`. It consumes only files and
configuration, defaults to GET-only dry-run planning, emits an immutable
sanitized plan, and requires `--live --approve-live <exact-plan-hash>`.
For an explicitly authorized one-shot live test, `--live` performs the complete
read-only preflight, persists its exact plan/hash, and immediately executes that
same plan without a prompt. Supplying `--approve-live` instead consumes only the
matching persisted plan.

The live plan is *capable of* covering exact Fabric definition readback,
release-owned Azure AI Search index/knowledge source/knowledge base names, and
release-owned Foundry Search/Fabric Data Agent connections. Which of those a
given run actually plans depends entirely on its configuration; see
"Fabric was not exercised by this run" below for what the recorded live run
did and did not cover. Existing `surface-tech-*` resources are never adopted
or modified by name.

Every reused Fabric item must use the bounded `fabric-kg-024-*` grammar and
provide a separately hash-bound ownership receipt matching release, attempt,
authority, stable item ID, type, display name, current definition hash, and
ETag. The receipt must also be present in the separately supplied, owner-only,
read-only registry pinned by `FABRIC_KG_OWNERSHIP_REGISTRY` and
`FABRIC_KG_OWNERSHIP_REGISTRY_SHA256`. Arbitrary, protected, legacy, and default
names fail before observation.

Before the first mutation, the executor atomically reserves immutable success
and failure receipt destinations for a unique attempt. Receipt collisions or
crash remnants block retries without mutation. Durable success receipt commit
is inside the rollback boundary; persistence failure triggers conditional
rollback and a separate failure receipt.

Current acceptance is GitHub Copilot invoking the base installed `fabric-kg`
CLI as a local subprocess. Set `foundry.deploy_builtin_agent` to `false`; the
base wheel is the complete 0.2.4 acceptance runtime.

## Acceptance Results

Record the final candidate SHA, archive hash, wheel hash, sdist hash, test
counts, external package origin, plan hash, and sanitized receipt hash here.
Do not paste access tokens, secrets, connection strings, or user configuration.

### Run of 2026-08-30

| Item | Value |
| --- | --- |
| Candidate SHA | `4e33ef6` |
| Wheel | `fabric_kg_builder-0.2.4-py3-none-any.whl` sha256 `f3927b305da36eee274461b365fa8b4d52c9a5f6ca6f8f1b740813b6ae182a70` |
| Sdist | `fabric_kg_builder-0.2.4.tar.gz` sha256 `39d28adc6c946989df6dc856627ba9f9722ce1fdf7419ba135ddeeb4d3370c5a` |
| External runtime | Python 3.12.13, non-editable install, `PYTHONPATH` unset, run outside the repository |
| Package origin | external venv `site-packages/fabric_kg_builder/__init__.py` |
| Top-level commands | 36 |
| Unit + contract | 4104 passed, 4 deselected |
| Integration | 7 passed |
| Dry-run smoke plan hash | `55201c7116b231ffc04f90b374907d59179217ce188775e60523a81f8cf222e5` |

Pipeline stages completed live against the real Foundry `gpt-4-1` deployment
through the installed CLI only:

| Stage | Result |
| --- | --- |
| L1 domain intake, review, approve | succeeded |
| L2 schema-constrained extraction | succeeded, 14,947 / 14,947 SourceUnits, receipt `stage-receipt:999b62c887a5dc537d040150ede7e851` |
| L7 release transaction (`app deploy-l7 --live`) | superseded by the run of 2026-08-30 (b), below |

Schema-2 L3 evidence validation, serving projection, and publication remain
excluded from CLI activation in this release line per SPEC-005, so the
schema-2 corpus is not yet the input to `deploy-l7`. The live L7 transaction
was exercised with release-owned Search artifacts derived from the real L2
SourceUnits.

### Run of 2026-08-30 (b) — live GO

The Search authorization blocker recorded below was cleared by an
administrator, and the live transaction was then re-run one-shot through the
installed CLI only.

| Item | Value |
| --- | --- |
| Candidate SHA | `9ee0ba7` |
| Wheel | `fabric_kg_builder-0.2.4-py3-none-any.whl` |
| External runtime | Python 3.12.13, non-editable install, `PYTHONPATH` unset, run outside the repository |
| Unit + contract | 4112 passed, 4 deselected |
| Live plan hash | `4621e880aacabe40f99bc9861169418c4af4d59ec224f8b0c35a31abd9242b3b` |
| Sanitized receipt hash | `b9ed5c0fb6d291b4cdf7631cdf4ff017f669d9d8c1e9f434a8d4d356c1b33733` |
| Attempt | `op-bcd610fd61bb208bde9bfe951548b27b7a0f0174ac810447f9fde1853ca80ff9` |
| Status | `succeeded` |

Command:

```
fabric-kg app deploy-l7 --config <ignored external config> --live \
  --plan build/release/l7-0.2.4-plan.json \
  --out build/release/l7-0.2.4-receipt.json \
  --log build/release/l7-0.2.4-events.jsonl
```

Mutated resources, release-owned only:

| Resource | Action | Verification |
| --- | --- | --- |
| Azure AI Search index `fabric-kg-024-surface-index` | create | readback verified, 400 of 400 declared documents, declared schema and document hashes match the approved plan |

No other resource was created, updated, or deleted. The pre-existing
`surface-tech-*` and `ks0001-*` indexes were not touched. The deferred
components were confirmed absent afterwards (`knowledgesources` and
`knowledgebases` both return `404`).

Deferred in this transaction, and explicitly not claimed as successful:
`search-knowledge-source`, `search-knowledge-base`,
`foundry-search-connection`, `foundry-fabric-connection`,
`foundry-built-in-agent`.

### Fabric was not exercised by this run

The configuration for this run declared `"fabric_definitions": []`, so the
approved plan contained **no Fabric action of any kind** — not a no-op action,
not a zero-diff comparison. The plan's six actions were the one Search index
create plus the five deferred components listed above. This run therefore
proves nothing about the Fabric Lakehouse, Semantic Model, Ontology, Graph
model, or Data Agent path, and must not be read as covering it.

Three independent reasons, each on its own sufficient:

1. No compiled definition bytes exist. `build/parquet`, `build/semantic`,
   `build/ontology`, `build/graph`, and `build/agents` are all empty in the
   acceptance workspace. L2 enrichment succeeded and wrote schema-2 candidates
   under `.fkg/l2/`, but no later stage has run. `app deploy-l7` requires
   compiled canonical definition bytes and a hash for every definition-bearing
   Fabric item, so there was nothing it could plan.
2. The release identity cannot reach the configured workspace.
   `GET /v1/workspaces/{configured id}` returns `403 InsufficientPrivileges`,
   and that workspace does not appear in the identity's workspace list. Even
   with artifacts present, preflight would have returned a capability NO-GO.
3. Lakehouse provisioning and table loading are outside `app deploy-l7` by
   design. Its Fabric item types are `DataAgent`, `GraphModel`, `Ontology`,
   and `SemanticModel` only; it does not create a Lakehouse, upload to
   OneLake, or register Delta tables. Those remain the separate
   `deploy-lakehouse`, `deploy-ontology`, `deploy-graph`, and
   `deploy-data-agent` commands.

A live Fabric deployment is therefore still outstanding as a separate,
currently blocked step. It is unblocked by granting the release identity
access to the intended workspace and by producing the compiled definition
artifacts; see the schema-2 stage gap noted below.

Two product defects were found and fixed by this run rather than worked
around. A failed live mutation reported only that rollback had completed,
discarding the cause; the failure receipt now carries `failure_cause`. And the
Search readback required the service to echo the submitted index verbatim,
which can never succeed because Azure AI Search populates every unset property
on create; the readback is now projected onto the declared shape, so declared
values still bind exactly while server defaults are ignored.

### Azure AI Search preview agentic capability (still deferred)

The Search service managed identity holds no role on the Foundry account, so
the preview knowledge source and knowledge base cannot be deployed. Clearing it
requires an administrator to assign `Cognitive Services User` to the Search
service managed identity on the Foundry account. Until then the release must be
run with `search.agentic_components: "deferred"`, which proves the direct index
path and records both components as deferred. Preview agentic success is not
claimed.

## L5a structured publication: compile proven, live mutation NO-GO

`fabric-kg app publish-structured` compiles a sealed L4 run into the four L5a
Fabric target definitions (`parquet`, `semantic_model`, `ontology`, `graph`),
seals an immutable plan JSON, and defaults to dry-run. It is the only shipped
path that derives a publication crosswalk; before 0.2.4 a crosswalk existed
only as a hand-built test fixture, so nothing in the wheel could produce one.

Compiled against the real 14,947-unit Surface corpus (sealed L4 receipt
`stage-receipt:771cbbd993624cbd401a726228285175`):

- 20 publication tables
- 8,756 entities across 7 semantic types
- 808 relationships across 6 relationship types
- 0 asserted properties — a real, documented gap, not an omission. Property
  owner and value are not persisted by the L2 candidate schema exercised on
  this corpus, so L3 cannot ground them without a full re-enrichment.
- 0 required-member manifests — this corpus seals none, which is a valid
  modelling outcome on a `required_role_set` domain contract.

**Live publication of these four targets is a capability NO-GO on the 0.2.4
line.** This verdict is empirical, not inferred from documentation:

| layer | ETag observed | `If-Match` honored | CAS fenceable |
| --- | --- | --- | --- |
| Fabric item control plane | `""` (empty string) on `GET` | no — `DELETE` with a bogus `If-Match` answers **404 ItemNotFound**, not 412 Precondition Failed | **no** |
| OneLake data plane (ADLS Gen2) | real, e.g. `"0x8DEFA188A771F90"` | standard ADLS semantics | yes |

None of the four `fabric-kg-024-*` items exist in the target workspace, so all
four require first-create. `run_l5a` routes any target whose prior state is
absent to `cleanup()` — a delete — on rollback. Create is therefore blocked
*and* its rollback would itself be unfenced. The OneLake data plane is
fenceable, but it inherits the item gate because the lakehouse must be created
first.

The plan records this in-band rather than only in prose: it carries a full
`capabilities` map, a sorted `blocked_capabilities` list, and
`live_publication_supported: false`. `--live` with the exact `--approve-live`
plan hash is still refused with the capability reason; a correct approval is
never mistaken for a capability.

Reinterpreting `cleanup()` as restore-to-captured-prior-definition would make
first-create fenceable, but the target-client protocol documents `cleanup` as
"atomically delete only when the persisted token still matches". Changing that
is a named contract change and is explicitly tracked as separate future work,
not folded into 0.2.4.

## Environment and Administrative Blockers

Record exact capability NO-GO results, including missing Search managed-identity
roles, unsupported Fabric `getDefinition` operations, or unavailable exact
Foundry rollback. A direct Search fallback does not satisfy preview agentic
success.

### Azure AI Search authorization (live blocker, 2026-08-30 — since cleared)

`app deploy-l7 --live` failed closed during preflight readback:

```
Search indexes readback failed with HTTP 403; the release identity lacks Azure
AI Search data-plane authorization on https://<search-service>.search.windows.net
```

The release identity holds exactly one role assignment in the target
subscription, `Foundry User` on the Foundry account. It has no Azure AI Search
control-plane or data-plane role, so even `Microsoft.Search/searchServices/read`
is denied, and it cannot grant itself the missing roles. Clearing this requires
a subscription administrator to assign Search Service Contributor plus Search
Index Data Contributor to the release identity and to permit Microsoft Entra
authentication on the service.

The transaction performed zero mutations. The emitted failure event reports
`causal_stage=preflight` and `mutation_possible=false`, and advertises no
receipt paths because no receipt was reserved.

An administrator subsequently assigned Search Service Contributor and Search
Index Data Contributor to the release identity, after which the live run of
2026-08-30 (b) above succeeded.

Fabric first-create intent is modeled separately from managed-existing intent,
but current Fabric create/delete contracts do not document ETag and conditional
delete CAS authority. The Azure backend therefore reports create capability
NO-GO before mutation. Empty-workspace live creation must wait for supported
rollback authority; fake lifecycle tests do not claim live platform support.

For a reproducible product defect, preserve the sanitized JSONL/receipt, analyze
the failing causal stage and rollback status, and open a GitHub issue with the
installed CLI version, candidate SHA, stable resource types, hashes, HTTP status
classes, and reproduction command. Never attach credentials or source content.

## Operator-authorized live Fabric deployment (outside the fenced product path)

The product's fenced deploy path still reports `fabric.<item>.create` as NO-GO,
for the reasons proven above, and that verdict is unchanged by this section. The
deployment recorded here was performed as an explicitly authorized operator
action using direct Fabric REST calls, accepting the documented rollback risk.
It is recorded because the release must describe what actually exists in the
target workspace, not only what the fenced path is willing to do.

Workspace `570d838d-88ff-437f-93cd-a639908b397f`. Source publication is the
sealed L4 v3 run compiled by `fabric-kg app publish-structured`, plan hash
`dcf03606b18f6ac30fc771eb516451e4bd7fd6e99ae74b2c9d4e45d88dc937e5`, 20 tables.

| item | type | id | readback |
| --- | --- | --- | --- |
| `fabric_kg_024_lakehouse` | Lakehouse | `76c658f3-c066-43db-a63b-d5c2f3778708` | 20/20 Managed delta tables |
| `fabric_kg_024_ontology` | Ontology | `07615fe9-fe1f-47ad-8ae2-ed6c0896e917` | 30 parts, 8 entity types, 6 relationship types |
| `fabric_kg_024_ontology_graph_07615fe9…` | GraphModel | `93631e64-57f6-41a2-b699-38004db2491a` | auto-provisioned by the Ontology |
| `fabric_kg_024_semantic_model` | SemanticModel | `85ec4072-326e-4cd1-b536-70a5591e7e3b` | 20/20 DirectLake tables |
| `fabric_kg_024_data_agent` | DataAgent | `e3faa373-dc55-45ae-bfbe-97e80c3b5e52` | 1 datasource, type `ontology` |

A pre-deployment item listing was captured as a baseline. Diffing it against the
post-deployment listing shows zero removed and zero modified items; every added
item is `fabric_kg_024_*`. No pre-existing item was touched.

### The graph target needs no separate creation

Creating a Fabric Ontology auto-provisions a companion Lakehouse, SQLEndpoint and
GraphModel named after the ontology's id. The L5a `graph` target is therefore
satisfied by the Ontology's own GraphModel rather than by an independent item.

### Data Agent scope

The Data Agent is deliberately narrow: exactly one datasource of type `ontology`
pointing at `fabric_kg_024_ontology`, with all eight entity types selected. It
has **no** Lakehouse datasource and **no** Azure AI Search datasource, and the
readback above is the evidence. Its instructions state that restriction to the
model so it reports a gap rather than substituting general knowledge. A separate
orchestrator agent spanning ontology and Search is explicitly out of scope here.

### Definitions are compiled by reviewable product code, not hand-built

`deploy/fabric_ontology_definition.py` and
`deploy/fabric_semantic_model_definition.py` translate the L5a target definitions
into Fabric's item formats. Recompiling the semantic model and diffing against
the live `getDefinition` readback yields 24/24 parts matching with zero semantic
difference, so the committed code provably reproduces the deployed item.

Two Fabric contract details cost a failed create and are pinned by regression
tests. First, `sourceTableProperties` is deserialized polymorphically and its
`sourceType` discriminator must be the object's first key; sorting keys causes
`ALMOperationImportFailed`. Second, an invalid definition does not fail
synchronously — the create returns 202, the item briefly appears, then Fabric
deletes it. Only the long-running-operation status endpoint reports the real
error, so it must always be polled rather than trusting the 202.

### Narrowings applied, stated rather than hidden

- One relationship type, `assertion-supported-by-evidence`, admits five source
  types where Fabric permits one. Its source is **widened** to an abstract base
  entity type bound to the all-entities table. Narrowing to a single physical
  type would have falsely claimed only components carry evidence. The compiler
  returns the widening so it is reported, never silent.
- Twelve array-typed columns on the seven `l4_*` base tables are excluded from
  the DirectLake semantic model because DirectLake cannot project complex types.
  The seven `l5a_type_*` and six `l5a_rel_*` tables are fully scalar and are
  projected complete. The compiler returns every exclusion.

### Honest gaps that remain

- Entity **properties are 0** in this corpus. Owner and value are not persisted
  by L2, so the graph carries identifiers and relationships but no attributes.
  **This is not a minor omission: it makes the deployed Data Agent unable to
  answer any natural-language question.** See below.
- Three orphaned companion items remain from a first ontology create that Fabric
  rolled back (`…_lh_47c0af0a…`, its SQLEndpoint, and `…_graph_47c0af0a…`).
  Deleting them was subsequently authorized and **attempted**, and Fabric
  **refused**. See "Ontology companions cannot be cleaned up" below: this is a
  platform limitation, not a decision left open.
- The provenance of the 400 documents in the live `fabric-kg-024-surface-index`
  is still unestablished.
- The Search managed identity still has no visible `Cognitive Services User`
  grant on the Foundry account, so preview agentic Search remains unproven.

### Ontology companions cannot be cleaned up

Removing the three orphans left behind by the rolled-back first ontology create
was explicitly authorized, attempted, and **refused by Fabric**. The attempt and
its result are recorded here because the outcome strengthens the fenced path's
NO-GO rather than weakening it.

Before attempting anything, the orphans were confirmed to be orphans. All three
carry the display-name suffix `47c0af0a…`, the id of the ontology Fabric deleted;
`GET` on that ontology returns 404. Every live definition was decoded and scanned
for references to them — ontology (30 parts), semantic model (25 parts) and data
agent (7 parts) all reference **none** of the three. The orphaned lakehouse holds
**0 tables**. There was no live state to protect.

| target | endpoint | result |
| --- | --- | --- |
| GraphModel `06d6cff6…` | `DELETE /items/{id}` | `400 UnknownError`, `isRetriable: false` |
| GraphModel `06d6cff6…` | `DELETE /graphmodels/{id}` | `400 UnknownError` |
| Lakehouse `e66e58cc…` | `DELETE /items/{id}` | `400 UnknownError`, `isRetriable: false` |
| Lakehouse `e66e58cc…` | `DELETE /lakehouses/{id}` | `400 UnknownError` |
| SQLEndpoint `8828ed8c…` | `DELETE /sqlEndpoints/{id}` | `400 OperationNotSupportedForItem` |

The failure is not a broken delete verb. A control probe in the same workspace,
with the same identity and the same endpoint, created an ordinary lakehouse and
deleted it: `201` → `200` → readback `404`, and the workspace returned to exactly
63 items with the probe's own auto-provisioned SQLEndpoint removed with it. So
`DELETE` works; these specific items are system-managed children of an ontology,
and once that parent is gone they are unreachable through the REST surface. The
item payload exposes no flag distinguishing them — only the naming convention
does.

The consequence is the important part. **A failed Fabric ontology create is not
rollback-able.** It leaves up to three permanent items that no API can remove.
This is independent of the CAS problem recorded above and compounds it: the
control plane offers neither conditional mutation nor cleanup after a partial
failure. `agent/l7_release.py` therefore continues to report
`fabric.<item>.create` as NO-GO, and that verdict now rests on two proven
platform limitations rather than one.

### Two behaviours that require manual verification in the Fabric portal

Neither of the following is claimed as verified. Both are reachable only through
the portal UI, so the check a reviewer should perform is written out here rather
than left silently unconfirmed.

**The Data Agent has never been asked a question.** Its configuration is proven
by definition readback — exactly one datasource of `type: "ontology"`, all eight
entity types selected, zero lakehouse and zero Search datasources — but its
*behaviour* is untested. No public v1 REST chat endpoint exists;
`/aiskills/{id}/aiassistant/openai` and `/dataAgents/{id}/publish` both return
404. To verify manually: open `fabric_kg_024_data_agent` in the workspace and ask
a question answerable only from the graph, for example "which components does the
Surface Laptop device relate to?". A correct result cites ontology entity types
by name and traverses a relationship. The check that matters is the **negative**
one: the answer must not cite a Lakehouse table or a Search document, because no
such source is configured. If it does, the scoping guarantee is wrong.

**The GraphModel materializes rows — measured, not assumed.** This was carried
as unverified for most of the release, on the correct reasoning that Fabric
accepting a definition is not the same as populating a graph from its bindings.
It has since been measured directly through the documented `executeQuery` beta
API, and the graph ingested everything:

| relationship | live | L5a expected | |
| --- | --- | --- | --- |
| `procedure_repairs_component` | 415 | 415 | match |
| `assertion_supported_by_evidence` | 203 | 203 | match |
| `procedure_requires_tool` | 122 | 122 | match |
| `device_has_component` | 44 | 44 | match |
| `procedure_has_warning` | 15 | 15 | match |
| `symptom_has_cause_resolution` | 9 | 9 | match |

Node counts agree the same way: device 726, component 2,711, symptom 96,
procedure 1,722, tool 1,500, warning 1,245, evidence 756 — **8,756 typed nodes,
exactly the published entity count**. The remaining 8,756 of the 17,512 total
are base-label duplicates, which is #102 measured rather than inferred.

Multi-hop traversal was confirmed with a control probe rather than assumed:

```
MATCH (d:surface_device)-[:device_has_component]->(c:surface_component)
      <-[:procedure_repairs_component]-(p:surface_procedure)
   -> 81
```

The control matters because a *different* two-hop chain
(symptom → procedure → warning) returns zero, which looks alarming until the
arithmetic is done: 9 procedures carry a cause-resolution edge and 15 carry a
warning, drawn from 1,722 procedures. Expected overlap is 0.078 procedures, so
**zero is the ~92% likely outcome**. Finding a match would have been the
surprise. An empty result from a sparse join is not evidence of an empty graph,
and the control probe is what distinguishes the two.

This retires the earlier caution that a `Completed` refresh proved structure but
not population. It proved both.

### The deployed graph was empty: a hardcoded `dbo` schema (fixed)

Both behaviours were verified by the operator in the portal at the time, and both
**failed**. The query *"list complete steps to replace
battery for surface 10 pro"* returned no answer: the Data Agent's
`analyze_ontology` call reported *"The Graph Model is not ready. Please try
again later."*, and the agent then declined to answer rather than inventing
one — the anti-hallucination behaviour working exactly as intended, on top of a
graph that was genuinely empty.

**Root cause.** A Fabric Lakehouse is schema-enabled or it is not, and the
choice is fixed at creation. It decides where Delta tables physically live:

| Lakehouse | `defaultSchema` | OneLake path |
| --- | --- | --- |
| schema-enabled | `dbo` | `Tables/dbo/<table>` |
| not schema-enabled | `null` | `Tables/<table>` |

`fabric_kg_024_lakehouse` (`76c658f3`) was created with a plain
`POST /items {"type":"Lakehouse"}` and **no** `creationPayload.enableSchemas`,
so its `defaultSchema` is `null` and its 20 tables live at `Tables/<table>`.
Both 0.2.4 definition compilers, written independently, hardcoded the schema
anyway — `sourceSchema: "dbo"` at two sites in the ontology compiler (entity
data bindings and relationship contextualizations) and `schemaName: dbo` in the
semantic model's DirectLake partitions.

Fabric propagated that into the auto-provisioned GraphModel's data sources as
`abfss://…/Tables/dbo/<table>`. That path does not exist — a OneLake listing of
`Tables/dbo` returns `PathNotFound` — so the graph's refresh job failed:

```
GET /v1/workspaces/{ws}/items/{graphModelId}/jobs/instances
  jobType: Refresh   status: Failed   isRetriable: false
  errorCode: GraphNotRefreshable
  "Graph doesn't have valid content and cannot be refreshed."
```

One wrong path segment, three visible symptoms: the ontology failed to open in
the portal, the graph stayed empty, and the Data Agent could not ground an
answer.

**Why it is insidious.** The definition was structurally valid, passed schema
validation, imported without error, and read back byte-identical. Every check
the deployment performs passed. Nothing distinguishes a binding to a table that
does not exist from one that does until something tries to read it.

**Two plausible explanations that the evidence ruled out.** Both are recorded
because each looked more likely than the real cause at the outset.

- *The `ARRAY` column errors.* The SQL analytics endpoint reports five
  unsupported-type errors covering twelve `list<string>` columns across five
  tables. These are real, but they are not this. They are non-fatal warnings —
  a `refreshMetadata` call reports `lastSuccessfulSyncDateTime` for all 20
  tables and zero failures, so the columns are dropped and the tables stay
  exposed. More decisively, the GraphModel reads Delta **directly over
  OneLake** and never touches the SQL endpoint. Tracked separately; not a
  blocker for the graph.
- *Excluding arrays from the ontology bindings.* This would have been a no-op.
  Ontology bindings only ever reference the identity column and declared
  entity properties, all scalar; no array column was ever bound. Verified
  against the live definition.

**Fix.** `deploy/lakehouse_schema.py` resolves the segment once, from the
lakehouse's actual `properties.defaultSchema`, and every consumer asks it
rather than assuming. `sourceSchema` is optional in the published data-binding
schema (`required: [sourceType, workspaceId, itemId, sourceTableName]`), so for
a lakehouse without schemas the key is **omitted** rather than set to null,
matching what Fabric itself emits.

The `lakehouse` argument is **required** on both compilers. Defaulting it would
have replaced one silent wrong assumption with another in the opposite
direction; the target lakehouse is something a caller always knows and should
have to state.

Recurrence is pinned by tests that fail against the original code, including
one asserting the two ontology sites can never disagree — they are separate
code paths over the same physical tables, which is how the original hardcoding
survived review.

**Change surface.** Recompiling against the live L5a definition and diffing
part-by-part against the deployed item: 30 parts, paths identical, and exactly
**14** parts change — the 8 entity data bindings and 6 relationship
contextualizations — each solely by removing `sourceSchema`. The remaining
parts are byte-identical or differ only in Fabric's own re-serialization.

Delivery is `updateDefinition` only. The ontology is never deleted and
recreated: per the finding above, a failed create leaves permanently
undeletable companion items.

**The semantic model carries the same defect, confirmed rather than assumed.**
Reading its live definition back shows `schemaName: dbo` in all 20 table parts
and `sourceLineageTag: [dbo].[…]` alongside it. Unlike the GraphModel there is
no jobs endpoint to observe a failure through — a semantic model exposes none —
so this is established from the definition itself, not from an error. It needs
the same update. Recompiling gives 24 parts, of which 23 change only by
dropping the schema and lineage qualifiers and one differs by a trailing blank
line; `.platform` is supplied at deployment time and is not compiler output.

### The fix applied live, and the graph refreshed

Applied to the two live items on 2026-08-31 under operator authorization, by
`updateDefinition` only. Neither item was deleted or recreated.

Pre-flight, against the live target:

| check | result |
| --- | --- |
| `fabric_kg_024_lakehouse.properties.defaultSchema` | `null` |
| OneLake `…/Tables` | HTTP 200 |
| OneLake `…/Tables/dbo` | HTTP 404 |
| workspace items | 63 |

**Ontology** (`07615fe9`) — `POST updateDefinition` returned 200 synchronously.
All 14 table references moved from `"dbo"` to `null`.

A note on how that was verified, because the obvious check gives the wrong
answer. Fabric normalizes an *omitted* key into an explicit
`"sourceSchema": null`, so grepping the readback for `sourceSchema` still finds
it in all 14 parts and looks like the update silently failed. Only comparing
the **values** shows the change landed. A substring check would have concluded
the opposite, in either direction.

**Fabric regenerated the companion GraphModel's data sources on its own**, from
`abfss://…/Tables/dbo/<table>` to `abfss://…/Tables/<table>`, all 14 of them,
and auto-triggered a refresh seconds after the update. A refresh cannot be
triggered by hand — `POST jobs/instances?jobType=Refresh` returns
`InvalidJobType` — so this is the only path to one.

```
13:42:35  Refresh  Failed     GraphNotRefreshable
16:09:03  Refresh  Completed  -
```

That transition is the proof. The definition edit alone proves nothing: this
entire defect consisted of a definition that validated, imported, and read back
byte-identical while pointing nowhere.

**Semantic model** (`85ec4072`) — `updateDefinition` returned 202; the
operation reached `Succeeded`. Polling mattered: an invalid definition also
returns 202, after which Fabric deletes the item outright. Readback confirms
the item alive with zero `schemaName` qualifiers, zero `[dbo]` lineage tags,
and all 20 DirectLake partitions intact.

**Blast radius: none.** The workspace holds 63 items, unchanged. No item was
created, deleted, or otherwise modified.

Two behaviours still need a human in the portal, and are not claimed here: the
node and edge counts the graph actually materialized, and the Data Agent
answering the battery question while citing only ontology and graph. A
completed refresh job is strong evidence the graph has valid content, but it is
not the same as counting rows.

### The graph is real, and the Data Agent still cannot answer

The user tested the deployed agent in the portal. Scoping held: its only
datasource is the ontology, and no answer cited the Lakehouse or Search. But all
six questions failed with `No data found after query execution`.

Measured directly against the live GraphModel through the documented
`executeQuery` beta API — read-only, no mutation:

| measure | value |
| --- | --- |
| nodes | 17,512 |
| edges | 808 |

Edges resolve per type: `procedure_repairs_component` 415,
`assertion_supported_by_evidence` 203, `procedure_requires_tool` 122,
`device_has_component` 44, `procedure_has_warning` 15,
`symptom_has_cause_resolution` 9. The 808 matches the L4 figure exactly, and the
structure is traversable. The `dbo` fix worked.

Every declared property, however, is empty. `MATCH (n:<label>) WHERE n.<prop> IS
NOT NULL RETURN COUNT(n)` returns **0** for all seven labels — device,
component, symptom, procedure, tool, warning, evidence. The only populated field
is the identity column, an opaque content hash:

```
{"id":"entity:4cbef1a4ec12aa5b031f5252f552531f","model_id":null}
```

The agent's own failing predicate, replayed directly, returns zero, and no
identifier anywhere in the graph contains readable text:

```
MATCH (n:`surface_device`) WHERE LOWER(n.`model_id`) = LOWER("Surface Pro 10")
  RETURN COUNT(n)                                            -> 0
MATCH (n) WHERE n.`id` CONTAINS "battery" RETURN COUNT(n)    -> 0
```

Nothing in the agent is broken. It is correctly scoped, it generated valid GQL,
it executed that GQL successfully against a live graph, and it then correctly
refused to invent an answer. A graph with 17,512 nodes, 808 edges and a
`Completed` refresh is indistinguishable from a working one until you ask
whether any property holds a value.

That is the third artifact in this release that validates, imports, reads back
byte-identical, and is unusable. The first pointed at a path that did not exist;
this one has nodes that cannot be named. Structural validity and semantic
usability are different things, and only the first was ever checked.

It is worth being precise about what this does and does not mean. The agent is
deliberately ontology-only — Search and the Lakehouse were excluded on purpose,
with a separate orchestrator planned — so some vocabulary limitation was
expected by design. But this is not thin descriptions. **No entity can be
identified at all**, and that wall does not move when Search is added: an
orchestrator must still resolve a mention like "Surface Pro 10" to a starting
node before it can expand. With every property null there is no entry point from
language into the graph, only traversal between opaque hashes.

Tracked as #105, which also records that #14 specified a blocking gate for
exactly this, stated this failure verbatim as its example, and was closed as
completed — while no such gate ran in the 0.2.4 publication path. Also confirmed
here: #102's base-type duplication is real and measurable, 8,756 base plus 8,756
typed nodes making up the 17,512 total.

### Labels are recoverable downstream, without re-running extraction

Scoping only; no code changed in 0.2.4. Recorded because it determines whether
#105 is expensive or cheap to close, and the answer is not obvious.

The verbatim source text survives all the way into **sealed L3 output**. Each
run directory carries an `evidence-spans/` store, and every span holds a `quote`:

```
'speaker meshes'          'Display Module'
'bonding frame tool'      'PSA strips'
'Ultra 5 16GB – 13” EP2-29720'
'Anti-static wrist strap (1 MOhm resistance)'
```

Across 3,245 sampled spans, **100%** carry a non-empty quote, the median length
is 28 characters, and 79% are 60 characters or fewer — these are mention spans,
not paragraphs. Term coverage on the corpus: `battery` 210, `kickstand` 38.

L4 already holds them. `_verified_evidence` indexes every span by ID, quote
included, and `_serving_rows` already resolves each entity's spans through
`_require_evidence` — which **raises** when an asserted candidate lacks verified
evidence. Full label coverage therefore is not a hope; it follows from an
invariant the pipeline already enforces. The text is loaded, hash-verified, and
indexed per entity, and then simply never written to a column.

So a label property is recoverable by an L4 recompile alone — no L2 re-run, no
L3 re-run. The cost is a schema change: adding a column to
`l4_semantic_asserted_entities` changes its `row_hash` and requires a registry
version bump. That is a real cost, but a legitimate one, since the semantics
genuinely change.

Two things this would **not** do, stated so the scope is not overread. It would
not make `semantic_asserted_properties` non-zero — property *candidates* are
about 1% of extraction and none assert, which is the separate half of #105 and
does need the L2 work. And it would not make the original failing question work:
the device label would read `Ultra 5 16GB – 13” EP2-29720`, because the phrase
"Surface Pro 10" does not appear in this corpus at all. That is a property of the
source material, not a defect in the pipeline.

### Delivered: labels projected from evidence (PR #109)

The recompile above was carried out. Every asserted entity now carries a `label`
and a `label_evidence_span_id` that proves where the label came from.

Two corrections to the plan as first sketched, both forced by evidence:

The label is **not** derived from `normalized_business_key`, which was the
originally approved source. That field is not reachable at L4 —
`enrichment/schema2_evidence.py:1248` states plainly that "the frozen carrier
does not persist the normalized business key", and it appears nowhere under
`serving/`. Using it would have meant reading unsealed L2 working state for a
value carrying no hash. The label is taken instead from sealed evidence quotes,
which are hash-verified, better-cased, and higher-coverage.

The two new columns are **nullable**. This follows directly from the guardrail
that uncovered entities get null rather than a fabricated placeholder: 775 of
8,756 entities (8.9%) have no span short enough to be a mention rather than
prose, so a non-nullable column could only be satisfied by inventing values for
them. Nullability is what makes the no-placeholder rule expressible.

Measured coverage, verified identically at L4 and again in the L5a published
typed tables — 7,981 of 8,756 entities, **91.1%**:

| type | rows | labelled |
|---|---|---|
| device | 726 | 99.9% |
| tool | 1,500 | 99.5% |
| component | 2,711 | 99.2% |
| symptom | 96 | 91.7% |
| procedure | 1,722 | 88.0% |
| evidence | 756 | 75.7% |
| warning | 1,245 | 72.3% |

Terms that previously matched nothing now match: `battery` 493, `surface` 425,
`display` 311, `kickstand` 93. Entity and relationship counts are unchanged at
8,756 and 808, so no data drifted.

The 120-character cap was measured rather than chosen. Because selection takes
the *shortest* qualifying quote, raising the cap can never degrade an entity that
already has a crisp mention; it only decides the fate of entities whose every
span is long. Coverage runs 65.1% at cap 40, 77.1% at 60, 91.1% at 120, and 100%
uncapped, while the median selected label moves only from 20 to 30 characters
across that whole range. Inspection of both sides of the boundary showed 60–120
still reads as a mention and beyond 120 is multi-sentence prose.

**What this does not fix.** `semantic_asserted_properties` is still zero, so
`model_id`, `component_id` and the other property columns remain null — and #108
records that L5a would write null into them regardless of upstream data. A
natural-language question must therefore match on `label`, not on those columns.
And the original failing question still fails: devices label as
`13-inch Platinum Mexico D0M-00036`, because "Surface Pro 10" is genuinely absent
from the corpus. The label column is a separate improvement from Path B, not a
substitute for it.

Note also that the label is a **verbatim mention**, not a canonical name. The
pipeline elects no canonical name, and this projection does not invent one — the
value is one phrase a source document happens to use, chosen by a fixed
deterministic rule. It should be read as "a way this entity is referred to".


### Deployed live: labels reach the graph

The label projection above was applied to the live estate, in the order the
Delta log proved necessary — tables first, then the definitions that bind them.
Binding a column before it exists produces an item that reads back cleanly and a
graph that cannot refresh, so the ordering is a correctness requirement rather
than a preference.

**Lakehouse — additive evolution, not a rebuild.** Eight of the twenty published
tables gained a column; the other twelve were byte-identical and were left
untouched. Whether an overwrite *evolves* a table or silently replaces it is not
visible from a row count, so it was checked directly: each table's Delta
`metadata.id` was recorded before the write and compared after. A drop-and-create
mints a new identifier; an overwrite preserves it.

| check | result |
| --- | --- |
| tables written | 8 of 20 |
| `metadata.id` preserved | 8 of 8 |
| Delta version | 0 → 1, version 0 retained |
| columns removed | 0 |
| row-count drift | 0 |
| untouched tables still at version 0 | 12 of 12 |

**Ontology.** `updateDefinition` changed exactly the 16 parts predicted — eight
entity types, each contributing a definition and a data binding — and the
readback matched the intended compilation exactly. Every binding's
`sourceColumnName` was then resolved against the live Delta schema of the table
it binds, which is what rules out the failure mode above by measurement instead
of by argument.

**Graph.** The ontology update triggered a refresh that reached `Completed`.
That is structural evidence only, so the graph was queried directly: labels come
back as readable text, and node and edge counts are unchanged at 17,512 and 808.

**Semantic model.** `updateDefinition` changed exactly the 8 predicted parts.
The live TMDL declares every column explicitly, so DirectLake does not discover a
new column on its own — this step is required, not optional. The workspace item
count was 63 before and after.

#### `label` is a reserved word in Fabric GQL

Reading the property requires backticks. Unquoted, it is a syntax error rather
than an empty result:

```
MATCH (n:`surface_component`) RETURN n.label LIMIT 5
-> 42000  Reserved keyword 'label' cannot be used as an unquoted identifier.

MATCH (n:`surface_component`) RETURN n.`label` LIMIT 5
-> "plastic guide", "USB ports", "P/N: 13N4-1EN0R01", ...
```

The same dialect expects `FILTER` where earlier observed agent output used
`WHERE`. This matters for interpreting the Data Agent: a generator that does not
quote the identifier fails *loudly*, with a syntax error, which is a different
symptom from the empty results that motivated this work and should not be read as
a regression in the data. Whether the generator quotes it is not yet known.
Tracked in #112.

A third rule surfaced alongside it: aggregates require an alias. `RETURN
COUNT(n)` is rejected with the same `42000` class of error, while `RETURN
COUNT(n) AS c` succeeds. Case is irrelevant; the alias is the only difference.
So a generated query must satisfy three constraints — backtick `label`, use
`FILTER` rather than `WHERE`, and alias every aggregate — and each one fails
audibly rather than returning an empty table, which is what makes them
separable from a genuine zero-row answer.

#### The rollback anchor was verified, not assumed

Version 0 of every rewritten table was retained, but "retained" is a claim about
the log rather than about readability, so it was read back after the deployment
had already succeeded:

| version | rows | columns | `__label` |
| --- | --- | --- | --- |
| 0 | 2,711 | 5 | absent |
| 1 | 2,711 | 6 | present |

Both commits survive in the history, and `DeltaTable.restore` is available in the
pinned `deltalake` version, so a restore is a genuine option rather than a
hypothetical one. Restore is itself a new commit, which makes it safe to attempt
and idempotent to repeat. It was never needed — every step verified clean on the
first attempt — but an untested rollback path is indistinguishable from an absent
one, so it is recorded here as measured.
