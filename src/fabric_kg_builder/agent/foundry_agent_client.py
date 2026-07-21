"""agent/foundry_agent_client.py — Azure AI Projects 2.x prompt-agent client.

Implements the authoritative Foundry prompt-agent lifecycle against
``azure-ai-projects>=2.3.0``.

Live lifecycle:
  1. validate_schema   — list agents, find existing by name (id/name/version)
  2. create_version    — project.agents.create_version(name, PromptAgentDefinition, metadata)
  3. check_ready       — list agents, confirm the newly versioned agent exists
  4. invoke            — project.get_openai_client(agent_name)
                         → conversations.create()
                         → responses.create(conversation=..., input=prompt)
                         → response.output_text
  5. Persist returned id / name / version in DeploymentContext + lineage

SDK endpoint format:
  https://<resource>.services.ai.azure.com/api/projects/<project>

References:
  https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent
  https://learn.microsoft.com/python/api/azure-ai-projects/
      azure.ai.projects.operations.agentsoperations

No secrets stored in this class; credentials are injected via DefaultAzureCredential
or an injectable transport for unit tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_SDK_AVAILABLE: bool = False
try:
    from azure.ai.projects import AIProjectClient  # type: ignore[import]
    from azure.ai.projects.models import (  # type: ignore[import]
        AISearchIndexResource,
        AzureAISearchTool,
        AzureAISearchToolResource,
        FabricDataAgentToolParameters,
        MicrosoftFabricPreviewTool,
        PromptAgentDefinition,
        ToolProjectConnection,
    )
    _SDK_AVAILABLE = True
except ImportError:
    pass


class FoundryClientError(Exception):
    """Raised when the Foundry client encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Injectable transport protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentTransport(Protocol):
    """Minimal injectable transport — offline tests override this."""

    def get_agent(self, agent_name: str) -> dict[str, Any] | None: ...
    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]: ...
    def invoke_agent(self, agent_name: str, prompt: str, timeout_s: int) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Fake transport (deterministic test double — zero network calls)
# ---------------------------------------------------------------------------


class FakeAgentTransport:
    """Test double for AgentTransport.

    Records every call as a dict in ``self.calls`` keyed by ``"method"``.
    Supports convenience constructor aliases so production-contract tests
    can express failure scenarios clearly.
    """

    def __init__(
        self,
        *,
        existing_agent: dict[str, Any] | None = None,
        create_response: dict[str, Any] | None = None,
        invoke_response: dict[str, Any] | None = None,
        raise_on_invoke: Exception | None = None,
        raise_on_create: Exception | None = None,
        raise_on_validate: Exception | None = None,
        # Convenience aliases used in production-contract tests:
        validate_raises: Exception | None = None,
        create_raises: Exception | None = None,
        ready: bool = True,
        smoke_answer: str | None = None,
    ) -> None:
        self._existing = existing_agent or (
            {"id": "agent_fake_001", "name": "fabric-kg-grounded-agent",
             "version": "1", "status": "ready"}
            if ready else None
        )
        self._create_resp = create_response or {
            "id": "agent_fake_001",
            "name": "fabric-kg-grounded-agent",
            "version": "2",
        }
        if smoke_answer is not None:
            self._invoke_resp = {"answer": smoke_answer, "output_text": smoke_answer, "status": "completed"}
        else:
            self._invoke_resp = invoke_response or {
                "answer": "route_type: search — I am ready.",
                "output_text": "route_type: search — I am ready.",
                "status": "completed",
            }
        self._raise_invoke = raise_on_invoke
        self._raise_create = raise_on_create or create_raises
        self._raise_validate = raise_on_validate or validate_raises
        self.calls: list[dict[str, Any]] = []

    def get_agent(self, agent_name: str) -> dict[str, Any] | None:
        self.calls.append({"method": "validate_schema", "agent_name": agent_name})
        if self._raise_validate:
            raise self._raise_validate
        return self._existing

    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({
            "method": "create_or_update_agent",
            "name": definition.get("name"),
            "definition": dict(definition),
        })
        if self._raise_create:
            raise self._raise_create
        resp = dict(self._create_resp)
        resp.setdefault("name", definition.get("name", ""))
        return resp

    def invoke_agent(self, agent_name: str, prompt: str, timeout_s: int) -> dict[str, Any]:
        self.calls.append({"method": "invoke", "agent_name": agent_name})
        if self._raise_invoke:
            raise self._raise_invoke
        return self._invoke_resp


# ---------------------------------------------------------------------------
# SDK transport — azure-ai-projects 2.x (live only)
# ---------------------------------------------------------------------------


class SDKAgentTransport:
    """Transport backed by azure-ai-projects >= 2.3.0.

    Endpoint format:
        https://<resource>.services.ai.azure.com/api/projects/<project>

    All network errors are wrapped in FoundryClientError.
    """

    def __init__(
        self,
        *,
        project_endpoint: str,
        credential: Any,
    ) -> None:
        if not _SDK_AVAILABLE:
            raise FoundryClientError(
                "azure-ai-projects>=2.3.0 is required for live agent deployment.\n"
                "Install: pip install 'azure-ai-projects>=2.3.0' azure-identity"
            )
        self._project = AIProjectClient(
            endpoint=project_endpoint,
            credential=credential,
            allow_preview=True,
        )

    # ── get_agent ────────────────────────────────────────────────────────────

    def get_agent(self, agent_name: str) -> dict[str, Any] | None:
        """List project agents and return the latest version for agent_name, or None."""
        try:
            agents_page = self._project.agents.list()
            # Collect all versions for this name; pick highest version number.
            candidates: list[dict[str, Any]] = []
            for agent in agents_page:
                name = _attr_or_key(agent, "name")
                if name == agent_name:
                    candidates.append(_to_dict(agent))
            if not candidates:
                return None
            # Sort by version (string → int if numeric, else lexicographic)
            def _ver_key(d: dict) -> int:
                try:
                    return int(d.get("version", 0))
                except (TypeError, ValueError):
                    return 0
            return max(candidates, key=_ver_key)
        except Exception as exc:
            raise FoundryClientError(f"Failed to list agents: {exc}") from exc

    # ── create_or_update_agent ───────────────────────────────────────────────

    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
        """Create a new versioned prompt agent via project.agents.create_version().

        Uses the 2.x API:
            create_version(agent_name, *, definition=PromptAgentDefinition(...),
                           metadata=..., description=...)

        Returns a dict with id / name / version from the returned agent object.
        """
        agent_name = definition["name"]
        try:
            tools: list[Any] = []
            for tool_spec in definition.get("tools", []):
                tool_type = tool_spec.get("type")
                if tool_type == "azure_ai_search":
                    tools.append(
                        AzureAISearchTool(
                            azure_ai_search=AzureAISearchToolResource(
                                indexes=[
                                    AISearchIndexResource(
                                        project_connection_id=tool_spec[
                                            "project_connection_id"
                                        ],
                                        index_name=tool_spec["index_name"],
                                        query_type=tool_spec.get(
                                            "query_type",
                                            "vector_semantic_hybrid",
                                        ),
                                        top_k=int(tool_spec.get("top_k", 5)),
                                    )
                                ]
                            )
                        )
                    )
                elif tool_type == "fabric_data_agent":
                    tools.append(
                        MicrosoftFabricPreviewTool(
                            fabric_dataagent_preview=FabricDataAgentToolParameters(
                                project_connections=[
                                    ToolProjectConnection(
                                        project_connection_id=tool_spec[
                                            "project_connection_id"
                                        ]
                                    )
                                ]
                            )
                        )
                    )
                elif tool_type == "mcp":
                    from azure.ai.projects.models import MCPTool  # type: ignore[import-not-found]

                    tools.append(
                        MCPTool(
                            server_label=tool_spec["server_label"],
                            server_url=tool_spec["server_url"],
                            project_connection_id=tool_spec[
                                "project_connection_id"
                            ],
                            require_approval=tool_spec.get(
                                "require_approval", "never"
                            ),
                            allowed_tools=tool_spec.get("allowed_tools", []),
                        )
                    )
                else:
                    raise FoundryClientError(
                        f"Unsupported prompt-agent tool type: {tool_type!r}"
                    )
            prompt_def = PromptAgentDefinition(
                model=definition["model"],
                instructions=definition["system_prompt"],
                temperature=float(definition.get("temperature", 0.0)),
                top_p=float(definition.get("top_p", 1.0)),
                tools=tools,
            )
            agent = self._project.agents.create_version(
                agent_name,
                definition=prompt_def,
                metadata={
                    "instructions_version": str(definition.get("system_prompt_version", "")),
                    "instructions_hash": str(definition.get("instructions_hash", "")),
                },
                description=definition.get("description", ""),
            )
            return _to_dict(agent)
        except Exception as exc:
            raise FoundryClientError(f"create_version failed: {exc}") from exc

    # ── invoke_agent ─────────────────────────────────────────────────────────

    def invoke_agent(self, agent_name: str, prompt: str, timeout_s: int) -> dict[str, Any]:
        """Invoke via the project-bound OpenAI client (2.x path).

        Exact SDK sequence:
            openai = project.get_openai_client(agent_name=agent_name)
            conversation = openai.conversations.create()
            response = openai.responses.create(
                conversation=conversation.id, input=prompt
            )
            return response.output_text
        """
        try:
            openai_client = self._project.get_openai_client(agent_name=agent_name)
            conversation = openai_client.conversations.create()
            conv_id = _attr_or_key(conversation, "id")
            response = openai_client.responses.create(
                conversation=conv_id,
                input=prompt,
            )
            output_text: str = (
                getattr(response, "output_text", None)
                or (
                    response.get("output_text", "")
                    if isinstance(response, dict)
                    else ""
                )
            )
            if not output_text:
                raise FoundryClientError(
                    "response.output_text is empty — agent may not be responding."
                )
            return {"answer": output_text, "output_text": output_text, "status": "completed"}
        except FoundryClientError:
            raise
        except Exception as exc:
            raise FoundryClientError(f"Invoke failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attr_or_key(obj: Any, key: str, default: str = "") -> str:
    """Get attribute or dict key safely."""
    if isinstance(obj, dict):
        return str(obj.get(key, default))
    return str(getattr(obj, key, default))


def _to_dict(obj: Any) -> dict[str, Any]:
    """Normalise SDK model objects → plain dict."""
    if isinstance(obj, dict):
        return obj
    # Pydantic-based SDK models expose model_dump / model_dump_json
    try:
        return obj.model_dump()
    except AttributeError:
        pass
    try:
        return json.loads(obj.model_dump_json())
    except AttributeError:
        pass
    # Fallback: extract known fields
    result: dict[str, Any] = {}
    for k in ("id", "name", "version", "status", "model", "description"):
        v = getattr(obj, k, None)
        if v is not None:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Public FoundryAgentClient façade
# ---------------------------------------------------------------------------


class FoundryAgentClient:
    """High-level Foundry prompt-agent client used by the deployer.

    Delegates all I/O to an ``AgentTransport``.  Inject ``FakeAgentTransport``
    for unit tests; ``SDKAgentTransport`` is built automatically from
    ``project_endpoint`` and ``credential`` when no transport is provided.

    Parameters
    ----------
    project_endpoint:
        Azure AI Foundry project endpoint URL.
        Format: ``https://<resource>.services.ai.azure.com/api/projects/<project>``
    credential:
        Azure identity credential (``DefaultAzureCredential`` or mock).
    transport / _transport:
        Injected ``AgentTransport`` for unit tests.
    smoke_timeout_s:
        Seconds to wait for smoke invocation.
    """

    def __init__(
        self,
        *,
        project_endpoint: str = "",
        credential: Any = None,
        _transport: AgentTransport | None = None,
        transport: AgentTransport | None = None,
        smoke_timeout_s: int = 60,
    ) -> None:
        self._endpoint = project_endpoint
        self._credential = credential
        self._smoke_timeout = smoke_timeout_s
        # Accept either _transport (internal) or transport (test convenience)
        injected = _transport or transport
        if injected is not None:
            self._transport: AgentTransport = injected
        elif project_endpoint:
            self._transport = SDKAgentTransport(
                project_endpoint=project_endpoint,
                credential=credential,
            )
        else:
            raise FoundryClientError(
                "Either project_endpoint or transport must be provided."
            )
        # Populated after create_or_update_agent: id / name / version
        self._agent_id: str = ""
        self._agent_version: str = ""

    def validate_schema(self, agent_name: str) -> dict[str, Any]:
        """List agents and verify the project is reachable.

        An existing agent in a terminal error state is invalid. A missing agent
        is valid because this probe runs before first creation.

        Returns:
            {
                "valid": True,
                "agent_id": str,     # id of existing version, or ""
                "agent_version": str,# version of existing agent, or ""
                "existing": dict | None,
            }

        Raises FoundryClientError if the project endpoint is unreachable.
        """
        existing = self._transport.get_agent(agent_name)
        agent_id = _attr_or_key(existing or {}, "id") if existing else ""
        agent_version = _attr_or_key(existing or {}, "version") if existing else ""
        status = str(_attr_or_key(existing or {}, "status") or "").lower()
        validation_errors = (
            [f"existing agent is in terminal state {status!r}"]
            if status in {"error", "failed", "canceled", "cancelled"}
            else []
        )
        if agent_id:
            self._agent_id = agent_id
            self._agent_version = agent_version
        return {
            "valid": not validation_errors,
            "agent_id": agent_id,
            "agent_version": agent_version,
            "existing": existing,
            "errors": validation_errors,
        }

    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
        """Create a new versioned prompt agent.

        Returns:
            {
                "version_id": str,   # alias for id (deployer compat)
                "id": str,
                "name": str,
                "version": str,
            }

        Raises FoundryClientError on failure.
        """
        result = self._transport.create_or_update_agent(definition)
        agent_id = _attr_or_key(result, "id")
        agent_version = _attr_or_key(result, "version")
        if agent_id:
            self._agent_id = agent_id
            self._agent_version = agent_version
        result["version_id"] = agent_id
        return result

    def check_ready(self, agent_name: str) -> bool:
        """Verify the agent exists and is in a ready state after create_version."""
        existing = self._transport.get_agent(agent_name)
        if not existing:
            return False
        status = existing.get("status", "ready")
        return status in ("ready", "active", "")

    def invoke(self, agent_name: str, prompt: str) -> dict[str, Any]:
        """Invoke the smoke prompt via the project-bound OpenAI client.

        Returns dict with ``answer`` (= ``output_text``) and ``status``.
        """
        return self._transport.invoke_agent(agent_name, prompt, self._smoke_timeout)


# ---------------------------------------------------------------------------
# build_client_from_metadata helper
# ---------------------------------------------------------------------------


def build_client_from_metadata(
    metadata: Any,  # AgentMetadata (avoids circular import)
    environment: str | None = None,
    *,
    _transport: AgentTransport | None = None,
) -> FoundryAgentClient:
    """Build a FoundryAgentClient from agent-metadata.yaml + DefaultAzureCredential.

    Endpoint format validated to be non-empty; real SDK transport built unless
    _transport is injected (for tests).

    Args:
        metadata:    Loaded AgentMetadata.
        environment: Target env; defaults to metadata.defaultEnvironment.
        _transport:  Injected transport for tests (skips DefaultAzureCredential).

    Returns:
        FoundryAgentClient ready for live deployment.

    Raises:
        FoundryClientError: When endpoint is missing or azure-identity unavailable.
    """
    env = environment or metadata.defaultEnvironment
    env_cfg = metadata.env_config(env)
    project_endpoint = env_cfg.projectEndpoint

    if not project_endpoint:
        raise FoundryClientError(
            f"environments.{env}.projectEndpoint is empty in agent-metadata.yaml. "
            "Set it to: https://<resource>.services.ai.azure.com/api/projects/<project>"
        )

    if _transport is not None:
        return FoundryAgentClient(
            project_endpoint=project_endpoint,
            credential=None,
            _transport=_transport,
        )

    try:
        from azure.identity import DefaultAzureCredential  # type: ignore[import]
        credential = DefaultAzureCredential()
    except ImportError as exc:
        raise FoundryClientError(
            "azure-identity is required for live agent deployment.\n"
            "Install: pip install 'azure-ai-projects>=2.3.0' azure-identity"
        ) from exc

    return FoundryAgentClient(
        project_endpoint=project_endpoint,
        credential=credential,
    )
