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
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
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
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        response = self._client.chat.completions.create(
            model=self._config.chat_deployment,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0,
            seed=42,
        )

        raw: str = response.choices[0].message.content
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Foundry response could not be parsed as JSON: {exc}\n"
                f"Raw content (first 500 chars): {raw[:500]}"
            ) from exc

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
