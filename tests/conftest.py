"""Shared pytest fixtures for fabric-kg-builder test suite.

Per SPEC-005 §9: provides mock factories for all external services so no test
ever makes a live network call. Import these fixtures in any test module — pytest
discovers them automatically via conftest.py.

Mock targets
------------
- Azure OpenAI SDK       : openai.AzureOpenAI (chat + embeddings)
- Azure Blob Storage     : azure.storage.blob.BlobServiceClient
- Azure AI Search        : azure.search.documents.SearchClient
- Azure Document Intelligence : azure.ai.documentintelligence.DocumentIntelligenceClient
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Session setup: ensure per-environment config exists for CLI tests.
# ---------------------------------------------------------------------------
# ontology/environments/{dev,test,prod}.json are gitignored (they hold real
# resource IDs). A fresh checkout / CI only has the committed *.json.example
# templates. Several CLI tests read these files via `--env dev`, so this
# autouse, session-scoped fixture materializes them from the templates when
# missing and removes only the copies it created.

_REPO_ROOT = Path(__file__).parent.parent
_ENV_DIR = _REPO_ROOT / "ontology" / "environments"


@pytest.fixture(scope="session", autouse=True)
def _ensure_env_configs() -> "object":
    created: list[Path] = []
    for env in ("dev", "test", "prod"):
        target = _ENV_DIR / f"{env}.json"
        template = _ENV_DIR / f"{env}.json.example"
        if not target.exists() and template.exists():
            shutil.copyfile(template, target)
            created.append(target)
    yield
    for path in created:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Click CliRunner — version-agnostic (separate stdout/stderr).
# ---------------------------------------------------------------------------
# Click 8.2 removed the `mix_stderr` argument and always captures stdout and
# stderr separately. On Click < 8.2 we must pass mix_stderr=False to get the
# same behavior. Tests should read combined output via `combined_output()`.


def make_cli_runner():
    """Return a click CliRunner that captures stdout and stderr separately,
    working across Click versions (8.1 needs mix_stderr=False; 8.2+ drops it)."""
    from click.testing import CliRunner

    try:
        return CliRunner(mix_stderr=False)  # Click < 8.2
    except TypeError:
        return CliRunner()  # Click >= 8.2 (streams always separate)


def combined_output(result) -> str:
    """Return stdout + stderr from a click Result, tolerant of Click versions."""
    out = result.output or ""
    try:
        err = result.stderr or ""
    except (ValueError, AttributeError):
        err = ""
    return out + err


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_LLM_FIXTURE = _FIXTURES_DIR / "llm" / "sample_enrichment.json"
_DI_FIXTURE = _FIXTURES_DIR / "document_intelligence" / "analyze_result.json"
_DI_TABLES_FIXTURE = _FIXTURES_DIR / "document_intelligence" / "analyze_result_tables.json"
_PARQUET_DIR = _FIXTURES_DIR / "parquet" / "valid"
_CSV_FIXTURE = _FIXTURES_DIR / "csv" / "sample.csv"


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_build_dir(tmp_path: Path) -> Path:
    """Return a fresh temporary build directory for each test.

    Structure mirrors what fabric-kg writes during a real pipeline run::

        <tmp>/build/enriched/
        <tmp>/build/parquet/
        <tmp>/build/ontology/
        <tmp>/build/search/
        <tmp>/dist/
    """
    for sub in ("build/enriched", "build/parquet", "build/ontology", "build/search", "dist"):
        (tmp_path / sub).mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def sample_csv_path() -> Path:
    """Return the path to the tiny sample CSV fixture.

    File: tests/fixtures/csv/sample.csv — three rows about Surface hardware.
    """
    assert _CSV_FIXTURE.exists(), f"Sample CSV fixture missing: {_CSV_FIXTURE}"
    return _CSV_FIXTURE


# ---------------------------------------------------------------------------
# Mock: Azure OpenAI SDK (openai.AzureOpenAI)
# ---------------------------------------------------------------------------


def make_foundry_client(fixture_json: dict | None = None) -> MagicMock:
    """Factory: return an AzureOpenAI MagicMock wired to ``fixture_json``.

    If ``fixture_json`` is None the default ``sample_enrichment.json`` fixture
    is used.  Patch at the *constructor* level, not function level::

        with patch("fabric_kg_builder.enrichment.foundry_client", make_foundry_client()):
            ...

    The returned mock satisfies the call chain::

        client.chat.completions.create(messages=...) ->
            MagicMock(choices=[MagicMock(message=MagicMock(content=<json>))])
    """
    if fixture_json is None:
        fixture_json = json.loads(_LLM_FIXTURE.read_text())

    content_str = json.dumps(fixture_json)
    client = MagicMock()
    completion = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content_str))]
    )
    client.chat.completions.create.return_value = completion
    return client


@pytest.fixture()
def mock_foundry_client() -> MagicMock:
    """Pytest fixture: Foundry SDK client returning the default enrichment fixture.

    Per SPEC-005 §9 mocking strategy — no live calls, deterministic output.
    Other tests may call :func:`make_foundry_client` directly with custom JSON.
    """
    return make_foundry_client()


# ---------------------------------------------------------------------------
# Mock: Azure Blob Storage (azure.storage.blob.BlobServiceClient)
# ---------------------------------------------------------------------------


def make_blob_uploader(url_pattern: str = "https://fake.blob.core.windows.net/kg-assets/e2e/{asset_id}.{ext}") -> MagicMock:
    """Factory: return a BlobUploader MagicMock with a deterministic URL generator.

    All ``upload(asset_id, data, ext)`` calls return a fake URL; the mock records
    every call for assertion::

        assert mock.upload.call_count == 1
        mock.upload.assert_called_with("figure1", b"...", "png")
    """
    uploader = MagicMock()
    uploader.upload.side_effect = lambda asset_id, data, ext: url_pattern.format(
        asset_id=asset_id, ext=ext
    )
    return uploader


@pytest.fixture()
def mock_blob_uploader() -> MagicMock:
    """Pytest fixture: BlobUploader returning deterministic fake URLs.

    Fake URL pattern: ``https://fake.blob.core.windows.net/kg-assets/e2e/{id}.{ext}``
    """
    return make_blob_uploader()


# ---------------------------------------------------------------------------
# Mock: Azure AI Search (azure.search.documents.SearchClient)
# ---------------------------------------------------------------------------


def make_search_client(search_results: list[dict] | None = None) -> MagicMock:
    """Factory: return an AI Search MagicMock.

    ``search()`` returns an iterable of the supplied ``search_results`` dicts.
    ``upload_documents()`` records calls and returns a success response.
    ``merge_or_upload_documents()`` behaves the same way.
    """
    if search_results is None:
        search_results = []

    client = MagicMock()
    client.search.return_value = iter(search_results)
    client.upload_documents.return_value = [
        MagicMock(succeeded=True, key=r.get("chunk_id", "unknown")) for r in search_results
    ]
    client.merge_or_upload_documents.return_value = client.upload_documents.return_value
    return client


@pytest.fixture()
def mock_search_client() -> MagicMock:
    """Pytest fixture: AI Search client returning an empty result set by default."""
    return make_search_client()


# ---------------------------------------------------------------------------
# Mock: Azure AI Document Intelligence
# ---------------------------------------------------------------------------


def make_document_intelligence_client(fixture_path: Path | None = None) -> MagicMock:
    """Factory: return a DocumentIntelligenceClient MagicMock.

    ``begin_analyze_document(...).result()`` returns a MagicMock whose attributes
    are populated from the JSON fixture at ``fixture_path`` (defaults to the
    shared ``analyze_result.json`` stub).

    Patch at constructor level::

        with patch(
            "azure.ai.documentintelligence.DocumentIntelligenceClient",
            return_value=make_document_intelligence_client(),
        ):
            ...
    """
    if fixture_path is None:
        fixture_path = _DI_FIXTURE

    raw = json.loads(fixture_path.read_text())

    client = MagicMock()
    poller = MagicMock()

    # Build a MagicMock that surfaces top-level keys as attributes AND items.
    result_mock = MagicMock()
    for key, value in raw.items():
        setattr(result_mock, key, value)
    result_mock.__getitem__ = lambda self, k: raw[k]

    poller.result.return_value = result_mock
    client.begin_analyze_document.return_value = poller
    return client


@pytest.fixture()
def mock_document_intelligence_client() -> MagicMock:
    """Pytest fixture: Document Intelligence client returning the default fixture.

    Fixture file: tests/fixtures/document_intelligence/analyze_result.json
    """
    return make_document_intelligence_client()


def make_document_intelligence_client_with_tables() -> MagicMock:
    """Factory: DI client mock whose result carries a 3-row parts table.

    Fixture: tests/fixtures/document_intelligence/analyze_result_tables.json
    Table has one header row (Part | Part Number | Quantity) and two data rows
    (Battery | M1287099-003 | 1) and (Display | M1234567-001 | 1).
    """
    return make_document_intelligence_client(fixture_path=_DI_TABLES_FIXTURE)


@pytest.fixture()
def mock_document_intelligence_client_with_tables() -> MagicMock:
    """Pytest fixture: DI client returning a result with a 3-row parts table."""
    return make_document_intelligence_client_with_tables()


# ---------------------------------------------------------------------------
# Parquet tables helper (loads valid fixture parquet files if they exist)
# ---------------------------------------------------------------------------


@pytest.fixture()
def parquet_tables(tmp_path: Path) -> dict:
    """Load all Parquet fixture files from tests/fixtures/parquet/valid/.

    Returns a dict mapping stem → ``pandas.DataFrame``.  If the directory is
    empty (Sprint 1 — tables not yet generated) returns an empty dict rather
    than raising.
    """
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError:
        return {}

    tables: dict = {}
    if _PARQUET_DIR.exists():
        for parquet_file in _PARQUET_DIR.glob("*.parquet"):
            tables[parquet_file.stem] = pd.read_parquet(parquet_file)
    return tables


# ---------------------------------------------------------------------------
# Convenience re-exports so other modules can ``from tests.conftest import ...``
# ---------------------------------------------------------------------------

__all__ = [
    "tmp_build_dir",
    "sample_csv_path",
    "mock_foundry_client",
    "mock_blob_uploader",
    "mock_search_client",
    "mock_document_intelligence_client",
    "parquet_tables",
    # factories (importable by other test modules)
    "make_foundry_client",
    "make_blob_uploader",
    "make_search_client",
    "make_document_intelligence_client",
    "make_document_intelligence_client_with_tables",
]
