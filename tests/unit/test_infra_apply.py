from __future__ import annotations

import json

import pytest

from fabric_kg_builder.infra.apply import (
    _arm_authority_record,
    apply_plan,
    load_outputs,
    load_state,
    save_outputs,
    save_state,
)
from fabric_kg_builder.infra.runner import FakeCommandRunner
from fabric_kg_builder.infra.runtime_sync import sync_runtime_configuration
from fabric_kg_builder.infra.schema import (
    InfraManifest,
    InfraPlan,
    PlanAction,
    PlanItem,
    ResourceMode,
)


def _connected_manifest() -> InfraManifest:
    return InfraManifest.model_validate(
        {
            "environment": "dev",
            "azure": {
                "subscription_id": "sub-001",
                "resource_group": {"mode": "connect", "name": "rg-existing"},
            },
            "resources": {
                "storage": {"mode": "connect", "name": "storage-existing"},
                "document_intelligence": {
                    "mode": "connect",
                    "name": "documents-existing",
                },
                "foundry": {
                    "mode": "connect",
                    "name": "foundry-existing",
                    "project_name": "project-existing",
                    "models": {
                        "chat": {
                            "model": "gpt-4.1",
                            "deployment_name": "chat-existing",
                        },
                        "embedding": {
                            "model": "text-embedding-3-large",
                            "deployment_name": "embedding-existing",
                        },
                    },
                },
                "search": {"mode": "connect", "name": "search-existing"},
            },
        }
    )


def _resource_id(provider_type: str, name: str) -> str:
    return (
        "/subscriptions/sub-001/resourceGroups/rg-existing/providers/"
        f"{provider_type}/{name}"
    )


def _adopt_item(resource_type: str, resource_name: str) -> PlanItem:
    return PlanItem(
        resource_type=resource_type,
        resource_name=resource_name,
        action=PlanAction.ADOPT,
    )


def _add_resource_response(
    runner: FakeCommandRunner,
    resource_id: str,
    payload: dict,
) -> None:
    runner.add_response(
        ["az", "resource", "show", "--subscription", "sub-001", "--ids", resource_id],
        stdout=json.dumps(payload),
    )


def test_connected_arm_endpoints_are_persisted_without_host_synthesis(tmp_path):
    manifest = _connected_manifest()
    storage_id = _resource_id(
        "Microsoft.Storage/storageAccounts", "storage-existing"
    )
    documents_id = _resource_id(
        "Microsoft.CognitiveServices/accounts", "documents-existing"
    )
    foundry_id = _resource_id(
        "Microsoft.CognitiveServices/accounts", "foundry-existing"
    )
    project_id = f"{foundry_id}/projects/project-existing"
    search_id = _resource_id(
        "Microsoft.Search/searchServices", "search-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        storage_id,
        {
            "name": "storage-existing",
            "properties": {
                "primaryEndpoints": {
                    "blob": "https://private-storage.example.test/"
                }
            },
        },
    )
    _add_resource_response(
        runner,
        documents_id,
        {
            "name": "documents-existing",
            "properties": {
                "endpoint": "https://documents-authoritative.example.test/"
            },
        },
    )
    _add_resource_response(
        runner,
        foundry_id,
        {
            "name": "foundry-existing",
            "properties": {
                "endpoints": {
                    "AI Foundry API": "https://foundry-authoritative.example.test/",
                    "OpenAI Language Model Instance API": (
                        "https://openai-authoritative.example.test/"
                    ),
                }
            },
        },
    )
    _add_resource_response(
        runner,
        project_id,
        {
            "name": "foundry-existing/project-arm-name",
            "properties": {
                "endpoint": (
                    "https://project-authoritative.example.test/"
                    "api/projects/project-arm-name"
                )
            },
        },
    )
    _add_resource_response(
        runner,
        search_id,
        {
            "name": "search-existing",
            "properties": {
                "endpoint": (
                    "https://SEARCH-AUTHORITATIVE.EXAMPLE.TEST/api"
                )
            },
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            _adopt_item(
                "Microsoft.Storage/storageAccounts", "storage-existing"
            ),
            _adopt_item(
                "Microsoft.CognitiveServices/accounts", "documents-existing"
            ),
            _adopt_item(
                "Microsoft.CognitiveServices/accounts", "foundry-existing"
            ),
            _adopt_item(
                "Microsoft.CognitiveServices/accounts/projects",
                "foundry-existing/project-existing",
            ),
            _adopt_item(
                "Microsoft.Search/searchServices", "search-existing"
            ),
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert status.succeeded
    outputs = load_outputs(tmp_path, "dev")
    assert outputs["blobEndpoint"] == "https://private-storage.example.test/"
    assert (
        outputs["documentIntelligenceEndpoint"]
        == "https://documents-authoritative.example.test/"
    )
    assert (
        outputs["foundryEndpoint"]
        == "https://foundry-authoritative.example.test/"
    )
    assert (
        outputs["foundryOpenAIEndpoint"]
        == "https://openai-authoritative.example.test/"
    )
    assert outputs["foundryProjectEndpoint"] == (
        "https://project-authoritative.example.test/"
        "api/projects/project-arm-name"
    )
    assert (
        outputs["searchEndpoint"]
        == "https://search-authoritative.example.test/api"
    )
    fabric_environment = tmp_path / "runtime.json"
    sync_runtime_configuration(
        environment="dev",
        manifest=manifest,
        outputs=outputs,
        fabric_environment_path=fabric_environment,
        agent_metadata_path=tmp_path / "metadata.yaml",
    )
    assert json.loads(fabric_environment.read_text(encoding="utf-8"))[
        "ai_search"
    ]["endpoint"] == outputs["searchEndpoint"]


def test_connected_resource_missing_endpoint_fails_with_id_and_property_path(
    tmp_path,
):
    manifest = _connected_manifest()
    resource_id = _resource_id(
        "Microsoft.CognitiveServices/accounts", "documents-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        resource_id,
        {"name": "documents-existing", "properties": {}},
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            _adopt_item(
                "Microsoft.CognitiveServices/accounts", "documents-existing"
            )
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert not status.succeeded
    assert resource_id in status.errors[0]
    assert "properties.endpoint" in status.errors[0]
    outputs = load_outputs(tmp_path, "dev")
    assert "documentIntelligenceEndpoint" not in outputs


def test_connected_resource_rejects_non_https_arm_endpoint(tmp_path):
    manifest = _connected_manifest()
    resource_id = _resource_id(
        "Microsoft.Search/searchServices", "search-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        resource_id,
        {
            "name": "search-existing",
            "properties": {"endpoint": "http://search.example.test"},
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            _adopt_item(
                "Microsoft.Search/searchServices", "search-existing"
            )
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert not status.succeeded
    assert resource_id in status.errors[0]
    assert "properties.endpoint" in status.errors[0]
    assert "malformed HTTPS endpoint" in status.errors[0]


def test_connected_resource_rejects_endpoint_query_and_fragment(tmp_path):
    manifest = _connected_manifest()
    resource_id = _resource_id(
        "Microsoft.Search/searchServices", "search-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        resource_id,
        {
            "name": "search-existing",
            "properties": {
                "endpoint": (
                    "https://search.example.test/path?sig=opaque#fragment"
                )
            },
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            _adopt_item(
                "Microsoft.Search/searchServices", "search-existing"
            )
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert not status.succeeded
    assert "malformed HTTPS endpoint" in status.errors[0]
    assert "searchEndpoint" not in load_outputs(tmp_path, "dev")


def test_save_outputs_rejects_query_endpoint_before_persistence(tmp_path):
    with pytest.raises(ValueError, match="safe HTTPS endpoint"):
        save_outputs(
            {
                "searchEndpoint": (
                    "https://search.example.test/path?sig=opaque"
                )
            },
            tmp_path,
            "dev",
        )

    assert not (tmp_path / "infra" / "dev" / "outputs.json").exists()


def test_save_outputs_omits_nested_credential_containers(tmp_path):
    secret = "opaque-short-secret"
    path = save_outputs(
        {
            "searchEndpoint": "https://search.example.test",
            "credentials": {
                "primaryKey": secret,
                "nested": [{"secondaryKey": secret}],
            },
            "keys": [{"secretAccessKey": secret}],
        },
        tmp_path,
        "dev",
    )

    persisted = path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert json.loads(persisted) == {
        "searchEndpoint": "https://search.example.test"
    }


def test_save_outputs_rejects_unknown_nested_authority(tmp_path):
    with pytest.raises(ValueError, match="Unknown infrastructure output"):
        save_outputs(
            {
                "metadata": {
                    "credentials": {"primaryKey": "opaque-short"}
                }
            },
            tmp_path,
            "dev",
        )


@pytest.mark.parametrize("value", [7, True, {"value": "nested"}])
def test_save_outputs_rejects_non_string_target_values(
    tmp_path,
    value,
):
    with pytest.raises(ValueError, match="must be a string|scalar"):
        save_outputs(
            {"searchEndpoint": value},
            tmp_path,
            "dev",
        )


def test_load_outputs_rewrites_nested_credentials(tmp_path):
    path = tmp_path / "infra" / "dev" / "outputs.json"
    path.parent.mkdir(parents=True)
    secret = "opaque-loaded-secret"
    path.write_text(
        json.dumps({
            "searchEndpoint": "https://search.example.test",
            "credentials": {
                "keys": [{"primaryKey": secret}]
            },
        }),
        encoding="utf-8",
    )

    outputs = load_outputs(tmp_path, "dev")

    assert outputs == {
        "searchEndpoint": "https://search.example.test"
    }
    assert secret not in path.read_text(encoding="utf-8")


def test_load_and_save_state_sanitizes_nested_credentials(tmp_path):
    state_path = tmp_path / "infra" / "dev" / "state.json"
    state_path.parent.mkdir(parents=True)
    secret = "opaque-state-secret"
    state_path.write_text(
        json.dumps({
            "schema_version": "1.0",
            "environment": "dev",
            "last_operation": "apply",
            "last_operation_id": (
                "00000000-0000-4000-8000-000000000001"
            ),
            "last_operation_status": "succeeded",
            "managed_resource_ids": {
                "Microsoft.Search/searchServices": (
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.Search/searchServices/search-one"
                )
            },
            "adopted_resource_ids": {},
            "outputs": {
                "searchEndpoint": "https://search.example.test",
                "credentials": {"primaryKey": secret},
            },
            "credentials": {
                "keys": [{"secretAccessKey": secret}]
            },
        }),
        encoding="utf-8",
    )

    state = load_state(tmp_path, "dev")
    saved = save_state(state, tmp_path)
    persisted = saved.read_text(encoding="utf-8")

    assert secret not in persisted
    assert state.outputs == {
        "searchEndpoint": "https://search.example.test"
    }
    assert "credentials" not in json.loads(persisted)


@pytest.mark.parametrize(
    "state_update",
    [
        {"last_operation_id": "A" * 32},
        {
            "managed_resource_ids": {
                "Fabric/Workspace": "A" * 32,
            }
        },
    ],
)
def test_load_state_rejects_secret_shaped_allowed_fields(
    tmp_path,
    state_update,
):
    state_path = tmp_path / "infra" / "dev" / "state.json"
    state_path.parent.mkdir(parents=True)
    payload = {
        "schema_version": "1.0",
        "environment": "dev",
        "last_operation": "apply",
        "last_operation_id": (
            "00000000-0000-4000-8000-000000000001"
        ),
        "last_operation_status": "succeeded",
        "managed_resource_ids": {},
        "adopted_resource_ids": {},
        "outputs": {},
    }
    payload.update(state_update)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="UUID|invalid ID"):
        load_state(tmp_path, "dev")


def test_connected_foundry_project_never_synthesizes_missing_endpoint(
    tmp_path,
):
    manifest = _connected_manifest()
    foundry_id = _resource_id(
        "Microsoft.CognitiveServices/accounts", "foundry-existing"
    )
    project_id = f"{foundry_id}/projects/project-existing"
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        project_id,
        {
            "name": "foundry-existing/project-existing",
            "properties": {},
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            _adopt_item(
                "Microsoft.CognitiveServices/accounts/projects",
                "foundry-existing/project-existing",
            )
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert not status.succeeded
    assert project_id in status.errors[0]
    assert "properties.endpoint" in status.errors[0]
    assert 'properties.endpoints["AI Foundry API"]' in status.errors[0]
    assert "services.ai.azure.com" not in status.errors[0]


def test_connected_arm_endpoint_overrides_mixed_bicep_output(tmp_path):
    manifest = _connected_manifest()
    manifest = manifest.model_copy(
        update={
            "resources": manifest.resources.model_copy(
                update={
                    "document_intelligence": (
                        manifest.resources.document_intelligence.model_copy(
                            update={"mode": ResourceMode.CREATE}
                        )
                    )
                }
            )
        }
    )
    storage_id = _resource_id(
        "Microsoft.Storage/storageAccounts", "storage-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        storage_id,
        {
            "name": "storage-existing",
            "properties": {
                "primaryEndpoints": {
                    "blob": "https://blob-from-arm.example.test/"
                }
            },
        },
    )
    runner.add_response(
        ["az", "deployment", "group", "create"],
        stdout=json.dumps(
            {
                "properties": {
                    "outputs": {
                        "blobEndpoint": {
                            "type": "String",
                            "value": "https://blob-from-bicep.example.test/",
                        },
                        "documentIntelligenceEndpoint": {
                            "type": "String",
                            "value": "https://documents-from-bicep.example.test/",
                        },
                    }
                }
            }
        ),
    )
    infra_dir = tmp_path / "templates"
    infra_dir.mkdir()
    (infra_dir / "main.bicep").write_text("// mocked by FakeCommandRunner\n")
    plan = InfraPlan(
        environment="dev",
        items=[
            _adopt_item(
                "Microsoft.Storage/storageAccounts", "storage-existing"
            ),
            PlanItem(
                resource_type="Microsoft.CognitiveServices/accounts",
                resource_name="documents-existing",
                action=PlanAction.CREATE,
            ),
        ],
    )

    status = apply_plan(
        manifest,
        plan,
        runner,
        build_root=tmp_path,
        infra_dir=infra_dir,
    )

    assert status.succeeded
    outputs = load_outputs(tmp_path, "dev")
    assert outputs["blobEndpoint"] == "https://blob-from-arm.example.test/"
    assert outputs["documentIntelligenceEndpoint"] == (
        "https://documents-from-bicep.example.test/"
    )


def test_no_op_apply_refreshes_stale_authoritative_endpoint(tmp_path):
    manifest = _connected_manifest()
    previous = {
        "blobEndpoint": "https://storage-existing.blob.core.windows.net/",
        "documentIntelligenceEndpoint": "https://documents.arm.example.test/",
        "foundryEndpoint": "https://foundry.arm.example.test/",
        "foundryOpenAIEndpoint": "https://openai.arm.example.test/",
        "foundryProjectEndpoint": (
            "https://foundry.arm.example.test/api/projects/project-existing"
        ),
        "searchEndpoint": "https://search.arm.example.test",
    }
    save_outputs(previous, tmp_path, "dev")
    runner = FakeCommandRunner()
    storage_id = _resource_id(
        "Microsoft.Storage/storageAccounts", "storage-existing"
    )
    _add_resource_response(
        runner,
        storage_id,
        {
            "type": "Microsoft.Storage/storageAccounts",
            "name": "storage-existing",
            "properties": {
                "primaryEndpoints": {
                    "blob": "https://private-storage.example.test/"
                }
            },
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            PlanItem(
                resource_type="Microsoft.Storage/storageAccounts",
                resource_name="storage-existing",
                action=PlanAction.NO_OP,
            )
        ],
    )

    status = apply_plan(
        manifest,
        plan,
        runner,
        build_root=tmp_path,
    )

    assert status.succeeded
    outputs = load_outputs(tmp_path, "dev")
    assert outputs["blobEndpoint"] == "https://private-storage.example.test/"
    for key, endpoint in previous.items():
        if key == "blobEndpoint":
            continue
        assert outputs[key] == endpoint


def test_no_op_apply_fails_closed_and_removes_stale_endpoint(tmp_path):
    manifest = _connected_manifest()
    save_outputs(
        {
            "blobEndpoint": (
                "https://storage-existing.blob.core.windows.net/"
            )
        },
        tmp_path,
        "dev",
    )
    storage_id = _resource_id(
        "Microsoft.Storage/storageAccounts", "storage-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        storage_id,
        {
            "type": "Microsoft.Storage/storageAccounts",
            "name": "storage-existing",
            "properties": {"primaryEndpoints": {}},
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            PlanItem(
                resource_type="Microsoft.Storage/storageAccounts",
                resource_name="storage-existing",
                action=PlanAction.NO_OP,
            )
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert not status.succeeded
    assert storage_id in status.errors[0]
    assert "properties.primaryEndpoints.blob" in status.errors[0]
    assert "blobEndpoint" not in load_outputs(tmp_path, "dev")


def test_no_op_apply_rejects_arm_type_mismatch(tmp_path):
    manifest = _connected_manifest()
    storage_id = _resource_id(
        "Microsoft.Storage/storageAccounts", "storage-existing"
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        storage_id,
        {
            "type": "Microsoft.Search/searchServices",
            "name": "storage-existing",
            "properties": {
                "primaryEndpoints": {
                    "blob": "https://private-storage.example.test/"
                }
            },
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[
            PlanItem(
                resource_type="Microsoft.Storage/storageAccounts",
                resource_name="storage-existing",
                action=PlanAction.NO_OP,
            )
        ],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert not status.succeeded
    assert "type mismatch" in status.errors[0]


def test_connected_deployment_authority_tracks_model_version() -> None:
    first = _arm_authority_record(
        state_key=(
            "Microsoft.CognitiveServices/accounts/deployments/chat"
        ),
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/model/chat",
        resource_type="Microsoft.CognitiveServices/accounts/deployments",
        payload={
            "type": "Microsoft.CognitiveServices/accounts/deployments",
            "properties": {
                "model": {
                    "format": "OpenAI",
                    "name": "gpt-4.1",
                    "version": "2026-01-01",
                }
            },
        },
        runtime_outputs={},
    )
    second = _arm_authority_record(
        state_key=(
            "Microsoft.CognitiveServices/accounts/deployments/chat"
        ),
        resource_id="/subscriptions/sub/resourceGroups/rg/providers/model/chat",
        resource_type="Microsoft.CognitiveServices/accounts/deployments",
        payload={
            "type": "Microsoft.CognitiveServices/accounts/deployments",
            "properties": {
                "model": {
                    "format": "OpenAI",
                    "name": "gpt-4.1",
                    "version": "2026-02-01",
                }
            },
        },
        runtime_outputs={},
    )

    assert first["model"]["version"] != second["model"]["version"]


def test_no_op_connected_registry_refreshes_login_server(tmp_path):
    manifest = _connected_manifest()
    manifest = manifest.model_copy(
        update={
            "resources": manifest.resources.model_copy(
                update={
                    "container_registry": (
                        manifest.resources.container_registry.model_copy(
                            update={
                                "mode": ResourceMode.CONNECT,
                                "name": "registry-existing",
                            }
                        )
                    )
                }
            ),
        }
    )
    registry_id = _resource_id(
        "Microsoft.ContainerRegistry/registries", "registry-existing"
    )
    save_outputs(
        {"containerRegistryLoginServer": "stale.azurecr.io"},
        tmp_path,
        "dev",
    )
    runner = FakeCommandRunner()
    _add_resource_response(
        runner,
        registry_id,
        {
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "registry-existing",
            "properties": {"loginServer": "authoritative.azurecr.io"},
        },
    )
    plan = InfraPlan(
        environment="dev",
        items=[],
    )

    status = apply_plan(manifest, plan, runner, build_root=tmp_path)

    assert status.succeeded
    assert load_outputs(tmp_path, "dev")[
        "containerRegistryLoginServer"
    ] == "authoritative.azurecr.io"
