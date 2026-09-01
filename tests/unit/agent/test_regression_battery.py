"""tests/unit/agent/test_regression_battery.py

Regression coverage for agent/regression_battery.py (issue #138).

Context: prompt-only enforcement of the "unsupported-gate" checklist was
verified, across three shipped instruction versions, to be non-deterministic
per-query — one query observed only 1/5 passes across identical fresh
reruns. A single successful transcript is therefore not sound evidence a
case is fixed. These tests assert the battery:
  - repeats each test case N times (never grades on a single run),
  - classifies pass (N/N) / fail (0/N) / flaky (neither) — and NEVER
    collapses flaky into a majority-vote pass,
  - only gates the deploy on REQUIRED cases; non-required (known-flaky
    canary) cases are recorded but never block.
"""

from __future__ import annotations

from typing import Any

import pytest

from fabric_kg_builder.agent.metadata import TestCase as AgentTestCase
from fabric_kg_builder.agent.regression_battery import (
    RegressionBatteryError,
    enforce_battery_gate,
    run_test_case_battery,
)


class _ScriptedClient:
    """Test double: returns a scripted sequence of invoke() responses.

    Each call to `invoke` pops the next response for that test-case id (by
    insertion order); raises if the script runs out, so tests fail loudly on
    a wiring mistake rather than silently reusing a stale response.
    """

    def __init__(self, scripts: dict[str, list[dict[str, Any]]]) -> None:
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.invoke_calls: list[str] = []

    def invoke(self, agent_name: str, prompt: str) -> dict[str, Any]:
        # Find which test-case script this prompt belongs to by prompt text.
        for _key, responses in self._scripts.items():
            if prompt == _key:
                self.invoke_calls.append(prompt)
                if not responses:
                    raise AssertionError(f"script exhausted for prompt {prompt!r}")
                return responses.pop(0)
        raise AssertionError(f"no script registered for prompt {prompt!r}")


def _resp(*, route_type: str, citations: list[str] | None = None) -> dict[str, Any]:
    text = f"route_type: {route_type}\n\nAnswer text.\n"
    for c in citations or []:
        text += f"\nCITATIONS:\n[citation] source_type={c} source_id=1 chunk_id=0\n"
    return {"answer": text, "output_text": text, "status": "completed"}


def test_all_pass_classifies_as_pass() -> None:
    tc = AgentTestCase(id="tc1", input="q1", expectedRouteType="ontology")
    client = _ScriptedClient({"q1": [_resp(route_type="ontology")] * 3})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=3)

    assert len(results) == 1
    assert results[0].classification == "pass"
    assert results[0].pass_count == 3
    enforce_battery_gate(results)  # must not raise


def test_all_fail_classifies_as_fail_and_gates_when_required() -> None:
    tc = AgentTestCase(id="tc1", input="q1", expectedRouteType="ontology")
    client = _ScriptedClient({"q1": [_resp(route_type="unsupported")] * 3})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=3)

    assert results[0].classification == "fail"
    with pytest.raises(RegressionBatteryError, match="tc1"):
        enforce_battery_gate(results)


def test_mixed_results_classify_as_flaky_never_averaged_to_pass() -> None:
    """The core issue #138 regression: a case that sometimes passes must
    NEVER be reported as a clean pass just because most attempts succeeded."""
    tc = AgentTestCase(id="surflink-screw", input="q1", expectedRouteType="mixed")
    # 1 pass, 4 fails — mirrors the real 1/5 pass rate observed live.
    responses = [_resp(route_type="mixed")] + [_resp(route_type="unsupported")] * 4
    client = _ScriptedClient({"q1": responses})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=5)

    assert results[0].classification == "flaky"
    assert results[0].pass_count == 1
    assert len(results[0].attempts) == 5
    # Flaky must still gate when required=True (the default) — it is NOT
    # a pass, and must not be silently treated as good enough.
    with pytest.raises(RegressionBatteryError, match="flaky"):
        enforce_battery_gate(results)


def test_non_required_flaky_case_does_not_gate_but_is_recorded() -> None:
    """A known-flaky canary (required=False) must be recorded for visibility
    but must never block the deploy on its own."""
    tc = AgentTestCase(
        id="known-flaky-canary", input="q1", expectedRouteType="mixed", required=False
    )
    responses = [_resp(route_type="mixed")] + [_resp(route_type="unsupported")] * 2
    client = _ScriptedClient({"q1": responses})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=3)

    assert results[0].classification == "flaky"
    assert results[0].required is False
    enforce_battery_gate(results)  # must NOT raise — non-required never gates


def test_per_case_repeat_override_wins_over_default() -> None:
    tc = AgentTestCase(id="tc1", input="q1", expectedRouteType="ontology", repeat=2)
    client = _ScriptedClient({"q1": [_resp(route_type="ontology")] * 2})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=10)

    assert len(results[0].attempts) == 2
    assert client.invoke_calls == ["q1", "q1"]


def test_required_citation_fields_must_all_be_present() -> None:
    tc = AgentTestCase(
        id="tc1",
        input="q1",
        requiredCitationFields=["ontology", "search"],
    )
    # Only "ontology" citation present -> missing "search" -> should fail.
    client = _ScriptedClient({"q1": [_resp(route_type="mixed", citations=["ontology"])]})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=1)

    assert results[0].classification == "fail"
    assert "search" in results[0].attempts[0].failure_reason


def test_required_refusal_expects_unsupported_route_type() -> None:
    tc = AgentTestCase(id="tc1", input="q1", requiredRefusal=True)
    client = _ScriptedClient({"q1": [_resp(route_type="unsupported")]})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=1)

    assert results[0].classification == "pass"


def test_invoke_exception_counts_as_a_failed_attempt() -> None:
    class _RaisingClient:
        def invoke(self, agent_name: str, prompt: str) -> dict[str, Any]:
            raise RuntimeError("transport down")

    tc = AgentTestCase(id="tc1", input="q1")
    results = run_test_case_battery(_RaisingClient(), "agent", [tc], default_repeat=2)

    assert results[0].classification == "fail"
    assert all("transport down" in a.failure_reason for a in results[0].attempts)


def test_tool_types_extracted_when_transport_provides_trace() -> None:
    tc = AgentTestCase(id="tc1", input="q1", expectedRouteType="mixed")
    resp = _resp(route_type="mixed")
    resp["tool_types"] = ["fabric_dataagent_preview_call", "mcp_call", "message"]
    client = _ScriptedClient({"q1": [resp]})

    results = run_test_case_battery(client, "agent", [tc], default_repeat=1)

    assert results[0].attempts[0].tool_types == [
        "fabric_dataagent_preview_call",
        "mcp_call",
        "message",
    ]


def test_multiple_test_cases_are_each_graded_independently() -> None:
    tc1 = AgentTestCase(id="tc1", input="q1", expectedRouteType="ontology")
    tc2 = AgentTestCase(id="tc2", input="q2", expectedRouteType="unsupported", required=False)
    client = _ScriptedClient(
        {
            "q1": [_resp(route_type="ontology")] * 3,
            "q2": [_resp(route_type="ontology")] * 3,  # tc2 always "fails" its expectation
        }
    )

    results = run_test_case_battery(client, "agent", [tc1, tc2], default_repeat=3)

    assert results[0].test_case_id == "tc1"
    assert results[0].classification == "pass"
    assert results[1].test_case_id == "tc2"
    assert results[1].classification == "fail"
    # tc2 is required=False, so despite failing, the gate must not raise.
    enforce_battery_gate(results)
