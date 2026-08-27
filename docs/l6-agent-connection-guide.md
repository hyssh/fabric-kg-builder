# L6 Agent Connection Guide

L6 definitions are local artifacts in version 0.2.3. Do not deploy them from
this stage.

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
contents. A production multi-process host must use a durable adapter preserving
the same atomic claim/completion/failure transitions. A durable host injects an immutable
`L6AuthorityKeyringSnapshot` through `L6AuthorityKeyringProvider`. Snapshots
carry authority ID/version/algorithm, validity window, state, and verifier;
atomic versioned replacement supports rotation, disable, and revocation.
Unknown, inactive, early, or expired authority keys fail closed. Key material
and verifiers are internal host configuration and are never exposed as tools.

L7 must deploy the endpoint and definition, verify project connection
audiences/RBAC, run live Graph and Search acceptance, and confirm definition
read-back from Microsoft Foundry and Fabric Data Agent.
