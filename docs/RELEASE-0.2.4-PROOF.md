# fabric-kg 0.2.4 Release Proof

## Candidate

- Version: `0.2.4`
- Base: `bfb9f2b24ff820174267932bf1dd3171788077a0`
- Acceptance runtime: installed `fabric-kg` wheel in an external Python 3.12 virtual environment
- Top-level command inventory: 36

## Scope

The release candidate adds `fabric-kg app deploy-l7`. It consumes only files and
configuration, defaults to GET-only dry-run planning, emits an immutable
sanitized plan, and requires `--live --approve-live <exact-plan-hash>`.
For an explicitly authorized one-shot live test, `--live` performs the complete
read-only preflight, persists its exact plan/hash, and immediately executes that
same plan without a prompt. Supplying `--approve-live` instead consumes only the
matching persisted plan.

The live plan covers exact Fabric definition readback, release-owned Azure AI
Search index/knowledge source/knowledge base names, and release-owned Foundry
Search/Fabric Data Agent connections. Existing `surface-tech-*` resources are
never adopted or modified by name.

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
| L7 release transaction (`app deploy-l7 --live`) | blocked before any mutation by an environment authorization blocker (below) |

Schema-2 L3 evidence validation, serving projection, and publication remain
excluded from CLI activation in this release line per SPEC-005, so the
schema-2 corpus is not yet the input to `deploy-l7`. The live L7 transaction
was exercised with release-owned Search artifacts derived from the real L2
SourceUnits.

## Environment and Administrative Blockers

Record exact capability NO-GO results, including missing Search managed-identity
roles, unsupported Fabric `getDefinition` operations, or unavailable exact
Foundry rollback. A direct Search fallback does not satisfy preview agentic
success.

### Azure AI Search authorization (live blocker, 2026-08-30)

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
receipt paths because no receipt was reserved. Search publication and the
preview agentic knowledge base therefore remain unverified live; no direct
fallback is claimed as preview success.

Fabric first-create intent is modeled separately from managed-existing intent,
but current Fabric create/delete contracts do not document ETag and conditional
delete CAS authority. The Azure backend therefore reports create capability
NO-GO before mutation. Empty-workspace live creation must wait for supported
rollback authority; fake lifecycle tests do not claim live platform support.

For a reproducible product defect, preserve the sanitized JSONL/receipt, analyze
the failing causal stage and rollback status, and open a GitHub issue with the
installed CLI version, candidate SHA, stable resource types, hashes, HTTP status
classes, and reproduction command. Never attach credentials or source content.
