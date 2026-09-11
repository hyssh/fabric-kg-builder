"""Same-item presentation repair transport and immutable approval regression."""

import copy
import json
import re
import uuid

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy import schema2_ontology_presentation as m
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy.fabric_ontology_definition import _part
from tests.unit.test_schema2_prototype_agent import _Response, WORKSPACE
from tests.unit.test_schema2_prototype_reconcile import paused, _accept  # noqa: F401

PRODUCTION_RENDER = m._render
PRODUCTION_CODES = m._code_hashes


@pytest.fixture(autouse=True)
def legacy_native_compilation(monkeypatch):
    original = p._ontology_parts
    original_compile = p._compile

    def legacy(*args, **kwargs):
        kwargs["legacy_names"] = True
        return original(*args, **kwargs)

    def legacy_compile(*args, **kwargs):
        compiled = original_compile(*args, **kwargs)
        compiled.definitions["ontology"].pop("presentation_catalog", None)
        for name, definition in compiled.definitions.items():
            definition["publication_code_version"] = "l5a-publication/1.1.0"
            compiled.provenance["definition_hashes"][name] = canonical_sha256(definition)
        return compiled

    monkeypatch.setattr(p, "_ontology_parts", legacy)
    monkeypatch.setattr(p, "_compile", legacy_compile)
    return original_compile


def _render(definition, compilation, source):
    result = copy.deepcopy(definition)
    mapping = []
    for index, part in enumerate(result["parts"]):
        path = part["path"]
        if path.startswith("EntityTypes/") and path.endswith("/definition.json"):
            entity = m._decode({"parts": [part]})[path]
            entity["name"] = "Readable_" + entity["id"]
            entity["semanticEnrichment"]["description"] = "Approved source type"
            entity["displayNamePropertyId"] = next(
                prop["id"] for prop in entity["properties"] if prop["name"] == "label"
            )
            result["parts"][index] = _part(path, entity)
            mapping.append({"id": entity["id"], "name": entity["name"]})
    return result, mapping


@pytest.fixture
def repair(paused, monkeypatch, legacy_native_compilation):
    case = paused
    _accept(case)
    monkeypatch.setattr(p, "_compile", legacy_native_compilation)
    # These tests isolate repair's independent invariants and protocol. Renderer
    # authority/naming tests exercise the production transform separately.
    monkeypatch.setattr(m, "_render", _render)
    monkeypatch.setattr(m, "_code_hashes", lambda: {
        "compiler_hash": "presentation-runtime",
        "naming_policy_hash": "fixture-readable-v1", "repair_code_hash": "fixture-repair-v1",
    })
    monkeypatch.setattr(m, "_retry", lambda _: None)
    original_request = case.backend.request
    case.updates = []
    case.mode = "200-empty"
    case.operation_id = str(uuid.uuid4())
    case.read_lro = False
    case.pending_definition = None
    case.operation_status = "Succeeded"

    def request(method, url, **kwargs):
        path = url.split("?", 1)[0]
        if method == "POST" and path.endswith("/updateDefinition"):
            assert url.endswith("?updateMetadata=false")
            assert (case.state / "update-intent.json").is_file()
            assert kwargs["json"].keys() == {"definition"}
            case.updates.append(copy.deepcopy(kwargs["json"]))
            if case.mode == "lost-before":
                raise TimeoutError("Lost connection before known server outcome")
            if case.mode != "mismatch":
                case.backend.definitions[case.item_id] = copy.deepcopy(kwargs["json"]["definition"])
            if case.mode == "service-default":
                definition = case.backend.definitions[case.item_id]
                for index, part in enumerate(definition["parts"]):
                    if part["path"].startswith("RelationshipTypes/") and part["path"].endswith("/definition.json"):
                        payload = m._decode({"parts": [part]})[part["path"]]
                        payload["semanticEnrichment"]["customAttributes"] = {}
                        definition["parts"][index] = _part(part["path"], payload)
            if case.mode == "lost-after":
                raise TimeoutError("Lost connection after accepted update")
            if case.mode == "metadata-drift":
                case.backend.items[case.item_id]["sensitivityLabel"] = {"id": "changed"}
            if case.mode == "202-null":
                response = _Response(None, 202, {
                    "Location": f"{p.API}/operations/{case.operation_id}",
                    "x-ms-operation-id": case.operation_id, "Retry-After": "0",
                })
                response.content = b"null"
                response.json = lambda: pytest.fail("202 body must not be parsed before LRO headers")
                return response
            response = _Response(None, 200)
            response.content = b""
            response.json = lambda: pytest.fail("200 empty response must not be JSON-decoded")
            return response
        if case.read_lro and path.endswith("/getDefinition"):
            item_id = path.split("/")[-2]
            case.pending_definition = copy.deepcopy(case.backend.definitions[item_id])
            response = _Response(None, 202, {
                "Location": f"{p.API}/operations/{case.operation_id}",
                "x-ms-operation-id": case.operation_id,
            })
            response.content = b"null"
            response.json = lambda: pytest.fail("getDefinition 202 must process headers before body")
            return response
        if "/operations/" in path:
            case.backend.calls.append((method, url, None))
            if path.endswith("/result"):
                return _Response({"definition": case.pending_definition})
            return _Response({"status": case.operation_status})
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(p._Run, "request", lambda self, method, url, **kw: request(method, url, **kw))
    case.state = case.kwargs["plan_path"].parent / "presentation"
    case.args = {
        "workspace_id": WORKSPACE, "ontology_id": case.item_id,
        "plan_path": case.kwargs["plan_path"], "journal_path": case.kwargs["journal_path"],
        "materialize": case.kwargs["materialize_dir"],
        "l4_run": case.kwargs["l4_run"], "l3_root": case.kwargs["l3_root"],
        "state": case.state,
    }
    return case


def _plan(case):
    return m.repair_ontology_names(**case.args)


def _live(case, plan, **kwargs):
    return m.repair_ontology_names(
        **case.args, live=True, approve_plan=plan["plan_hash"],
        acknowledge_nontransactional=True, **kwargs,
    )


def test_plan_reads_only_complete_backup_and_live_preserves_originals(repair):
    case = repair
    frozen = {
        path: path.read_bytes()
        for root in (case.kwargs["materialize_dir"], case.kwargs["l4_run"])
        for path in root.rglob("*") if path.is_file()
    }
    frozen.update({
        path: path.read_bytes() for path in (case.args["plan_path"], case.args["journal_path"])
    })
    metadata = copy.deepcopy(case.backend.items[case.item_id])
    metadata["sensitivityLabel"] = {"id": str(uuid.uuid4())}
    # Reconciliation is exact, so retain its original metadata here.
    case.backend.items[case.item_id] = metadata
    journal = p._read_json(case.args["journal_path"])
    proof = journal["runtime_repair"]["review"]["proof"]
    proof["metadata"] = metadata
    journal["runtime_repair"]["review_hash"] = canonical_sha256(journal["runtime_repair"]["review"])
    journal["actions"]["create:ontology"]["reconciliation_review_hash"] = journal["runtime_repair"]["review_hash"]
    p._atomic_json(case.args["journal_path"], journal)
    frozen[case.args["journal_path"]] = case.args["journal_path"].read_bytes()
    plan = _plan(case)
    assert case.updates == []
    backup = p._read_json(case.state / "backup.json")
    assert backup["metadata"] == metadata
    assert backup["definition"] == case.backend.definitions[case.item_id]
    sealed = p._read_json(case.state / "plan.json")
    assert sealed["unchanged_invariants"]["entity_type_count"] > 0
    assert sealed["lineage"]["reconciliation"]["review_hash"]
    receipt = _live(case, plan)
    assert len(case.updates) == 1
    assert receipt["ontology_id"] == case.item_id
    assert receipt["lakehouse_id"] == backup["lakehouse_metadata"]["id"]
    assert receipt["publication_snapshot"]["status"] == "SUPERSEDED"
    assert receipt["metadata"] == metadata
    assert all(path.read_bytes() == content for path, content in frozen.items())
    assert _live(case, plan, resume=True) == receipt
    assert len(case.updates) == 1


def test_service_platform_and_sensitivity_backup_are_preserved(repair):
    case = repair
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {
            "type": "Ontology", "displayName": case.plan["names"]["ontology"],
            "description": case.plan["description"],
        },
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }
    part = _part(".platform", platform)
    case.backend.definitions[case.item_id]["parts"].append(part)
    journal = p._read_json(case.args["journal_path"])
    journal["runtime_repair"]["review"]["proof"]["service_platform_hash"] = canonical_sha256(platform)
    journal["runtime_repair"]["review_hash"] = canonical_sha256(journal["runtime_repair"]["review"])
    journal["actions"]["create:ontology"]["reconciliation_review_hash"] = journal["runtime_repair"]["review_hash"]
    p._atomic_json(case.args["journal_path"], journal)
    plan = _plan(case)
    backup = p._read_json(case.state / "backup.json")
    assert part in backup["definition"]["parts"]
    _live(case, plan)
    assert part in case.backend.definitions[case.item_id]["parts"]
    assert p._read_json(case.state / "backup.json") == backup


@pytest.mark.parametrize("kwargs", [
    {"live": True}, {"live": True, "approve_plan": "wrong"},
    {"live": True, "acknowledge_nontransactional": True},
    {"approve_plan": "wrong"}, {"acknowledge_nontransactional": True}, {"resume": True},
])
def test_exact_live_approval_and_ack_required(repair, kwargs):
    with pytest.raises(m.Error, match="requires"):
        m.repair_ontology_names(**repair.args, **kwargs)
    assert not repair.updates and not repair.state.exists()


def test_wrong_plan_hash_cannot_mutate(repair):
    _plan(repair)
    with pytest.raises(m.Error, match="approval"):
        _live(repair, {"plan_hash": "wrong"})
    assert not repair.updates


@pytest.mark.parametrize("field,value", [
    ("id", "new-id"), ("baseEntityTypeId", "new-parent"),
    ("entityIdParts", ["new-identity"]), ("visibility", "Hidden"),
    ("newProtectedField", None),
])
def test_protected_nonname_changes_rejected(repair, monkeypatch, field, value):
    def bad(*args):
        result, mapping = _render(*args)
        index = next(i for i, x in enumerate(result["parts"]) if x["path"].startswith("EntityTypes/") and "/DataBindings/" not in x["path"])
        part = result["parts"][index]
        payload = m._decode({"parts": [part]})[part["path"]]
        payload[field] = value
        result["parts"][index] = _part(part["path"], payload)
        return result, mapping
    monkeypatch.setattr(m, "_render", bad)
    with pytest.raises(m.Error, match="Protected"):
        _plan(repair)
    assert not repair.updates


@pytest.mark.parametrize("field,value", [("synonyms", ["Additional alias"]), ("customAttributes", {"scope": "broader"})])
def test_live_repair_cannot_add_synonyms_or_custom_attributes(repair, monkeypatch, field, value):
    def bad(*args):
        result, mapping = _render(*args)
        index = next(i for i, part in enumerate(result["parts"]) if part["path"].startswith("EntityTypes/") and part["path"].endswith("/definition.json"))
        part = result["parts"][index]
        payload = m._decode({"parts": [part]})[part["path"]]
        payload["semanticEnrichment"][field] = value
        result["parts"][index] = _part(part["path"], payload)
        return result, mapping

    monkeypatch.setattr(m, "_render", bad)
    with pytest.raises(m.Error, match="Protected"):
        _plan(repair)
    assert not repair.updates


@pytest.mark.parametrize("mutation", ["binding", "duplicate", "metadata", "definition", "platform"])
def test_remote_drift_duplicate_parts_and_platform_rejected(repair, mutation):
    case = repair
    definition = case.backend.definitions[case.item_id]
    if mutation == "binding":
        index = next(i for i, part in enumerate(definition["parts"]) if "/DataBindings/" in part["path"])
        part = definition["parts"][index]
        payload = m._decode({"parts": [part]})[part["path"]]
        payload["dataBindingConfiguration"]["sourceTableProperties"]["itemId"] = str(uuid.uuid4())
        definition["parts"][index] = _part(part["path"], payload)
    elif mutation == "duplicate":
        definition["parts"].append(copy.deepcopy(definition["parts"][0]))
    elif mutation == "metadata":
        case.backend.items[case.item_id]["description"] = "full corpus"
    elif mutation == "platform":
        definition["parts"].append(_part(".platform", {"metadata": {"description": "foreign"}}))
    else:
        definition["parts"].pop()
    with pytest.raises(m.Error):
        _plan(case)
    assert not case.updates


@pytest.mark.parametrize("mutation", ["source", "code", "semantic-compiler", "journal", "remote", "binding", "replacement", "mapping"])
def test_live_rechecks_local_remote_and_code_drift(repair, monkeypatch, mutation):
    case = repair
    plan = _plan(case)
    if mutation == "code":
        monkeypatch.setattr(m, "_code_hashes", lambda: {"compiler_hash": "changed"})
    elif mutation == "semantic-compiler":
        original = p._compile

        def changed(*args, **kwargs):
            compilation = original(*args, **kwargs)
            compilation.definitions["ontology"]["unapproved_semantic_change"] = True
            compilation.provenance["definition_hashes"]["ontology"] = canonical_sha256(
                compilation.definitions["ontology"],
            )
            return compilation

        monkeypatch.setattr(p, "_compile", changed)
    elif mutation == "source":
        path = case.args["l4_run"] / "semantic-serving-projection.json"
        path.write_text("{}")
    elif mutation == "journal":
        journal = p._read_json(case.args["journal_path"])
        journal["unreviewed"] = True
        p._atomic_json(case.args["journal_path"], journal)
    elif mutation == "remote":
        case.backend.items[case.item_id]["sensitivityLabel"] = {"id": "changed"}
    elif mutation == "binding":
        definition = case.backend.definitions[case.item_id]
        index = next(i for i, part in enumerate(definition["parts"]) if "/DataBindings/" in part["path"])
        definition["parts"].pop(index)
    elif mutation == "mapping":
        p._atomic_json(case.state / "mapping.json", {"mappings": []})
    else:
        path = case.state / "replacement.json"
        p._atomic_json(path, {"parts": []})
    with pytest.raises((m.Error, ValueError)):
        _live(case, plan)
    assert not case.updates


def test_202_null_update_and_get_definition_lro(repair):
    case = repair
    case.read_lro = True
    plan = _plan(case)
    case.mode = "202-null"
    receipt = _live(case, plan)
    assert receipt["status"] == "presentation-verified-same-item"
    evidence = p._read_json(case.state / "update-response.json")
    assert evidence["http_status"] == 202
    assert evidence["raw_body_base64"] == "bnVsbA=="
    assert evidence["headers"]["x-ms-operation-id"] == case.operation_id
    assert any(url.endswith("/result") for _, url, _ in case.backend.calls)
    assert _live(case, plan, resume=True) == receipt
    assert len(case.updates) == 1


@pytest.mark.parametrize("mode", ["lost-before", "lost-after"])
def test_unknown_post_outcome_never_repeated(repair, mode):
    case = repair
    plan = _plan(case)
    case.mode = mode
    with pytest.raises(m.Error, match="Unknown update outcome"):
        _live(case, plan)
    assert (case.state / "update-intent.json").is_file()
    assert not (case.state / "update-response.json").exists()
    with pytest.raises(m.Error, match="use --resume"):
        _live(case, plan)
    if mode == "lost-after":
        assert _live(case, plan, resume=True)["status"] == "presentation-verified-same-item"
    else:
        with pytest.raises(m.Error, match="NEVER repeat POST"):
            _live(case, plan, resume=True)
    assert len(case.updates) == 1


@pytest.mark.parametrize("mode", ["mismatch", "metadata-drift"])
def test_readback_mismatch_is_not_success(repair, mode):
    plan = _plan(repair)
    repair.mode = mode
    with pytest.raises(m.Error, match="not successful"):
        _live(repair, plan)
    assert len(repair.updates) == 1
    assert not (repair.state / "receipt.json").exists()
    assert (repair.state / "backup.json").is_file()


def test_interrupt_after_response_and_partial_verification_can_resume(repair, monkeypatch):
    case = repair
    plan = _plan(case)
    original = m._exclusive

    def interrupted(path, payload):
        if path.name == "receipt.json":
            raise OSError("Crash after verification snapshot")
        original(path, payload)

    monkeypatch.setattr(m, "_exclusive", interrupted)
    with pytest.raises(OSError, match="Crash"):
        _live(case, plan)
    assert (case.state / "update-response.json").exists()
    assert (case.state / "verified-definition.json").exists()
    monkeypatch.setattr(m, "_exclusive", original)
    assert _live(case, plan, resume=True)["status"] == "presentation-verified-same-item"
    assert len(case.updates) == 1


def test_interrupt_before_recording_response_resumes_without_duplicate_post(repair, monkeypatch):
    case = repair
    plan = _plan(case)
    original = m._exclusive

    def interrupted(path, payload):
        if path.name == "update-response.json":
            raise OSError("Crash between response and durable recording")
        original(path, payload)

    monkeypatch.setattr(m, "_exclusive", interrupted)
    with pytest.raises(OSError, match="Crash"):
        _live(case, plan)
    assert (case.state / "update-intent.json").is_file()
    assert not (case.state / "update-response.json").exists()
    monkeypatch.setattr(m, "_exclusive", original)
    assert _live(case, plan, resume=True)["status"] == "presentation-verified-same-item"
    assert len(case.updates) == 1


def test_interrupt_after_intent_before_post_never_reissues_unknown_intent(repair, monkeypatch):
    case = repair
    plan = _plan(case)
    original = m._exclusive

    def interrupted(path, payload):
        original(path, payload)
        if path.name == "update-intent.json":
            raise OSError("Crash after intent, before request")

    monkeypatch.setattr(m, "_exclusive", interrupted)
    with pytest.raises(OSError, match="Crash"):
        _live(case, plan)
    monkeypatch.setattr(m, "_exclusive", original)
    with pytest.raises(m.Error, match="NEVER repeat POST"):
        _live(case, plan, resume=True)
    assert not case.updates


def test_interrupted_lro_resumes_by_polling_recorded_operation_only(repair, monkeypatch):
    case = repair
    plan = _plan(case)
    case.mode = "202-null"
    case.operation_status = "Running"
    monkeypatch.setattr(m, "MAX_POLLS", 1)
    with pytest.raises(m.Error, match="still pending"):
        _live(case, plan)
    # A real pending update may not have changed the definition yet.
    case.backend.definitions[case.item_id] = p._read_json(case.state / "backup.json")["definition"]
    original_poll = m._PresentationRun.poll

    def complete(run, headers, **kwargs):
        case.operation_status = "Succeeded"
        result = original_poll(run, headers, **kwargs)
        case.backend.definitions[case.item_id] = p._read_json(case.state / "replacement.json")
        return result

    monkeypatch.setattr(m._PresentationRun, "poll", complete)
    assert _live(case, plan, resume=True)["status"] == "presentation-verified-same-item"
    assert len(case.updates) == 1


def test_state_create_only_and_public_command(repair):
    flags = []
    aliases = {"plan_path": "publication-plan", "journal_path": "prototype-journal"}
    for key, value in repair.args.items():
        flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    command = ["app", "repair-ontology-names", *flags]
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    before = (repair.state / "plan.json").read_bytes()
    repeated = CliRunner().invoke(cli, command)
    assert repeated.exit_code != 0 and "NEW directory" in repeated.output
    assert (repair.state / "plan.json").read_bytes() == before
    result = CliRunner().invoke(cli, command + [
        "--live", "--approve-plan", plan["plan_hash"], "--acknowledge-nontransactional",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ontology_id"] == repair.item_id
    assert len(repair.updates) == 1


@pytest.mark.parametrize("mode", ["plan", "live", "resume"])
def test_root_global_dry_run_rejected_before_backend_or_state_writes(repair, monkeypatch, mode):
    def forbidden(**kwargs):
        pytest.fail("Global dry-run must fail before entering repair backend/client")

    monkeypatch.setattr(m, "repair_ontology_names", forbidden)
    flags = ["--dry-run", "app", "repair-ontology-names"]
    aliases = {"plan_path": "publication-plan", "journal_path": "prototype-journal"}
    for key, value in repair.args.items():
        flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    if mode in ("live", "resume"):
        flags += ["--live", "--approve-plan", "a" * 64, "--acknowledge-nontransactional"]
    if mode == "resume":
        flags.append("--resume")
    calls_before = len(repair.backend.calls)
    result = CliRunner().invoke(cli, flags)
    assert result.exit_code == 2, result.output
    assert "Global --dry-run" in result.output
    if mode == "plan":
        assert "creates a local backup/plan" in result.output
    assert len(repair.backend.calls) == calls_before
    assert not repair.updates and not repair.state.exists()


def test_production_renderer_uses_sealed_domain_catalog(repair, monkeypatch):
    monkeypatch.setattr(m, "_render", PRODUCTION_RENDER)
    monkeypatch.setattr(m, "_code_hashes", PRODUCTION_CODES)
    plan = _plan(repair)
    sealed = p._read_json(repair.state / "plan.json")
    assert sealed["local_evidence"]["approved_domain_contract"]["candidate_model"]
    assert sealed["local_evidence"]["publication_crosswalk"]
    assert set(sealed["local_evidence"]["historical_semantic_comparison"][
        "verified_1_1_to_1_2_version_marker_transitions"
    ]) == {"parquet", "semantic_model", "ontology", "graph"}
    assert sealed["mapping"]
    sidecar = p._read_json(repair.state / "mapping.json")
    assert sidecar["mappings"] == sealed["mapping"]
    assert "underscores" in sidecar["native_name_notice"]
    assert all("display_name" in row and "canonical_id" in row for row in sidecar["mappings"])
    result = _live(repair, plan)
    assert result["status"] == "presentation-verified-same-item"
    assert len(repair.updates) == 1


def _relationship_definition(enrichment):
    return {"parts": [_part("RelationshipTypes/7/definition.json", {
        "id": "7", "name": "Approved_Relationship",
        "source": {"entityTypeId": "1"}, "target": {"entityTypeId": "2"},
        "semanticEnrichment": enrichment,
    })]}


def test_pairwise_empty_relationship_default_preserves_raw_hash():
    expected = _relationship_definition({"description": "Approved description"})
    actual = _relationship_definition({"description": "Approved description", "customAttributes": {}})
    before = copy.deepcopy(actual)
    result = m._readback_equivalence(expected, actual)
    assert result["approved_planned_after_hash"] == m._content_hash(expected)
    assert result["raw_observed_after_hash"] == m._content_hash(actual)
    assert result["raw_observed_after_hash"] != result["approved_planned_after_hash"]
    assert result["normalized_comparison_hash"] == result["approved_planned_after_hash"]
    assert result["service_default_paths"] == [{
        "part": "RelationshipTypes/7/definition.json",
        "pointer": "/semanticEnrichment/customAttributes", "planned": "absent", "observed": {},
    }]
    assert actual == before


@pytest.mark.parametrize("planned,observed", [
    ({"description": "Approved"}, {"description": "Approved", "customAttributes": {"scope": "new"}}),
    ({"description": "Approved", "customAttributes": {"keep": 1}}, {"description": "Approved", "customAttributes": {}}),
    ({"description": "Approved", "customAttributes": {}}, {"description": "Approved"}),
    ({"description": "Approved"}, {"description": "Changed", "customAttributes": {}}),
    ({"description": "Approved"}, {"description": "Approved", "customAttributes": {}, "synonyms": []}),
    ({}, {"customAttributes": {}}),
    ({"description": "Approved"}, {"description": "Approved", "customAttributes": []}),
])
def test_pairwise_default_rejects_nonempty_existing_or_other_changes(planned, observed):
    with pytest.raises(m.Error, match="Readback definition mismatch"):
        m._readback_equivalence(_relationship_definition(planned), _relationship_definition(observed))


@pytest.mark.parametrize("mutation", ["name", "id", "endpoint", "path", "binding"])
def test_pairwise_default_cannot_hide_identity_binding_or_name_drift(mutation):
    expected = _relationship_definition({"description": "Approved"})
    actual = _relationship_definition({"description": "Approved", "customAttributes": {}})
    part = actual["parts"][0]
    payload = m._decode(actual)[part["path"]]
    if mutation in ("name", "id"):
        payload[mutation] = "changed"
    elif mutation == "endpoint":
        payload["source"]["entityTypeId"] = "changed"
    elif mutation == "path":
        part["path"] = "EntityTypes/7/definition.json"
    else:
        expected["parts"].append(_part("RelationshipTypes/7/Contextualizations/binding.json", {"id": "old"}))
        actual["parts"].append(_part("RelationshipTypes/7/Contextualizations/binding.json", {"id": "new"}))
    actual["parts"][0] = _part(part["path"], payload)
    with pytest.raises(m.Error, match="Readback definition mismatch"):
        m._readback_equivalence(expected, actual)


@pytest.fixture
def completed_old_update(repair, monkeypatch):
    case = repair
    case.old_code_hash, case.new_code_hash = "a" * 64, "b" * 64
    case.base_codes = {
        "compiler_hash": "presentation-runtime", "naming_policy_hash": "fixture-readable-v1",
    }
    monkeypatch.setattr(m, "_code_hashes", lambda: {
        **case.base_codes, "repair_code_hash": case.old_code_hash,
    })

    def descriptions(*args):
        definition, mapping = _render(*args)
        for index, part in enumerate(definition["parts"]):
            if part["path"].startswith("RelationshipTypes/") and part["path"].endswith("/definition.json"):
                payload = m._decode({"parts": [part]})[part["path"]]
                payload["semanticEnrichment"] = {"description": "Approved relationship description"}
                definition["parts"][index] = _part(part["path"], payload)
        return definition, mapping

    monkeypatch.setattr(m, "_render", descriptions)
    case.repair_plan = _plan(case)
    case.mode = "service-default"
    current = m._readback_equivalence

    def old_verifier(planned, observed):
        if m._content_hash(planned) != m._content_hash(observed):
            raise m.Error("Old verifier rejects service-added empty defaults")
        return current(planned, observed)

    monkeypatch.setattr(m, "_readback_equivalence", old_verifier)
    with pytest.raises(m.Error, match="Old verifier"):
        _live(case, case.repair_plan)
    assert len(case.updates) == 1
    assert (case.state / "update-response.json").exists()
    assert not (case.state / "receipt.json").exists()
    monkeypatch.setattr(m, "_readback_equivalence", current)
    monkeypatch.setattr(m, "_code_hashes", lambda: {
        **case.base_codes, "repair_code_hash": case.new_code_hash,
    })
    case.frozen = {
        path: path.read_bytes()
        for path in [case.state / name for name in (
            "plan.json", "backup.json", "replacement.json", "mapping.json",
            "update-intent.json", "update-response.json",
        )] + [case.args["plan_path"], case.args["journal_path"]]
    }
    return case


def test_public_readback_only_verifier_upgrade_finishes_old_plan_without_post(completed_old_update):
    case = completed_old_update
    flags = ["app", "repair-ontology-names"]
    aliases = {"plan_path": "publication-plan", "journal_path": "prototype-journal"}
    for key, value in case.args.items():
        flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    flags += [
        "--live", "--resume", "--approve-plan", case.repair_plan["plan_hash"],
        "--acknowledge-nontransactional", "--accept-verifier-update", case.new_code_hash,
    ]
    result = CliRunner().invoke(cli, flags)
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["verifier_upgrade"]["original_repair_code_hash"] == case.old_code_hash
    assert receipt["verifier_upgrade"]["accepted_current_repair_code_hash"] == case.new_code_hash
    assert receipt["verifier_upgrade"]["update_post_authorized"] is False
    comparison = receipt["readback_equivalence"]
    assert comparison["service_default_paths"]
    assert comparison["raw_observed_after_hash"] != comparison["approved_planned_after_hash"]
    assert comparison["normalized_comparison_hash"] == comparison["approved_planned_after_hash"]
    assert all(path.read_bytes() == content for path, content in case.frozen.items())
    assert len(case.updates) == 1
    assert _live(case, case.repair_plan, resume=True, accept_verifier_update=case.new_code_hash) == receipt
    assert len(case.updates) == 1


@pytest.mark.parametrize("mode", ["no-upgrade", "wrong-hash", "no-resume", "wrong-plan", "no-ack", "plan"])
def test_verifier_upgrade_requires_all_explicit_approvals(completed_old_update, mode):
    case = completed_old_update
    args = {
        **case.args, "live": True, "resume": True,
        "approve_plan": case.repair_plan["plan_hash"], "acknowledge_nontransactional": True,
        "accept_verifier_update": case.new_code_hash,
    }
    if mode == "no-upgrade":
        args.pop("accept_verifier_update")
    elif mode == "wrong-hash":
        args["accept_verifier_update"] = "c" * 64
    elif mode == "no-resume":
        args["resume"] = False
    elif mode == "wrong-plan":
        args["approve_plan"] = "c" * 64
    elif mode == "no-ack":
        args["acknowledge_nontransactional"] = False
    else:
        args.update(live=False, resume=False, approve_plan=None, acknowledge_nontransactional=False)
    before = len(case.backend.calls)
    with pytest.raises(m.Error):
        m.repair_ontology_names(**args)
    assert len(case.backend.calls) == before and len(case.updates) == 1
    assert not (case.state / "receipt.json").exists()


@pytest.mark.parametrize("mutation", ["compiler", "naming", "journal", "intent", "response", "response-status", "response-body", "old-plan"])
def test_verifier_upgrade_rejects_stale_inputs_or_missing_durable_proof(completed_old_update, mutation):
    case = completed_old_update
    if mutation in ("compiler", "naming"):
        case.base_codes[{"compiler": "compiler_hash", "naming": "naming_policy_hash"}[mutation]] = "changed"
    elif mutation == "journal":
        journal = p._read_json(case.args["journal_path"])
        journal["changed"] = True
        p._atomic_json(case.args["journal_path"], journal)
    elif mutation in ("intent", "response"):
        (case.state / f"update-{mutation}.json").unlink()
    elif mutation.startswith("response-"):
        response = p._read_json(case.state / "update-response.json")
        if mutation == "response-status":
            response["http_status"] = 403
        else:
            response["raw_body_sha256"] = "c" * 64
        p._atomic_json(case.state / "update-response.json", response)
    else:
        plan = p._read_json(case.state / "plan.json")
        plan["local_evidence"]["repair_code_hash"] = "d" * 64
        p._atomic_json(case.state / "plan.json", plan)
    before = len(case.backend.calls)
    with pytest.raises(m.Error):
        _live(case, case.repair_plan, resume=True, accept_verifier_update=case.new_code_hash)
    assert len(case.backend.calls) == before and len(case.updates) == 1
    assert not (case.state / "receipt.json").exists()


def test_verifier_upgrade_does_not_retry_when_original_definition_still_present(completed_old_update):
    case = completed_old_update
    case.backend.definitions[case.item_id] = p._read_json(case.state / "backup.json")["definition"]
    with pytest.raises(m.Error, match="NEVER repeat POST"):
        _live(case, case.repair_plan, resume=True, accept_verifier_update=case.new_code_hash)
    assert len(case.updates) == 1


def test_verifier_upgrade_transport_forbids_update_even_if_enabled(completed_old_update):
    case = completed_old_update
    run = m._PresentationRun(case.args["journal_path"], case.plan, case.item_id)
    run.readback_only = True
    run.update_allowed = True
    with pytest.raises(m.Error, match="forbids"):
        run.request("POST", run.update_url, json={"definition": {}})
    assert len(case.updates) == 1


@pytest.fixture
def hyphen_source(monkeypatch):
    from tests.unit import test_l5a_structured_publication as fixtures

    original = fixtures._l3_with_sealed_manifest

    def source(*args, **kwargs):
        kwargs = copy.deepcopy(kwargs)
        for properties in kwargs["type_properties"].values():
            for prop in properties:
                prop["display_name"] = prop["display_name"].replace(" ", "-")
        return original(*args, **kwargs)

    monkeypatch.setattr(fixtures, "_l3_with_sealed_manifest", source)


@pytest.fixture
def historical_repair(hyphen_source, repair, monkeypatch):
    from fabric_kg_builder.deploy import ontology_names as names

    case = repair
    monkeypatch.setattr(m, "_render", PRODUCTION_RENDER)
    independent_diff = m.presentation_diff
    with monkeypatch.context() as historical:
        historical.setattr(names, "NATIVE_NAME_PATTERN", r"[A-Za-z][A-Za-z0-9_-]{0,127}")
        historical.setattr(names, "_label_name", lambda metadata, *, prefix: m._historical_names(
            {"test": metadata}, prefix, [],
        )["test"])
        historical.setattr(m, "presentation_diff", lambda before, after, **kwargs: independent_diff(
            before, after, _name_policy=m._LEGACY_NAME_POLICY,
        ))
        planned = _plan(case)
        old_plan = p._read_json(case.state / "plan.json")
        old_plan.pop("name_validation_policy")
        old_plan["plan_hash"] = canonical_sha256({k: v for k, v in old_plan.items() if k != "plan_hash"})
        p._atomic_json(case.state / "plan.json", old_plan)
        planned["plan_hash"] = old_plan["plan_hash"]
        case.mode = "service-default"
        case.previous_receipt = _live(case, planned)
    case.previous_state = case.state
    case.previous_plan = old_plan
    case.frozen = {
        path: path.read_bytes()
        for root in (case.previous_state, case.args["materialize"], case.args["l4_run"])
        for path in root.rglob("*") if path.is_file()
    }
    case.frozen.update({
        path: path.read_bytes() for path in (case.args["plan_path"], case.args["journal_path"])
    })
    case.state = case.state.parent / "second-presentation"
    case.args.update(state=case.state, previous_repair_state=case.previous_state)
    case.mode = "200-empty"
    case.updates.clear()
    return case


def test_public_second_repair_chains_verified_hyphen_baseline(historical_repair):
    case = historical_repair
    before = copy.deepcopy(case.backend.definitions[case.item_id])
    assert any("-" in prop["name"] for part in m._decode(before).values() if "properties" in part for prop in part["properties"])
    flags = ["app", "repair-ontology-names"]
    aliases = {"plan_path": "publication-plan", "journal_path": "prototype-journal"}
    for key, value in case.args.items():
        flags += ["--" + aliases.get(key, key.replace("_", "-")), str(value)]
    result = CliRunner().invoke(cli, flags)
    assert result.exit_code == 0, result.output
    planned = json.loads(result.output)
    assert not case.updates
    sealed = p._read_json(case.state / "plan.json")
    assert sealed["before_hash"] == case.previous_receipt["definition_hash"]
    assert sealed["name_validation_policy"] == m.NAME_POLICY
    chain = sealed["lineage"]["previous_repair"]
    assert chain["plan_hash"] == case.previous_plan["plan_hash"]
    assert chain["plan_bytes_hash"] == m._digest(case.previous_state / "plan.json")
    assert chain["receipt_bytes_hash"] == m._digest(case.previous_state / "receipt.json")
    assert p._read_json(case.state / "backup.json")["definition"] == before
    receipt = _live(case, planned)
    assert len(case.updates) == 1
    assert receipt["resources_created"] == receipt["resources_deleted"] == 0
    assert receipt["scope"] == case.previous_receipt["scope"]
    assert receipt["workspace_item_ids"] == case.previous_receipt["workspace_item_ids"]
    assert receipt["metadata"] == case.previous_receipt["metadata"]
    assert m._invariants(before) == m._invariants(case.backend.definitions[case.item_id])
    assert all(path.read_bytes() == data for path, data in case.frozen.items())
    assert _live(case, planned, resume=True) == receipt
    assert len(case.updates) == 1
    for path, payload in m._decode(case.backend.definitions[case.item_id]).items():
        if re.fullmatch(r"(EntityTypes|RelationshipTypes)/[^/]+/definition\.json", path):
            assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", payload["name"])
            assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", prop["name"]) for prop in payload.get("properties", []))


@pytest.mark.parametrize("artifact", [
    "plan.json", "backup.json", "replacement.json", "mapping.json",
    "update-intent.json", "update-response.json", "receipt.json", "verified-definition.json",
])
def test_second_repair_rejects_previous_artifact_tampering(historical_repair, artifact):
    case = historical_repair
    path = case.previous_state / artifact
    value = p._read_json(path)
    if artifact == "update-response.json":
        value["raw_body_sha256"] = "f" * 64
    else:
        value["tampered"] = True
    p._atomic_json(path, value)
    with pytest.raises((m.Error, KeyError, ValueError)):
        _plan(case)
    assert not case.updates and not case.state.exists()


@pytest.mark.parametrize("mutation", ["name", "property", "binding", "metadata", "source", "scope"])
def test_second_repair_rejects_current_or_source_drift(historical_repair, mutation):
    case = historical_repair
    if mutation in ("name", "property", "binding"):
        definition = case.backend.definitions[case.item_id]
        index = next(
            i for i, part in enumerate(definition["parts"])
            if ("/DataBindings/" in part["path"]) == (mutation == "binding")
            and part["path"].startswith("EntityTypes/")
        )
        part = definition["parts"][index]
        payload = m._decode({"parts": [part]})[part["path"]]
        if mutation == "property":
            payload["properties"][0]["valueType"] = "Boolean"
        else:
            payload["name" if mutation == "name" else "source"] = "unauthorized"
        definition["parts"][index] = _part(part["path"], payload)
    elif mutation == "metadata":
        case.backend.items[case.item_id]["sensitivityLabel"] = {"id": "changed"}
    elif mutation == "source":
        path = case.args["materialize"] / "definitions" / "ontology.json"
        value = p._read_json(path)
        value["changed"] = True
        p._atomic_json(path, value)
    else:
        path = case.previous_state / "plan.json"
        value = p._read_json(path)
        value["scope"] = {"not": "approved"}
        value["plan_hash"] = canonical_sha256({k: v for k, v in value.items() if k != "plan_hash"})
        p._atomic_json(path, value)
    with pytest.raises(m.Error):
        _plan(case)
    assert not case.updates and not case.state.exists()


def test_second_repair_revalidates_prior_receipt_before_live(historical_repair):
    case = historical_repair
    planned = _plan(case)
    path = case.previous_state / "receipt.json"
    receipt = p._read_json(path)
    receipt["resources_created"] = 1
    p._atomic_json(path, receipt)
    with pytest.raises(m.Error):
        _live(case, planned)
    assert not case.updates and not (case.state / "update-intent.json").exists()


@pytest.mark.parametrize("directory", ["original-relative", "unrelated"])
def test_second_repair_resolves_prior_receipt_relative_state(historical_repair, directory):
    case = historical_repair
    receipt = p._read_json(case.previous_state / "receipt.json")
    name = case.previous_state.name if directory == "original-relative" else "not-the-approved-state"
    receipt.update(backup=f"{name}/backup.json", mapping_sidecar=f"{name}/mapping.json")
    p._atomic_json(case.previous_state / "receipt.json", receipt)
    if directory == "original-relative":
        planned = _plan(case)
        assert _live(case, planned)["status"] == "presentation-verified-same-item"
        assert len(case.updates) == 1
    else:
        with pytest.raises(m.Error, match="artifact path differs"):
            _plan(case)
        assert not case.updates


def test_second_repair_rejects_self_signed_unknown_legacy_labels(historical_repair):
    case = historical_repair
    state = case.previous_state
    plan = p._read_json(state / "plan.json")
    backup = p._read_json(state / "backup.json")
    replacement = p._read_json(state / "replacement.json")
    mapping = p._read_json(state / "mapping.json")
    observed = p._read_json(state / "verified-definition.json")
    row = next(row for row in plan["mapping"] if row["kind"] == "relationship_type")
    row["new_name"] = "Self-Signed-Unknown"
    path = f"RelationshipTypes/{row['id']}/definition.json"
    for definition in (replacement, observed):
        index = next(i for i, part in enumerate(definition["parts"]) if part["path"] == path)
        payload = m._decode(definition)[path]
        payload["name"] = row["new_name"]
        definition["parts"][index] = _part(path, payload)
    plan["replacement_hash"] = canonical_sha256(replacement)
    plan["after_hash"] = m._content_hash(replacement)
    plan["allowed_field_diff"] = m.presentation_diff(
        backup["definition"], replacement, _name_policy=m._LEGACY_NAME_POLICY,
    )
    mapping.update(mappings=plan["mapping"], allowed_field_diff=plan["allowed_field_diff"])
    plan["mapping_sidecar_hash"] = canonical_sha256(mapping)
    plan["plan_hash"] = canonical_sha256({k: v for k, v in plan.items() if k != "plan_hash"})
    intent = p._read_json(state / "update-intent.json")
    intent.update(plan_hash=plan["plan_hash"], request_hash=canonical_sha256({"definition": replacement}))
    receipt = p._read_json(state / "receipt.json")
    receipt.update(
        plan_hash=plan["plan_hash"], definition_hash=m._content_hash(observed),
        readback_equivalence=m._readback_equivalence(replacement, observed),
    )
    for name, value in {
        "plan": plan, "replacement": replacement, "mapping": mapping,
        "update-intent": intent, "receipt": receipt, "verified-definition": observed,
    }.items():
        p._atomic_json(state / f"{name}.json", value)
    case.backend.definitions[case.item_id] = observed
    with pytest.raises(m.Error, match="historical mapping differs"):
        _plan(case)
    assert not case.updates and not case.state.exists()


def test_second_repair_rejects_new_naming_code_drift(historical_repair, monkeypatch):
    case = historical_repair
    planned = _plan(case)
    codes = m._code_hashes()
    monkeypatch.setattr(m, "_code_hashes", lambda: {**codes, "naming_policy_hash": "changed"})
    with pytest.raises(m.Error, match="drift"):
        _live(case, planned)
    assert not case.updates and not (case.state / "update-intent.json").exists()


@pytest.mark.parametrize("nested", [False, True])
def test_second_repair_state_cannot_overlap_previous(historical_repair, nested):
    case = historical_repair
    case.args["state"] = case.previous_state / "nested" if nested else case.previous_state
    with pytest.raises(m.Error, match="separate"):
        _plan(case)
    assert not case.updates


def test_second_repair_ambiguous_intent_never_posts_again(historical_repair, monkeypatch):
    case = historical_repair
    planned = _plan(case)
    original = m._exclusive

    def interrupted(path, payload):
        original(path, payload)
        if path.name == "update-intent.json":
            raise OSError("Crash after intent")

    monkeypatch.setattr(m, "_exclusive", interrupted)
    with pytest.raises(OSError, match="Crash"):
        _live(case, planned)
    monkeypatch.setattr(m, "_exclusive", original)
    with pytest.raises(m.Error, match="NEVER repeat POST"):
        _live(case, planned, resume=True)
    assert not case.updates


@pytest.mark.parametrize("kind", ["entity", "property", "relationship"])
def test_independent_diff_rejects_hyphens_even_when_unchanged(kind):
    if kind == "relationship":
        definition = _relationship_definition({"description": "Approved"})
        payload = m._decode(definition)["RelationshipTypes/7/definition.json"]
        payload["name"] = "Not-Allowed"
        definition["parts"][0] = _part("RelationshipTypes/7/definition.json", payload)
    else:
        definition = {"parts": [_part("EntityTypes/1/definition.json", {
            "id": "1", "name": "Not-Allowed" if kind == "entity" else "Allowed",
            "displayNamePropertyId": "2", "entityIdParts": ["2"],
            "properties": [{"id": "2", "name": "Not-Allowed" if kind == "property" else "Allowed", "valueType": "String"}],
        })]}
    with pytest.raises(m.Error, match="readable identifier"):
        m.presentation_diff(definition, definition)
    assert m.presentation_diff(definition, definition, _name_policy=m._LEGACY_NAME_POLICY) == []
