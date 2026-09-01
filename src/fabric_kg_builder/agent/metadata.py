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
    # Regression-battery fields (issue #138): a single run of a test case is
    # NOT sound evidence of correctness for this agent's multi-step
    # tool-invocation behavior — manual testing found a query with a 1/5
    # pass rate across identical reruns. Every test case is therefore run
    # `repeat` times (default set by the battery runner) and classified as
    # pass (N/N) / fail (0/N) / flaky (neither). `required=True` (default)
    # means the case MUST classify as "pass" or the deploy is blocked;
    # `required=False` marks a known-flaky canary that is recorded and
    # reported but never blocks the deploy on its own.
    required: bool = True
    repeat: int | None = None


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
