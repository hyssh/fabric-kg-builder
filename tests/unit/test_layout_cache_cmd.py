import json

import fitz
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.assessment import assess_documents
from tests.unit.test_schema2_extraction import _domain


def pdf(tmp_path, pages=1):
    path = tmp_path / "record.pdf"
    with fitz.open() as document:
        for _ in range(pages):
            document.new_page().insert_text((50, 50), "Record A requires approval.")
        document.save(path)
    return path


class Poller:
    def result(self, timeout):
        text = "Record A requires approval."
        return {
            "content": text,
            "stringIndexType": "unicodeCodePoint",
            "pages": [{
                "pageNumber": 1, "width": 8.5, "height": 11,
                "unit": "inch", "spans": [{"offset": 0, "length": len(text)}],
            }],
            "tables": [], "figures": [],
        }

    def done(self):
        return True


class Client:
    def __init__(self):
        self.calls = []

    def begin_analyze_document(self, **kwargs):
        self.calls.append(kwargs)
        return Poller()


def test_layout_cli_plans_then_caches_and_replays_without_another_post(tmp_path):
    path = pdf(tmp_path)
    cache = tmp_path / "ocr"
    args = [
        "domain", "analyze-layout", "--input", str(path),
        "--endpoint", "https://example.test", "--cache-dir", str(cache),
    ]
    client = Client()
    runner = CliRunner()
    plan = runner.invoke(cli, args, obj={"_docintel_client": client})
    assert plan.exit_code == 0, plan.output
    assert not cache.exists()
    assert not client.calls
    done = runner.invoke(cli, [*args, "--live"], obj={"_docintel_client": client})
    assert done.exit_code == 0, done.output
    result = json.loads(done.output)
    assert result["status"] == "analyzed"
    assert result["analysis_posts"] == 1
    replay = runner.invoke(cli, [*args, "--live"], obj={"_docintel_client": client})
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["status"] == "cached"
    assert len(client.calls) == 1
    report = assess_documents(
        path, _domain(), dry_run=True, ocr_cache=cache,
        ocr_identity=result["extractor_identity"],
    )
    assert report.windows
    assert report.windows[0].text == "Record A requires approval."
    assert report.windows[0].page == 1
    assert report.windows[0].extraction_ref == result["cache_key"]
    assert report.ocr_identity == result["extractor_identity"]


def test_layout_page_budget_prevents_any_paid_post(tmp_path):
    path = pdf(tmp_path, pages=2)
    client = Client()
    result = CliRunner().invoke(cli, [
        "domain", "analyze-layout", "--input", str(path),
        "--endpoint", "https://example.test", "--cache-dir", str(tmp_path / "cache"),
        "--live", "--max-pages", "1",
    ], obj={"_docintel_client": client})
    assert result.exit_code != 0
    assert "max-pages" in result.output
    assert not client.calls


def test_pending_layout_prevents_ambiguous_retries(tmp_path):
    class Uncertain(Client):
        def begin_analyze_document(self, **kwargs):
            self.calls.append(kwargs)
            raise TimeoutError("request outcome unknown")

    path = pdf(tmp_path)
    cache = tmp_path / "cache"
    client = Uncertain()
    args = [
        "domain", "analyze-layout", "--input", str(path),
        "--endpoint", "https://example.test", "--cache-dir", str(cache), "--live",
    ]
    runner = CliRunner()
    failed = runner.invoke(cli, args, obj={"_docintel_client": client})
    assert failed.exit_code != 0
    assert list(cache.glob("*.pending.json"))
    again = runner.invoke(cli, args, obj={"_docintel_client": client})
    assert again.exit_code != 0
    assert "earlier analysis" in again.output
    assert len(client.calls) == 1


def test_polling_access_denial_does_not_allow_another_paid_post(tmp_path):
    from types import SimpleNamespace
    from azure.core.exceptions import HttpResponseError

    class DeniedPoller(Poller):
        def result(self, timeout):
            raise HttpResponseError(
                message="polling denied",
                response=SimpleNamespace(
                    status_code=403, reason="Forbidden", headers={},
                    request=SimpleNamespace(method="GET"), text=lambda: "",
                ),
            )

    class Submitted(Client):
        def begin_analyze_document(self, **kwargs):
            self.calls.append(kwargs)
            return DeniedPoller()

    path = pdf(tmp_path)
    cache = tmp_path / "cache"
    client = Submitted()
    args = [
        "domain", "analyze-layout", "--input", str(path),
        "--endpoint", "https://example.test", "--cache-dir", str(cache), "--live",
    ]
    runner = CliRunner()
    denied = runner.invoke(cli, args, obj={"_docintel_client": client})
    assert denied.exit_code != 0
    assert "reservation retained" in denied.output
    assert list(cache.glob("*.pending.json"))
    again = runner.invoke(cli, args, obj={"_docintel_client": client})
    assert again.exit_code != 0
    assert len(client.calls) == 1


def test_assessment_normalizes_only_after_selecting_raw_ocr_spans(tmp_path):
    from fabric_kg_builder.domain.assessment import assessment_windows
    from fabric_kg_builder.sources.docintel_cache import make_cached_layout, store_cached_layout
    from tests.unit.test_docintel_cache import _identity

    source = pdf(tmp_path)
    raw = {
        "content": "Cafe\u0301 next",
        "stringIndexType": "unicodeCodePoint",
        "pages": [
            {"pageNumber": 1, "spans": [{"offset": 0, "length": 5}]},
        ],
    }
    record = make_cached_layout(source.read_bytes(), raw, _identity())
    cache = tmp_path / "cache"
    store_cached_layout(cache, record)
    _, dispositions, windows = assessment_windows(
        source, window_chars=256, ocr_cache=cache, ocr_identity=_identity(),
    )
    assert dispositions[0].status == "read"
    assert [(w.page, w.text) for w in windows] == [(1, "Caf\u00e9")]
    assert windows[0].extraction_ref == record.cache_key
    assert record.analyze_result["content"] == "Cafe\u0301 next"
