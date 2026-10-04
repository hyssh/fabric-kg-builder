"""Offline CLI boundary tests for additive, explicitly unapproved domain design."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner
from openai import APIStatusError
from pydantic import BaseModel, Field

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli import domain_design_cmd as commands
from tests.unit.test_l1_stage import _intake


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--domain-file", "seed.yaml"),
        ("--domain-description", "Facility asset management"),
        ("--domain-description", ""),
    ],
)
def test_schema2_init_rejects_ignored_seed_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, value: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init-domain", flag, value])
    assert result.exit_code == 2, result.output
    assert "does not consume" in result.output
    assert "domain design --seed-domain" in result.output
    assert not (tmp_path / ".fkg").exists()
    assert not (tmp_path / "domain.yaml").exists()


def test_schema2_init_honors_global_dry_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text("Governed records.", encoding="utf-8")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(_intake()), encoding="utf-8")
    state = tmp_path / "state"
    domain = tmp_path / "domain.yaml"
    result = CliRunner().invoke(
        cli,
        [
            "--dry-run", "init-domain", "--input", str(source),
            "--intake", str(intake), "--state-dir", str(state),
            "--out", str(domain),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "writes=0" in result.output
    assert not state.exists()
    assert not domain.exists()


class _Draft(BaseModel):
    """CLI adapter double; core validation is covered by the core worker's tests."""

    status: Literal["unapproved"] = "unapproved"
    draft_hash: str = "d" * 64
    context: dict[str, Any] = Field(default_factory=dict)
    seed: Any = None
    sketch: dict[str, Any] = Field(default_factory=lambda: {
        "types": [{"key": "context", "question_ids": [], "rationale": "Useful context."}],
        "question_routes": [{"question_id": "cq:q1", "unsupported_reason": "Not represented."}],
    })


class _Evaluation(BaseModel):
    evaluation_hash: str = "e" * 64
    draft_hash: str = "d" * 64
    compile_ready: bool = False
    kind: str = "local_structural"


@pytest.fixture()
def adapter_core(monkeypatch: pytest.MonkeyPatch):
    from fabric_kg_builder.domain import design as real_core

    def design(preflight, **kwargs):
        return _Draft(
            context={
                "project_id": preflight.base_identity.project_id,
                "description": kwargs.get("description"),
            },
            seed=real_core._read_seed(kwargs["seed_path"]),
        )

    def compile_draft(_draft, evaluation, **kwargs):
        if not evaluation.compile_ready:
            raise ValueError("DESIGN_COMPILE_BLOCKED: no covered questions")
        return SimpleNamespace(
            proposal=SimpleNamespace(proposal_hash="f" * 64),
            model_call_count=0, summary="Unapproved prepared design.",
        )

    core = SimpleNamespace(
        DomainDesignDraft=_Draft,
        DomainDesignEvaluation=_Evaluation,
        DomainDesignSketch=real_core.DomainDesignSketch,
        DesignInputs=real_core.DesignInputs,
        DesignSamples=real_core.DesignSamples,
        DesignSeedReference=real_core.DesignSeedReference,
        _read_seed=real_core._read_seed,
        save_design_artifact=real_core.save_design_artifact,
        load_domain_design=lambda path: _Draft.model_validate_json(path.read_text(encoding="utf-8")),
        load_design_evaluation=lambda path: _Evaluation.model_validate_json(path.read_text(encoding="utf-8")),
        generate_domain_design=Mock(side_effect=design),
        evaluate_domain_design=Mock(return_value=_Evaluation()),
        compile_domain_design=Mock(side_effect=compile_draft),
        design_preflight=Mock(return_value=SimpleNamespace(
            base_identity=SimpleNamespace(project_id="project:test"), run_id="run:test",
        )),
    )
    monkeypatch.setattr(commands, "finalize_l1_stage", Mock(return_value=SimpleNamespace(status="blocked")))
    monkeypatch.setattr(commands, "_design_core", lambda: core)
    return core


@pytest.fixture()
def design_paths(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text("Actual bounded source content.", encoding="utf-8")
    intake = tmp_path / "intake.yaml"
    intake.write_text(json.dumps(_intake()), encoding="utf-8")
    out = tmp_path / "new-output" / "draft.json"
    args = [
        "domain", "design", "--sample-only", "--input", str(source),
        "--intake", str(intake), "--out", str(out),
    ]
    return source, intake, out, args


def test_design_default_plan_has_no_client_or_writes(
    adapter_core, design_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, out, args = design_paths
    client = Mock(side_effect=AssertionError("planning cannot construct a model client"))
    monkeypatch.setattr(commands, "_build_client", client)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "planned"
    assert payload["artifact"] is None
    assert not out.parent.exists()
    client.assert_not_called()
    adapter_core.generate_domain_design.assert_not_called()
    assert payload["max_calls"] == 2
    assert payload["result"]["project_id"] == f"project:{source.name}"


@pytest.mark.parametrize("global_flag", [False, True])
def test_design_live_rejects_local_and_global_dry_run(
    adapter_core, design_paths, monkeypatch: pytest.MonkeyPatch, global_flag: bool,
) -> None:
    _, _, out, args = design_paths
    client = Mock()
    monkeypatch.setattr(commands, "_build_client", client)
    flags = ["--dry-run", *args, "--live"] if global_flag else [*args, "--dry-run", "--live"]
    result = CliRunner().invoke(cli, flags)
    assert result.exit_code == 2, result.output
    assert "cannot be combined" in result.output
    client.assert_not_called()
    adapter_core.generate_domain_design.assert_not_called()
    assert not out.parent.exists()


def test_design_forwards_full_generic_seed_and_exact_bytes_hash(
    adapter_core, design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, out, args = design_paths
    seed = tmp_path / "reference.yaml"
    raw = (
        b'seed_kind: user_reference_sketch\r\nauthority: design_input_only\r\n'
        b'entities:\r\n  - name: Part\r\n    illustrative_example: "001234"\r\n'
        b'    count_example: 2\r\nrelationships:\r\n  describes: "Design only"\r\n'
    )
    seed.write_bytes(raw)
    model_client = object()
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (model_client, "gpt-4.1"))
    result = CliRunner().invoke(cli, [
        *args, "--live", "--max-calls", "2", "--project-id", "surface-design-026",
        "--seed-domain", str(seed), "--description", "Repair decisions.",
    ])
    assert result.exit_code == 0, result.output
    supplied = adapter_core.generate_domain_design.call_args.kwargs
    assert supplied["seed_path"] == seed
    assert supplied["description"] == "Repair decisions."
    assert adapter_core.generate_domain_design.call_args.args[0].base_identity.project_id == "surface-design-026"
    assert supplied["client"] is model_client
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["status"] == "unapproved"
    assert saved["seed"]["raw_yaml"] == raw.decode("utf-8")
    assert saved["seed"]["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert saved["seed"]["parsed_content"]["entities"][0]["illustrative_example"] == "001234"
    assert saved["seed"]["parsed_content"]["entities"][0]["count_example"] == 2
    assert saved["seed"]["parsed_content"]["relationships"] == {"describes": "Design only"}
    assert saved["sketch"]["types"][0]["question_ids"] == []


@pytest.mark.parametrize("seed_text", [
    "!!python/object/apply:os.system ['do not execute']",
    "value: .nan",
    "1: numeric mapping key",
    "value: !!set {one: null}",
    "value: &cycle [*cycle]",
])
def test_invalid_seed_is_rejected_before_model_construction(
    adapter_core, design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed_text: str,
) -> None:
    _, _, out, args = design_paths
    seed = tmp_path / "invalid.yaml"
    seed.write_text(seed_text, encoding="utf-8")
    client = Mock()
    monkeypatch.setattr(commands, "_build_client", client)
    result = CliRunner().invoke(cli, [*args, "--live", "--seed-domain", str(seed)])
    assert result.exit_code == 1, result.output
    client.assert_not_called()
    adapter_core.generate_domain_design.assert_not_called()
    assert not out.parent.exists()


@pytest.mark.parametrize("budget", ["0", "-1", "9"])
def test_design_invalid_call_budget_stops_before_execution(
    adapter_core, design_paths, budget: str,
) -> None:
    result = CliRunner().invoke(cli, [*design_paths[3], "--live", "--max-calls", budget])
    assert result.exit_code == 2, result.output
    adapter_core.generate_domain_design.assert_not_called()


def test_design_refuses_overwrite_before_model_construction(
    adapter_core, design_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, out, args = design_paths
    out.parent.mkdir()
    out.write_text("original draft", encoding="utf-8")
    client = Mock()
    monkeypatch.setattr(commands, "_build_client", client)
    result = CliRunner().invoke(cli, [*args, "--live"])
    assert result.exit_code == 1, result.output
    assert out.read_text(encoding="utf-8") == "original draft"
    client.assert_not_called()
    adapter_core.generate_domain_design.assert_not_called()


def test_live_design_refuses_non_design_artifact(
    adapter_core, design_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_core.generate_domain_design.side_effect = None
    adapter_core.generate_domain_design.return_value = {"approval": {"status": "approved"}}
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (object(), "gpt-4.1"))
    result = CliRunner().invoke(cli, [*design_paths[3], "--live"])
    assert result.exit_code == 1, result.output
    assert "unapproved DesignDraft" in result.output
    assert not design_paths[2].exists()


@pytest.mark.parametrize("project_id", ["", "   "])
def test_explicit_empty_project_identity_is_not_silently_defaulted(
    adapter_core, design_paths, project_id: str,
) -> None:
    result = CliRunner().invoke(cli, [
        *design_paths[3], "--project-id", project_id,
    ])
    assert result.exit_code == 2, result.output
    adapter_core.generate_domain_design.assert_not_called()


@pytest.mark.parametrize("status,code", [
    (401, "MODEL_ACCESS_DENIED"), (503, "MODEL_REQUEST_FAILED"),
])
def test_design_model_failure_is_sanitized(
    adapter_core, design_paths, monkeypatch: pytest.MonkeyPatch, status: int, code: str,
) -> None:
    _, _, out, args = design_paths
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    adapter_core.generate_domain_design.side_effect = APIStatusError(
        "provider-private-detail", response=response, body=None,
    )
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (object(), "gpt-4.1"))
    result = CliRunner().invoke(cli, [*args, "--live"])
    assert result.exit_code == 1, result.output
    assert code in result.output
    assert "provider-private-detail" not in result.output
    assert "response-cache" not in result.output
    assert not out.exists()


def test_design_schema_needs_no_configuration_or_client(
    adapter_core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    client = Mock(side_effect=AssertionError("schema export cannot initialize auth"))
    monkeypatch.setattr(commands, "_build_client", client)
    result = CliRunner().invoke(cli, ["domain", "design-schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["schemas"]["_Draft"]["properties"]["status"]["const"] == "unapproved"
    client.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_evaluation_retains_unsupported_design_without_live_claims(
    adapter_core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    draft_path = tmp_path / "draft.json"
    original = _Draft().model_dump_json()
    draft_path.write_text(original, encoding="utf-8")
    output = tmp_path / "evaluation" / "report.json"
    client = Mock(side_effect=AssertionError("evaluation is local"))
    monkeypatch.setattr(commands, "_build_client", client)
    args = ["domain", "evaluate-design", "--file", str(draft_path), "--out", str(output)]
    result = CliRunner().invoke(cli, (["--dry-run"] if dry_run else []) + args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evaluation_kind"] == "local_structural_not_live_answerability"
    assert payload["result"]["compile_ready"] is False
    assert output.exists() is not dry_run
    assert draft_path.read_text(encoding="utf-8") == original
    client.assert_not_called()


@pytest.mark.parametrize("ready,ack,dry_run,success", [
    (False, "e" * 64, False, False),
    (True, "0" * 64, False, False),
    (True, "e" * 64, True, True),
    (True, "e" * 64, False, True),
])
def test_compile_forwards_exact_review_and_dry_run_without_model(
    adapter_core, design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ready: bool, ack: str, dry_run: bool, success: bool,
) -> None:
    source = design_paths[0]
    draft = tmp_path / "draft.json"
    draft.write_text(_Draft().model_dump_json(), encoding="utf-8")
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text(_Evaluation(compile_ready=ready).model_dump_json(), encoding="utf-8")
    state = tmp_path / "compiled-state"
    domain = tmp_path / "compiled-domain.yaml"
    client = Mock(side_effect=AssertionError("compilation is local"))
    monkeypatch.setattr(commands, "_build_client", client)
    args = [
        "domain", "compile-design", "--file", str(draft), "--input", str(source),
        "--evaluation", str(evaluation), "--accept-evaluation-hash", ack,
        "--out-state", str(state), "--out-domain", str(domain),
    ]
    result = CliRunner().invoke(cli, (["--dry-run"] if dry_run else []) + args)
    assert (result.exit_code == 0) is success, result.output
    if ack != "e" * 64:
        adapter_core.compile_domain_design.assert_not_called()
    else:
        assert adapter_core.design_preflight.call_args.args[1] == source
        adapter_core.compile_domain_design.assert_called_once()
    if success:
        assert json.loads(result.output)["approval_granted"] is False
        if not dry_run:
            kwargs = commands.finalize_l1_stage.call_args.kwargs
            assert kwargs["decision"] is None and kwargs["actor"] is None
            assert kwargs["state_root"] == state and kwargs["domain_path"] == domain
    if not success or dry_run:
        commands.finalize_l1_stage.assert_not_called()
    if dry_run:
        assert not state.exists() and not domain.exists()
    assert draft.exists() and evaluation.exists()
    client.assert_not_called()


def _design_response(*, supported: bool) -> dict:
    question_ids = [f"cq:q{number}" for number in range(1, 6)]

    def entity(key: str) -> dict:
        return {
            "key": key, "display_name": key.title(), "description": f"A governed {key}.",
            "parent_key": None, "identity_property_keys": [],
            "question_ids": question_ids if supported else [],
            "evidence_ids": [], "rationale": "Retain useful source-scoped context.",
            "classification": "common",
        }

    return {
        "domain_name": "Records", "domain_description": "Evidence-backed record review.",
        "types": [entity("record"), entity("subject")] if supported else [entity("context")],
        "properties": [{
            "owner_key": "subject", "key": "description", "display_name": "Description",
            "value_type": "string", "required": False,
        }] if supported else [],
        "relationships": [{
            "key": "describes", "display_name": "Describes", "description": "Record describes subject.",
            "source_key": "record", "target_key": "subject",
            "question_ids": question_ids, "evidence_ids": [],
            "rationale": "Retain the task's record-subject relationship.",
        }] if supported else [],
        "question_routes": [{
            "question_id": question_id, "source_key": "record", "target_key": "subject",
            "answer_property_keys": ["subject.description"],
            "rationale": "Describe the governed subject; evidence execution remains unverified.",
            "unsupported_reason": None,
        } for question_id in question_ids] if supported else [],
        "completeness": [{
            "key": "subject_role", "relationship_key": "describes", "kind": "required_role",
            "question_ids": question_ids, "ordered": False, "ordinal_property_key": None,
            "rationale": "Review needs the governed subject role.",
        }] if supported else [],
        "review_concerns": [],
    }


class _RecordedDesignClient:
    def __init__(self, *, supported: bool) -> None:
        self.response = _design_response(supported=supported)
        self.requests: list[dict] = []

    def complete_json(self, **request) -> dict:
        self.requests.append(request)
        return json.loads(json.dumps(self.response))


def _actual_design_and_evaluation(
    design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, supported: bool,
) -> tuple[Path, Path, _RecordedDesignClient]:
    model = _RecordedDesignClient(supported=supported)
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (model, "unit-design-model"))
    result = CliRunner().invoke(cli, [*design_paths[3], "--live", "--max-calls", "2"])
    assert result.exit_code == 0, result.output
    draft = design_paths[2]
    evaluation = tmp_path / "actual-evaluation.json"
    result = CliRunner().invoke(cli, [
        "domain", "evaluate-design", "--file", str(draft), "--out", str(evaluation),
    ])
    assert result.exit_code == 0, result.output
    return draft, evaluation, model


def test_actual_core_saves_zero_coverage_and_blocks_compile(
    design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft, evaluation, model = _actual_design_and_evaluation(
        design_paths, tmp_path, monkeypatch, supported=False,
    )
    saved = json.loads(draft.read_text(encoding="utf-8"))
    report = json.loads(evaluation.read_text(encoding="utf-8"))
    assert saved["artifact_kind"] == "domain.design_draft"
    assert saved["sketch"]["types"][0]["question_ids"] == []
    assert saved["sketch"]["question_routes"] == []
    assert report["answer_verification"] == "not_performed"
    assert report["compiler_limitations"]
    assert len(model.requests) == 1
    before = draft.read_bytes(), evaluation.read_bytes()
    state, domain = tmp_path / "state", tmp_path / "domain.yaml"
    result = CliRunner().invoke(cli, [
        "domain", "compile-design", "--file", str(draft), "--input", str(design_paths[0]),
        "--evaluation", str(evaluation), "--accept-evaluation-hash", report["evaluation_hash"],
        "--out-state", str(state), "--out-domain", str(domain),
    ])
    assert result.exit_code == 1, result.output
    assert not state.exists() and not domain.exists()
    assert (draft.read_bytes(), evaluation.read_bytes()) == before
    assert len(model.requests) == 1


@pytest.mark.parametrize("dry_run,source_drift", [(True, False), (False, False), (False, True)])
def test_actual_core_compilation_preserves_unapproved_l1_and_source_binding(
    design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    dry_run: bool, source_drift: bool,
) -> None:
    draft, evaluation, model = _actual_design_and_evaluation(
        design_paths, tmp_path, monkeypatch, supported=True,
    )
    report = json.loads(evaluation.read_text(encoding="utf-8"))
    if source_drift:
        (design_paths[0] / "records.txt").write_text("Changed after design.", encoding="utf-8")
    state, domain = tmp_path / "state", tmp_path / "domain.yaml"
    args = [
        "domain", "compile-design", "--file", str(draft), "--input", str(design_paths[0]),
        "--evaluation", str(evaluation), "--accept-evaluation-hash", report["evaluation_hash"],
        "--out-state", str(state), "--out-domain", str(domain),
    ]
    result = CliRunner().invoke(cli, (["--dry-run"] if dry_run else []) + args)
    assert result.exit_code == (1 if source_drift else 0), result.output
    assert len(model.requests) == 1
    if dry_run or source_drift:
        assert not state.exists() and not domain.exists()
    else:
        from fabric_kg_builder.domain.service import load_domain_contract
        compiled = load_domain_contract(str(domain))
        assert compiled.approval.status != "approved"
        assert len(compiled.candidate_model.entity_types) == 2
        assert len(compiled.candidate_model.relationship_types) == 1
        receipt = json.loads((state / "stage-receipt.json").read_text(encoding="utf-8"))
        assert receipt["status"] == "blocked"
        assert json.loads(result.output)["approval_granted"] is False


def test_actual_core_prompt_and_saved_seed_preserve_reference_context(
    design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = tmp_path / "seed.yaml"
    raw = (
        b'seed_kind: user_reference_sketch\nauthority: design_input_only\n'
        b'entities:\n  - name: Part\n    illustrative_example: "001234"\n'
        b'relationships:\n  installed_on: "Reference only"\n'
    )
    seed.write_bytes(raw)
    model = _RecordedDesignClient(supported=False)
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (model, "unit-design-model"))
    result = CliRunner().invoke(cli, [
        *design_paths[3], "--seed-domain", str(seed), "--live",
    ])
    assert result.exit_code == 0, result.output
    saved = json.loads(design_paths[2].read_text(encoding="utf-8"))
    assert saved["seed"]["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert saved["seed"]["raw_yaml"] == raw.decode("utf-8")
    assert saved["seed"]["parsed_content"]["entities"][0]["illustrative_example"] == "001234"
    assert saved["seed"]["authority"] == "reference_only"
    assert "installed_on" in model.requests[0]["user"]
    assert "001234" in model.requests[0]["user"]
    assert all("001234" not in json.dumps(span) for span in saved["samples"]["evidence_spans"])


def test_actual_core_description_is_saved_and_reaches_prompt(
    design_paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = "Additional maintenance design context."
    model = _RecordedDesignClient(supported=False)
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (model, "unit-design-model"))
    result = CliRunner().invoke(cli, [
        *design_paths[3], "--live", "--description", description,
    ])
    assert result.exit_code == 0, result.output
    assert description in model.requests[0]["user"]
    assert description in design_paths[2].read_text(encoding="utf-8")


@pytest.mark.parametrize("live", [False, True])
def test_opt_in_trace_is_private_and_never_written_during_planning(
    design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool,
) -> None:
    trace = tmp_path / "traces"
    model = _RecordedDesignClient(supported=False)
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (model, "unit-design-model"))
    args = [*design_paths[3], "--proposal-trace-dir", str(trace)]
    result = CliRunner().invoke(cli, args + (["--live"] if live else []))
    assert result.exit_code == 0, result.output
    if not live:
        assert not trace.exists()
        assert model.requests == []
    else:
        events = list(trace.glob("*/*.json"))
        assert len(events) == 2
        assert {json.loads(path.read_text())["event"] for path in events} == {
            "request_started", "response_completed",
        }
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in events)


def test_invalid_model_design_retains_completed_response_trace(
    design_paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "traces"
    model = _RecordedDesignClient(supported=False)
    model.response["unexpected_field"] = "invalid but auditable"
    monkeypatch.setattr(commands, "_build_client", lambda _ctx: (model, "unit-design-model"))
    result = CliRunner().invoke(cli, [
        *design_paths[3], "--live", "--proposal-trace-dir", str(trace),
    ])
    assert result.exit_code == 1, result.output
    assert not design_paths[2].exists()
    completed = list(trace.glob("*/001-response_completed-*.json"))
    assert len(completed) == 1
    assert json.loads(completed[0].read_text())["response"] == model.response
