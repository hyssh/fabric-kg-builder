"""Content-sensitive operation reuse never becomes cross-command authority."""

import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, deterministic_contract_id
from fabric_kg_builder.domain import design, window_run as engine
from fabric_kg_builder.domain.window_run_acceptance import validate_window_run_acceptance
from fabric_kg_builder.domain.window_validation import WindowValidationOperation, _fingerprint
from fabric_kg_builder.domain.window_validation import validation_context
from fabric_kg_builder.enrichment.window_run_reuse import checked_window_run
from tests.unit.test_window_schema_projection_cli import original_case


def _counter(monkeypatch):
    original, calls = engine._validate_exchanges, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_validate_exchanges", counted)
    return calls


@pytest.mark.parametrize("mutate", [
    lambda run: list.clear(run.chunks),
    lambda run: list.clear(run.prepared.source_units),
    lambda run: list.clear(run.final_snapshot.concepts),
    lambda run: dict.update(run.context.intake_raw, business_goal="Changed context"),
    lambda run: dict.update(run.context.intake_raw, business_goal="Cafe\u0301"),
])
def test_nested_mutation_cannot_reuse_validated_authority(original_case, mutate):
    draft = design.load_domain_design(original_case[2])
    operation = WindowValidationOperation()
    checked = operation.run(draft.window_run)
    mutate(checked)
    with pytest.raises(ValueError):
        operation.run(checked)


def test_mutated_cached_object_requires_new_full_replay_even_for_original_input(original_case, monkeypatch):
    draft = design.load_domain_design(original_case[2])
    calls = _counter(monkeypatch)
    operation = WindowValidationOperation()
    checked = operation.run(draft.window_run)
    assert len(calls) == len(draft.window_run.logs)
    assert operation.run(draft.window_run) is checked
    assert len(calls) == len(draft.window_run.logs)
    list.clear(checked.chunks)
    restored = operation.run(draft.window_run)
    assert restored is not checked and restored.chunks
    assert len(calls) == 2 * len(draft.window_run.logs)


def test_acceptance_and_hash_mutation_refuse_within_operation(original_case):
    draft = design.load_domain_design(original_case[2])
    operation = WindowValidationOperation()
    checked_window_run(draft.window_run, draft.window_run_acceptance, _validation=operation)
    draft.window_run_acceptance.selected_committed_chunk_ids.clear()
    with pytest.raises(ValueError):
        validate_window_run_acceptance(draft.window_run_acceptance, draft.window_run, _validation=operation)
    payload = draft.window_run.model_dump(mode="python")
    payload["artifact_hash"] = "f" * 64
    with pytest.raises(ValueError):
        operation.run(payload)


def test_actual_source_mutation_refuses_even_on_cache_hit(original_case):
    source, _, path, _ = original_case
    draft = design.load_domain_design(path)
    operation = WindowValidationOperation()
    operation.run(draft.window_run)
    html = next(source.glob("*.html"))
    original = html.read_bytes()
    try:
        html.write_bytes(original + b"<p>Changed source bytes.</p>")
        with pytest.raises(ValueError):
            operation.run(draft.window_run)
    finally:
        html.write_bytes(original)


def test_bare_helpers_and_new_commands_each_revalidate(original_case, monkeypatch, tmp_path):
    draft = design.load_domain_design(original_case[2])
    calls = _counter(monkeypatch)
    for index in range(2):
        checked_window_run(draft.window_run, draft.window_run_acceptance)
        assert len(calls) == (index + 1) * len(draft.window_run.logs)
    for index in range(2):
        result = CliRunner().invoke(cli, [
            "domain", "evaluate-design", "--file", str(original_case[2]),
            "--out", str(tmp_path / f"evaluation-{index}.json"),
        ])
        assert result.exit_code == 0, result.output
        assert len(calls) == (index + 3) * len(draft.window_run.logs)
    corrupted = json.loads(original_case[2].read_text())
    corrupted["window_run"]["chunks"] = []
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(corrupted))
    failed = CliRunner().invoke(cli, [
        "domain", "evaluate-design", "--file", str(altered), "--out", str(tmp_path / "never.json"),
    ])
    assert failed.exit_code != 0 and not (tmp_path / "never.json").exists()


def test_fingerprints_do_not_alias_unicode_or_strict_python_types():
    assert _fingerprint("Café") != _fingerprint("Cafe\u0301")
    assert _fingerprint(True) != _fingerprint(1)
    assert _fingerprint(["a", "b"]) != _fingerprint(("a", "b"))
    assert _fingerprint({"b": 2, "a": 1}) == _fingerprint({"a": 1, "b": 2})


@pytest.mark.parametrize("target", ["request", "sketch", "parent"])
def test_reused_run_does_not_skip_request_or_projection_derivation_checks(original_case, target):
    from fabric_kg_builder.domain.window_schema_projection import retain_window_schema

    operation = WindowValidationOperation()
    parent = design.load_domain_design(original_case[2], _validation=operation)
    projected = retain_window_schema(parent, _validation=operation)
    values = projected.model_dump(mode="json", exclude={"draft_hash", "draft_id"})
    if target == "request":
        values["request_hash"] = "f" * 64
    elif target == "sketch":
        values["sketch"]["types"][0]["description"] += " Forged alteration."
    else:
        projection = values["schema_projection"]
        projection["parent_sketch"]["types"][0]["description"] += " Forged parent."
        projection["projection_hash"] = canonical_sha256({
            key: value for key, value in projection.items() if key != "projection_hash"
        })
    digest = canonical_sha256(values)
    with pytest.raises(ValueError):
        design.DomainDesignDraft.model_validate_json(canonical_json({
            **values, "draft_hash": digest,
            "draft_id": deterministic_contract_id("domain-design-draft", {"draft_hash": digest}),
        }), context=validation_context(operation))
