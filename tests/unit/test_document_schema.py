"""Offline whole-document schema discovery, using the existing approval bridge."""

import copy
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain import document_schema
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.domain.window_run import (
    RunConfig, load_windowed_run, plan_window_run, run_windowed, windowed_status,
)
from tests.unit.test_window_run import inputs, budget


def profile(**overrides):
    return {
        "deployment": "offline-gpt41", "model_name": "gpt-4.1", "model_version": "2025-04-14",
        "deployment_sku": "GlobalStandard", "endpoint": "https://offline.openai.azure.com",
        "context_tokens": 128000, "max_input_tokens": 128000, "max_output_tokens": 32768,
        "source": "offline-test-explicit-profile", **overrides,
    }


def config(**overrides):
    return RunConfig(**{
        "discovery_mode": "whole-document", "prompt_version": document_schema.PROMPT_VERSION,
        "capability_profile": profile(), "max_chunk_chars": 128, **overrides,
    })


def document_sdk(callback, *, response_model="gpt-4.1-2025-04-14", transport="chat_completions"):
    def create(**kwargs):
        if transport == "project_responses":
            user = kwargs["input"].removeprefix("Return only a valid JSON object for this request.\n")
            system = kwargs["instructions"]
        else:
            system, user = [row["content"] for row in kwargs["messages"]]
        raw = callback(
            user=user, system=system, json_schema=document_schema.DocumentSchemaResponse.model_json_schema())
        return SimpleNamespace(
            model=response_model, output_text=canonical_json(raw), status="completed",
            choices=[SimpleNamespace(message=SimpleNamespace(content=canonical_json(raw)), finish_reason="stop")])

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                          responses=SimpleNamespace(create=create))

    def with_options(**kwargs):
        assert kwargs == {"max_retries": 0}
        return sdk

    sdk.with_options = with_options
    return sdk


class DocumentModel(FoundryClient):
    def __init__(self, callback=None):
        self.requests = []
        self.callback = callback
        self._config = FoundryConfig(
            endpoint=profile()["endpoint"], openai_endpoint=profile()["endpoint"],
            chat_deployment=profile()["deployment"], chat_model=profile()["model_name"])
        self._client = document_sdk(self.complete_json)

    def complete_json(self, **request):
        self.requests.append(copy.deepcopy(request))
        payload = json.loads(request["user"])["input"]
        if self.callback:
            return self.callback(payload)
        sections = payload["document"]["sections"]
        concept = {
            "concept_id": "type:record", "kind": "entity", "name": "Record",
            "definition": "A reusable governed record", "identity_policy": {"mode": "unresolved"},
        }
        known = payload["schema"]["concepts"]
        if known:
            concept["definition"] = "A reusable governed record refined by the next document"
        return {"schema_proposals": [{
            "action": "update_concept" if known else "add_concept", "concept": concept,
            "reason": "Reconcile the reusable business role",
            "witnesses": [{"ref": sections[-1]["ref"], "quote": sections[-1]["text"][:80]}],
            **({"layer": "common", "scope_change": "broadening" if known else "additive",
                "generalization_reason": "Governed records remain reusable across documents."}
               if "CROSS-DOCUMENT GENERALIZATION" in request.get(
                   "system", "CROSS-DOCUMENT GENERALIZATION") else {}),
        }], "pending": []}


@pytest.mark.parametrize("output_tokens", [8192, 128000])
def test_gpt54_custom_alias_document_preflight_and_dispatch(tmp_path, output_tokens):
    data = inputs(tmp_path, files=1, paragraphs=3)
    model = DocumentModel()
    model._config = model._config.model_copy(update={"chat_model": "gpt-5.4"})
    model._client = document_sdk(model.complete_json, response_model="gpt-5.4-2026-03-05")
    cfg = config(max_completion_tokens=output_tokens, capability_profile=profile(
        model_name="gpt-5.4", model_version="2026-03-05", context_tokens=1050000,
        max_input_tokens=922000, max_output_tokens=128000))
    assert plan_window_run(data, cfg)["minimum_schema_calls"] == 1
    result = run_windowed(inputs=data, output_dir=tmp_path / "gpt54", config=cfg,
                          budget=budget(max_calls=1), client=model)
    assert result.state == "complete" and len(model.requests) == 1
    assert result.final_snapshot.concepts[0].name == "Record"
    physical = json.loads(next((tmp_path / "gpt54/physical-requests").glob("*.json")).read_text())
    assert physical["payload"]["request_accounting"]["output_reserve_tokens"] == output_tokens


def test_serial_complete_documents_resume_and_exact_earlier_chunk_coverage(tmp_path):
    data = inputs(tmp_path, files=2, paragraphs=5, discovery=True)
    model = DocumentModel()
    cfg = config(window_size=1, max_concurrency=16)
    plan = plan_window_run(data, cfg)
    assert plan["window_count"] == plan["minimum_schema_calls"] == 2
    assert plan["minimum_extraction_calls"] == 0
    assert all(row["reserved_tokens"] > 8192 for row in plan["input_preflight"]["documents"])
    root = tmp_path / "documents"
    first = run_windowed(inputs=data, output_dir=root, config=cfg,
                         budget=budget(stop_after_document=1), client=model)
    assert first.state == "partial" and len(first.logs) == len(model.requests) == 1
    second = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(), client=model)
    assert second.state == "complete" and len(model.requests) == 2
    assert second.final_snapshot.version == 2
    assert second.chunk_plan == data.chunk_plan
    assert len(second.chunks) == len(data.chunk_plan) > 2
    assert not second.final_mapping.records
    assert all(not item.raw_response["candidates"] for item in second.chunks)
    assert "refined" in second.final_snapshot.concepts[0].definition
    assert load_windowed_run(root) == second.run
    from fabric_kg_builder.domain.window_design_context import window_design_context
    design_context = window_design_context(second.run)
    assert design_context["schema_layers"] == {"type:record": "common"}
    assert design_context["generalization_policy"]["prompt_version"] == document_schema.PROMPT_VERSION
    status = windowed_status(root)
    assert status["discovery_mode"] == "whole-document"
    assert status["token_accounting"] == "serialized_sdk_envelope_tokenizer_estimate_with_headroom"
    for index, request in enumerate(model.requests):
        payload = json.loads(request["user"])["input"]
        expected = [unit for unit in data.prepared.source_units
                    if unit.source_file_id == data.prepared.sources[index].source_file_id]
        assert [row["text"] for row in payload["document"]["sections"]] == [unit.text for unit in expected]
        assert payload["context"]["intake"] == data.context.intake_raw
        assert payload["schema"]["version"] == index
        assert "candidates" not in request["json_schema"]["properties"]
        assert "source_unit_id" not in canonical_json(payload["document"])
        assert "locator_hash" not in canonical_json(payload["document"])
        assert "untrusted data" in request["system"]
    before = canonical_sha256(second.run)
    replay = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(max_calls=0))
    assert canonical_sha256(replay.run) == before and replay.model_call_count == 0
    assert len(model.requests) == 2


@pytest.mark.parametrize("change", ["candidates", "bad-ref", "bad-quote", "owner", "identity"])
def test_reject_instances_unbound_witnesses_and_invalid_definitions(tmp_path, change):
    data = inputs(tmp_path, files=1, paragraphs=2)
    default = DocumentModel()

    def respond(payload):
        raw = default.complete_json(user=canonical_json({"input": payload}))
        proposal = raw["schema_proposals"][0]
        if change == "candidates":
            raw["candidates"] = []
        elif change == "bad-ref":
            proposal["witnesses"][0]["ref"] = "other-document:s1"
        elif change == "bad-quote":
            proposal["witnesses"][0]["quote"] = "not found in any source"
        elif change == "owner":
            proposal["concept"].update(kind="property", owner_type_ids=["missing:owner"], value_type="number")
        else:
            proposal["concept"]["identity_policy"] = {"mode": "business_key"}
        return raw

    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config(),
                          budget=budget(), client=DocumentModel(respond))
    assert result.state == "partial" and not result.final_snapshot.concepts
    if change == "candidates":
        assert result.cursor == 0 and "response_validation_failed" in result.reason
    else:
        assert result.logs[0].diagnostics
    assert not result.final_mapping.records


def test_unknown_capability_and_overflow_fail_before_dispatch(tmp_path):
    with pytest.raises(ValueError):
        config(capability_profile=profile(model_name="unknown"))
    data = inputs(tmp_path, files=1, paragraphs=5)
    model = DocumentModel()
    small = config(capability_profile=profile(
        context_tokens=2048, max_input_tokens=2048, max_output_tokens=256), max_completion_tokens=256)
    with pytest.raises(ValueError, match="DOCUMENT_MODEL_CONTEXT_EXCEEDED"):
        plan_window_run(data, small)
    root = tmp_path / "windows"
    with pytest.raises(ValueError, match="DOCUMENT_MODEL_CONTEXT_EXCEEDED"):
        run_windowed(inputs=data, output_dir=root, config=small, budget=budget(), client=model)
    assert not model.requests
    assert not list((root / "dispatches").glob("*.json"))
    model._config = model._config.model_copy(update={"chat_deployment": "wrong"})
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        run_windowed(inputs=data, output_dir=tmp_path / "wrong", config=config(), budget=budget(), client=model)


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
@pytest.mark.parametrize("response_model", [None, "gpt-4.1-2099-01-01", "gpt-4.1-2025-04-14"])
def test_actual_provider_model_guard_and_persistent_binding(tmp_path, transport, response_model):
    data = inputs(tmp_path, files=1, paragraphs=2)
    model = DocumentModel()
    model._config = model._config.model_copy(update={"inference_api": transport})
    model._client = document_sdk(model.complete_json, response_model=response_model, transport=transport)
    root = tmp_path / "windows"
    cfg = config(model_transport=transport)
    result = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(), client=model)
    physical = list((root / "physical-requests").glob("*.json"))
    assert len(physical) == len(model.requests) == 1
    if response_model == "gpt-4.1-2025-04-14":
        assert result.state == "complete"
        assert result.logs[0].exchanges[0].response.payload["provider_model"] == response_model
        assert load_windowed_run(root) == result.run
        return
    assert result.cursor == 0 and not result.final_snapshot.concepts
    assert result.reason == "received_invalid_response_requires_explicit_retry"
    diagnostic = json.loads(next((root / "diagnostics").glob("*.json")).read_text())
    assert diagnostic["diagnostics"]["parse_error"]["provider_model"] == response_model
    assert not list((root / "responses").glob("*.json"))
    # Fixing a deployment's target does not silently authorize another request.
    model._client = document_sdk(model.complete_json, transport=transport)
    paused = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(), client=model)
    assert paused.model_call_count == 0
    resumed = run_windowed(inputs=data, output_dir=root, config=cfg,
                          budget=budget(retry_invalid_response=True), client=model)
    assert resumed.state == "complete" and len(model.requests) == 2


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
@pytest.mark.parametrize("status_code", [400, 500])
def test_hidden_retries_require_new_budgeted_coordinator_dispatch(tmp_path, monkeypatch, transport, status_code):
    import httpx
    from openai import BadRequestError, InternalServerError
    from fabric_kg_builder.enrichment import foundry_client

    strict_schema = lambda _: {"type": "object", "properties": {}, "additionalProperties": False}
    monkeypatch.setattr(foundry_client, "_azure_strict_schema", strict_schema)
    monkeypatch.setattr(foundry_client._TRANSPORT_OUTAGE_BREAKER, "sleep", lambda _: None)
    model = DocumentModel()
    model._config = model._config.model_copy(update={"inference_api": transport})
    attempts = []

    def reject(**kwargs):
        attempts.append(kwargs)
        error_type = BadRequestError if status_code == 400 else InternalServerError
        raise error_type(
            "strict response format rejected" if status_code == 400 else "server failed",
            response=httpx.Response(status_code, request=httpx.Request("POST", "https://example.test")),
            body={"code": "invalid_json_schema", "param": "response_format"},
        )

    model._client.chat.completions.create = reject
    model._client.responses.create = reject
    root = tmp_path / "windows"
    data = inputs(tmp_path, files=1)
    cfg = config(model_transport=transport)
    result = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(max_calls=1), client=model)
    assert result.state == "partial" and result.cursor == 0
    assert result.model_call_count == 1 and result.reserved_tokens > cfg.max_completion_tokens
    assert len(attempts) == len(list((root / "physical-requests").glob("*.json"))) == 1
    errors = list((root / "physical-errors").glob("*.json"))
    assert len(errors) == 1
    assert json.loads(errors[0].read_text())["payload"]["status_code"] == status_code
    errors = [json.loads(path.read_text()) for path in (root / "errors").glob("*.json")]
    assert any(row["payload"]["error_type"] == (
        "BadRequestError" if status_code == 400 else "InternalServerError") for row in errors)
    model._client = document_sdk(model.complete_json, transport=transport)
    for retry_budget in (
        budget(max_calls=0, retry_uncertain=True),
        budget(max_calls=1, max_tokens=result.reserved_tokens - 1, retry_uncertain=True),
        budget(max_calls=1),
    ):
        stopped = run_windowed(inputs=data, output_dir=root, config=cfg, budget=retry_budget, client=model)
        assert stopped.model_call_count == stopped.reserved_tokens == 0
        assert not model.requests and len(list((root / "physical-requests").glob("*.json"))) == 1
    resumed = run_windowed(
        inputs=data, output_dir=root, config=cfg, client=model,
        budget=budget(max_calls=1, max_tokens=result.reserved_tokens, retry_uncertain=True))
    assert resumed.state == "complete" and resumed.model_call_count == 1 and len(model.requests) == 1
    assert resumed.run.model_call_count == 2
    assert resumed.run.reserved_tokens == result.reserved_tokens + resumed.reserved_tokens
    physical = [json.loads(path.read_text()) for path in (root / "physical-requests").glob("*.json")]
    assert len(physical) == 2 and len({row["payload"]["dispatch_hash"] for row in physical}) == 2
    assert all(row["payload"]["request_accounting"]["estimated_total_tokens"] <= result.reserved_tokens
               for row in physical)


@pytest.mark.parametrize("extra_tokens,error", [
    (4096, "PHYSICAL_RESERVATION_DRIFT"), (140000, "DOCUMENT_MODEL_CONTEXT_EXCEEDED"),
])
def test_actual_sdk_request_is_checked_before_physical_reservation(tmp_path, monkeypatch, extra_tokens, error):
    original = FoundryClient.complete_json

    def drift(self, **request):
        return original(self, **{**request, "user": request["user"] + " excess" * extra_tokens})

    monkeypatch.setattr(FoundryClient, "complete_json", drift)
    model = DocumentModel()
    root = tmp_path / "windows"
    result = run_windowed(inputs=inputs(tmp_path, files=1), output_dir=root,
                          config=config(), budget=budget(), client=model)
    assert result.state == "partial" and result.cursor == 0
    assert not model.requests and not list((root / "physical-requests").glob("*.json"))
    errors = [json.loads(path.read_text()) for path in (root / "errors").glob("*.json")]
    assert any(error in row["payload"]["error"] for row in errors)


def test_legacy_config_hash_serialization_unchanged():
    assert RunConfig().model_dump(mode="json") == {
        "window_size": 8, "max_concurrency": 4, "max_request_chars": 96000,
        "max_completion_tokens": 8192, "max_chunk_chars": 8000,
        "prompt_version": "raw-working-window/1.1.0",
    }
    with pytest.raises(ValueError, match="own prompt"):
        RunConfig(discovery_mode="whole-document", capability_profile=profile())
    with pytest.raises(ValueError, match="chunked"):
        RunConfig(capability_profile=profile())


def test_legacy_whole_document_replay_retains_exact_prompt_and_response(tmp_path):
    data = inputs(tmp_path, files=2, paragraphs=2)
    root = tmp_path / "legacy-documents"
    cfg = config(prompt_version=document_schema.LEGACY_PROMPT_VERSION)
    model = DocumentModel()
    first = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(), client=model)
    assert first.state == "complete"
    assert all(log.exchanges[0].request.payload["request"]["system"] == document_schema.LEGACY_SYSTEM
               for log in first.logs)
    assert all("schema_layers" not in json.loads(request["user"])["input"] for request in model.requests)
    replay = run_windowed(inputs=data, output_dir=root, config=cfg, budget=budget(max_calls=0))
    assert replay.model_call_count == 0 and replay.run == first.run
    from fabric_kg_builder.domain.window_design_context import window_design_context
    legacy_context = window_design_context(first.run)
    assert "schema_layers" not in legacy_context and "generalization_policy" not in legacy_context


@pytest.mark.parametrize("change", ["narrowing", "layer", "rename", "parent"])
def test_later_document_cannot_specialize_earlier_common_schema(tmp_path, change):
    data = inputs(tmp_path, files=2, paragraphs=2)
    default = DocumentModel()

    def respond(payload):
        raw = default.complete_json(user=canonical_json({"input": payload}))
        if payload["schema"]["concepts"]:
            assert payload["schema_layers"] == {"type:record": "common"}
            proposal = raw["schema_proposals"][0]
            if change == "narrowing":
                proposal["scope_change"] = "narrowing"
            elif change == "layer":
                proposal["layer"] = "domain"
            elif change == "rename":
                proposal["concept"]["name"] = "Document Specific Record"
            else:
                proposal["concept"]["parent_type_id"] = "type:specific"
        return raw

    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config(),
                          budget=budget(), client=DocumentModel(respond))
    assert len(result.logs) == 2
    assert result.final_snapshot.concepts[0].definition == "A reusable governed record"
    assert len(result.final_snapshot.concepts) == 1
    assert "schema generalization:" in result.logs[1].diagnostics[0].reason
    assert load_windowed_run(tmp_path / "windows") == result.run


@pytest.mark.parametrize("change", ["owner_type_ids", "aliases", "endpoint_policy", "value_type"])
def test_generalization_guard_preserves_prior_property_scope(change):
    prior = document_schema.WorkingConcept(
        concept_id="domain.quantity", kind="property", name="Quantity",
        definition="A required item quantity.", owner_type_ids=["type:part", "type:tool"],
        aliases=["Count"], value_type="integer", endpoint_policy="allow_subtypes")
    values = {
        "owner_type_ids": ["type:part"], "aliases": [], "endpoint_policy": "exact",
        "value_type": "string",
    }
    concept = prior.model_copy(update={change: values[change]})
    proposal = document_schema.GeneralizedDefinitionProposal(
        action="update_concept", concept=concept, reason="Latest document update",
        witnesses=[{"ref": "s1", "quote": "a quantity"}], layer="domain",
        scope_change="broadening", generalization_reason="Claimed broadening is not enough.")
    with pytest.raises(ValueError, match="schema generalization:"):
        document_schema._validate_generalization(proposal, prior)
    widened = proposal.model_copy(update={"concept": prior.model_copy(update={"value_type": "number"})})
    document_schema._validate_generalization(widened, prior)


def test_generalized_prompt_examples_are_valid_and_not_source_evidence():
    system = document_schema.system_prompt(document_schema.PROMPT_VERSION)
    assert "{{a_few_shot}}" not in system
    examples = json.loads(system[system.index('[\n  {\n    "source"'):])
    for example in examples:
        document_schema.response_model(document_schema.PROMPT_VERSION).model_validate(example["response"])
    assert examples[1]["response"]["schema_proposals"] == []
    assert "NOT evidence for this run" in system
    assert "one complete document" in system.lower()


def test_parent_removal_requires_review_to_preserve_inherited_scope():
    prior = document_schema.WorkingConcept(
        concept_id="domain.pump", kind="entity", name="Pump", definition="A fluid-moving device.",
        parent_type_id="common.equipment", identity_policy={"mode": "unresolved"})
    proposal = document_schema.GeneralizedDefinitionProposal(
        action="update_concept", concept=prior.model_copy(update={"parent_type_id": None}),
        reason="Remove parent", witnesses=[{"ref": "s1", "quote": "Pump"}],
        layer="domain", scope_change="broadening",
        generalization_reason="Claimed broadening would remove inherited property/endpoint scope.")
    with pytest.raises(ValueError, match="reparenting requires review"):
        document_schema._validate_generalization(proposal, prior)


def test_empty_first_document_does_not_prevent_later_schema_discovery(tmp_path):
    data = inputs(tmp_path, files=2, paragraphs=2)
    fallback = DocumentModel()
    calls = []

    def respond(payload):
        calls.append(payload)
        return ({"schema_proposals": [], "pending": []} if len(calls) == 1
                else fallback.complete_json(user=canonical_json({"input": payload})))

    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config(),
                          budget=budget(), client=DocumentModel(respond))
    assert result.state == "complete" and result.final_snapshot.version == 2
    assert len(result.chunks) == len(result.chunk_plan)
    assert not calls[1]["schema"]["concepts"]


def test_compact_ref_drift_fails_on_sealed_read(tmp_path):
    from fabric_kg_builder.domain.discovery import _seal
    from fabric_kg_builder.domain.window_run import WindowedRun, WindowedLog, WindowedExchange, _Ledger

    data = inputs(tmp_path, files=1, paragraphs=3)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config(),
                          budget=budget(), client=DocumentModel())
    log = result.logs[0]
    original = log.exchanges[0]
    payload = copy.deepcopy(original.request.model_dump(mode="json")["payload"])
    payload["source_references"]["s1"]["source_unit_id"] = "other-document"
    request = _seal(_Ledger, kind="request", payload=payload)
    response = _seal(_Ledger, kind="response", payload={
        **original.response.payload, "request_hash": request.artifact_hash})
    changed_log = _seal(WindowedLog, **{
        **{key: getattr(log, key) for key in type(log).model_fields if key != "artifact_hash"},
        "request_hashes": [request.artifact_hash], "response_hashes": [response.artifact_hash],
        "exchanges": [WindowedExchange(request=request, response=response)],
    })
    with pytest.raises(ValueError, match="compact source reference"):
        _seal(WindowedRun, **{
            **{key: getattr(result.run, key) for key in WindowedRun.model_fields if key != "artifact_hash"},
            "logs": [changed_log],
        })


@pytest.mark.parametrize("profile_option", ["--model-capabilities", "--capability-profile"])
def test_public_cli_opt_in_plan_and_profile_required(tmp_path, profile_option):
    inputs(tmp_path, files=2, paragraphs=3)
    args = [
        "domain", "window-run", "--prepared", str(tmp_path / "prepared.json"),
        "--intake", str(tmp_path / "intake.json"), "--out-state", str(tmp_path / "windows"),
        "--discovery-mode", "whole-document",
    ]
    missing = CliRunner().invoke(cli, args)
    assert missing.exit_code != 0 and "explicit capability profile" in missing.output
    path = tmp_path / "profile.json"
    path.write_text(canonical_json(profile()))
    planned = CliRunner().invoke(cli, [*args, profile_option, str(path)])
    assert planned.exit_code == 0, (planned.output, planned.exception)
    result = json.loads(planned.output)
    assert result["writes"] == result["model_calls"] == 0
    assert result["result"]["minimum_schema_calls"] == 2
    assert not (tmp_path / "windows").exists()


def test_public_approval_then_fresh_extraction_revisits_both_documents(tmp_path, monkeypatch):
    from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
    from fabric_kg_builder.domain.service import load_domain_contract
    from fabric_kg_builder.domain.stage import preflight_l1_inputs
    from fabric_kg_builder.enrichment.approved_reextraction import run_approved_reextraction
    from fabric_kg_builder.enrichment.foundry_client import FoundryClient
    from fabric_kg_builder.enrichment.window_run_reuse import prepare_window_run_reuse
    from fabric_kg_builder.sources.preparation import indexed_corpus_reader
    from tests.unit.test_approved_reextraction import _config, _no_source_reads, _sdk
    from tests.unit.test_domain_discovery_cli import _invoke, _paths
    from tests.unit.test_schema2_two_documents import _approved_response
    from tests.unit.test_window_run_approved_cli import IntegratedModel, _approve_integrated, _review

    source, intake, _, _, _ = _paths(tmp_path, count=2)
    for index, suffix in enumerate(("Alpha", "Beta")):
        (source / f"records-{index}.html").write_text(
            f"<p>A governed record {suffix} describes a governed subject {suffix} at {80.5 + index} volts.</p>")
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:offline-document-prepared",
        model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"))
    prepared_path = tmp_path / "prepared.json"
    prepared_path.write_text(canonical_json(prepared))
    profile_path = tmp_path / "capability.json"
    profile_path.write_text(canonical_json(profile()))
    windows = tmp_path / "whole-documents"

    class BridgeModel(IntegratedModel, FoundryClient):
        _config = DocumentModel()._config

        def __init__(self):
            super().__init__()
            self.document_requests = []
            self._client = document_sdk(self.complete_json)

        def complete_json(self, **request):
            if "candidates" not in request["json_schema"]["properties"] and "schema_proposals" in request["json_schema"]["properties"]:
                payload = json.loads(request["user"])["input"]
                self.document_requests.append(payload)
                if payload["schema"]["concepts"]:
                    return {"schema_proposals": [], "pending": []}
                witness = {"ref": payload["document"]["sections"][0]["ref"],
                           "quote": payload["document"]["sections"][0]["text"]}
                concepts = [
                    {"concept_id": "working:record", "kind": "entity", "name": "Service Record",
                     "definition": "A source service record.", "identity_policy": {"mode": "unresolved"}},
                    {"concept_id": "working:subject", "kind": "entity", "name": "Service Subject",
                     "definition": "A common service subject.", "identity_policy": {"mode": "unresolved"}},
                    {"concept_id": "working:describes", "kind": "relationship", "name": "Describes",
                     "definition": "Record describes a subject.", "source_type_ids": ["working:record"],
                     "target_type_ids": ["working:subject"],
                     "identity_policy": {"mode": "unresolved", "context_policy": "source record"}},
                    {"concept_id": "working:voltage", "kind": "property", "name": "Observed Voltage",
                     "definition": "Subject voltage.", "owner_type_ids": ["working:subject"], "value_type": "number"},
                ]
                return {"schema_proposals": [
                    {"action": "add_concept", "concept": concept, "witnesses": [witness],
                     "reason": "Reusable role, predicate or property in the source",
                     "layer": "domain", "scope_change": "additive",
                     "generalization_reason": "Service roles remain reusable across source documents."}
                    for concept in concepts
                ], "pending": []}
            return super().complete_json(**request)

    model = BridgeModel()
    result = _invoke([
        "domain", "window-run", "--prepared", str(prepared_path), "--intake", str(intake),
        "--discovery-mode", "whole-document", "--capability-profile", str(profile_path),
        "--out-state", str(windows), "--max-calls", "2", "--live",
    ], model=model)
    assert result["status"] == "complete"
    assert len(model.document_requests) == 2
    assert not model.document_requests[0]["schema"]["concepts"]
    assert len(model.document_requests[1]["schema"]["concepts"]) == 4
    blocked = CliRunner().invoke(cli, [
        "lineage", "trace", "record-1", "--l2-state", str(windows),
        "--l1-state", str(tmp_path / "l1"), "--domain", str(tmp_path / "domain.yaml"), "--format", "json",
    ])
    assert blocked.exit_code != 0 and "LINEAGE_SCHEMA_NOT_FROZEN" in blocked.output
    l1, domain, _ = _approve_integrated(tmp_path, source, intake, windows, model)
    _review(windows, domain, tmp_path / "mapping-review.json")
    with pytest.raises(ValueError, match="WHOLE_DOCUMENT_SCHEMA_REQUIRES_REEXTRACTION"):
        prepare_window_run_reuse(windows, source, l1, domain)
    before = canonical_sha256(load_windowed_run(windows))
    contract_hash = load_domain_contract(domain).approval.contract_hash
    _no_source_reads(monkeypatch)
    sdk, requests = _sdk(_approved_response)
    l2 = tmp_path / "l2"
    options = dict(
        source_path=source, l1_state_root=l1, domain_path=domain, state_root=l2,
        window_run_path=windows, foundry_config=_config(), max_calls=2, max_output_tokens=4096)
    dry = run_approved_reextraction(
        **options, dry_run=True, client_factory=lambda: pytest.fail("dry run constructed client"))
    assert dry["planned_chunks"] == 2 and dry["original_source_reads"] == 0
    fresh = run_approved_reextraction(
        **options, client_factory=lambda: FoundryClient(_config(), _sdk_client=sdk))
    assert fresh["logical_calls"] == fresh["physical_calls"] == len(requests) == 2
    assert fresh["data_lineage"]["enabled_after"] == "L1_approval"
    payloads = [json.loads(request["messages"][1]["content"]) for request in requests]
    assert len({payload["source_identity"]["source_unit_id"] for payload in payloads}) == 2
    assert {payload["domain_contract_hash"] for payload in payloads} == {contract_hash}
    assert canonical_sha256(load_windowed_run(windows)) == before
