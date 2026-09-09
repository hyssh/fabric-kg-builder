"""agent/metadata.py — load and validate .foundry/agent-metadata.yaml.

This is the single source of truth for the Foundry agent deployment.
No secrets are stored here — only resource references resolved at runtime
via environment variables.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")

_DEFAULT_METADATA_PATH = Path(".foundry") / "agent-metadata.yaml"


def _resolve_env_vars(text: str) -> str:
    """Replace ${VAR:-default} placeholders with env values or defaults."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default_val = match.group(2) or ""
        return os.environ.get(var_name, default_val)
    return _ENV_VAR_PATTERN.sub(replacer, text)


class EnvironmentConfig(BaseModel):
    projectEndpoint: str
    resourceGroup: str = ""
    subscriptionId: str = ""
    deployments: dict[str, str] = Field(default_factory=dict)
    observability: dict[str, Any] = Field(default_factory=dict)
    acr: dict[str, Any] = Field(default_factory=dict)
    connections: dict[str, str | None] = Field(default_factory=dict)
    knowledge: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        # Normalize None values to empty strings in observability/acr dicts.
        self.observability = {k: (v or "") for k, v in self.observability.items()}
        self.acr = {k: (v or "") for k, v in self.acr.items()}
        self.connections = {k: (v or "") for k, v in self.connections.items()}
        self.knowledge = {k: (v or "") for k, v in self.knowledge.items()}


class PromptAgentConfig(BaseModel):
    systemPromptVersion: str = "v1"
    instructionsVariant: str = "search-ontology-mixed"
    temperature: float = 0.0
    maxTokens: int = 2048
    topP: float = 1.0
    seed: int = 42


class ModelConfig(BaseModel):
    deploymentName: str = "gpt-5-4-mini"
    apiVersion: str = "2024-12-01-preview"


class TestCase(BaseModel):
    id: str
    description: str = ""
    input: str
    expectedRouteType: str = ""
    requiredCitationFields: list[str] = Field(default_factory=list)
    requiredRefusal: bool = False


class AgentMetadata(BaseModel):
    """Validated agent metadata from .foundry/agent-metadata.yaml."""

    schemaVersion: str = "1.0"
    agentName: str
    defaultEnvironment: str = "dev"
    model: ModelConfig = Field(default_factory=ModelConfig)
    promptAgent: PromptAgentConfig = Field(default_factory=PromptAgentConfig)
    environments: dict[str, EnvironmentConfig] = Field(default_factory=dict)
    testCases: list[TestCase] = Field(default_factory=list)
    deploymentContext: dict[str, Any] = Field(default_factory=dict)

    def env_config(self, environment: str | None = None) -> EnvironmentConfig:
        """Return the EnvironmentConfig for the given env (or defaultEnvironment)."""
        env = environment or self.defaultEnvironment
        if env not in self.environments:
            raise KeyError(
                f"Environment '{env}' not found in agent-metadata.yaml. "
                f"Available: {list(self.environments)}"
            )
        return self.environments[env]

    def project_endpoint(self, environment: str | None = None) -> str:
        return self.env_config(environment).projectEndpoint

    def chat_deployment(self, environment: str | None = None) -> str:
        cfg = self.env_config(environment)
        return cfg.deployments.get("chat", self.model.deploymentName)


class AgentMetadataError(Exception):
    """Raised when agent-metadata.yaml cannot be loaded or validated."""


def question_routing_readiness(
    context: dict[str, Any] | None,
    *,
    fabric_data_agent_connection_id: str | None = None,
    data_agent_snapshot: Any | None = None,
    source_hash: str | None = None,
) -> dict[str, Any] | None:
    """Describe configured sources without promoting declarations to SQL readiness."""
    if context is None:
        return None
    from fabric_kg_builder.domain.question_routing import QuestionRoutingContext

    routing = QuestionRoutingContext.model_validate(context).model_dump(mode="json")
    if source_hash is not None and re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise ValueError("question routing source hash must be SHA-256")
    sources = data_agent_snapshot.source_receipts() if data_agent_snapshot is not None else []
    sql_sources = [
        source for source in sources
        if source["source_type"] in {"lakehouse", "lakehouse_tables", "data_warehouse"}
    ]
    return {
        "question_routing_context": routing,
        "source_hash": source_hash,
        "fabric_data_agent_connection_configured": bool(fabric_data_agent_connection_id),
        "data_agent_stage": data_agent_snapshot.stage if data_agent_snapshot is not None else None,
        "configured_sql_source_references": sql_sources,
        "sql_execution": {
            "execution_verified": False,
            "physical_binding_state": "unresolved",
            "status": "configured_not_verified" if sql_sources else "source_binding_not_verified",
            "blocking_reasons": [
                "physical_table_field_time_bindings_unresolved",
                "sql_execution_not_verified",
            ],
        },
    }


def matching_declared_sql_questions(
    context: dict[str, Any] | None, question: str,
) -> tuple[str, ...]:
    """Match registered question wording only; this is not a free-form SQL classifier."""
    if context is None:
        return ()
    from fabric_kg_builder.domain.question_routing import QuestionRoutingContext

    validated = QuestionRoutingContext.model_validate(context)
    text = question.strip().casefold()
    return tuple(
        item.question_id for item in validated.questions
        if item.routing.backend == "lakehouse_sql" and item.question.strip().casefold() == text
    )


def load_agent_metadata(
    path: str | Path | None = None,
) -> AgentMetadata:
    """Load and validate .foundry/agent-metadata.yaml.

    Resolves ${ENV_VAR:-default} placeholders before YAML parsing.

    Args:
        path: Override the default `.foundry/agent-metadata.yaml` path.

    Raises:
        AgentMetadataError: If the file is missing or fails validation.
    """
    metadata_path = Path(path) if path else _DEFAULT_METADATA_PATH
    if not metadata_path.exists():
        raise AgentMetadataError(
            f"Agent metadata not found at '{metadata_path}'. "
            "Ensure .foundry/agent-metadata.yaml exists."
        )
    try:
        raw_text = metadata_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentMetadataError(f"Cannot read '{metadata_path}': {exc}") from exc

    interpolated = _resolve_env_vars(raw_text)
    try:
        loaded = yaml.safe_load(interpolated)
    except yaml.YAMLError as exc:
        raise AgentMetadataError(f"YAML error in '{metadata_path}': {exc}") from exc

    if not isinstance(loaded, dict):
        raise AgentMetadataError(f"'{metadata_path}' must be a YAML mapping.")

    try:
        return AgentMetadata.model_validate(loaded)
    except Exception as exc:
        raise AgentMetadataError(f"Validation failed for '{metadata_path}': {exc}") from exc
