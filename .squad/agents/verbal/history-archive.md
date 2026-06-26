# Verbal — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** AI Integration Dev
- **Joined:** 2026-06-24T17:38:25.163Z

## Learnings

<!-- Append learnings below -->

### 2026-06-24T20:07:00-07:00 — Three Surface PDF live-run bugs: UTF-8, entities=0, chunk leniency

**Bug 1 — UnicodeEncodeError (exit 4) on Windows cp1252 console.**
- Root cause: `click.echo(f"[enrich] enriched {name} → {out_dir}")` in `enrich_cmd.py` uses the literal → (U+2192). Python's `sys.stdout.write()` on a cp1252-encoded Windows console raises `UnicodeEncodeError`. This is caught by the per-file `try/except`, increments `errors`, and produces `ctx.exit(4)`.
- Fix: Extracted `_configure_utf8_console()` into `cli/main.py`. Called from `main()` before `cli()`. Uses `stream.reconfigure(encoding='utf-8', errors='replace')` guarded by `sys.platform == 'win32'` and `hasattr(stream, 'reconfigure')`. `errors='replace'` ensures any remaining unencodable chars become '?' rather than crashing.
- Also removed `→` from the `_log.info(...)` format in `orchestrator.py` (changed to `—`) to avoid the same crash in logging output.

**Bug 2 — entities=0 / relationships=0 in canonical JSON.**
- Root cause: `enrich_documents` used to concatenate ALL document elements into ONE giant LLM call. When the LLM returned chunks with missing `chunk_type`/`content`, pydantic `ValidationError` aborted the entire pass (entities were never added to `all_records`).
- Fix: Refactored `enrich_documents` to **batch by section_path**. Groups elements into `defaultdict(list)` keyed by `section_path or "__root__"`. Calls `enrich_batch` once per section via a `section_batch_key = f"{source_file_id}:section:{section_key}"` so each section has an independent checkpoint entry. Each section call is wrapped in `try/except` — one bad section logs an error and `continue`s; others' entities are still aggregated.
- `enrich_batch` gained a `batch_key: str | None = None` parameter. When set, it drives the checkpoint entry and intermediate JSON filename while `source_file_id` continues to drive canonical record FKs. Backward compatible: existing calls without `batch_key` behave identically.
- Checkpoint two-level: section-specific keys for partial-run resume; document-level `source_file_id` written at the end of `enrich_documents` for document-level skip. The existing `resume` tests pass unchanged because the document-level check is performed first.

**Bug 3 — chunks missing chunk_type/content abort the pass.**
- Root cause: `Chunk.chunk_type: str` and `Chunk.content: str` were required pydantic fields. Any LLM response that omitted them triggered `ValidationError`, bypassing entity/relationship capture entirely.
- Fix: Both fields are now `Optional[str] = None` in `output_schema.py`. In `canonicalize_llm_output`, chunks with `not chunk.content` are dropped with `_log.warning()` before the content hash is computed (avoids `content_hash(None)` error). The `effective_chunk_type` fallback `"raw_page_text"` still applies when `chunk_type` is absent but content is present.
- This is the correct design: LLM-supplied chunks are supplementary; the authoritative chunks come from the `Chunker`. Dropping incomplete LLM chunks is safe.

**Test additions (12 new unit tests in `tests/unit/test_utf8_unicode_enrich.py`):**
- `TestUtf8ConsoleReconfiguration` (5 tests): `_configure_utf8_console()` reconfigures on win32, no-ops on Linux, silences exceptions, handles streams without `reconfigure`, and the enrich arrow echo exits 0 via CliRunner.
- `TestMultiSectionEntityCapture` (4 tests): two sections aggregate entities; bad section doesn't abort; section+doc checkpoint keys written; document-level resume skips all LLM calls.
- `TestChunkLeniency` (3 tests): chunk with `content=None` dropped; entities survive all-malformed chunks; end-to-end `enrich_batch` captures entities despite malformed chunks.
- **683 passed** (was 671 before this sprint).

### 2026-06-24T18:30:00-07:00 — Sprint 2 Implementation: enrich PDF routing + search embeddings

- **Dispatch by extension at the CLI seam, not in the orchestrator.** The `_enrich_document_file()` helper in `enrich_cmd.py` handles document routing: `router.extract()` → `Chunker.extract()` → `enrich_documents()` → `link_text_evidence()` → write canonical JSON. The orchestrator and chunker stay agnostic to where they're called from.
- **Canonical intermediate JSON goes to `{safe_id}_canonical.json`.** It contains `source_file`, `document_elements`, `chunks` (from Chunker), `entities`, `relationships`, and `evidence` (LLM-extracted + text-linked). The per-pass LLM output files written by `enrich_batch` still exist alongside it — two artifacts, different granularities.
- **`link_text_evidence()` on every structural chunk creates the FK bridge.** After `enrich_documents()` returns, iterate `chunk_result.chunks` and call `link_text_evidence(source_file_id, chunk_id, document_element_id, ...)` for each. This produces evidence rows linking structural search chunks back to their document element origin — the FK chain entities → evidence → chunk → document_element.
- **`generate_embeddings()` composes Fenster's `linkage.derive_chunk_doc()` — does not duplicate it.** Import `derive_chunk_doc` with a local import inside the function to avoid circular import risk. The function's job is caching + batching + embedding; field mapping lives in linkage.
- **Cache by `content_hash`, not by `chunk_id`.** The same text can appear with different chunk IDs across pipeline re-runs (if the source file changes). `content_hash` is the stable dedup key. An empty string hash (`content_hash("")`) acts as a valid key if a chunk lacks a hash — handle gracefully.
- **PowerShell pipe deadlock on large `--cov` output.** When the full test suite (3000+ lines tracked) runs with coverage and output is piped through `| Select-Object`, Python blocks once the pipe buffer fills. Use `mode="async"` with no pipe, or pass explicit test file paths. The individual test groups and new test files all pass cleanly.
- **Integration tests on real large PDFs (14MB) don't belong in `tests/unit/` without guards.** Concurrent agents can add `@pytest.mark.integration` test classes that skip if a fixture dir is absent — but if the dir EXISTS with 22 large PDFs, the tests run in `tests/unit` and dominate runtime. Keep the sprint scope clean by running explicit file lists when verifying.
- **16 new tests (6 PDF enrich cmd + 10 search embeddings), 456 total, 40.5s.** The security invariant (domain text in USER only) is tested via a call-capturing side_effect that asserts `"UNIQUE_DOMAIN_TOKEN"` never appears in `role="system"` messages on the document code path.

### 2026-06-24T19:58:00-07:00 — Enrich resilience: evidence id_hint/source_type optional, per-item drop

- **Root cause of exit 4 on Surface PDFs:** `Evidence.id_hint` and `Evidence.source_type` were `str` (required) in `output_schema.py`, so pydantic hard-failed any batch where gpt-5-4-mini returned `{"text": "...", "confidence": 0.98}` on evidence items (the model's normal output when the schema doesn't explicitly force these fields).
- **Fix 1 — relax the schema:** `Evidence.id_hint` and `Evidence.source_type` are now `Optional[str] = None`. `Chunk.id_hint` is also now optional. This is the correct design: the LLM provides HINTS and the canonicalize step mints stable IDs — requiring the model to emit perfect evidence IDs is wrong and brittle (SPEC-004 §1.1).
- **Fix 2 — synthesize in canonicalize:** `canonicalize_llm_output()` gains a `default_source_type` parameter (default `"document_span"`; pass `"csv_row"` for tabular sources). Missing `source_type` → filled from default. Missing `chunk.id_hint` → `make_chunk_id()` synthesizes from content hash. All synthesized IDs are deterministic — same input always produces same ID.
- **Fix 3 — per-item resilience:** Every entity/chunk/evidence item in `canonicalize_llm_output()` is wrapped in `try/except`. Unsalvageable items are dropped with `_log.warning()`; the rest are kept. The function never raises on a single bad item.
- **Fix 4 — batch-level resilience in `enrich_batch()`:** `validate()` failures attempt a light coercion pass (inject missing `source_file_id` / `pass` envelope fields) and retry. If the second attempt also fails, the pass is skipped with an error log and remaining passes continue. The file is never aborted because of one bad pass.
- **Fix 5 — system prompt strengthened:** `_ENRICH_SYSTEM_PROMPT` now explicitly asks the model for `id_hint` and `source_type` on evidence items and `id_hint` on chunks as best-effort. The security invariant is preserved: domain/user text is still USER-only, the system prompt remains a fixed literal.
- **enrich_documents passes `default_source_type="document_span"`; CSV path in enrich_cmd.py passes `"csv_row"`.** Context flows from the CLI seam through to canonicalize.
- **19 new tests in `tests/unit/test_enrich_resilience.py`; 671 unit tests total (up from 652), all green.**



- **The old `azure-ai-projects.AIProjectClient` call chain was unverified; the new `openai.AzureOpenAI` path is live-confirmed.** The old chain was `client.inference.get_chat_completions_client().complete(...)` and `client.inference.get_embeddings_client().embed(...)`. The new verified chain is `client.chat.completions.create(...)` and `client.embeddings.create(...)` — standard OpenAI SDK semantics on an AzureOpenAI client.
- **Two distinct endpoints for one AI service: `services.ai.azure.com` (Foundry project) vs `openai.azure.com` (AzureOpenAI SDK).** `AZURE_AI_FOUNDRY_ENDPOINT` continues to serve the project URL used by Foundry-native APIs. `AZURE_OPENAI_ENDPOINT` (new) carries `https://<account>.openai.azure.com/` used by `AzureOpenAI`. Both are non-secret and go in `.env.example`/`dev.json`/`fabric-kg.yaml`. Never conflate them.
- **Auth via `get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")`.** This is the correct token scope for Azure OpenAI. Pass the result as `azure_ad_token_provider=tp` to `AzureOpenAI(...)`. If `AZURE_AI_FOUNDRY_API_KEY` or `AZURE_OPENAI_API_KEY` is set, use `api_key=` instead — the key check must precede the token provider path.
- **`response_format={"type":"json_object"}` is the working mode for gpt-5-4-mini.** json_schema mode may not be supported on all deployments. Keep `json_schema` in `complete_json()` signature for callers but drive the model with `json_object` mode — proven to return valid JSON.
- **`FoundryConfig.openai_endpoint` defaults to `""` so test mocks don't need it.** `_build_sdk_client` is only called when `_sdk_client` is not injected. Tests always inject a `MagicMock`, so the empty default causes no validation error.
- **New call chain mock pattern:** `sdk_mock.chat.completions.create.return_value = completion` and `sdk_mock.embeddings.create.return_value = MagicMock(data=...)`. All 7 test files (conftest, test_foundry_client, test_domain, test_enrich_cmd, test_enrich_cmd_pdf, test_enrich_documents, test_orchestrator) were updated to the new chain. All 652 tests still pass.
- **Live smoke confirmed:** `FoundryClient(cfg.foundry).embed(["test"])` returns vectors of `dims=1536` against `https://example-aiservices.openai.azure.com/` with `DefaultAzureCredential`.

 (image-extractor, blob-uploader, docintel, evidence linking)

- **Build on the seam, don't replace it.** `pdf_extractor.extract_images` was a named placeholder that returned `[]`. The real logic lives in `image_extractor.py` and imports pdfplumber directly — the seam's only value is that `PdfExtractor.extract_images()` now delegates there for any callers that needed a static method entry point.
- **pdfplumber `.images` gives metadata dicts, not raw bytes.** Each entry has a `"stream"` key that can be a `PDFStream`, `bytes`, or bytearray. The safest extraction order is: `stream.rawdata` → `stream.read()` → `bytes(stream)`. If none of those work, skip the entry. A `None` stream means no embedded image.
- **Dedup by SHA-256 hash, not by position.** The same image can be embedded on multiple pages (e.g., a logo). Track `seen_hashes: set[str]` across pages and skip duplicates. This keeps visual_assets deduplicated per source file before the Blob upload step.
- **BlobUploader dedup pattern: `get_blob_properties()` raises on not-found.** The Azure Blob SDK raises an exception (not a None return) when a blob doesn't exist. Wrap in a `try/except Exception` to distinguish "exists → return URL" from "not found → upload then return URL". Don't catch by specific exception type because the SDK exception class may vary across SDK versions.
- **`upload(asset_id, data, ext) -> str` matches the conftest mock signature exactly.** The `make_blob_uploader()` mock uses `side_effect = lambda asset_id, data, ext: url_pattern.format(...)`. Keep the real `BlobUploader.upload()` signature identical so the mock can substitute without any adapter code.
- **Document Intelligence result objects can be dicts OR attribute-objects.** The conftest mock sets attributes with `setattr(result_mock, key, value)` from a dict, so `_get(obj, key)` must try `getattr` first and `dict.get` for dict objects. The same helper works for both live SDK objects (attribute-based) and test fixtures (dict or mock).
- **Pure mapping functions are the testable core.** `map_di_result_to_visual_regions` takes any object and returns a `DocIntelResult` — no SDK calls inside. This means tests can pass raw dicts or MagicMocks without wrapping in a `DocIntelClient`. Keep SDK calls isolated in the `analyze_document_bytes` / `analyze_document_url` methods.
- **`enrich_documents` is a thin bridge, not new logic.** It assembles `source_content` from DocumentElementRow list (respecting `section_path` as a context prefix) and delegates to `enrich_batch`. The bridge pattern avoids duplicating checkpoint, retry, and JSON-writing logic.
- **`link_text_evidence` / `link_visual_evidence` encode the FK contract in their signatures.** Keyword-only args make it impossible to accidentally swap `chunk_id` and `document_element_id`. `source_type` is computed from the presence of `chunk_id` / `callout_id` — the caller never needs to remember which string to use.
- **Evidence IDs must be stable across runs.** Both linking helpers use `make_evidence_id(source_file_id, source_type, context_key, text_hash)` where `context_key` encodes the relevant FKs as a colon-separated string. Same FKs + same text → same evidence_id → idempotent pipeline re-runs produce no duplicate evidence rows.
- **86 new unit tests, 0 live calls.** Every test that touches external services (Blob, DI, Foundry LLM) uses injected mocks. The conftest `make_*` factories are the single source of truth for mock shape — test files import them directly rather than rebuilding the mock inline.

### 2026-06-24T17:15:00-07:00 — Sprint 1 Implementation: domain intake, orchestrator, CLI wiring

- **Hard-code the system prompt as a module-level literal constant.** `_DOMAIN_SYSTEM_PROMPT` and `_ENRICH_SYSTEM_PROMPT` are bare string literals at module scope. This makes it structurally impossible to accidentally inject user text into them — no f-strings, no concatenation. The security test captures calls through a subclass override of `complete_json`, which is more robust than inspecting mock call args.
- **`CapturingClient` subclass pattern for security tests.** Subclass `FoundryClient`, override `complete_json` to record `system` and `user` args, then assert user text appears only in `user`. This is cleaner than inspecting mock call_args tuples and documents the security boundary as a first-class test.
- **`ctx.obj` injection for CLI testability without live config.** Passing `obj={"_foundry_client": mock_client}` to `CliRunner.invoke` threads the mock into the CLI via `ctx.obj.get("_foundry_client")`. The `cli()` group calls `ctx.ensure_object(dict)` which leaves an already-dict obj intact, so extra keys survive. No `patch()` context managers needed in the test.
- **Pre-write domain.json to skip the rephrase pass in CLI tests.** When testing `enrich`, pre-writing `{out_dir}/domain.json` means the command loads it directly and doesn't make a domain rephrase call. This lets the mock be wired to a single return value (the LLM entity extraction output) rather than requiring `side_effect` sequencing.
- **`side_effect` with a call counter for multi-call mock sequences.** When you need the mock to return different JSON on call 1 vs. call 2, use a closure with a mutable index dict (`call_index = {"i": 0}`) and a `side_effect` function. This is more readable than `side_effect = iter([...])` and survives repeated test runs.
- **Canonicalization is id_hint → stable ID, not id_hint → id_hint.** The key insight: `canonicalize_llm_output` builds a `hint_to_entity_id` dict from the entity list, then uses it when processing relationships. Any relationship whose source or target hint was filtered out (below threshold or unknown) is silently dropped. This prevents dangling reference rows in Parquet.
- **Dedup by canonical_key, not by id_hint.** Two LLM entities with different id_hints but the same `normalize_canonical_key(type, label)` result are the same logical entity. The canonicalize step merges aliases and keeps the higher confidence score. Relationships still resolve correctly because both hints map to the same entity_id.
- **Checkpoint skip is zero-cost: return empty CanonicalRecords immediately.** When `resume=True` and the source_file_id is in `.checkpoint.json`, return an empty `CanonicalRecords()` before building any prompt or calling the LLM. The test confirms this by wiring the mock to raise if called — if the LLM were invoked, the test would fail with `AssertionError`.
- **`model_dump(default=str)` for datetime serialization in JSON.** `EntityRow` and others carry `datetime` fields. `json.dumps(..., default=str)` converts datetimes to ISO strings without requiring a custom encoder. Works fine for intermediate build output.
- **`enrich_batch` output filename uses sanitized source_file_id.** `source_file_id` contains colons (e.g. `src:abc123`) which are invalid in Windows filenames. Replace `:` with `_` before using it in a filename. `source_file_id.replace(":", "_")` is sufficient for build artifacts.

### 2026-06-24T17:01:06-07:00 — Sprint 1 Implementation: FoundryClient + output_schema

- **Code against an internal interface, isolate the SDK call behind one method.** `_build_sdk_client()` is the single point of contact with `azure-ai-projects`. Everything else works against the injected `_sdk_client`. This means the whole class is testable without installing the SDK at all.
- **`_sdk_client` injection beats `unittest.mock.patch`.** The conftest `make_foundry_client()` factory returns a fully-wired MagicMock. Accepting it via `_sdk_client=` kwarg makes every test a one-liner construction — no `with patch(...)` context manager needed, and the interface contract is visible in the constructor signature.
- **`pass` is a Python keyword.** Pydantic's `Field(alias="pass")` with `model_config = ConfigDict(populate_by_name=True)` is the right pattern. `validate()` receives raw dicts from JSON so the alias `"pass"` works; code accesses `parsed.pass_`. Don't forget `populate_by_name=True` or you can't build the model with both the alias and the Python name.
- **MagicMock auto-chains are fragile across call boundaries.** `get_chat_completions_client()` and `get_embeddings_client()` each return a new mock on every call unless you set `.return_value`. The conftest sets `get_chat_completions_client.return_value.complete.return_value`. For embeddings I need to do the same: `get_embeddings_client.return_value.embed.return_value = ...`. Always set `.return_value` explicitly on chained mock methods — don't rely on auto-creation.
- **Pydantic `ge`/`le` on `confidence` gives free range validation.** No custom validators needed for confidence ∈ [0.0, 1.0]. A single `Field(ge=0.0, le=1.0)` raises `ValidationError` for out-of-range values; tests confirm both directions.
- **`LLM_OUTPUT_JSON_SCHEMA` is a module-level constant from `model_json_schema()`.** Computing it once at import time avoids repeated serialisation and makes it easy to inject into prompts and pass to `complete_json(json_schema=...)`.
- **Never put `dimensions=1536` in the embed call as a magic number.** Always read from `self._config.embedding_dimensions`. Changing the dimension requires an AI Search index rebuild (SPEC-004 §9.2) — coupling the value to config makes that constraint auditable.
- **Foundry SDK package decision: `azure-ai-projects`.** Already in `pyproject.toml` (decision 10 in `.squad/decisions.md`). The mock call chain matches `AIProjectClient.inference.get_chat_completions_client().complete(...)` / `get_embeddings_client().embed(...)`. Both method paths marked with `TODO: verify against current Foundry SDK` since exact surface wasn't confirmed from live docs.

### 2026-06-24T15:41:07.842-07:00 — REQUIREMENTS-001 fabric-cicd + kg_lakehouse update

- **fabric-cicd is REQUIRED, not optional.** The coordinator decision (coordinator-lakehouse-rename-fabriccicd.md) is explicit: fabric-cicd is the primary deploy mechanism for deploy-lakehouse, deploy-ontology, and deploy-search. Fabric REST API is a fallback only. Document this clearly — using hedging language ("wraps both") is incorrect and misleading.
- **New tool = new prereq section, not a footnote.** fabric-cicd needs its own install + verify block (§4), an entry in the overview table, a check in the verify checklist (§9.7), and a note in the RBAC table. Spreading the information across those four places ensures engineers hit it at every stage of setup.
- **auth sharing reduces friction.** fabric-cicd uses DefaultAzureCredential / az login — no additional keys or SPN setup needed in dev. Always state this explicitly alongside install instructions so engineers don't hunt for a separate auth mechanism.
- **Rename cascades are easy to miss.** `fabrickg_lakehouse` → `kg_lakehouse` appeared in 4 distinct places: §1 overview bullet, RBAC table, Fabric prereqs section text, and Fabric prereqs table. A grep after editing confirmed all instances were updated. Always grep for the old name after a rename.
- **dev.json is the authoritative source for OneLake paths.** Rather than hardcoding OneLake Tables/Files paths in the docs (which would go stale), reference `ontology/environments/dev.json`. This keeps docs evergreen while giving engineers a clear pointer to the live values.

### 2026-06-24T15:41:07.842-07:00 — REQUIREMENTS-001, Model Default Correction (gpt-5.4-mini), 200K TPM

- **gpt-5.5-mini does not exist.** The provisioning facts from Coordinator confirmed the newest mini variant is **gpt-5.4-mini** (deployment name `gpt-5-4-mini`). All three specs (SPEC-001, SPEC-004, INFRA-001) previously referenced a non-existent model. Always validate model names against the authoritative provisioning decision file before writing them into specs.
- **200K TPM is a hard minimum for the enrich stage.** `capacity 200 = 200K TPM` is not just a dev choice — it is a documented pipeline requirement. The enrichment stage sends high-volume concurrent requests; any deployment below 200K TPM will throttle under realistic corpus load. This is now prominently documented in REQUIREMENTS-001 §6, SPEC-001 §5.1, SPEC-004 §9.2, and INFRA-001 §1a.
- **Deployment name ≠ model name.** The deployment name in `fabric-kg.yaml` is `gpt-5-4-mini` (hyphen-separated); the underlying model is `gpt-5.4-mini` (dot notation). Engineers must use the deployment name in config, not the model name. This distinction matters because the CLI passes the deployment name to the Foundry SDK, not the model name.
- **AI Search is IN MVP — not optional.** The coordinator decision file explicitly states `ai_search.enabled=true` in `dev.json`. Three specs had hedging language calling AI Search "optional" or "disabled by default". Corrected in all three. Be careful about hedging when the decision is already locked.
- **Prerequisites docs belong in `docs/` not `docs/specs/`.** REQUIREMENTS-001 is an engineer-facing setup guide, not an architectural spec. It lives at `docs/REQUIREMENTS-001-cli-prerequisites.md`. Cross-referenced from all three specs.
- **Action items should be closed when done.** INFRA-001 had two open action items that were both completed on 2026-06-24. Leaving them open creates misleading "what's left to do" signals. Close them with date and artifact references in the same revision.
- **Provisioning examples are valuable.** Including the `az cognitiveservices account deployment create` command in REQUIREMENTS-001 §6 gives engineers a concrete, verifiable way to check capacity and, if needed, reprovision. Always cite the exact `--sku-capacity` value that satisfies the TPM requirement.

### 2026-06-24T13:24:31.077-07:00 — SPEC-004 Canonical-Naming Reconciliation

- **Coordinator canonical-naming.md is authoritative:** All specs must use the names in that decision file. When there's a contradiction between a spec and the canonical decision, the decision file wins.
- **Env var naming pattern:** Azure AI Foundry vars are `AZURE_AI_FOUNDRY_ENDPOINT` and `AZURE_AI_FOUNDRY_API_KEY` — the `AI_` infix is required (not `AZURE_FOUNDRY_*`). Check every spec for the shorter incorrect form.
- **yaml config restructure:** `foundry.*` section should only hold non-model connection settings (`endpoint`, `project`). Model deployment names belong under `enrichment.*` (`chat_deployment`, `embedding_deployment`, `embedding_dimensions`, `vision_deployment`). This matches how the runner actually uses them — enrichment code reads `enrichment` config, not Foundry connection config.
- **Endpoints in yaml must be `${ENV_VAR}` interpolated** — no literal URLs in committed yaml. The endpoint string is a secret-adjacent value (it's not a key, but it's environment-specific and discloses hub topology). Canonical rule: only `.env` holds literal values.
- **`text_deployment` was a legacy alias** — it's now fully removed. Use `enrichment.chat_deployment` everywhere. Any existing code reading `config["foundry"]["text_deployment"]` must be updated.
- **`compile-search` not `compile-search-index`** — the `-index` suffix was non-canonical. The stage is called `compile-search`. Affects pipeline diagrams, CLI docs, and any reference to the stage order.
- **Vision deployment default = chat deployment (multimodal)** — vision is not a separate deployment by default. The chat deployment (GPT-5.5-mini / gpt-4.1) handles vision natively. Only override `enrichment.vision_deployment` if you have a dedicated vision deployment (e.g. `gpt-4o` on example-vision). This collapses a prior contradiction where vision_default was `gpt-4o-vision` but the canonical decision says use chat deployment.
- **Domain CLI contract clarified:** `set-domain --prompt` persists; `enrich --domain-prompt` / `enrich --domain-file` are per-run overrides. Both documented in §2.5 and §11.1. Old vague `--domain` flag mention removed.
- **Appendix B Q7 closed** — vision deployment is no longer an open question; it defaults to the chat deployment with `gpt-4o` as documented alternative.



- **Model decisions locked by Hyunsuk Shin — captured in §9.2:**
  - Chat/enrichment default: **GPT-5.5-mini** (Foundry deployment). Interim dev for example-aiservices sandbox: **gpt-4.1** until GPT-5.5-mini is deployed. Both documented in spec; `chat_deployment` key added to `fabric-kg.yaml` block.
  - Embedding primary: **text-embedding-3-large @ 1536 dims** (dev deployment name: `embedding`). Fallback: **text-embedding-3-small @ 1536 dims**. Keys `embedding_deployment` and `embedding_dimensions` added to `fabric-kg.yaml`.
  - The `embedding_dimensions: 1536` value is **coupled to the AI Search vector field** — changing it requires full re-indexing. Noted prominently in spec with reference to SPEC-001/INFRA-001 (Keyser).
- **Config split pattern (learned from SPEC-001):** Deployment names are non-secret and go in `fabric-kg.yaml`; API keys and endpoints go in `.env` only. Following this strictly for new embedding and AI Search vars.
- **New env vars documented (§9.4):** `AZURE_FOUNDRY_ENDPOINT`, `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_KEY` added — needed for AI Search grounding in §12.
- **Research fold into §12:** Replaced the thin orchestration stub with full production-grade two-phase retrieval spec, citing RESEARCH-001. Key patterns committed to spec:
  - **Two-phase retrieval:** Phase 1 = bounded GQL traversal (rel-type + hop cap, top ~10–20 entities → entity_ids, canonical_keys, aliases, graph_path); Phase 2 = AI Search hybrid query using Phase 1 output.
  - **Filter-vs-query-term rule:** entity IDs → `search.in()` filter; aliases → appended to keyword `search` param. `search.in()` is the correct tool for large ID sets (sub-second, avoids OR-clause limits).
  - **Concrete request body:** hybrid `search` + `vectorQueries` (k=50) + `filter: search.in(entity_ids,...)` + `vectorFilterMode: preFilter` + `queryType: semantic` + semantic config + scoring profile + provenance `select` fields + captions/answers. This is the minimum quality bar.
  - **Fallback branch:** Phase 1 returns 0 entities → pure hybrid without filter, log the event.
  - **Numbered SOURCES prompt contract:** system = fixed cite-or-abstain instruction; user = GRAPH CONTEXT + numbered [1][2]... SOURCES (source_path, entity, graph_path, caption, text) + USER QUESTION. Reused fixed-system / user-content separation from §2.3. Captions preferred over raw chunk_text to save tokens.
  - **Anti-hallucination guardrails:** reranker score threshold ~1.5, dual-source corroboration (graph relationship AND text chunk), shorter graph_path = higher confidence, qualifier wording for single-source claims, provenance in every answer.
  - **Agentic Retrieval option (§12.8):** GA `api-version=2026-04-01`. Higher multi-turn accuracy + built-in citations; higher latency + cost. Standard hybrid recommended for single-turn; Agentic Retrieval for multi-turn chat agents.
  - **Structured data boundary reaffirmed (§12.9):** AI Search = text/visual chunks only. Entity properties, relationship facts = Fabric Lakehouse only. Table added with data type → source → query mechanism.
- **Appendix B Q6 closed:** GPT-5.5-mini locked as default; gpt-4.1 as interim dev. Q7 (vision model) remains open.
- **Revision history discipline:** Each spec edit gets a dated row with a precise summary. This makes it easy to trace when a decision was made without reading the full diff.

### 2026-06-24T11:46:10.517-07:00 — SPEC-004 Revision v2 (Foundry SDK, Domain Intake, Doc Intelligence, AI Search Grounding)

- **SDK replaced:** Switched from raw OpenAI SDK to **Microsoft Foundry SDK (Azure AI Foundry)** for all LLM and vision model calls. Client init uses `AIProjectClient` (verify package: `azure-ai-projects`). Auth options: API key from `.env`, or `DefaultAzureCredential` for hosted envs. Code skeletons marked "verify against current Foundry SDK" where exact API surface is uncertain.
- **Config split enforced:** Following Keyser's SPEC-001 pattern — non-secrets (project_endpoint, deployment names, temperature, thresholds) in `fabric-kg.yaml`; all secrets (API keys, endpoints) in `.env` only. Never in committed yaml, never in `.squad/` files.
- **Domain Intake added (§2):** Pre-enrichment step where user provides a raw domain prompt. LLM rephrases/normalizes it into a structured domain brief (JSON: `domain_brief`, `key_entity_types`, `key_relationship_types`, `extraction_constraints`). Stored at `build/enriched/domain.json`. Injected into every P1–P8 pass as delimited user-message content.
- **Security: user domain text → user message only (§2.3):** The domain prompt and its rephrased form must NEVER appear in the system/developer message. Injecting user input into the system prompt is a prompt-injection / privilege-escalation vector. Defense: fixed developer-controlled system prompt; all user context in clearly delimited user-message blocks. This is stated as a hard security constraint, not a preference.
- **Doc Intelligence added as primary visual extractor (§8.1):** Azure AI Document Intelligence (Layout/Read) handles OCR text, bounding polygons, callout coordinates, and page geometry — before any LLM call. The Foundry vision model (P6) then handles semantic description, callout meaning, and label→entity linking. Clear division-of-labor table added.
- **AI Search second-query grounding (§12):** Defined the two-step graph+retrieval pattern: (1) GQL over Fabric Ontology fetches structured facts from Fabric Lakehouse; (2) entity `canonical_key`s/aliases drive a second AI Search query for text/visual chunks; (3) both are assembled as grounding context in the user message for a final Foundry LLM call. Structured data (entities, relationships) comes from Lakehouse only — AI Search is text/visual retrieval only.
- **Section renumbering:** Old §2–§10 → new §3–§11. New §2 = Domain Intake; new §12 = AI Search Grounding. All internal cross-references updated accordingly.
- **Revision history:** Added to spec header with date 2026-06-24T11:46:10.517-07:00.

### 2026-06-24 — SPEC-004 LLM Enrichment Spec

- **Spec created:** `docs/specs/SPEC-004-llm-enrichment.md`
- **Core principle enforced:** LLM output is intermediate JSON only — never writes Parquet, AI Search, or ontology directly (PRD §6).
- **12 LLM tasks organized into 8 pass types:** P1 schema inference, P2 entity extraction, P3 relationship extraction, P4 normalization, P5 evidence linking, P6 visual description, P7 chunk/table summarization, P8 placeholder suggestion.
- **id_hint semantics:** LLM produces scoped slugs; canonicalize step resolves to stable IDs. Exact hash algorithm deferred to Fenster's data-model spec.
- **Blob URL rule:** The LLM must never mint or modify Blob URLs. Runner injects pre-existing URLs; LLM echoes them unchanged. This is a hard security/integrity constraint.
- **Confidence thresholds:** include >= 0.70, flag 0.50–0.69, drop < 0.50 — configurable in `config/enrich.yaml`.
- **Prompt architecture:** system + developer message for format enforcement + user context + user task; temperature=0.0, seed=42 for determinism; structured outputs / json_schema response_format.
- **Chunking behavior:** 9 chunk types from PRD §10; table chunking produces table_html + table_row + LLM summary; embedding_text assembled after P7.
- **Vision model tasks:** description, OCR, callout identification, label detection, region candidates, visual relationship linking (PRD §13.5 types).
- **Checkpointing:** per-file atomic JSON writes to `build/enriched/`; checkpoint manifest at `build/enriched/.checkpoint.json`; `--force` to re-run all.
- **CLI contract:** `fabric-kg enrich` with `--input`, `--out`, `--passes`, `--sample`, `--dry-run`, `--estimate-cost`, `--strict`, `--force`, `--config`, `--env`; outputs to `build/enriched/`.
- **Schema file deferred:** `config/schemas/llm-intermediate.schema.json` must be created before first implementation merge.
- **Model defaults are open decisions:** PRD §26 Q6 and Q7 — gpt-4o recommended but not locked.
