"""Subprocess command runner abstraction.

Provides a ``CommandRunner`` protocol and two implementations:
- ``RealCommandRunner``: invokes real subprocess commands (used by apply/preflight).
- ``FakeCommandRunner``: returns scripted results for deterministic unit tests.

Commands MUST be passed as argument arrays (never shell=True) per SPEC-006 §6.1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Structured output from a single command execution."""
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    def raise_for_status(self, error_prefix: str = "") -> None:
        """Raise ``CommandError`` when returncode is non-zero."""
        if not self.succeeded:
            prefix = f"{error_prefix}: " if error_prefix else ""
            raise CommandError(
                f"{prefix}Command {self.args[0]!r} exited with code "
                f"{self.returncode}.\nstdout: {self.stdout}\nstderr: {self.stderr}",
                result=self,
            )


class CommandError(RuntimeError):
    """Raised when a command returns a non-zero exit code."""
    def __init__(self, message: str, result: CommandResult) -> None:
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CommandRunner(Protocol):
    """Run external commands without shell=True."""

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: int = 120,
    ) -> CommandResult:
        """Execute *args* and return a ``CommandResult``."""
        ...


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------


class RealCommandRunner:
    """Invoke real subprocesses.  Never uses shell=True.

    Live commands must only be called from ``infra apply`` or opted-in smoke
    tests; unit tests must use FakeCommandRunner.
    """

    @staticmethod
    def _resolve_command(args: list[str]) -> list[str]:
        """Resolve Windows command shims before invoking them without a shell."""
        if os.name != "nt" or not args or os.path.splitext(args[0])[1]:
            return args
        resolved = shutil.which(args[0])
        if not resolved:
            return args
        return [resolved, *args[1:]]

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: int = 120,
    ) -> CommandResult:
        if not args:
            raise ValueError("Command args must not be empty.")
        resolved_args = self._resolve_command(args)
        try:
            proc = subprocess.run(
                resolved_args,
                capture_output=True,
                text=True,
                cwd=cwd,
                env=env,
                input=input,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                f"Command not found: {args[0]!r}. "
                "Ensure Azure CLI (az) and Azure Developer CLI (azd) are installed.",
                result=CommandResult(
                    args=args, returncode=127, stdout="", stderr=str(exc)
                ),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                f"Command {args[0]!r} timed out after {timeout}s.",
                result=CommandResult(
                    args=args, returncode=-1, stdout="", stderr=str(exc)
                ),
            ) from exc
        return CommandResult(
            args=list(args),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


# ---------------------------------------------------------------------------
# Fake implementation for unit tests
# ---------------------------------------------------------------------------


@dataclass
class FakeCommandRunner:
    """Scripted command runner for deterministic unit tests.

    Configure expected responses via ``responses``:

        runner = FakeCommandRunner()
        runner.add_response(["az", "account", "show"], stdout='{"id":"sub-1"}')
        result = runner.run(["az", "account", "show"])
        assert result.stdout == '{"id":"sub-1"}'

    If the exact args are not found the runner raises ``AssertionError`` to
    prevent silent pass on unexpected calls.
    """

    responses: list[tuple[list[str], CommandResult]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    strict: bool = True

    def add_response(
        self,
        args_prefix: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> "FakeCommandRunner":
        """Register a scripted response for commands starting with *args_prefix*."""
        result = CommandResult(
            args=args_prefix,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        self.responses.append((args_prefix, result))
        return self

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: int = 120,
    ) -> CommandResult:
        self.calls.append(list(args))
        for prefix, result in self.responses:
            if args[: len(prefix)] == prefix:
                return CommandResult(
                    args=list(args),
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
        if self.strict:
            raise AssertionError(
                f"FakeCommandRunner has no response for args: {args!r}.\n"
                f"Registered prefixes: {[r[0] for r in self.responses]}"
            )
        return CommandResult(args=list(args), returncode=0, stdout="", stderr="")
