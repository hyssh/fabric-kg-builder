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
(v1.5) direct the agent to consult the Fabric Data Agent (Ontology/graph)
first and use Azure AI Search only to fill gaps, treating the ontology as the
source of top-level concepts and Search as the source of detail and quotable
citations, while steering around three known Fabric GQL pitfalls (#112).
Instructions v1.5 additionally requires an explicit entity-id handoff: when
the Ontology tool resolves one or more entity ids for a query, those ids must
be passed to Azure AI Search as an `entity_ids` filter rather than re-derived
from free text, so the two tools stay grounded in the same entities.

**This feature is now query-ready.** A first live dev deploy on 2026-08-31
created the agent successfully (`agent_version: 1`) but surfaced two defects
once real queries were attempted (hardcoded Azure AI Search `query_type`,
and a Fabric Data Agent `ItemNotFound` at invocation time). Both were
diagnosed, fixed, and the agent was **live-redeployed and independently
re-verified on the same day**:

- Azure AI Search `query_type` hardcoding (#121, closed): the deploy path now
  auto-detects live vectorizer support on the target index and falls back to
  `semantic` search when none is present, with an explicit override still
  available in `agent-metadata.yaml`. **Verified live** — `agent_version: 2`
  answered 3 different test queries against the no-vectorizer
  `surface-tech-kg-chunks` index with no vectorizer errors, returning cited
  service-manual text.
- Fabric Data Agent `ItemNotFound` (#122, closed): root cause was a tool
  declaration typo, corrected directly in Fabric. Diagnosing this also
  surfaced a design gap — the agent had no explicit instruction to carry an
  Ontology-resolved entity id forward into the Search call — which is what
  instructions v1.5 (entity-id handoff, above) now closes.

Live verification evidence for `agent_version: 2` (instructions v1.5,
hash `081a17dd1e78d242`): a query for the `surface_component` labeled
"Motherboard Module" caused the Fabric Data Agent tool to resolve three
matching entity ids, and the subsequent Azure AI Search tool call carried
those exact ids as its `entity_ids` filter argument
(`entity:7808d3df822de53c0a2886bb42abeddf`, `entity:4a7ddb96f7387e3eecf2d7084ef0c742`,
`entity:1dc2bbefe326a5bcfe9b175afe168409`), with the final answer citing both
the ontology and search sources. This was confirmed by inspecting the live
tool-call argument trace, not just the final answer text.

Known limitation carried into 0.2.4: most `surface_device`/`surface_component`
properties beyond `id`/`label` (e.g. `model_id`) are not populated in this
release's dataset, so natural-language questions that depend on those
properties (e.g. "Surface Pro 10") may correctly resolve to "no data found"
rather than a hallucinated answer — this is a data-population gap, not a
routing or handoff defect.
