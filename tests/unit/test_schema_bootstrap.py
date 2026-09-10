"""Offline initial-window inference from exact prepared source slices."""

import copy
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain import schema_bootstrap as core
from fabric_kg_builder.domain.discovery import save_discovery
from fabric_kg_builder.domain.window_schema import DesignReference, WorkingSchemaSnapshot
from tests.unit.test_domain_discovery import _prepared, _run


@pytest.fixture
def discovery(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2, paragraphs=4)
    run = _run(tmp_path, prepared, max_chunk_chars=128)
    path = tmp_path / "discovery.json"
    save_discovery(path, run)
    return path, run


def _response(request):
    chunks = json.loads(request["user"])["input"]["chunks"]
    concepts = [
        {"concept_id": "record", "kind": "entity", "name": "Record",
         "definition": "A governed document record", "aliases": ["Governed record"],
         "identity_policy": {"mode": "unresolved", "suggestions": ["Record identifier"]}},
        {"concept_id": "subject", "kind": "entity", "name": "Subject",
         "definition": "A subject described by a record", "identity_policy": {"mode": "unresolved"}},
        {"concept_id": "describes", "kind": "relationship", "name": "Describes",
         "definition": "A record describes a subject", "source_type_ids": ["record"],
         "target_type_ids": ["subject"], "direction": "source_to_target",
         "identity_policy": {"mode": "unresolved", "context_policy": "Within the document"}},
        {"concept_id": "status", "kind": "property", "name": "Cancellation status",
         "definition": "Cancellation applicability of a record", "owner_type_ids": ["record"],
         "identity_policy": {"mode": "unresolved"}, "value_type": "string"},
    ]
    return {
        "reference": {"concepts": concepts},
        "evidence": [{"concept_id": item["concept_id"], "chunk_id": chunks[-1]["chunk_id"],
                      "quote": chunks[-1]["text"]} for item in concepts],
        "uncertainties": ["Record identity is unresolved"],
        "domain_description": "Governed records and subjects",
    }


class Client:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def complete_json(self, **request):
        self.calls.append(copy.deepcopy(request))
        response = _response(request)
        if self.mutate:
            self.mutate(response)
        return response


def _factory(client):
    return lambda: (client, "fixture", canonical_sha256({"model": "fixture"}))


def _no_client():
    pytest.fail("No client construction or calls permitted")


def test_first_document_all_exact_slices_no_previous_context(discovery, tmp_path):
    path, run = discovery
    binding, request = core.prepare_bootstrap(path, tmp_path / "state")
    first = next(entry for entry in run.prepared.corpus.entries if entry.disposition == "eligible")
    assert binding["source_file_id"] == first.source_file_id
    payload = json.loads(request["user"])["input"]
    expected = [item.chunk for item in run.chunks if item.chunk.source_file_id == first.source_file_id]
    assert {item["chunk_id"] for item in payload["chunks"]} == {item.chunk_id for item in expected}
    units = {unit.source_unit_id: unit for unit in run.prepared.source_units}
    for item in payload["chunks"]:
        unit = units[item["source_unit_id"]]
        assert item["text"] == unit.text[item["slice_start"]:item["slice_end"]]
        assert item["locator"] == unit.locator.model_dump(mode="json")
    last = max(expected, key=lambda item: (units[item.source_unit_id].ordinal, item.slice_start))
    assert payload["chunks"][-1]["chunk_id"] == last.chunk_id
    for entry in run.prepared.corpus.entries:
        if entry.source_file_id != first.source_file_id:
            assert entry.source_file_id not in request["user"]
    assert "Open record" not in request["user"]
    assert "covered_input_ids" not in request["user"]
    assert "competency" not in request["user"]
    assert "candidate" not in request["user"]
    assert "concepts" not in payload
    assert request["max_completion_tokens"] == 16384 and request["max_attempts"] == 1


def test_explicit_selection(discovery, tmp_path):
    path, run = discovery
    chosen = run.prepared.corpus.entries[-1].source_file_id
    binding, request = core.prepare_bootstrap(path, tmp_path / "state", chosen)
    assert binding["source_file_id"] == chosen
    assert all(item["source_file_id"] == chosen
               for item in json.loads(request["user"])["input"]["chunks"])


def test_plan_no_writes_or_client_and_help(discovery, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    path, _ = discovery
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: _no_client())
    state = tmp_path / "state"
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(cli, ["domain", "window-bootstrap", "--discovery", str(path),
                                     "--out-state", str(state)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "planned" and payload["writes"] == payload["model_calls"] == 0
    assert not state.exists()
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    help_result = CliRunner().invoke(cli, ["domain", "window-bootstrap", "--help"])
    assert help_result.exit_code == 0 and "--source-file-id" in help_result.output
    result = CliRunner().invoke(cli, ["--dry-run", "domain", "window-bootstrap",
                                     "--discovery", str(path), "--out-state", str(state), "--live"])
    assert result.exit_code == 2 and "conflicts" in result.output
    assert not state.exists()


def test_complete_and_zero_call_resume_with_provisional_directed_reference(discovery, tmp_path):
    path, _ = discovery
    root = tmp_path / "state"
    client = Client()
    result = core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(client))
    assert len(client.calls) == result["model_calls"] == 1
    assert result["concept_count"] == result["pending_concept_count"] == 4
    ref = DesignReference.model_validate_json((root / "schema-reference.json").read_text())
    snapshot = WorkingSchemaSnapshot.model_validate_json((root / "schema-1.json").read_text())
    empty = WorkingSchemaSnapshot.model_validate_json((root / "schema-0.json").read_text())
    assert snapshot.version == 1 and snapshot.before_hash == empty.artifact_hash
    assert not empty.concepts and set(snapshot.provisional_concept_ids) == {c.concept_id for c in ref.concepts}
    relationship = next(c for c in ref.concepts if c.kind == "relationship")
    assert relationship.source_type_ids == ["record"] and relationship.target_type_ids == ["subject"]
    assert relationship.direction == "source_to_target"
    assert all(c.identity_policy["mode"] == "unresolved" for c in ref.concepts)
    before = {p: p.read_bytes() for p in root.iterdir()}
    for live in (False, True):
        resumed = core.bootstrap(discovery=path, out_state=root, resume=True, live=live,
                                 client_factory=_no_client)
        assert resumed["model_calls"] == resumed["writes"] == 0
        assert resumed["result_hash"] == result["result_hash"]
        assert before == {p: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_no_client)


@pytest.mark.parametrize("mutation", [
    lambda r: r["evidence"][0].update(quote="Fabricated impossible quote"),
    lambda r: r["evidence"][0].update(chunk_id="other-document-chunk"),
    lambda r: r["evidence"].pop(),
    lambda r: r["reference"]["concepts"][2].update(target_type_ids=["unknown"]),
    lambda r: r["reference"]["concepts"][0].update(identity_policy={"mode": "approved"}),
    lambda r: r["reference"]["concepts"][0].update(name="Non-NFC e\u0301"),
])
def test_invalid_output_keeps_raw_and_never_recalls(discovery, tmp_path, mutation):
    path, _ = discovery
    root = tmp_path / "state"
    client = Client(mutation)
    with pytest.raises(ValueError):
        core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(client))
    assert len(client.calls) == 1
    raw = (root / "response.json").read_bytes()
    assert not (root / "result.json").exists()
    with pytest.raises(ValueError):
        core.bootstrap(discovery=path, out_state=root, resume=True, live=True, client_factory=_no_client)
    assert (root / "response.json").read_bytes() == raw


@pytest.mark.parametrize("artifact", ["request.json", "response.json", "result.json", "schema-reference.json"])
def test_resume_hash_drift_rejected(discovery, tmp_path, artifact):
    path, _ = discovery
    root = tmp_path / "state"
    core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(Client()))
    data = json.loads((root / artifact).read_text())
    data["tampering"] = True
    (root / artifact).write_text(json.dumps(data))
    with pytest.raises(ValueError):
        core.bootstrap(discovery=path, out_state=root, resume=True, live=True, client_factory=_no_client)


def test_input_output_paths_and_source_drift(discovery, tmp_path):
    path, run = discovery
    for out in (path.parent, path, tmp_path / "source" / "state"):
        with pytest.raises(ValueError, match="overlap"):
            core.bootstrap(discovery=path, out_state=out, live=True, client_factory=_no_client)
    root = tmp_path / "state"
    core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(Client()))
    moved = tmp_path / "moved-state"
    root.rename(moved)
    with pytest.raises(ValueError, match="drift"):
        core.bootstrap(discovery=path, out_state=moved, resume=True, client_factory=_no_client)
    moved.rename(root)
    other = run.prepared.corpus.entries[-1].source_file_id
    with pytest.raises(ValueError, match="drift"):
        core.bootstrap(discovery=path, out_state=root, source_file_id=other, resume=True,
                       client_factory=_no_client)
    source = next((tmp_path / "source").iterdir())
    source.write_text("changed")
    with pytest.raises(ValueError):
        core.bootstrap(discovery=path, out_state=root, resume=True, client_factory=_no_client)


def test_oversize_defers_before_client_or_writes(discovery, tmp_path, monkeypatch):
    path, _ = discovery
    monkeypatch.setattr(core, "MAX_REQUEST_CHARS", 100)
    with pytest.raises(ValueError, match="never truncated"):
        core.bootstrap(discovery=path, out_state=tmp_path / "state", live=True, client_factory=_no_client)
    assert not (tmp_path / "state").exists()


def test_gap_is_rejected_and_no_reparse(discovery, tmp_path, monkeypatch):
    path, run = discovery
    # Inject a post-load gap to exercise the additional full-coverage boundary.
    selected = run.prepared.corpus.entries[0].source_file_id
    removed = next(record for record in run.chunks if record.chunk.source_file_id == selected)
    altered = SimpleNamespace(prepared=run.prepared, run_hash=run.run_hash,
                              chunks=[c for c in run.chunks if c is not removed])
    monkeypatch.setattr(core, "load_discovery", lambda _: altered)
    checks = []
    monkeypatch.setattr(core, "validate_discovery", lambda *a, **kw: checks.append(kw))
    with pytest.raises(ValueError, match="coverage gap"):
        core.bootstrap(discovery=path, out_state=tmp_path / "state", live=True, client_factory=_no_client)
    assert checks and checks[0]["reparse"] is False


def test_selection_preserves_prepared_entry_order_not_alphabetical(discovery, tmp_path, monkeypatch):
    path, run = discovery
    entries = sorted(run.prepared.corpus.entries,
                     key=lambda item: item.relative_source_ref, reverse=True)
    prepared = SimpleNamespace(
        source_path=run.prepared.source_path, corpus=SimpleNamespace(entries=entries),
        sources=run.prepared.sources, source_units=run.prepared.source_units,
        prepared_hash=run.prepared.prepared_hash,
    )
    monkeypatch.setattr(core, "load_discovery", lambda _: SimpleNamespace(
        prepared=prepared, run_hash=run.run_hash, chunks=run.chunks))
    monkeypatch.setattr(core, "validate_discovery", lambda *a, **kw: None)
    binding, _ = core.prepare_bootstrap(path, tmp_path / "state")
    assert binding["source_name"] == entries[0].relative_source_ref


def test_interrupted_after_raw_response_finishes_without_model(discovery, tmp_path):
    path, _ = discovery
    root = tmp_path / "state"
    first = core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(Client()))
    for name in ("schema-reference.json", "schema-0.json", "schema-1.json",
                 "summary.json", "window-log.json", "result.json"):
        (root / name).unlink()
    resumed = core.bootstrap(discovery=path, out_state=root, resume=True, live=True,
                             client_factory=_no_client)
    assert resumed["model_calls"] == 0 and resumed["result_hash"] == first["result_hash"]


def test_missing_response_never_repeats_call(discovery, tmp_path):
    path, _ = discovery
    root = tmp_path / "state"
    core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(Client()))
    (root / "response.json").unlink()
    with pytest.raises(ValueError, match="no repeat call"):
        core.bootstrap(discovery=path, out_state=root, resume=True, live=True,
                       client_factory=_no_client)


def test_provider_invalid_json_is_persisted_without_retry(discovery, tmp_path):
    from fabric_kg_builder.enrichment.foundry_client import FoundryJSONResponseError

    class BrokenClient:
        def complete_json(self, **request):
            raise FoundryJSONResponseError("Truncated output", {"raw_output": '{"broken":'})

    path, _ = discovery
    root = tmp_path / "state"
    with pytest.raises(FoundryJSONResponseError):
        core.bootstrap(discovery=path, out_state=root, live=True, client_factory=_factory(BrokenClient()))
    raw = core._read(root / "response.json", "response")
    assert json.loads(raw.payload["provider_error_json"])["raw_output"] == '{"broken":'
    with pytest.raises(ValueError, match="Cached provider response"):
        core.bootstrap(discovery=path, out_state=root, resume=True, live=True, client_factory=_no_client)
