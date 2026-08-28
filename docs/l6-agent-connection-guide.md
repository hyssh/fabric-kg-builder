# L6 Agent Connection Guide

L6 definitions remain canonical artifacts in version 0.2.3. Deployment is an
explicit L7 operation and never occurs during L6 compilation.

## Required existing connections

1. A Fabric Data Agent project connection created through the existing
   `FoundryProjectConnectionClient.upsert_fabric_data_agent` abstraction.
2. A Foundry `RemoteTool` project connection created through
   `FoundryProjectConnectionClient.upsert_remote_tool`.

Pass only the resulting stable project connection IDs to
`build_l6_agent_definition`. Credentials, workspace secrets, ACL principals,
signed URLs, and provider metadata must not be embedded.
Accepted references are repo-defined `connection:<opaque>` IDs, stable UUIDs,
Fabric `fabric:workspace/<uuid>/item/<uuid>` references, or complete Azure ARM
resource IDs. Query strings, fragments, userinfo, encoded variants, traversal,
emails, principal strings, endpoint URLs, and credential material are rejected.

```python
from pathlib import Path

from fabric_kg_builder.agent.l6_integration import (
    build_l6_agent_definition,
    persist_l6_agent_definition,
)

definition = build_l6_agent_definition(
    agent_name="fabric-kg-evidence-agent",
    fabric_data_agent_connection_id="connection:fabric-data-agent",
    foundry_remote_tool_connection_id="connection:l6-remote-tool",
)
persist_l6_agent_definition(
    Path("build/agent/l6-agent-definition.json"),
    definition,
)
```

The generated instructions require Ontology/Graph first, one filtered Search
route second, exact citations, and partial/abstain when authority or coverage is
incomplete. The downstream agent may synthesize at most once from the returned
structured package. Fabric-kg never performs that synthesis.

Only `L6StableCitationPresentation` values may cross the sealed L6 boundary.
Never attach an authorized asset URL to them. Any short-lived URL must be added
ephemerally by an L7 UI adapter after the L6 package has been validated and must
not be persisted or included in a package/collection hash.

The L6 tool host must keep the `L6GraphReceiptAuthority` server-side. Each
`L6GraphQuery` carries an opaque `l6r-sha256:<64-hex>` run identity; its request
ID is derived exactly as `grq-sha256:<sha256(canonical request payload)>`.
The authority atomically claims the run plus an execution fingerprint covering
the query, resolved scopes, ACL/policy, L5 publication/read-back, crosswalk,
Graph model, Runtime 1.1 budget, and RequiredMember authority before calling
Graph. Only authority-identical completed retries reuse the persisted result
and receipt. Any changed authority, provider abort, or invalid result consumes
or rejects the run, wakes concurrent waiters, and performs no second Graph
call. The included
`L6InMemoryGraphReceiptAuthority` is the process-local test implementation.
Callers receive only the opaque receipt ID/hash; they cannot submit receipt
contents. A production multi-process host uses `AzureBlobL6GraphReceiptAuthority` to
preserve the same atomic claim/completion/failure transitions with finite Blob
leases and ETag compare-and-swap. A durable host injects an immutable
`L6AuthorityKeyringSnapshot` through `L6AuthorityKeyringProvider`. Snapshots
carry authority ID/version/algorithm, validity window, state, and verifier;
atomic versioned replacement supports rotation, disable, and revocation.
Unknown, inactive, early, or expired authority keys fail closed. Key material
and verifiers are internal host configuration and are never exposed as tools.

## L7 safe deployment workflow

Copy `.foundry/l7-deployment.json.example` to the ignored
`.foundry/l7-deployment.json` and replace every placeholder. The endpoint must
already be hosted on approved HTTPS compute; this release does not provision a
new compute resource.

```text
fabric-kg app deploy-l6 \
  --config .foundry/l7-deployment.json \
  --definition build/agent/l6-agent-definition.json \
  --plan build/release/l7-deployment-plan.json

fabric-kg app deploy-l6 \
  --config .foundry/l7-deployment.json \
  --definition build/agent/l6-agent-definition.json \
  --live \
  --plan build/release/l7-deployment-plan.json \
  --approve-live <exact-plan-hash>
```

Dry-run is the default and performs GET/read-only operations only. Live mode
reads the exact unexpired plan from disk and rechecks identity, configuration,
audience, resource ETags, canonical Fabric definition bytes/hashes, signed
connection ownership, and L5a/L5b/L6 hashes before its first mutation.
Authenticated RemoteTool readiness is checked again immediately before mutation
and before success. `--resume` does not re-plan or accept changed state.
Rollback is mandatory and journals every attempted mutation; there is no live
opt-out after the first mutation.

Foundry does not return `CustomKeys` credential values from connection GET.
Mutable `metadata.bindingHash` is not adoption authority. A preexisting
connection requires an independently signed durable Blob receipt for the exact
connection ID, ETag, category, target, audience, workspace, and Data Agent.
Without it, use a new release-owned connection name. Configure the opaque
authority factory as `FABRIC_KG_L7_OWNERSHIP_FACTORY=module:callable`; key
material remains outside config, Blob state, logs, plans, and receipts.
Configure
`FABRIC_KG_L7_REMOTE_PROBE_CREDENTIAL_FACTORY=module:callable` separately; it
must return a credential for the actual allowed Foundry managed identity.
The deployer credential is never substituted for this caller proof.

Every Fabric target in `.foundry/l7-deployment.json` must set
`definition_path`, `definition_hash`, and `definition_bytes_hash`. The file must
be canonical JSON. A `null` hash, type-only readback, or unavailable Data
Agent/Graph `getDefinition` API is a capability NO-GO.

The RemoteTool process is exposed separately:

```text
fabric-kg app serve-l6 \
  --config .foundry/l7-deployment.json \
  --definition build/agent/l6-agent-definition.json \
  --handler-factory my_package.l6_runtime:create_handler \
  --readiness-authority-factory my_package.l6_runtime:create_readiness
```

The handler factory must return configured canonical L6 authorities. The host
enforces Entra tenant/audience validation, strict request/response schemas,
request size and deadline limits, sanitized errors, health/readiness, and zero
synthesis. `/health` is operational only; live authority comes exclusively from
authenticated `/ready` with exact caller/app-role, OpenAPI, L6 definition, and
durable backend hashes. Reverse-proxy body limits are defense-in-depth; the
application still authenticates first and incrementally bounds streamed input.
Assign Storage Blob Data Contributor (or a tighter custom role with
read/write/lease permissions) to the host identity. Foundry and Fabric RBAC,
RemoteTool application registration/audience, and compute hosting remain release
prerequisites. Live Graph/Search acceptance is intentionally deferred to the
0.2.4 successor.
