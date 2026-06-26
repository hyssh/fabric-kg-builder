# DESIGN-001: Retrieval Agent Orchestration — Query-Time Grounded Answers

**Status:** Draft
**Date:** 2026-06-24T12:42:17.255-07:00
**Author:** Keyser (Lead / Architect)
**Requested by:** Hyunsuk Shin
**Scope:** Query-time runtime agent — NOT the build-time enrichment pipeline
**Feeds:** SPEC-004 §12 (two-phase retrieval), SPEC-003 §12 (graph-to-search bridge), RESEARCH-001 (production patterns), INFRA-001 (dev resources)

---

## 0. The Question

> "To support 'pass graph results as search.in(entity_ids,...) filters, not or-chains; filter on IDs, search on aliases' — do we BUILD a custom agent with predefined code on Microsoft Foundry, or is it handled by MCP / APIs as tools of an agent? How much effort / code?"

**Short answer:** Build a **Foundry Agent + a thin deterministic "retrieve" tool/function we own**. The LLM decides *when* to call the tool. The tool builds the OData/`search.in` filter from validated graph output — the LLM never hand-authors filters. Total effort is **small** (~400–600 lines of application code, plus config). Details below.

---

## 1. Architecture Decision Record (ADR)

### ADR-DESIGN-001: Retrieval Agent Architecture

**Status:** Proposed
**Date:** 2026-06-24T12:42:17.255-07:00
**Decision Makers:** Keyser, Hyunsuk

#### Context

The fabric-kg-builder pipeline produces:
- Canonical Parquet tables → Fabric Lakehouse (structured, queryable via GQL)
- Text/visual chunks → Azure AI Search index (unstructured, hybrid search)

At query time, a user asks a natural-language question. The agent must:
1. Traverse the Fabric Ontology graph (GQL) to find relevant entities
2. Build a deterministic `search.in()` filter from those entity IDs
3. Run a hybrid+semantic AI Search query with that filter
4. Compose a grounded, cited answer

The core concern: **who builds the OData filter?** The LLM must not.

#### Decision

**RECOMMENDED: Option A — Foundry Agent Service + custom function tool**

The retrieval agent is a Foundry Agent with a single registered function tool (`retrieve_grounding`) that encapsulates the graph-then-search pipeline. The LLM orchestrates *when* to call the tool and how to use its output; deterministic code builds the filter.

#### Options Evaluated

| Option | Description | Effort | Pros | Cons | When it wins |
|--------|-------------|--------|------|------|--------------|
| **A. Foundry Agent + custom function tool** (RECOMMENDED) | Register `retrieve_grounding` as a Foundry function tool. Agent calls it with NL query; tool returns grounded chunks + provenance. | **S–M** (~400–600 LOC + config) | Lowest integration effort; Foundry handles agent loop, auth, conversation; deterministic filter; single deployment | Tied to Foundry; tool is in-process | You're already on Foundry (we are); single agent; fastest time-to-value |
| **B. Azure AI Search Agentic Retrieval** | Use AI Search's built-in agentic retrieval (GA 2026-04-01) as the Phase-2 engine; let it decompose subqueries. | **S** (mostly config) | Least code; built-in citation tracking; multi-turn subquery decomposition | Does NOT do graph traversal — still need a graph tool for Phase 1; less control over filter construction | Phase 2 only; use as upgrade after Option A is working; good for multi-turn |
| **C. MCP server** | Expose `graph_query` and `search_grounding` as MCP tools via a standalone server (FastAPI/Express). | **M** (~600–900 LOC + infra) | Language-agnostic; reusable across agents/clients; decoupled lifecycle | More infra to host/monitor; extra network hop latency; auth boundary | Multiple agents or non-Foundry clients need the same tools; polyglot teams |
| **D. Fully custom orchestration** | Own agent loop (LangChain / LlamaIndex / raw loop). | **L** (~1500+ LOC) | Maximum flexibility; no platform dependency | Most code; own the retry/streaming/memory/auth plumbing; maintenance burden | Not on Foundry; need exotic orchestration patterns |

#### Consequences

- Option A is the starting point. The tool is a Python function registered with Foundry Agent Service.
- Option B (Agentic Retrieval) can be layered as the Phase-2 engine behind the same tool later.
- Option C (MCP) is a graduation path if multiple agents need these tools — wrap the same function as an MCP server.
- Option D is rejected unless we leave Foundry.

---

## 2. Build vs. Platform Breakdown

| Component | Build or Platform | Effort | Key Risk |
|-----------|-------------------|--------|----------|
| **Foundry Agent Service** (agent loop, conversation, model calls) | Platform | Config | SDK version churn |
| **Fabric GQL client wrapper** (traverse ontology, return entity set) | Build | **S** (~80–120 LOC) | GQL API surface; auth (DefaultAzureCredential) |
| **`search.in` filter + hybrid query builder** (the core concern) | Build | **S** (~60–100 LOC) | Must sanitize entity IDs; cap filter size; handle 0-entity fallback |
| **`azure-search-documents` call wrapper** (execute hybrid+semantic query) | Build | **S** (~80–120 LOC) | SDK version; semantic config must exist on index |
| **Grounded-answer assembler** (format graph context + sources into prompt) | Build | **S** (~80–120 LOC) | Token budget management; caption vs. full-text selection |
| **`retrieve_grounding` tool function** (orchestrates the above 4) | Build | **S** (~60–80 LOC) | Error handling; timeout; observability |
| **Agent registration / system prompt / config** | Config | **S** (YAML/JSON) | Foundry SDK config shape may change |

**Total build code:** ~400–600 lines of Python (excluding tests).
**Total config:** Agent definition YAML + system prompt text + env vars already in `.env`.

---

## 3. Retrieve Tool Contract

### 3.1 Tool / Function JSON Schema (what the agent sees)

```json
{
  "type": "function",
  "function": {
    "name": "retrieve_grounding",
    "description": "Retrieve grounding evidence for a user question by traversing the knowledge graph and searching the document index. Returns cited text chunks with provenance. Call this tool whenever the user asks a factual question about the knowledge domain.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "The user's natural-language question or information need."
        },
        "entity_hints": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Optional entity names or IDs the user mentioned explicitly. Helps scope the graph traversal."
        },
        "conversation_summary": {
          "type": "string",
          "description": "Optional summary of prior conversation turns for context continuity."
        }
      },
      "required": ["query"]
    }
  }
}
```

### 3.2 Internal Steps (deterministic — no LLM in this path)

```
retrieve_grounding(query, entity_hints?, conversation_summary?)
  │
  ├─ 1. GQL Traverse (bounded: max 2 hops, max 20 entities)
  │     Input:  query text, entity_hints
  │     Action: Match entities by canonical_key / alias lookup in Fabric Ontology
  │             Traverse evidenced_by / shown_in / indexed_as edges
  │     Output: entity_ids[], canonical_keys[], aliases[], graph_paths[]
  │
  ├─ 2. Build search.in filter + alias query terms
  │     Input:  entity_ids[] from step 1
  │     Action: Validate IDs (alphanumeric+hyphen only, cap at 20)
  │             Construct: search.in(entity_ids, 'id1,id2,...', ',')
  │             Extract top 3 aliases as keyword query terms
  │     Output: odata_filter string, alias_query string
  │
  ├─ 3. Hybrid + Semantic AI Search (preFilter)
  │     Input:  query, alias_query, odata_filter
  │     Action: POST to AI Search with hybrid (BM25+vector), semantic reranker,
  │             vectorFilterMode=preFilter, top=5, captions=extractive
  │     Fallback: If entity_ids is empty, omit filter (pure hybrid)
  │     Output: search_results[] with chunk_text, source_path, graph_path, etc.
  │
  └─ 4. Return grounded chunks + provenance
        Output: { sources: [...], graph_context: {...}, fallback_used: bool }
```

### 3.3 Return Schema

```json
{
  "sources": [
    {
      "index": 1,
      "chunk_id": "chunk:abc123",
      "chunk_text": "The battery pack contains...",
      "caption": "Battery pack contains three cell modules...",
      "source_path": "docs/surface-pro-service-guide.pdf",
      "blob_url": "https://examplestorageacct.blob.core.windows.net/...",
      "entity_ids": ["ent:uuid-1", "ent:uuid-2"],
      "canonical_key": "component:battery-pack",
      "graph_path": "Part \"Battery Pack\" --[evidenced_by]--> DocumentChunk \"chunk:abc123\"",
      "reranker_score": 3.21
    }
  ],
  "graph_context": {
    "entities": [
      { "entity_id": "ent:uuid-1", "label": "Battery Pack", "type": "Component" }
    ],
    "relationships": [
      { "source": "PartNumber BP-4200", "relation": "identifies", "target": "Battery Pack" }
    ],
    "traversal_path": "PartNumber \"BP-4200\" --[identifies]--> Part \"Battery Pack\" --[evidenced_by]--> DocumentChunk"
  },
  "fallback_used": false,
  "entity_count": 3
}
```

### 3.4 Python Pseudo-Code Skeleton (Foundry SDK Style)

> ⚠️ **Foundry SDK specifics marked `# VERIFY AGAINST CURRENT SDK`** — API shape may have changed since this writing.

```python
"""
retrieve_grounding tool — Foundry Agent function tool.
Deterministic graph-then-search pipeline. No LLM in this path.
"""
import re
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizableTextQuery

# VERIFY AGAINST CURRENT SDK: Foundry agent tool registration
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FunctionTool, ToolSet

# -- Constants --
MAX_ENTITIES = 20
MAX_HOPS = 2
SEARCH_TOP = 5
RERANKER_K = 50
ID_PATTERN = re.compile(r'^[a-zA-Z0-9:_-]+$')  # allowed ID chars


def _traverse_graph(query: str, entity_hints: list[str] | None,
                    gql_client) -> dict:
    """Phase 1: Bounded GQL traversal over Fabric Ontology."""
    # Match entities by alias/canonical_key lookup
    # Traverse evidenced_by, shown_in, indexed_as (max MAX_HOPS hops)
    # Cap results to MAX_ENTITIES ranked by traversal distance
    # Returns: { entity_ids, canonical_keys, aliases, graph_paths, relationships }
    ...  # GQL query execution — implementation depends on Fabric GQL client


def _validate_ids(entity_ids: list[str]) -> list[str]:
    """Sanitize entity IDs — only allow safe characters, cap count."""
    safe = [eid for eid in entity_ids if ID_PATTERN.match(eid)]
    return safe[:MAX_ENTITIES]


def _build_filter_and_query(graph_result: dict) -> tuple[str | None, str]:
    """Build deterministic OData filter + alias query terms."""
    ids = _validate_ids(graph_result.get("entity_ids", []))
    aliases = graph_result.get("aliases", [])[:3]
    alias_query = " ".join(aliases)

    if not ids:
        return None, alias_query  # fallback: no filter

    # search.in — safe because IDs are validated above
    id_list = ",".join(ids)
    odata_filter = f"search.in(entity_ids, '{id_list}', ',')"
    return odata_filter, alias_query


def _search_chunks(query: str, alias_query: str,
                   odata_filter: str | None,
                   search_client: SearchClient) -> list[dict]:
    """Phase 2: Hybrid + semantic AI Search with preFilter."""
    search_text = f"{query} {alias_query}".strip()
    vector_query = VectorizableTextQuery(
        text=query, k_nearest_neighbors=RERANKER_K, fields="chunk_vector"
    )
    kwargs = dict(
        search_text=search_text,
        vector_queries=[vector_query],
        query_type="semantic",
        semantic_configuration_name="fabric-semantic-config",
        scoring_profile="entity-boost",
        select=["chunk_id", "chunk_text", "source_path", "blob_url",
                "entity_ids", "canonical_key", "entity_aliases",
                "graph_path", "last_modified"],
        top=SEARCH_TOP,
        query_caption="extractive",
        query_answer="extractive|count-1",
    )
    if odata_filter:
        kwargs["filter"] = odata_filter
        kwargs["vector_filter_mode"] = "preFilter"

    results = search_client.search(**kwargs)
    return [_format_source(i + 1, r) for i, r in enumerate(results)]


def _format_source(index: int, result) -> dict:
    """Format a single search result into the source schema."""
    return {
        "index": index,
        "chunk_id": result["chunk_id"],
        "chunk_text": result["chunk_text"],
        "caption": (result.get("@search.captions", [{}])[0]
                    .get("text") if result.get("@search.captions") else None),
        "source_path": result["source_path"],
        "blob_url": result.get("blob_url"),
        "entity_ids": result.get("entity_ids", []),
        "canonical_key": result.get("canonical_key"),
        "graph_path": result.get("graph_path"),
        "reranker_score": result.get("@search.reranker_score"),
    }


def retrieve_grounding(query: str,
                       entity_hints: list[str] | None = None,
                       conversation_summary: str | None = None) -> dict:
    """
    Main tool entry point — called by the Foundry Agent.
    Deterministic: no LLM calls inside this function.
    """
    # -- Init clients (in production, these are injected / cached) --
    credential = DefaultAzureCredential()
    # gql_client = FabricGqlClient(credential, workspace_id=...) # TODO
    search_client = SearchClient(
        endpoint="https://example-search.search.windows.net",  # from env
        index_name="fabric-kg-chunks",                      # from config
        credential=credential,
    )

    # Phase 1: Graph traversal
    graph_result = _traverse_graph(query, entity_hints, gql_client=None)

    # Phase 2: Build filter (deterministic) + search
    odata_filter, alias_query = _build_filter_and_query(graph_result)
    sources = _search_chunks(query, alias_query, odata_filter, search_client)

    return {
        "sources": sources,
        "graph_context": {
            "entities": graph_result.get("entities", []),
            "relationships": graph_result.get("relationships", []),
            "traversal_path": graph_result.get("graph_paths", [""])[0],
        },
        "fallback_used": odata_filter is None,
        "entity_count": len(graph_result.get("entity_ids", [])),
    }


# -- Agent Registration (VERIFY AGAINST CURRENT SDK) --
def create_agent():
    """Register the retrieval agent with Foundry Agent Service."""
    project_client = AIProjectClient(
        credential=DefaultAzureCredential(),
        endpoint="https://example-aiservices.services.ai.azure.com",
        project_name="example-project",
    )

    # Register tool
    tools = ToolSet()
    tools.add(FunctionTool(functions=[retrieve_grounding]))

    agent = project_client.agents.create_agent(
        model="gpt-4.1",  # interim dev; target: gpt-5.5-mini
        name="fabric-kg-retrieval-agent",
        instructions=SYSTEM_PROMPT,
        toolset=tools,
    )
    return agent


SYSTEM_PROMPT = """You are a knowledge-grounded assistant for the Fabric KG.
When the user asks a factual question, call the retrieve_grounding tool.
Use the returned sources to answer. Cite each claim as [1], [2], etc.
If the sources don't contain the answer, say "I don't have enough information."
Do not invent facts. Do not infer relationships not stated in the sources."""
```

---

## 4. Why Deterministic Filters, Not LLM-Generated

| Risk | What goes wrong if the LLM writes filters | Our mitigation |
|------|-------------------------------------------|----------------|
| **OData / filter injection** | LLM could produce `entity_ids eq '' or 1 eq 1` — data exfiltration or unscoped queries | Tool builds filter from validated, sanitized IDs only; regex-checked (`^[a-zA-Z0-9:_-]+$`) |
| **Malformed `search.in`** | Missing quotes, wrong delimiter, unescaped commas → 400 error or silent wrong results | Deterministic string template: `search.in(entity_ids, '{id_list}', ',')` — one code path, always correct |
| **Label-vs-ID confusion** | LLM might use a display name ("Battery Pack") instead of the opaque ID ("ent:abc123") as a filter value | Tool receives only opaque IDs from the validated graph output; labels go into the `search` text param as aliases |
| **Filter size explosion** | LLM might dump all known entities into the filter string | Hard cap: `MAX_ENTITIES = 20`; validated at the code level |
| **Inconsistent query structure** | LLM might forget `vectorFilterMode: preFilter`, omit `queryType: semantic`, etc. | All query parameters are hardcoded constants in the tool; LLM never touches the request body |

**Principle:** The LLM supplies the natural-language query and decides *when* to call the tool. The tool builds the filter from validated graph output using stable IDs only. The LLM never sees or constructs OData syntax.

---

## 5. MCP vs. In-Process Tool

| Dimension | In-Process Foundry Function Tool | MCP Server |
|-----------|----------------------------------|------------|
| **Deployment** | Runs inside the Foundry agent process | Standalone server (FastAPI / Express) — separate deploy, scaling, monitoring |
| **Latency** | Lowest (same process) | +1 network hop per call |
| **Auth** | Inherits agent's credential chain | Needs its own auth boundary (API key / managed identity) |
| **Reusability** | Single agent only | Any MCP-compatible agent or client (VS Code, other Foundry agents, custom UIs) |
| **Language** | Python (must match agent runtime) | Any (MCP is protocol-level) |
| **Effort** | **S** (~50 LOC wrapper) | **M** (~200 LOC server + transport + health checks + deploy config) |
| **When to use** | Single agent, fastest path, this is the starting point | Multiple agents share the same tools; polyglot clients; separate scaling needed |

### Recommendation

**Start in-process.** The `retrieve_grounding` function is a regular Python function registered with Foundry's `FunctionTool`. This is the lowest-effort path for a single agent.

**Graduate to MCP if:**
- A second agent (e.g., a different domain or a VS Code extension) needs the same graph+search tools.
- The tool needs independent scaling or a different auth boundary.
- The team wants language-agnostic tool access.

The function's contract (§3.1 JSON schema, §3.3 return schema) is already MCP-compatible — wrapping it in an MCP server is mechanical (~200 LOC of FastAPI + SSE transport).

---

## 6. Effort Estimate

### 6.1 Module Breakdown

| Module | File(s) | LOC Range | Complexity | Notes |
|--------|---------|-----------|------------|-------|
| **Graph client wrapper** | `src/fabric_kg_builder/query/graph_client.py` | 80–120 | Low–Medium | GQL query execution; depends on Fabric GQL API surface; `DefaultAzureCredential` auth |
| **Filter builder** | `src/fabric_kg_builder/query/filter_builder.py` | 60–100 | Low | `search.in()` construction; ID validation; alias extraction; deterministic, well-tested |
| **Search client wrapper** | `src/fabric_kg_builder/query/search_client.py` | 80–120 | Low | `azure-search-documents` hybrid+semantic call; thin wrapper over SDK |
| **Retrieve tool** | `src/fabric_kg_builder/query/retrieve_tool.py` | 60–80 | Low | Orchestrates graph→filter→search→format; the glue |
| **Answer assembler** | `src/fabric_kg_builder/query/answer_assembler.py` | 80–120 | Medium | Formats graph context + sources into prompt; token budget management; caption selection |
| **Agent config** | `config/agent.yaml` + system prompt | ~30 lines YAML + ~10 lines prompt | Config | Foundry agent registration; model selection; tool binding |
| **CLI `query` command** | `src/fabric_kg_builder/cli/query.py` | 40–60 | Low | `fabric-kg query "question"` — invokes retrieve tool, prints grounded answer (dev/prototype) |
| **Tests** | `tests/query/test_*.py` | 200–300 | Medium | Unit tests for filter builder, integration tests with mocked search/graph |

### 6.2 Summary

| Metric | Value |
|--------|-------|
| **Application code** | ~400–600 LOC |
| **Test code** | ~200–300 LOC |
| **Config** | ~40 lines (YAML + prompt) |
| **New dependencies** | None — `azure-search-documents` and `azure-ai-projects` already in INFRA-001 |
| **Overall effort** | **Small** — roughly 1 developer-sprint of focused work |
| **What is code** | Graph client, filter builder, search wrapper, retrieve tool, answer assembler, CLI command |
| **What is config** | Agent YAML, system prompt, env vars (already in `.env`) |

### 6.3 Minimal-Viable First Slice (Prototype)

The smallest useful end-to-end slice against the dev resources in INFRA-001:

```
CLI: fabric-kg query "What components are in the battery pack?"
  → graph_client: GQL against Fabric workspace 9802a28a
  → filter_builder: search.in(entity_ids, '...', ',')
  → search_client: hybrid query against example-search
  → answer_assembler: format sources
  → stdout: grounded answer with citations
```

| Dev resource (INFRA-001) | Used for |
|--------------------------|----------|
| `example-aiservices` / `example-project` | Foundry chat model (gpt-4.1 interim) |
| `example-search` (swedencentral) | AI Search hybrid+semantic query |
| Fabric workspace `9802a28a` | GQL ontology traversal |
| `DefaultAzureCredential` (az login) | Auth for all services |

This prototype requires:
1. At least one entity and chunk indexed in `example-search`
2. An ontology deployed with `evidenced_by` edges
3. The `fabric-kg query` CLI command wired up

---

## 7. Relation to MVP

### What the MVP delivers (PRD)

The MVP pipeline is: CSV → Parquet → Ontology → Fabric Lakehouse.
Query-time grounding is **post-MVP** — it consumes the data the MVP produces but is not part of the MVP milestone.

### What this design reserves

This design reserves the **tool contract** (`retrieve_grounding` function signature, return schema, filter construction rules) so that:

1. The entity model (SPEC-002) already includes `entity_id`, `canonical_key`, `aliases` — the fields this tool filters/queries on.
2. The ontology (SPEC-003 §12) already defines `evidenced_by`, `shown_in`, `indexed_as` bridge relationships — the edges this tool traverses.
3. The AI Search index schema (RESEARCH-001 §4) already specifies `entity_ids` (filterable Collection), `entity_aliases` (searchable Collection) — the fields this tool's filter and query hit.
4. The grounding prompt contract (SPEC-004 §12.6) already defines the GRAPH CONTEXT + SOURCES format — what this tool's output feeds.

**No existing specs need editing.** The data model already supports query-time retrieval. This design is additive.

### Smallest prototype slice

A standalone `fabric-kg query` CLI command that runs the retrieve tool against the dev resources and prints a grounded answer to stdout. No agent registration needed for the prototype — just the deterministic graph→filter→search pipeline with a direct chat completion call at the end.

**Estimated prototype effort:** 2–3 days for a developer familiar with the codebase and Azure SDKs.

---

## Appendix A: Glossary

| Term | Meaning |
|------|---------|
| **Foundry Agent Service** | Azure AI Foundry's managed agent runtime — handles conversation loop, tool dispatch, model calls |
| **Function tool** | A Python function registered with the Foundry agent; called when the LLM decides to use it |
| **MCP** | Model Context Protocol — open standard for exposing tools to LLM agents over a network |
| **`search.in()`** | OData function for filtering on large value lists — sub-second for hundreds of values |
| **GQL** | GraphQL-like query language for Fabric Ontology traversal |
| **preFilter** | AI Search vector filter mode — constrains HNSW traversal to matching docs before ANN |
| **Agentic Retrieval** | AI Search GA feature (2026-04-01) — LLM subquery decomposition + citations |

## Appendix B: Related Documents

| Document | Relationship |
|----------|-------------|
| RESEARCH-001 | Production patterns for graph+search grounding (source material) |
| SPEC-003 §12 | Ontology bridge relationships and bounded traversal rules |
| SPEC-004 §12 | Two-phase retrieval algorithm and grounded-answer prompt |
| INFRA-001 | Dev resource inventory (example-aiservices, example-search, workspace) |
| PRD §6 | Data contract: canonical Parquet is source of truth |
