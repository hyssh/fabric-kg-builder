# Test Strategy — fabric-kg-builder

> **TL;DR** — `pytest` is always fast. Real-PDF tests are opt-in. LLM is always mocked.

---

## Three Tiers

### Tier 1 — Fast unit (default, every commit)

| What | How | Speed |
|------|-----|-------|
| Pure-function logic, CLI routing, schema validation | Mock LLM + Blob + Search | < 30 s for full suite |
| Markers | `@pytest.mark.unit`, `@pytest.mark.contract` | — |
| Runs | `pytest` (bare, no flags) | Default |

**Rule:** No tier-1 test ever touches `sample_data/`, a live service, or a multi-MB file.  
The `addopts` in `pyproject.toml` ensures bare `pytest` is always fast:

```
addopts = "… -m 'not slow and not integration'"
```

### Tier 2 — Integration (opt-in, real PDFs)

| What | How | Speed |
|------|-----|-------|
| Real Surface PDFs from `sample_data/` | Actual `pdfplumber` parse, no LLM | 5–60 s per file |
| Markers | `@pytest.mark.integration` + `@pytest.mark.slow` | — |
| Runs | `pytest -m integration` or `pytest -m slow` | Opt-in |

Tests **skip gracefully** if `sample_data/Surface_Troubleshootings/` is absent:

```python
if not REAL_SURFACE_PDF.exists():
    pytest.skip(f"Fixture PDF not present: {REAL_SURFACE_PDF}")
```

This keeps the repo portable (CI agents, fresh clones without the large sample files).

### Tier 3 — Smoke (post-deploy, never in CI merge gate)

Reserved for live-environment checks after deployment.  
Marker: `@pytest.mark.smoke`.  Not run in CI.

---

## The Golden Fixture

`tests/fixtures/golden/surface_mini_canonical.json` is a hand-crafted, trimmed
canonical record (2 entities, 1 relationship, 1 chunk, 1 evidence) shaped exactly
like real Surface PDF enrichment output.

`tests/unit/test_golden_canonical.py` runs compile-data assembly + data-gate
validation against it in < 1 s on every commit.  It keeps the full data pipeline
code path covered without live PDFs.

---

## LLM Mocking

All tier-1 tests use `make_foundry_client()` / `mock_foundry_client` from
`tests/conftest.py`.  The LLM is patched at constructor level — no live API calls,
ever.  The golden fixture `tests/fixtures/llm/sample_enrichment.json` drives the
default mock response.

Integration tests for enrichment (e.g. `test_enrich_cmd_pdf.py`) also use the
mock LLM — they test document routing and chunk assembly, not LLM output.

---

## Running Each Tier

```bash
# Default: fast unit + contract only
pytest

# All integration / real-PDF tests (opt-in)
pytest -m integration

# Slow tests only
pytest -m slow

# Run everything (unit + integration + slow)
pytest -m ""

# Run specific tier
pytest tests/unit -m "not slow and not integration"
pytest tests/integration
```

---

## CI Behaviour

| Trigger | Job | Tests run | Coverage gate |
|---------|-----|-----------|---------------|
| Every push / PR | `test` | Unit + contract (`not slow and not integration`) | Yes — 80% |
| `workflow_dispatch` (manual) | `integration` | `integration or slow` | No |

The integration job is `continue-on-error: true` — a real-PDF parse failure
never blocks a merge.

---

## Enriched Data Cache (batch enrich outputs)

When you run `fabric-kg enrich --input sample_data/ --out data/enriched/` it
writes `*_canonical.json` files to `data/enriched/`.  These are **golden
artifacts** — expensive to produce (LLM calls) and cheap to reuse.

- `--resume` flag skips already-enriched files (checkpoint in `.checkpoint.json`)
- Integration tests that re-validate enriched output should read from `data/enriched/`
  directly, not re-run the LLM
- Never commit `data/enriched/` to git (it contains derived data, not source)
