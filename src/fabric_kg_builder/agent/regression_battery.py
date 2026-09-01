"""agent/regression_battery.py — repeat-N test-case regression battery.

Runs each declared ``.foundry/agent-metadata.yaml`` ``testCase`` N times
against a (live or fake, for tests) agent client and classifies the result:

  - "pass"    — N/N attempts satisfied expectations
  - "fail"    — 0/N attempts satisfied expectations
  - "flaky"   — neither 0 nor N (some pass, some fail)

Rationale (issue #138): three rounds of prompt-only enforcement of the
"unsupported-gate" checklist (v1.8 -> v1.10) reduced but did not close a
routing regression where the Knowledge Base fallback tool is inconsistently
skipped. Manual re-testing of an *identical, unchanged* query found only
1 pass out of 5 fresh reruns — proof that a single successful transcript is
NOT sound evidence that a given query/behavior is fixed. This module always
repeats each test case (default 3x, overridable per-case) and NEVER averages
a flaky result into a misleading pass — flaky is a first-class, loudly
reported classification, not hidden inside a majority vote.

Gate policy:
  - ``required=True`` (the ``TestCase`` default) test cases MUST classify as
    "pass" (N/N) or the battery is a gate failure — callers should block the
    deploy.
  - ``required=False`` test cases (e.g. a known-flaky canary kept in the
    suite intentionally to track a known open bug) are recorded and reported
    but never block the deploy on their own.

This module performs grading/classification only; it does not decide
whether to abort a deployment — see ``enforce_battery_gate`` and its use in
``deployer.deploy_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fabric_kg_builder.agent.metadata import TestCase

DEFAULT_REPEAT = 3


class RegressionBatteryError(Exception):
    """Raised when one or more REQUIRED test cases fail the repeat-N gate."""


@dataclass
class AttemptResult:
    """The graded outcome of a single invocation of one test case."""

    attempt_index: int
    passed: bool
    route_type: str | None
    tool_types: list[str]
    answer_text: str
    failure_reason: str = ""


@dataclass
class TestCaseBatteryResult:
    """All attempts for one test case, plus its pass/fail/flaky classification."""

    test_case_id: str
    required: bool
    repeat: int
    attempts: list[AttemptResult] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for a in self.attempts if a.passed)

    @property
    def classification(self) -> str:
        total = len(self.attempts)
        if total == 0:
            return "skipped"
        passed = self.pass_count
        if passed == total:
            return "pass"
        if passed == 0:
            return "fail"
        return "flaky"

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_case_id": self.test_case_id,
            "required": self.required,
            "repeat": self.repeat,
            "pass_count": self.pass_count,
            "total": len(self.attempts),
            "classification": self.classification,
        }


def _parse_route_type(answer_text: str) -> str | None:
    """Extract the ``route_type: <value>`` line the agent is instructed to emit."""
    for line in answer_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("route_type:"):
            return stripped.split(":", 1)[1].strip().lower()
    return None


def _extract_tool_types(invoke_response: dict[str, Any]) -> list[str]:
    """Best-effort tool-call trace from an ``invoke()`` response.

    Only populated when the transport opts in via a ``tool_types`` key
    (``SDKAgentTransport`` records ``response.output`` item types there).
    An empty list means the transport does not expose trace data, in which
    case grading falls back to route_type/citation-text checks only — it
    never fails a case purely for missing trace data.
    """
    raw = invoke_response.get("tool_types")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


def _grade_attempt(
    test_case: TestCase, invoke_response: dict[str, Any], attempt_index: int
) -> AttemptResult:
    answer_text = str(
        invoke_response.get("answer")
        or invoke_response.get("output_text")
        or invoke_response.get("content")
        or ""
    )
    tool_types = _extract_tool_types(invoke_response)
    route_type = _parse_route_type(answer_text)

    reasons: list[str] = []
    if test_case.expectedRouteType and route_type != test_case.expectedRouteType.strip().lower():
        reasons.append(
            f"expected route_type={test_case.expectedRouteType!r}, got {route_type!r}"
        )
    if test_case.requiredRefusal and route_type != "unsupported":
        reasons.append(
            f"expected refusal (route_type=unsupported), got {route_type!r}"
        )
    for field_name in test_case.requiredCitationFields:
        marker = f"source_type={field_name}"
        if marker not in answer_text:
            reasons.append(f"missing required citation field {field_name!r}")

    return AttemptResult(
        attempt_index=attempt_index,
        passed=not reasons,
        route_type=route_type,
        tool_types=tool_types,
        answer_text=answer_text,
        failure_reason="; ".join(reasons),
    )


def run_test_case_battery(
    client: Any,
    agent_name: str,
    test_cases: list[TestCase],
    *,
    default_repeat: int = DEFAULT_REPEAT,
) -> list[TestCaseBatteryResult]:
    """Run every declared test case ``repeat`` times and classify the result.

    Args:
        client: object exposing ``invoke(agent_name, prompt) -> dict``
            (``FoundryAgentClient`` or any test double with the same shape).
        agent_name: the deployed agent's name.
        test_cases: declared ``.foundry/agent-metadata.yaml`` testCases.
        default_repeat: repeat count used when a test case does not declare
            its own ``repeat`` override.

    Returns:
        One ``TestCaseBatteryResult`` per test case, in the given order.
        Grading/classification only — does not raise on fail/flaky; see
        ``enforce_battery_gate`` for that.
    """
    results: list[TestCaseBatteryResult] = []
    for test_case in test_cases:
        repeat = test_case.repeat if test_case.repeat is not None else default_repeat
        battery = TestCaseBatteryResult(
            test_case_id=test_case.id, required=test_case.required, repeat=repeat
        )
        for attempt_index in range(repeat):
            try:
                response = client.invoke(agent_name, test_case.input)
            except Exception as exc:  # transport/network failure counts as a fail
                battery.attempts.append(
                    AttemptResult(
                        attempt_index=attempt_index,
                        passed=False,
                        route_type=None,
                        tool_types=[],
                        answer_text="",
                        failure_reason=f"invoke raised: {exc}",
                    )
                )
                continue
            battery.attempts.append(_grade_attempt(test_case, response, attempt_index))
        results.append(battery)
    return results


def enforce_battery_gate(results: list[TestCaseBatteryResult]) -> None:
    """Raise ``RegressionBatteryError`` if any REQUIRED test case is not N/N.

    Non-required cases (known-flaky canaries) never block the gate — their
    classification remains in ``results`` for reporting regardless of outcome.
    """
    failing = [r for r in results if r.required and r.classification != "pass"]
    if not failing:
        return
    lines = [
        f"  - {r.test_case_id}: {r.classification} ({r.pass_count}/{len(r.attempts)} passed)"
        for r in failing
    ]
    raise RegressionBatteryError(
        "Required regression test case(s) failed the repeat-N gate "
        "(non-required/known-flaky cases are reported separately and never "
        "gate the deploy):\n" + "\n".join(lines)
    )
