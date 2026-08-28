from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ServiceRequestError

from fabric_kg_builder.agent.l7_adapters import (
    AzureL7ReadOnlyProbe,
    SDKL7FoundryAgentBackend,
    build_azure_l7_adapters,
)
from fabric_kg_builder.agent.l7_deployment import (
    L7DeploymentError,
    L7RemoteReadinessObservation,
    L7ResourceReadback,
)
from fabric_kg_builder.agent.l7_remote_tool import build_l6_openapi_spec
from fabric_kg_builder.contracts.base import canonical_sha256
from tests.unit.test_l7_deployment import _config, _definition


def test_foundry_get_resolves_versions_latest_and_hashes_effective_definition():
    definition = {
        "model": "model",
        "instructions": "canonical",
        "tools": [{"type": "openapi"}],
    }
    agent = SimpleNamespace(
        name="Canonical L6 Agent",
        versions=SimpleNamespace(
            latest=SimpleNamespace(version="7"),
        ),
    )

    class Operations:
        def list(self):
            return [agent]

        def get_version(self, *, agent_name, agent_version):
            assert agent_name == "Canonical L6 Agent"
            assert agent_version == "7"
            return SimpleNamespace(
                definition=definition,
                metadata={"l6_definition_hash": "a" * 64},
            )

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    backend._reconciliation_timeout = 0.01
    backend._reconciliation_poll = 0.001
    readback = backend.get(
        project_resource_id="/subscriptions/sub/projects/project",
        agent_name="Canonical L6 Agent",
    )
    assert readback.etag == "7"
    assert readback.properties_hash == canonical_sha256(definition)
    assert readback.definition_hash == "a" * 64


class _Response:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = (
            body if isinstance(body, str) else json.dumps(body, default=str)
        )

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _fabric_probe(session):
    probe = object.__new__(AzureL7ReadOnlyProbe)
    probe._credential = SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token="token")
    )
    probe._session = session
    return probe


def test_fabric_definition_readback_uses_post():
    session = _Session(
        [_Response(200, {"definition": {"parts": [{"path": "definition.json"}]}})]
    )
    result = _fabric_probe(session)._fabric_definition(
        workspace_id="workspace",
        item_id="item",
    )
    assert "definition" in result
    assert session.calls[0][0] == "POST"
    assert session.calls[0][2]["json"] == {}


def test_fabric_definition_readback_polls_202_result():
    session = _Session(
        [
            _Response(
                202,
                {},
                {
                    "Location": (
                        "https://api.fabric.microsoft.com/v1/operations/op"
                    )
                },
            ),
            _Response(
                200,
                {
                    "status": "Succeeded",
                    "result": {"definition": {"parts": [{"path": "definition.json"}]}},
                },
            ),
        ]
    )
    result = _fabric_probe(session)._fabric_definition(
        workspace_id="workspace",
        item_id="item",
    )
    assert result["definition"]["parts"][0]["path"] == "definition.json"
    assert [call[0] for call in session.calls] == ["POST", "GET"]


def test_data_agent_without_definition_api_is_explicit_no_go():
    config = _config()
    target = config.fabric_items[0]
    session = _Session(
        [
            _Response(
                200,
                {
                    "id": target.item_id,
                    "workspaceId": config.fabric_workspace_id,
                    "type": "DataAgent",
                    "displayName": "agent",
                },
            ),
            _Response(404, {}),
        ]
    )
    probe = _fabric_probe(session)
    with pytest.raises(L7DeploymentError, match="definition POST failed"):
        probe.get_fabric_item(
            workspace_id=config.fabric_workspace_id,
            item=target,
        )
    assert [call[0] for call in session.calls] == ["GET", "POST"]


def _jwt(claims):
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


def test_authenticated_readiness_binds_all_live_authorities():
    config = _config()
    definition = _definition(config)
    now = datetime.now(timezone.utc)
    values = {
        "endpoint": config.remote_tool_endpoint,
        "tenant_id": config.tenant_id,
        "audience": config.remote_tool_audience,
        "caller_object_id": config.remote_tool_allowed_caller_object_ids[0],
        "app_role": config.remote_tool_required_app_role,
        "openapi_schema_hash": canonical_sha256(
            build_l6_openapi_spec(endpoint=config.remote_tool_endpoint)
        ),
        "l6_definition_hash": definition.definition_hash,
        "authority_backend": "azure_blob",
        "authority_version": "1",
        "checked_at": now,
        "expires_at": now + timedelta(minutes=1),
    }
    hash_values = {
        **values,
        "checked_at": values["checked_at"].isoformat().replace("+00:00", "Z"),
        "expires_at": values["expires_at"].isoformat().replace("+00:00", "Z"),
    }
    observation = L7RemoteReadinessObservation(
        **values,
        readiness_hash=canonical_sha256(hash_values),
    )
    token = _jwt(
        {
            "tid": config.tenant_id,
            "oid": config.remote_tool_allowed_caller_object_ids[0],
            "roles": [config.remote_tool_required_app_role],
        }
    )
    session = _Session([_Response(200, observation.model_dump_json())])
    probe = object.__new__(AzureL7ReadOnlyProbe)
    probe._credential = SimpleNamespace(
        get_token=lambda scope: (_ for _ in ()).throw(
            AssertionError("deployment credential must not probe readiness")
        )
    )
    probe._remote_probe_credential = SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token=token)
    )
    probe._session = session
    assert probe.probe_remote_readiness(
        config=config,
        definition=definition,
    ) == observation
    assert session.calls[0][2]["timeout"] == (5, 15)


def test_readiness_wrong_probe_role_fails_before_network():
    config = _config()
    token = _jwt(
        {
            "tid": config.tenant_id,
            "oid": config.remote_tool_allowed_caller_object_ids[0],
            "roles": ["Wrong.Role"],
        }
    )
    session = _Session([])
    probe = object.__new__(AzureL7ReadOnlyProbe)
    probe._credential = SimpleNamespace(
        get_token=lambda scope: (_ for _ in ()).throw(
            AssertionError("deployment credential must not probe readiness")
        )
    )
    probe._remote_probe_credential = SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token=token)
    )
    probe._session = session
    with pytest.raises(L7DeploymentError, match="caller/app role"):
        probe.probe_remote_readiness(
            config=config,
            definition=_definition(config),
        )
    assert session.calls == []


@pytest.mark.parametrize(
    ("failure", "expected_exception"),
    [
        (ValueError("parser"), L7DeploymentError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
)
def test_foundry_post_create_non_success_deletes_new_version(
    failure,
    expected_exception,
):
    deleted = []

    class Operations:
        def create_version(self, *args, **kwargs):
            return SimpleNamespace(version="9")

        def delete_version(self, *, agent_name, agent_version):
            deleted.append((agent_name, agent_version))

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    backend._build_prompt_definition = lambda **kwargs: {"definition": "trusted"}
    calls = 0

    def get(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return L7ResourceReadback(
                resource_kind="foundry_agent",
                stable_id=(
                    f"{kwargs['project_resource_id']}/agents/"
                    f"{kwargs['agent_name']}"
                ),
                exists=False,
            )
        raise failure

    backend.get = get
    config = _config()
    definition = _definition(config)
    with pytest.raises(expected_exception):
        backend.upsert(
            config=config,
            definition=definition,
            expected_etag=None,
            create_only=True,
        )
    assert deleted == [(definition.agent_name, "9")]


@pytest.mark.parametrize(
    "create_result",
    [
        ServiceRequestError("response lost after commit"),
        SimpleNamespace(version=""),
    ],
)
def test_foundry_uncertain_create_reconciles_and_deletes_exact_new_version(
    create_result,
):
    deleted = []

    class Operations:
        def __init__(self):
            self.metadata = None

        def create_version(self, *args, **kwargs):
            self.metadata = kwargs["metadata"]
            if isinstance(create_result, BaseException):
                raise create_result
            return create_result

        def delete_version(self, *, agent_name, agent_version):
            deleted.append((agent_name, agent_version))

        def list_versions(self, *, agent_name):
            return [
                SimpleNamespace(
                    version="10",
                    metadata=self.metadata,
                )
            ]

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    backend._build_prompt_definition = lambda **kwargs: {"definition": "trusted"}
    desired_hash = canonical_sha256({"definition": "trusted"})
    calls = 0

    def get(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return L7ResourceReadback(
                resource_kind="foundry_agent",
                stable_id=(
                    f"{kwargs['project_resource_id']}/agents/"
                    f"{kwargs['agent_name']}"
                ),
                exists=False,
            )
        return L7ResourceReadback(
            resource_kind="foundry_agent",
            stable_id=(
                f"{kwargs['project_resource_id']}/agents/"
                f"{kwargs['agent_name']}"
            ),
            exists=True,
            etag="10",
            resource_type="PromptAgent",
            properties_hash=desired_hash,
        )

    backend.get = get
    config = _config()
    definition = _definition(config)
    with pytest.raises(L7DeploymentError, match="reconciled"):
        backend.upsert(
            config=config,
            definition=definition,
            expected_etag=None,
            create_only=True,
        )
    assert deleted == [(definition.agent_name, "10")]


def test_foundry_reconciliation_never_deletes_concurrent_same_definition():
    deleted = []

    class Operations:
        def list_versions(self, *, agent_name):
            return [
                SimpleNamespace(
                    version="11",
                    metadata={"l7_attempt_id": "op-sha256:" + "b" * 64},
                )
            ]

        def list(self):
            return [
                SimpleNamespace(
                    name="Canonical L6 Agent",
                    versions=SimpleNamespace(
                        latest=SimpleNamespace(version="11")
                    ),
                )
            ]

        def delete_version(self, *, agent_name, agent_version):
            deleted.append((agent_name, agent_version))

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    backend._reconciliation_timeout = 0.01
    backend._reconciliation_poll = 0.001
    config = _config()
    definition = _definition(config)
    with pytest.raises(L7DeploymentError, match="was not found"):
        backend._reconcile_uncertain_version(
            config=config,
            definition=definition,
            previous_etag=None,
            attempt_id="op-sha256:" + "a" * 64,
        )
    assert deleted == []


def test_foundry_reconciliation_waits_for_delayed_attempt_version():
    deleted = []
    calls = 0
    attempt_id = "op-sha256:" + "a" * 64

    class Operations:
        def list_versions(self, *, agent_name):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return [
                SimpleNamespace(
                    version="12",
                    metadata={"l7_attempt_id": attempt_id},
                )
            ]

        def list(self):
            return [
                SimpleNamespace(
                    name="Canonical L6 Agent",
                    versions=SimpleNamespace(
                        latest=SimpleNamespace(version="12")
                    ),
                )
            ]

        def delete_version(self, *, agent_name, agent_version):
            deleted.append((agent_name, agent_version))

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    backend._reconciliation_timeout = 1
    backend._reconciliation_poll = 0.001
    config = _config()
    definition = _definition(config)
    backend._reconcile_uncertain_version(
        config=config,
        definition=definition,
        previous_etag=None,
        attempt_id=attempt_id,
    )
    assert calls == 2
    assert deleted == [(definition.agent_name, "12")]


def test_foundry_reconciliation_retries_transient_list_failure():
    deleted = []
    calls = 0
    attempt_id = "op-sha256:" + "a" * 64

    class Operations:
        def list_versions(self, *, agent_name):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ServiceRequestError("transient")
            return [
                SimpleNamespace(
                    version="13",
                    metadata={"l7_attempt_id": attempt_id},
                )
            ]

        def delete_version(self, *, agent_name, agent_version):
            deleted.append((agent_name, agent_version))

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    backend._reconciliation_timeout = 1
    backend._reconciliation_poll = 0.001
    config = _config()
    definition = _definition(config)
    backend._reconcile_uncertain_version(
        config=config,
        definition=definition,
        previous_etag=None,
        attempt_id=attempt_id,
    )
    assert calls == 2
    assert deleted == [(definition.agent_name, "13")]


def test_production_adapter_builder_wires_distinct_remote_probe_credential():
    config = _config()
    deployment_credential = SimpleNamespace()
    remote_credential = SimpleNamespace()
    probe, mutations = build_azure_l7_adapters(
        config=config,
        foundry_backend=SimpleNamespace(),
        ownership_authority=SimpleNamespace(),
        remote_probe_credential=remote_credential,
        credential=deployment_credential,
        request=lambda *args, **kwargs: None,
        session=SimpleNamespace(),
    )
    assert probe._credential is deployment_credential
    assert probe._remote_probe_credential is remote_credential
    assert mutations._probe is probe
