# fabric-kg 0.2.4 Release Notes

Version 0.2.4 adds a strict L7 release transaction under
`fabric-kg app deploy-l7`. Planning is the default and performs no mutations.
Live execution requires the exact immutable plan hash, current configuration,
identity, tenant, unexpired approval window, and immediate readback agreement.
An explicitly authorized one-shot `--live` invocation generates and persists
the preflight plan and immediately consumes that same exact hash without a
prompt; `--approve-live` remains available for consuming a prior exact plan.

The narrowed live scope uses existing Fabric definition APIs, Azure AI Search,
Fabric Data Agent, and Foundry built-in project connections. Every enabled
mutation has a before/after journal and conditional rollback. Release-owned
names must begin with `fabric-kg-024-`; existing `surface-tech-*` and `ks3001`
resources cannot be adopted or deleted.

Existing Fabric items additionally require an immutable ownership receipt whose
stable ID, type, display name, definition hash, ETag, attempt, and authority
match live readback. The executor reserves receipt destinations before mutation
and does not report success until the immutable receipt is atomically committed
and fsynced.

Create intent is distinct from managed intent. Because current Fabric
create/delete APIs do not document ETag/CAS rollback, the live Azure adapter
fails first-create preflight rather than risk deleting a concurrently changed
item.

The supported acceptance architecture is a base-wheel `fabric-kg` subprocess
invoked locally by GitHub Copilot with `deploy_builtin_agent=false`.

## Foundry Prompt Agent (`compile-agent` / `deploy-agent`)

0.2.4 also adds an Ontology-first, Azure-AI-Search-second Foundry Prompt Agent
path (`app compile-agent`, `app deploy-agent --dry-run|--env`). Instructions
(v1.4) direct the agent to consult the Fabric Data Agent (Ontology/graph)
first and use Azure AI Search only to fill gaps, treating the ontology as the
source of top-level concepts and Search as the source of detail and quotable
citations, while steering around three known Fabric GQL pitfalls (#112).

**This feature is not yet query-ready.** A live dev deploy on 2026-08-31
created the agent successfully (`agent_version: 1`), but two defects were
found once real queries were attempted:

- Azure AI Search tool: hardcoded `query_type` broke on indexes without an
  integrated vectorizer. **Fixed in this release** — the deploy path now
  auto-detects vectorizer support and falls back to `semantic` search, with an
  explicit override still available in `agent-metadata.yaml`. (#121)
- Fabric Data Agent tool: fails with `ItemNotFound` at invocation time. The
  underlying Fabric item is confirmed published and correctly configured, so
  this looks like a Foundry↔Fabric connection/permission gap rather than a
  Fabric authoring problem — **still open, unresolved**. (#122)

No further live agent redeploys are planned until #122 is resolved and
independently verified. Treat this feature as deployed-but-not-functional for
0.2.4, not as a completed capability.
