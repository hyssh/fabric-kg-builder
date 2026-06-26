# RESEARCH-001: Connecting a Knowledge Graph with Azure AI Search for Grounded Agents

Status: Research findings (cited)
Date: 2026-06-24T12:16:08.893-07:00
Author: Squad (research agent, reviewed by Coordinator)
Scope: Production best practices for combining the Fabric Ontology (knowledge graph) with an Azure AI Search index so a production agent can ground and enrich answers. Feeds the graph→search bridge in SPEC-002 (§11), SPEC-003 (§12), and SPEC-004 (§12).

> Architecture context: canonical data in Microsoft Fabric Lakehouse (Parquet) + a Fabric Ontology (entities/relationships); an optional Azure AI Search index over **text/visual chunks only** (not structured data). An agent runs a graph query (GQL) to find entities/relationships, then uses those results (entity IDs, canonical_keys, aliases) as a **second query/filter** against AI Search to retrieve grounding chunks, then composes a grounded answer.

---

## Executive Summary — Top Production Practices

1. **Adopt the "graph-then-search" two-phase retrieval pattern as the primary architecture.** Run a GQL traversal first to get a scoped set of entity IDs / canonical keys, then construct a filtered + hybrid AI Search query. This mirrors GraphRAG **Local Search** and CosmosAIGraph **OmniRAG** — structured precision (graph) + unstructured recall (vector/keyword). [graphrag local_search](https://microsoft.github.io/graphrag/query/local_search/)
2. **Hybrid search (vector + keyword) with semantic reranking is the minimum quality bar.** BM25 alone misses synonyms; vectors alone miss exact entity IDs/codes. RRF merges both; the semantic reranker adds an L2 pass over the top ~50. [hybrid-search-overview](https://learn.microsoft.com/en-us/azure/search/hybrid-search-overview)
3. **Pass entity ID lists from the graph using `search.in()`, not chained `or`/`eq`.** Sub-second response for hundreds–thousands of values; avoids request-size limits. [search.in()](https://learn.microsoft.com/en-us/azure/search/search-query-odata-search-in-function)
4. **Design chunk documents with entity-linkage fields:** `entity_ids` (filterable `Collection(Edm.String)`), `canonical_key` (filterable `Edm.String`), `entity_aliases` (searchable `Collection(Edm.String)`), `entity_types` (filterable+facetable), alongside `chunk_text` + vector. Repeat parent metadata on every child chunk via index projections. [index-projections](https://learn.microsoft.com/en-us/azure/search/search-how-to-define-index-projections)
5. **Use `vectorFilterMode: preFilter`** when combining graph-derived entity filters with vector queries — constrains HNSW traversal to graph-relevant docs before ANN. [vector-search-filters](https://learn.microsoft.com/en-us/azure/search/vector-search-filters)
6. **Production freshness:** OneLake indexer (with built-in change detection) for unstructured **Files**; Push API / Fabric Data Pipelines for structured/Parquet. ⚠️ The OneLake indexer does **not** support Parquet/Delta — structured entity data must be pushed via a custom pipeline. [onelake-files](https://learn.microsoft.com/en-us/azure/search/search-how-to-index-onelake-files)
7. **Consider Agentic Retrieval (GA, 2026-04-01 API)** for complex multi-turn agents — LLM subquery decomposition, parallel execution, structured responses with citations + activity log. Can serve as phase 2 of graph-then-search. [agentic-retrieval-overview](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview)
8. **Always return provenance** (chunk ID, source path, graph traversal path, entity IDs) with the answer to enable citations and prevent hallucination. [rag-overview](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview)

---

## 1. GraphRAG vs. Vector RAG vs. Hybrid

**Vector RAG** works for point lookups but fails at "connecting the dots" (synthesizing facts linked through shared entities) and holistic/summarization queries. Microsoft Research's GraphRAG showed baseline RAG returning *"the text does not provide specific information"* where graph traversal produced a detailed cited answer. [research blog](https://www.microsoft.com/en-us/research/blog/graphrag-unlocking-llm-discovery-on-narrative-private-data/) · [arxiv 2404.16130](https://arxiv.org/abs/2404.16130)

**GraphRAG query modes:** Local (specific entity questions), Global (holistic/thematic), DRIFT (balanced depth+breadth), Basic (vector fallback). [drift_search](https://microsoft.github.io/graphrag/query/drift_search/)

**Recommendation for our architecture:** We already have a structured ontology in Fabric — we do **not** need to LLM-extract a graph from text (as GraphRAG does). Instead:
- **Phase 1:** GQL traversal for the graph step (entity match, hop traversal, relationship filtering) — faster and more precise than LLM-extracted graphs.
- **Phase 2:** AI Search for text/visual grounding (hybrid vector+keyword with entity filter).
- **Holistic queries:** optionally store community/cluster summaries as separate "summary documents" in AI Search.

This is the CosmosAIGraph **OmniRAG** pattern: use all data sources in their original format, minimizing movement. [CosmosAIGraph](https://github.com/AzureCosmosDB/CosmosAIGraph)

---

## 2. The "Graph-then-Search" Two-Phase Pattern

GraphRAG **Local Search** is the reference: the graph step identifies *access points* (entities) into the knowledge structure, and those access points gate retrieval of raw text. [local_search](https://microsoft.github.io/graphrag/query/local_search/)

### Filter vs. query term — when to use each

| Scenario | Filter | Query term |
|---|---|---|
| Entity ID / canonical key lookup | ✅ `search.in(entity_ids, …)` | ❌ (not text-searchable) |
| Entity alias / alternate name | ❌ | ✅ add to `search` param |
| Security / access trimming | ✅ | ❌ |
| Large entity set (>50 IDs) | ✅ `search.in()` | ❌ hits `or`-clause limit |
| Partial / fuzzy names | ❌ | ✅ keyword/vector |

`search.in()` gives sub-second response for hundreds–thousands of values and avoids the `eq`/`or` performance cliff and request-size limits (16 MB POST / 8 KB GET). [search.in()](https://learn.microsoft.com/en-us/azure/search/search-query-odata-search-in-function) · [filters](https://learn.microsoft.com/en-us/azure/search/search-filters)

---

## 3. Azure AI Search Features for Grounding Quality

| Feature | How it improves grounding | When |
|---|---|---|
| Hybrid (BM25 + vector + RRF) | Term precision + semantic recall | Always |
| Semantic ranker | L2 rerank + captions + query rewrite | Always (standard tier+); feed `k=50` |
| Agentic retrieval | LLM subquery decomposition, parallel exec, citations | Multi-turn agents; adds latency; GA 2026-04-01 |
| `preFilter` | Restricts HNSW to graph-relevant docs before ANN | Selective, well-defined entity filters |
| Scoring profiles | Boost matches in entity-title field over body | Boost alias/title matches |
| Faceting | Enumerate entity types / content types | Query understanding, summarization |
| `select` trimming | Return only grounding fields; reduce tokens | Always |

Semantic ranker = L2 re-ranking over the top 50 (Bing-derived models) + verbatim captions/highlights + query rewrite (up to 10 variants). Captions reduce prompt length by extracting the answering passage. [semantic-search-overview](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview)

---

## 4. Index Design to Support Entity Linkage

### Recommended chunk-document field attributes

| Field | searchable | filterable | retrievable | facetable | Type |
|---|---|---|---|---|---|
| `chunk_id` (key) | ❌ | ✅ | ✅ | ❌ | `Edm.String` |
| `parent_doc_id` | ❌ | ✅ | ✅ | ❌ | `Edm.String` |
| `entity_ids` | ❌ | ✅ | ✅ | ❌ | `Collection(Edm.String)` |
| `canonical_key` | ❌ | ✅ | ✅ | ❌ | `Edm.String` |
| `entity_aliases` | ✅ | ❌ | ✅ | ❌ | `Collection(Edm.String)` |
| `entity_types` | ❌ | ✅ | ✅ | ✅ | `Collection(Edm.String)` |
| `chunk_text` | ✅ | ❌ | ✅ | ❌ | `Edm.String` |
| `chunk_vector` | (vector) | ❌ | ❌ | ❌ | `Collection(Edm.Single)` |
| `source_path` | ❌ | ✅ | ✅ | ❌ | `Edm.String` |
| `graph_path` | ❌ | ❌ | ✅ | ❌ | `Edm.String` |
| `blob_url` | ❌ | ✅ | ✅ | ❌ | `Edm.String` |
| `last_modified` | ❌ | ✅ | ✅ | ❌ | `Edm.DateTimeOffset` |
| `content_type` | ❌ | ✅ | ✅ | ✅ | `Edm.String` |

Key decisions:
- `entity_ids`: filterable, not searchable — opaque IDs, exact-match filter; collection allows multiple entities per chunk.
- `entity_aliases`: searchable, not filterable — human-readable names for BM25/keyword matching.
- `canonical_key`: filterable single key for the primary entity a chunk represents.
- `graph_path`: serialized traversal (e.g., `"PartNumber --[identifies]--> Part --[has_part]--> Component"`) passed to the LLM for citation.
- Set `filterable: false` on fields you won't filter (index-size cost).

Use **index projections** for one-to-many chunking (single index, parent fields repeated per chunk, `projectionMode: skipIndexingParentDocuments`). [index-projections](https://learn.microsoft.com/en-us/azure/search/search-how-to-define-index-projections)

Semantic config: treat `entity_aliases` as the title field, `entity_types` as keywords, `chunk_text` as content. Scoring profile: boost `entity_aliases` (e.g., weight 5) over `chunk_text` (weight 1). [scoring-profiles](https://learn.microsoft.com/en-us/azure/search/index-add-scoring-profiles)

---

## 5. Keeping KG and Index Consistent

| Approach | Freshness | Best for |
|---|---|---|
| OneLake indexer + schedule | minutes (5-min min) | Unstructured Files in Lakehouse |
| Push API (real-time) | seconds | Entity metadata / ontology changes |
| Fabric Data Pipeline → Push | near real-time | Structured/Parquet (OneLake indexer can't read Parquet) |
| Eventstream → Function → Push | seconds | High-frequency entity updates |

⚠️ OneLake indexer **does not support Parquet/Delta** — only the Files location. Structured entity metadata must be pushed via a custom pipeline or converted to JSON/CSV. [onelake-files](https://learn.microsoft.com/en-us/azure/search/search-how-to-index-onelake-files)

**Sync on entity change:** KG change event → re-query affected entity's linked documents → partial-update `entity_ids`/`canonical_key`/`entity_aliases` via Push API (merge) → re-chunk/re-embed only if canonical text changed materially. On entity deletion, remove the entity_id from `entity_ids` of linked chunks; do not delete chunks unless the source doc is removed. Use the **enrichment cache** to avoid re-embedding unchanged chunks. [enrichment cache](https://learn.microsoft.com/en-us/azure/search/cognitive-search-concept-intro)

---

## 6. Citations, Provenance, Hallucination Prevention

Carry on every chunk: `chunk_id`, `source_path`, `parent_doc_id`, `entity_ids`, `entity_aliases`, `graph_path`, `blob_url`. Pass provenance to the LLM as numbered structured sources and instruct: *"Answer using only the provided sources; cite [1],[2]; if not derivable, say so."*

Confidence signals: reranker score threshold (drop `@search.rerankerScore` < ~1.5), shorter graph path = higher relevance, dual-source corroboration (assert only if in **both** a graph relationship and a text chunk), qualifier wording when confidence is moderate. Agentic retrieval provides built-in citation tracking + activity log. [rag-overview](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview)

---

## 7. Pitfalls / Anti-Patterns

| Pitfall | Risk | Mitigation |
|---|---|---|
| Treating retrieval rows as graph edges | Co-occurrence ≠ relationship; misses multi-hop | Use the real graph (GQL) for relationships; AI Search only for evidence |
| Label vs. ID confusion | Labels change / non-unique | Filter on immutable entity IDs; keep names in `entity_aliases` |
| Over-fetching from the graph | Hundreds of IDs → filter size limits | Cap GQL to top 10–20 ranked entities, scope by rel type + hops |
| `or`-chaining ID filters | Perf cliff, size limits | Use `search.in()` |
| Pure vector only | Misses IDs, codes, jargon | Always hybrid + `queryType: semantic` |
| graphrag-accelerator as base | **Deprecated/archived** | Use `microsoft/graphrag` directly |
| Indexing Parquet via OneLake indexer | Unsupported format | Push API / convert to JSON-CSV in Files |
| Omitting `vectorFilterMode` | Wasteful postFilter, false negatives | Use `preFilter` (index created after 2023-10-15) |
| No provenance in `select` | LLM can't cite | Include `graph_path`, `source_path`, `entity_ids` |
| No fallback to pure search | Zero results when entity not in graph | Filter-optional fallback |

---

## 8. Recommended Pattern for Our Architecture

```
USER QUERY
   |
 AGENT (Azure AI Foundry)
   |  Phase 1: Graph Query (GQL over Fabric Ontology)
   v  -> entity_ids[]  -> canonical_keys[]  -> aliases[]  -> graph_path
 Fabric Ontology
   |  Phase 2: Construct AI Search query
   v  hybrid: BM25 on aliases + vector on text + filter on IDs + semantic rerank
 Azure AI Search (text/visual chunks only)
   |  grounding chunks + captions + citations
   v
 LLM (Foundry) -> GROUNDED ANSWER + CITATIONS
```

**Step 1 — GQL traversal** returns scoped `entity_ids`, `canonical_key`, `aliases`, `graph_path` (cap ~20).

**Step 2 — Build filter:** `search.in(entity_ids, 'uuid-1,uuid-2,…', ',')`; build `alias_query` from top 3 aliases.

**Step 3 — Hybrid + semantic query** (`api-version=2026-04-01`):
```jsonc
{
  "search": "<<user query>> <<alias_query>>",
  "vectorQueries": [{ "kind": "text", "text": "<<user query>>", "fields": "chunk_vector", "k": 50 }],
  "filter": "search.in(entity_ids, 'uuid-1,uuid-2,uuid-3', ',')",
  "vectorFilterMode": "preFilter",
  "queryType": "semantic",
  "semanticConfiguration": "fabric-semantic-config",
  "scoringProfile": "entity-boost",
  "select": "chunk_id, chunk_text, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path, last_modified",
  "top": 5,
  "captions": "extractive",
  "answers": "extractive|count-1"
}
```

**Step 4 — Fallback:** when GQL returns 0 entities, run pure hybrid search without the filter.

**Step 5 — Compose grounded prompt** with GRAPH CONTEXT (traversal path, entities) + numbered GROUNDING SOURCES (source_path, entity, caption, text) and a strict cite-or-abstain instruction.

**Step 6 — Multi-turn:** switch to Agentic Retrieval (knowledge base over the index; pass conversation history; receives decomposed subqueries + citations). [get-started-agentic-retrieval](https://learn.microsoft.com/en-us/azure/search/search-get-started-agentic-retrieval)

---

## Reference Implementations & Docs

| Resource | Status | Relevance |
|---|---|---|
| [microsoft/graphrag](https://github.com/microsoft/graphrag) | Active | KG extraction + local/global/DRIFT search; study local search |
| [AzureCosmosDB/CosmosAIGraph](https://github.com/AzureCosmosDB/CosmosAIGraph) | Active | OmniRAG: intent -> KG/vector/hybrid routing |
| [Azure-Samples/azure-search-openai-demo](https://github.com/Azure-Samples/azure-search-openai-demo) | Active | Reference RAG app: retrieval + citation patterns |
| [azure-search-dotnet-samples/quickstart-agentic-retrieval](https://github.com/Azure-Samples/azure-search-dotnet-samples/tree/main/quickstart-agentic-retrieval) | Active | Agentic retrieval quickstart |
| [Azure/azure-search-vector-samples](https://github.com/Azure/azure-search-vector-samples) | Active | Vector + filter combinations |
| [Azure-Samples/graphrag-accelerator](https://github.com/Azure-Samples/graphrag-accelerator) | Deprecated | Do not use as a base |

Key Microsoft Learn docs: [RAG overview](https://learn.microsoft.com/en-us/azure/search/retrieval-augmented-generation-overview) · [Agentic retrieval](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview) · [Hybrid search](https://learn.microsoft.com/en-us/azure/search/hybrid-search-overview) · [Filters](https://learn.microsoft.com/en-us/azure/search/search-filters) · [search.in()](https://learn.microsoft.com/en-us/azure/search/search-query-odata-search-in-function) · [Vector filters](https://learn.microsoft.com/en-us/azure/search/vector-search-filters) · [Semantic ranking](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview) · [Scoring profiles](https://learn.microsoft.com/en-us/azure/search/index-add-scoring-profiles) · [Index projections](https://learn.microsoft.com/en-us/azure/search/search-how-to-define-index-projections) · [OneLake indexer](https://learn.microsoft.com/en-us/azure/search/search-how-to-index-onelake-files) · [Integrated vectorization](https://learn.microsoft.com/en-us/azure/search/vector-search-integrated-vectorization) · [GraphRAG docs](https://microsoft.github.io/graphrag/)
