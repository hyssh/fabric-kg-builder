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
import logging
import math
import os
import threading
import time
from typing import Any, Callable

from pydantic import ValidationError
from ..config.schema import FoundryConfig

_LOGGER = logging.getLogger(__name__)

# Transport failures that are safe to retry with an identical deterministic
# request.  Configuration, authority, and schema errors are never retried.
_RETRYABLE_TRANSPORT_TYPE_NAMES = frozenset(
    {
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    }
)
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 413, 422})
_TRANSPORT_RETRY_MAX_ATTEMPTS = 6
_TRANSPORT_RETRY_BASE_SECONDS = 1.0
_TRANSPORT_RETRY_MAX_SECONDS = 30.0

# A long checkpoint-resumable stage must survive a provider outage that
# outlives the request-local retry budget.  The shared breaker below holds a
# single wall-clock budget for the whole run so concurrent workers escalate
# together instead of each burning an independent budget.
_TRANSPORT_OUTAGE_BUDGET_SECONDS = 900.0
_TRANSPORT_OUTAGE_MAX_SECONDS = 60.0
# Outage waits are taken in slices so a worker resumes promptly once another
# worker proves the provider is back, instead of sleeping out a stale backoff.
_TRANSPORT_OUTAGE_POLL_SECONDS = 5.0
_TRANSPORT_OUTAGE_BUDGET_ENV = "FABRIC_KG_FOUNDRY_OUTAGE_BUDGET_SECONDS"


def _configured_outage_budget_seconds() -> float:
    """Return the outage budget, honouring an explicit environment override.

    An unparseable, negative, or non-finite override is a configuration error
    and must not be silently coerced into the default.  An infinite or NaN
    budget would defeat the bound entirely and retry forever.
    """
    raw = os.environ.get(_TRANSPORT_OUTAGE_BUDGET_ENV)
    if raw is None or not raw.strip():
        return _TRANSPORT_OUTAGE_BUDGET_SECONDS
    try:
        budget = float(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{_TRANSPORT_OUTAGE_BUDGET_ENV} must be a number of seconds; "
            f"got {raw.strip()!r}"
        ) from exc
    if not math.isfinite(budget):
        raise ValueError(
            f"{_TRANSPORT_OUTAGE_BUDGET_ENV} must be finite so retries stay "
            f"bounded; got {raw.strip()!r}"
        )
    if budget < 0.0:
        raise ValueError(
            f"{_TRANSPORT_OUTAGE_BUDGET_ENV} must not be negative; "
            f"got {budget}"
        )
    return budget


def _transport_error_is_retryable(exc: BaseException) -> bool:
    """Return True when *exc* is a transient transport failure.

    Retrying is only safe for connectivity, timeout, throttling, and server
    faults.  Request, authentication, authorization, and validation failures
    are deterministic and must surface immediately.
    """
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return False
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status in _NON_RETRYABLE_STATUS_CODES:
            return False
        if status in _RETRYABLE_STATUS_CODES or status >= 500:
            return True
    return any(
        klass.__name__ in _RETRYABLE_TRANSPORT_TYPE_NAMES
        for klass in type(exc).__mro__
    )


def _transport_retry_sleep(seconds: float) -> None:
    """Indirection point so tests can stub backoff without touching stdlib."""
    time.sleep(seconds)


class TransportOutageError(RuntimeError):
    """Raised when a provider outage outlives the shared outage budget."""

    def __init__(self, elapsed_seconds: float, budget_seconds: float) -> None:
        super().__init__(
            "Foundry transport unavailable for "
            f"{elapsed_seconds:.1f}s, exceeding the {budget_seconds:.1f}s "
            f"outage budget (raise {_TRANSPORT_OUTAGE_BUDGET_ENV} to wait "
            "longer); completed work remains checkpointed and resumable"
        )
        self.elapsed_seconds = elapsed_seconds
        self.budget_seconds = budget_seconds


class _TransportOutageBreaker:
    """Shared, bounded delayed-retry policy for provider outages.

    Every worker that exhausts its request-local retry budget reports here.
    The first report opens an outage window; concurrent workers then share
    that window's wall-clock budget and its escalating backoff rather than
    each retrying independently.  A single success closes the window for
    everyone.  When the budget is spent the breaker latches open so queued
    work fails fast instead of prolonging a dead run.

    ``monotonic`` and ``sleep`` are injected so tests can drive the policy
    with a fake clock instead of real time.
    """

    def __init__(
        self,
        *,
        budget_seconds: float | None = None,
        base_seconds: float = _TRANSPORT_RETRY_BASE_SECONDS,
        max_seconds: float = _TRANSPORT_OUTAGE_MAX_SECONDS,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._budget_override = budget_seconds
        self._base_seconds = base_seconds
        self._max_seconds = max_seconds
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._opened_at: float | None = None
        self._resume_at = 0.0
        self._delay = base_seconds
        self._exhausted = False
        self._outages = 0
        self._delayed_retries = 0
        self._delay_seconds_total = 0.0
        self._longest_outage_seconds = 0.0
        self._recovered_outages = 0

    def _budget(self) -> float:
        if self._budget_override is not None:
            return self._budget_override
        return _configured_outage_budget_seconds()

    def await_recovery(self) -> None:
        """Hold a worker back while an outage window is already open.

        This is what keeps eight concurrent workers from hammering a provider
        that one of them has already found to be down.  The wait is taken in
        slices so a worker resumes promptly once another worker proves the
        provider is back.
        """
        while True:
            with self._lock:
                self._raise_if_spent_locked()
                if self._opened_at is None:
                    return
                remaining = self._resume_at - self._monotonic()
                if remaining <= 0.0:
                    return
            self.sleep(
                min(remaining, self._max_seconds, _TRANSPORT_OUTAGE_POLL_SECONDS)
            )

    def wait_before_retry(self, seconds: float) -> None:
        """Sleep *seconds*, abandoning the wait early once the outage closes."""
        remaining = seconds
        while remaining > 0.0:
            slice_seconds = min(remaining, _TRANSPORT_OUTAGE_POLL_SECONDS)
            self.sleep(slice_seconds)
            remaining -= slice_seconds
            with self._lock:
                if self._opened_at is None or self._exhausted:
                    return

    def ensure_within_budget(self) -> None:
        """Refuse to start new work once an open outage has spent its budget.

        The budget bounds the time the breaker will keep *initiating* requests.
        A request already in flight still runs to the SDK's own timeout.
        """
        with self._lock:
            self._raise_if_spent_locked()

    def remaining_budget(self) -> float:
        """Seconds left in the open outage window, or infinity when closed."""
        with self._lock:
            if self._opened_at is None:
                return math.inf
            return max(0.0, self._budget() - self._elapsed_locked())

    def _raise_if_spent_locked(self) -> None:
        budget = self._budget()
        if self._exhausted:
            raise TransportOutageError(self._elapsed_locked(), budget)
        if self._opened_at is None:
            return
        elapsed = self._elapsed_locked()
        if elapsed < budget:
            return
        self._exhausted = True
        self._longest_outage_seconds = max(
            self._longest_outage_seconds, elapsed
        )
        _LOGGER.error(
            "foundry transport outage budget exhausted; "
            "elapsed_seconds=%.1f; budget_seconds=%.1f",
            elapsed,
            budget,
        )
        raise TransportOutageError(elapsed, budget)

    def _elapsed_locked(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._monotonic() - self._opened_at)

    def record_outage(self) -> float:
        """Register an exhausted request-local budget and return the wait.

        Raises :class:`TransportOutageError` once the shared budget is spent.
        """
        budget = self._budget()
        with self._lock:
            now = self._monotonic()
            if self._opened_at is None and not self._exhausted:
                self._opened_at = now
                self._delay = self._base_seconds
                self._outages += 1
                _LOGGER.warning(
                    "foundry transport outage opened; "
                    "budget_seconds=%.1f",
                    budget,
                )
            self._raise_if_spent_locked()
            elapsed = max(0.0, now - self._opened_at)
            remaining = budget - elapsed
            delay = min(self._delay, self._max_seconds, remaining)
            self._delay = min(self._delay * 2.0, self._max_seconds)
            self._resume_at = now + delay
            self._delayed_retries += 1
            self._delay_seconds_total += delay
            self._longest_outage_seconds = max(
                self._longest_outage_seconds, elapsed
            )
        return delay

    def record_success(self) -> None:
        """Close any open outage window."""
        with self._lock:
            if self._opened_at is None:
                return
            elapsed = max(0.0, self._monotonic() - self._opened_at)
            self._longest_outage_seconds = max(
                self._longest_outage_seconds, elapsed
            )
            self._recovered_outages += 1
            self._opened_at = None
            self._resume_at = 0.0
            self._delay = self._base_seconds
        _LOGGER.warning(
            "foundry transport outage recovered; elapsed_seconds=%.1f",
            elapsed,
        )

    def sleep(self, seconds: float) -> None:
        """Sleep via the injected clock, or the late-bound module default."""
        if self._sleep is None:
            _transport_retry_sleep(seconds)
        else:
            self._sleep(seconds)

    def reset(self) -> None:
        with self._lock:
            self._opened_at = None
            self._resume_at = 0.0
            self._delay = self._base_seconds
            self._exhausted = False
            self._outages = 0
            self._delayed_retries = 0
            self._delay_seconds_total = 0.0
            self._longest_outage_seconds = 0.0
            self._recovered_outages = 0

    def metrics(self) -> dict[str, object]:
        """Return sanitized counters.

        These describe timing and counts only — never request content,
        prompts, tokens, credentials, or endpoints.
        """
        with self._lock:
            return {
                "transport_outages": self._outages,
                "transport_outages_recovered": self._recovered_outages,
                "transport_delayed_retries": self._delayed_retries,
                "transport_delay_seconds_total": round(
                    self._delay_seconds_total, 6
                ),
                "transport_longest_outage_seconds": round(
                    self._longest_outage_seconds, 6
                ),
                "transport_outage_budget_exhausted": self._exhausted,
                "contains_source_content": False,
            }


_TRANSPORT_OUTAGE_BREAKER = _TransportOutageBreaker()


def transport_retry_metrics() -> dict[str, object]:
    """Sanitized transport retry counters for the current process."""
    return _TRANSPORT_OUTAGE_BREAKER.metrics()


def reset_transport_retry_state() -> None:
    """Clear shared outage state (used by tests and per-run setup)."""
    _TRANSPORT_OUTAGE_BREAKER.reset()


def _call_with_transport_retry(
    operation: Callable[[], Any],
    *,
    max_attempts: int = _TRANSPORT_RETRY_MAX_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
    breaker: _TransportOutageBreaker | None = None,
) -> Any:
    """Invoke *operation*, retrying only transient transport failures.

    Two bounded tiers protect an identical, deterministic request:

    * a fast request-local tier of ``max_attempts`` attempts with exponential
      backoff, which absorbs ordinary blips; and
    * a shared outage tier that applies delayed backoff across all workers so
      a multi-minute provider outage does not terminate a resumable stage.

    Both tiers are bounded.  Deterministic request, authentication,
    authorization, validation, and schema failures are never retried.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")
    if breaker is None:
        breaker = _TRANSPORT_OUTAGE_BREAKER
    backoff = sleep if sleep is not None else breaker.sleep
    while True:
        breaker.await_recovery()
        delay = _TRANSPORT_RETRY_BASE_SECONDS
        last_exc: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            # Never start new work once an open outage has spent its budget.
            breaker.ensure_within_budget()
            try:
                result = operation()
            except Exception as exc:
                if not _transport_error_is_retryable(exc):
                    raise
                last_exc = exc
                if attempt >= max_attempts:
                    break
                backoff(
                    min(
                        delay,
                        _TRANSPORT_RETRY_MAX_SECONDS,
                        breaker.remaining_budget(),
                    )
                )
                delay *= 2
            else:
                breaker.record_success()
                return result
        try:
            wait = breaker.record_outage()
        except TransportOutageError as exhausted:
            raise exhausted from last_exc
        breaker.wait_before_retry(wait)


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
        strict_rejected = False
        empty_response = False
        for attempt in range(max_attempts):
            attempt_system = system + schema_instruction
            if attempt:
                attempt_system += retry_instruction
            strict_schema = None
            if json_schema and not strict_rejected:
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
            request_values = {
                "model": self._config.chat_deployment,
                "messages": [
                    {"role": "system", "content": attempt_system},
                    {"role": "user", "content": user},
                ],
                "response_format": response_format,
                "temperature": 0.0,
                "seed": 42,
                "max_completion_tokens": max_completion_tokens,
            }
            try:
                response = _call_with_transport_retry(
                    lambda: self._client.chat.completions.create(
                        **request_values
                    )
                )
            except Exception as exc:
                if (
                    strict_schema is None
                    or (
                        getattr(exc, "status_code", None) != 400
                        and not isinstance(exc, ValidationError)
                    )
                ):
                    raise
                request_values["response_format"] = {
                    "type": "json_object"
                }
                strict_rejected = True
                response = _call_with_transport_retry(
                    lambda: self._client.chat.completions.create(
                        **request_values
                    )
                )
            raw = response.choices[0].message.content
            if not raw or not raw.strip():
                last_error = json.JSONDecodeError(
                    "empty model response", "", 0
                )
                empty_response = True
                continue
            empty_response = False
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                last_error = exc

        assert last_error is not None
        if empty_response:
            raise ValueError(
                "Foundry returned an empty completion after "
                f"{max_attempts} attempt(s); the deployment produced no "
                "content for this request"
            )
        raise ValueError(
            f"Foundry response could not be parsed as JSON after {max_attempts} "
            f"attempt(s); line={last_error.lineno}; column={last_error.colno}"
        )

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
        response = _call_with_transport_retry(
            lambda: self._client.embeddings.create(
                model=self._config.embedding_deployment,
                input=texts,
                dimensions=self._config.embedding_dimensions,
            )
        )
        return [item.embedding for item in response.data]
def _azure_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert generated schemas to Azure structured-output subset."""
    normalized = json.loads(json.dumps(schema))
    property_count = 0
    unsupported_constraints = {
        "contains",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "patternProperties",
        "uniqueItems",
    }

    def visit(value: Any, object_depth: int = 0) -> None:
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
                object_depth += 1
                if object_depth > 5:
                    raise ValueError(
                        "Azure strict schema exceeds nesting limit"
                    )
                property_count += len(properties)
                value["additionalProperties"] = False
                value["required"] = list(properties)
            if "oneOf" in value:
                value["anyOf"] = value.pop("oneOf")
            if "allOf" in value:
                raise ValueError(
                    "Azure strict schema cannot contain allOf"
                )
            for key, child in value.items():
                visit(child, 0 if key == "$defs" else object_depth)
        elif isinstance(value, list):
            for child in value:
                visit(child, object_depth)

    if normalized.get("type") != "object" or "anyOf" in normalized:
        raise ValueError("Azure strict schema root must be one object")
    visit(normalized)
    if property_count > 100:
        raise ValueError(
            "Azure strict schema exceeds the 100-property limit"
        )
    return normalized
