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
