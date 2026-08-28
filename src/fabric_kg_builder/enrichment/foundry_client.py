"""Thin Azure OpenAI SDK wrapper for chat-JSON completions and embeddings.

Security note — domain text separation
---------------------------------------
``system`` MUST be a **fixed, developer-controlled** instruction string.
``user`` carries source context AND any user-supplied domain text (delimited).

Domain text supplied by end-users MUST ONLY appear in the *user* message.
It must NEVER be placed in the system/developer prompt.  Placing user-controlled
text in the system message is a prompt-injection / privilege-escalation vector
that can override output-format constraints, safety rules, and extraction
behaviour.  See SPEC-004 §2.3 for the authoritative security requirement.

Mockability
-----------
The underlying SDK client is injected via ``_sdk_client``::

    from unittest.mock import MagicMock
    mock = MagicMock()
    client = FoundryClient(config, _sdk_client=mock)

The injected object must satisfy the call chains::

    # Chat completions:
    _sdk_client.chat.completions.create(
        model=..., messages=..., **kwargs
    ) -> obj with obj.choices[0].message.content == "<json string>"

    # Embeddings:
    _sdk_client.embeddings.create(
        model=..., input=..., dimensions=..., **kwargs
    ) -> obj with obj.data[i].embedding == list[float]

This matches the ``make_foundry_client`` factory in tests/conftest.py.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..config.schema import FoundryConfig


class FoundryClient:
    """Thin wrapper around the Azure OpenAI SDK (openai.AzureOpenAI).

    Supports:
    - :meth:`complete_json` — structured/JSON-mode chat completion.
    - :meth:`embed` — batch text embeddings at a fixed dimension.

    Auth
    ----
    ``DefaultAzureCredential`` is used by default (managed identity, service
    principal, or ``az login`` in local dev) via a bearer token provider.
    If ``AZURE_AI_FOUNDRY_API_KEY`` or ``AZURE_OPENAI_API_KEY`` is present in
    the environment, an API key is used instead.  Keys are **never** stored in
    code or config files.

    Parameters
    ----------
    config:
        ``FoundryConfig`` from the project ``Config`` object.  Contains
        non-secret settings only (openai_endpoint, deployment names, dimensions).
    _sdk_client:
        Optional pre-built client for testing.  Pass a ``MagicMock`` that
        satisfies the call chains documented in the module docstring.
    """

    def __init__(
        self,
        config: FoundryConfig,
        *,
        _sdk_client: Any = None,
    ) -> None:
        self._config = config
        self._client = (
            _sdk_client if _sdk_client is not None else self._build_sdk_client(config)
        )

    # ------------------------------------------------------------------
    # SDK construction — isolated so the rest of the class stays testable
    # ------------------------------------------------------------------

    @staticmethod
    def _build_sdk_client(config: FoundryConfig) -> Any:
        """Construct an ``openai.AzureOpenAI`` client from *config*.

        Verified working call pattern::

            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AzureOpenAI
            tp = get_bearer_token_provider(DefaultAzureCredential(),
                                           "https://cognitiveservices.azure.com/.default")
            client = AzureOpenAI(azure_endpoint=..., azure_ad_token_provider=tp,
                                 api_version=...)

        If ``AZURE_AI_FOUNDRY_API_KEY`` or ``AZURE_OPENAI_API_KEY`` is set,
        ``api_key=`` is used instead of the token provider.
        """
        try:
            from openai import AzureOpenAI  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "openai>=1.0 is required for live Foundry calls. "
                "Install it with: pip install openai"
            ) from exc

        openai_endpoint = config.openai_endpoint
        if not openai_endpoint:
            raise EnvironmentError(
                "FoundryConfig.openai_endpoint is not set. "
                "Set AZURE_OPENAI_ENDPOINT in your .env or foundry.openai_endpoint in fabric-kg.yaml."
            )

        api_key = os.environ.get("AZURE_AI_FOUNDRY_API_KEY") or os.environ.get(
            "AZURE_OPENAI_API_KEY"
        )
        if api_key:
            return AzureOpenAI(
                azure_endpoint=openai_endpoint,
                api_key=api_key,
                api_version=config.api_version,
                timeout=config.request_timeout_seconds,
            )

        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # type: ignore[import]

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        return AzureOpenAI(
            azure_endpoint=openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=config.api_version,
            timeout=config.request_timeout_seconds,
        )

    def execution_identity(self) -> dict[str, Any]:
        """Return non-secret model and request settings that affect outputs."""
        return {
            "provider": "azure_openai",
            "chat_deployment": self._config.chat_deployment,
            "api_version": self._config.api_version,
            "request_timeout_seconds": self._config.request_timeout_seconds,
            "completion_format": (
                "json_schema_strict_when_compatible_else_json_object"
            ),
            "temperature": 0.0,
            "seed": 42,
            "max_completion_tokens": 4_096,
            "max_attempts": 2,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        *,
        max_completion_tokens: int = 4_096,
        max_attempts: int = 2,
    ) -> dict:
        """Call the chat deployment and return the parsed JSON response.

        Parameters
        ----------
        system:
            **Developer-controlled** instruction string (role, output contract,
            constraints).  MUST NOT contain any user-supplied domain text.
            See SPEC-004 §2.3 for the hard security requirement.
        user:
            User message carrying source context and/or domain text.
            Domain text must be clearly delimited (see SPEC-004 §6.4).
        json_schema:
            JSON Schema dict.  Used to augment the system prompt with schema
            expectations; ``response_format={"type":"json_object"}`` is sent
            to the model (proven working with gpt-5-4-mini).

        Returns
        -------
        dict
            Parsed JSON object from the model response.

        Raises
        ------
        ValueError
            When the model returns content that cannot be parsed as JSON.
        """
        schema_instruction = ""
        if json_schema:
            schema_instruction = (
                "\nReturn an object that validates exactly against this JSON "
                "Schema. Do not add fields that the schema does not permit.\n"
                f"{json.dumps(json_schema, sort_keys=True)}"
            )
        if max_completion_tokens < 256:
            raise ValueError("max_completion_tokens must be at least 256.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

        retry_instruction = (
            "\nYour previous response was not valid JSON. Return a smaller, "
            "complete JSON object now. Prefer fewer high-confidence items over "
            "truncation. Do not include prose or Markdown."
        )
        last_error: json.JSONDecodeError | None = None
        raw = ""
        for attempt in range(max_attempts):
            attempt_system = system + schema_instruction
            if attempt:
                attempt_system += retry_instruction
            strict_schema = None
            if json_schema:
                try:
                    strict_schema = _azure_strict_schema(json_schema)
                except ValueError:
                    strict_schema = None
            response_format = (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "fabric_kg_structured_response",
                        "strict": True,
                        "schema": strict_schema,
                    },
                }
                if strict_schema is not None
                else {"type": "json_object"}
            )
            response = self._client.chat.completions.create(
                model=self._config.chat_deployment,
                messages=[
                    {"role": "system", "content": attempt_system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format,
                temperature=0.0,
                seed=42,
                max_completion_tokens=max_completion_tokens,
            )
            raw = response.choices[0].message.content
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                last_error = exc

        assert last_error is not None
        raise ValueError(
            f"Foundry response could not be parsed as JSON after {max_attempts} "
            f"attempt(s): {last_error}\nRaw content (first 500 chars): {raw[:500]}"
        ) from last_error

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* and return one float vector per input string.

        Parameters
        ----------
        texts:
            Strings to embed.  Each string should be the prepared
            ``embedding_text`` value (SPEC-004 §7.4), max 512 tokens.

        Returns
        -------
        list[list[float]]
            One vector per input string.  Length of each vector equals
            ``config.embedding_dimensions`` (default: 1536).

        Notes
        -----
        The ``dimensions`` parameter requests output truncation at the
        configured dimension (1536).  Changing this value requires a full
        rebuild of the AI Search vector index — see SPEC-004 §9.2.
        """
        response = self._client.embeddings.create(
            model=self._config.embedding_deployment,
            input=texts,
            dimensions=self._config.embedding_dimensions,
        )
        return [item.embedding for item in response.data]
def _azure_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert generated schemas to Azure structured-output subset."""
    normalized = json.loads(json.dumps(schema))
    property_count = 0
    unsupported_constraints = {
        "maxItems",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
    }

    def visit(value: Any) -> None:
        nonlocal property_count
        if isinstance(value, dict):
            if not value:
                raise ValueError(
                    "Azure strict schema cannot contain untyped branches"
                )
            value.pop("default", None)
            for keyword in unsupported_constraints:
                value.pop(keyword, None)
            properties = value.get("properties")
            if value.get("type") == "object" and isinstance(properties, dict):
                property_count += len(properties)
                value["additionalProperties"] = False
                value["required"] = list(properties)
            if "oneOf" in value:
                value["anyOf"] = value.pop("oneOf")
            if "allOf" in value:
                raise ValueError(
                    "Azure strict schema cannot contain allOf"
                )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(normalized)
    if property_count > 100:
        raise ValueError(
            "Azure strict schema exceeds the 100-property limit"
        )
    return normalized
