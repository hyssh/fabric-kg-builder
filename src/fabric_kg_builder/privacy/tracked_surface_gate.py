"""
Production privacy-surface gate for CI and packaging checks.

Scans all git-tracked repository surfaces for structural markers of
diagnostic, customer-derived, or private data. Designed to be called from
CI, packaging pipelines, and test suites.

Fail-closed policy
------------------
``PrivacyGateError`` is raised (never silently swallowed) for any condition
that would prevent a reliable scan:

* ``git`` not found, command failure, or timeout;
* malformed or absolute path in git output;
* ``..`` path-traversal component in git output;
* resolved path outside the repository root (symlink or absolute injection);
* file read errors.

Forbidden marker policy
-----------------------
All patterns are structural and contextual — they identify field names,
path fragments, and discriminator keys that should never appear in tracked
fixtures or package source.  No value-length heuristics are used.

Exemption policy
----------------
Only three test/implementation files carry narrow, explicit, full-file
exemptions. Each
exemption is audited below:

* ``src/fabric_kg_builder/privacy/tracked_surface_gate.py``
  (this file) — defines forbidden-pattern regex source as string literals;
  scanning would produce false positives for the pattern definitions
  themselves.
* ``tests/unit/test_spec008a_privacy_boundary.py`` — contains inline
  synthetic strings for the positive-detection tests that prove each
  pattern fires; these strings intentionally resemble forbidden content.
* ``tests/unit/test_spec008a_production_proof.py`` — contains additional
  synthetic production-path privacy probes.

No broad directory exclusions are used (e.g. no blanket ``tests/unit/``
exclusion).

The existing ``.squad/decisions.md`` coordination ledger has a narrower
single-pattern exemption for historical local development paths only. Other
privacy marker classes in that file remain scanned.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Repository root (resolved once at import time)
# ---------------------------------------------------------------------------
# File location: src/fabric_kg_builder/privacy/tracked_surface_gate.py
# parents[0] = privacy/   parents[1] = fabric_kg_builder/
# parents[2] = src/        parents[3] = <repo root>
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class PrivacyGateError(Exception):
    """Fatal privacy-gate failure: scan cannot proceed reliably.

    Raised when a condition is detected that would allow an incomplete or
    unreliable scan to be mistaken for a clean result.  Callers MUST treat
    this as a blocking failure, not as a skippable warning.
    """


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One detected forbidden marker occurrence in a tracked file."""

    rel_path: str        # POSIX repo-relative path
    line: int            # 1-based line number
    pattern_name: str    # key from FORBIDDEN_MARKERS
    excerpt: str         # repr() of first 60 chars of the match


# ---------------------------------------------------------------------------
# Forbidden markers
# ---------------------------------------------------------------------------
# All patterns are structural/contextual — no value-length heuristics.
# Each entry identifies a class of diagnostic, customer-derived, or private
# data risk.

FORBIDDEN_MARKERS: Final[dict[str, re.Pattern[str]]] = {
    # Windows user home path fragment (e.g. C:\Users\alice.smith\)
    "local_win_user_path": re.compile(
        r"C:\\Users\\[A-Za-z][A-Za-z0-9._-]{1,64}\\",
        re.IGNORECASE,
    ),
    # Unix user home path fragment (e.g. /home/bobsmith/)
    "local_unix_home_path": re.compile(
        r"/home/[a-z][a-z0-9._-]{1,32}/",
    ),
    # Local Downloads directory fragment (path to a diagnostic export file)
    "local_downloads_path": re.compile(
        r"[Dd]ownloads[/\\][A-Za-z0-9_.-]{5,}",
    ),
    # Fabric diagnostic-export root JSON key
    "fabric_agent_diagnostic_export": re.compile(
        r'"agentDiagnosticExport"\s*:',
        re.IGNORECASE,
    ),
    # Fabric conversation-history array key (live diagnostic JSON structure)
    "fabric_conversation_history": re.compile(
        r'"conversationHistory"\s*:\s*\[',
        re.IGNORECASE,
    ),
    # Fabric diagnosticType discriminator with a non-synthetic value
    # (negative lookahead exempts test_, synthetic_, placeholder_ prefixes)
    "fabric_diagnostic_type": re.compile(
        r'"diagnosticType"\s*:\s*"(?!test_|synthetic_|placeholder_)',
        re.IGNORECASE,
    ),
    # Customer query field name presence (structural — no value-length heuristic)
    "customer_query_field": re.compile(
        r'"(?:userQuery|customerQuestion|user_question|naturalLanguageQuery)"\s*:',
        re.IGNORECASE,
    ),
    # Agent or bot response field name presence (structural — no value-length heuristic)
    "agent_response_field": re.compile(
        r'"(?:agentResponse|agent_answer|botResponse)"\s*:',
        re.IGNORECASE,
    ),
}


# ---------------------------------------------------------------------------
# Narrow file-scoped exemptions (auditable)
# ---------------------------------------------------------------------------
# Repo-relative POSIX paths.  Justification for each entry is in the module
# docstring.  This set MUST remain minimal; any addition requires explicit
# justification in code review.

SELF_EXEMPT_FILES: Final[frozenset[str]] = frozenset({
    "src/fabric_kg_builder/privacy/tracked_surface_gate.py",
    "tests/unit/test_spec008a_privacy_boundary.py",
    "tests/unit/test_spec008a_production_proof.py",
})

PATTERN_EXEMPTIONS: Final[dict[str, frozenset[str]]] = {
    ".squad/decisions.md": frozenset({"local_win_user_path"}),
}


# ---------------------------------------------------------------------------
# Default scan prefixes for run_full_gate
# ---------------------------------------------------------------------------
# All git-tracked files under these prefixes are scanned (including .py).
# SELF_EXEMPT_FILES exemptions apply within each prefix.

_DEFAULT_SCAN_PREFIXES: Final[tuple[str, ...]] = (
    "src/",
    "tests/",
    "docs/",
    "ontology/",
    "scripts/",
)

# Binary file extensions that cannot contain text-pattern violations.
# Scanning these is both unnecessary and error-prone (decode errors).
_BINARY_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".pyc", ".pyo", ".pyd",
    ".so", ".dll", ".dylib", ".lib", ".a",
    ".exe", ".bin",
    ".whl", ".egg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".bmp", ".webp",
    ".pdf",
    ".db", ".sqlite", ".sqlite3",
    ".pkl", ".pickle",
    ".lock",  # dependency lock files (uv.lock etc.) — no user data, binary-ish
})

_WORKING_SCAN_ROOTS: Final[tuple[str, ...]] = (
    "src",
    "tests",
    "docs",
    "ontology",
    "scripts",
)
_PACKAGE_METADATA_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "LICENSE.txt",
)


# ---------------------------------------------------------------------------
# Internal path validation helper
# ---------------------------------------------------------------------------


def _assert_within_root(path: Path, resolved: Path, effective_root: Path) -> None:
    """Raise ``PrivacyGateError`` if *resolved* is not under *effective_root*."""
    if not resolved.is_relative_to(effective_root):
        raise PrivacyGateError(
            f"Path escapes repository root: {str(path)!r} resolves to "
            f"{str(resolved)!r} (root: {str(effective_root)!r})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_forbidden_in_text(text: str) -> list[tuple[str, str]]:
    """Scan *text* for all forbidden markers and return findings.

    Returns a list of ``(pattern_name, excerpt)`` pairs — one entry per
    match, with *excerpt* being the first 60 characters of the matched text.

    This function performs no file I/O and is safe to call from tests with
    inline synthetic strings to prove each pattern class fires.
    """
    results: list[tuple[str, str]] = []
    for name, pattern in FORBIDDEN_MARKERS.items():
        for match in pattern.finditer(text):
            results.append((name, match.group()[:60]))
    return results


def _scan_text(text: str, rel_posix: str) -> list[Violation]:
    if rel_posix in SELF_EXEMPT_FILES:
        return []
    violations: list[Violation] = []
    exempt_patterns = PATTERN_EXEMPTIONS.get(rel_posix, frozenset())
    for name, pattern in FORBIDDEN_MARKERS.items():
        if name in exempt_patterns:
            continue
        for match in pattern.finditer(text):
            line_num = text[: match.start()].count("\n") + 1
            violations.append(
                Violation(rel_posix, line_num, name, repr(match.group()[:60]))
            )
    return violations


def tracked_surface_files(
    prefix: str,
    repo_root: Path | None = None,
    *,
    _git_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[Path]:
    """Return git-tracked (committed or staged) files under *prefix*.

    Uses ``git ls-files`` so only committed/staged content is enumerated.

    Raises ``PrivacyGateError`` on:

    * ``git`` executable not found (``FileNotFoundError``);
    * non-zero exit or ``CalledProcessError``;
    * process timeout (``TimeoutExpired``);
    * absolute path in git output (malformed);
    * ``..`` path-traversal component in git output (malformed);
    * resolved path that escapes the repository root.

    Parameters
    ----------
    prefix:
        Repo-relative path prefix passed to ``git ls-files``.
    repo_root:
        Override for repository root.  Defaults to the auto-detected root.
    _git_runner:
        Injection point for unit tests.  Must accept the same call
        signature as ``subprocess.run`` and return a
        ``CompletedProcess[str]``.
    """
    effective_root = (repo_root or _REPO_ROOT).resolve()
    runner = _git_runner or subprocess.run

    try:
        result: subprocess.CompletedProcess[str] = runner(
            ["git", "ls-files", "--", prefix],
            cwd=str(effective_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise PrivacyGateError(
            f"git not found; cannot enumerate tracked files for prefix {prefix!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PrivacyGateError(
            f"git ls-files failed for prefix {prefix!r}: exit {exc.returncode}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PrivacyGateError(
            f"git ls-files timed out for prefix {prefix!r}"
        ) from exc

    if result.stdout is None:
        raise PrivacyGateError(
            f"git ls-files returned no readable output for prefix {prefix!r}"
        )
    paths: list[Path] = []
    for raw_line in result.stdout.splitlines():
        rel = raw_line.strip()
        if not rel:
            continue

        rel_path = Path(rel)

        # Absolute paths in git ls-files output are always malformed.
        # Check both platform-native absolute (is_absolute) and Unix-style
        # root-relative paths on Windows (starting with / or \).
        if rel_path.is_absolute() or rel.startswith("/") or rel.startswith("\\"):
            raise PrivacyGateError(
                f"Malformed git output: absolute path in ls-files: {rel!r}"
            )
        # Reject traversal components before any filesystem access.
        if ".." in rel_path.parts:
            raise PrivacyGateError(
                f"Malformed git output: path contains traversal component: {rel!r}"
            )

        candidate = effective_root / rel
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise PrivacyGateError(
                f"Cannot resolve path from git output: {rel!r}: {exc}"
            ) from exc

        _assert_within_root(candidate, resolved, effective_root)
        paths.append(candidate)

    return paths


def scan_file(path: Path, repo_root: Path | None = None) -> list[Violation]:
    """Scan a single file for forbidden markers.

    Files listed in ``SELF_EXEMPT_FILES`` are skipped and return ``[]``.

    Raises ``PrivacyGateError`` on:

    * *path* resolving outside the repository root;
    * any OS-level read error (permissions, missing file, etc.).

    Parameters
    ----------
    path:
        Absolute or repo-relative path to scan.
    repo_root:
        Override for repository root.  Defaults to the auto-detected root.
    """
    effective_root = (repo_root or _REPO_ROOT).resolve()

    try:
        resolved = path.resolve()
    except OSError as exc:
        raise PrivacyGateError(f"Cannot resolve path: {str(path)!r}: {exc}") from exc

    _assert_within_root(path, resolved, effective_root)

    try:
        rel_posix = resolved.relative_to(effective_root).as_posix()
    except ValueError as exc:
        raise PrivacyGateError(
            f"Cannot compute repo-relative path for: {str(path)!r}"
        ) from exc

    if rel_posix in SELF_EXEMPT_FILES:
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PrivacyGateError(f"Cannot read file: {str(path)!r}: {exc}") from exc

    return _scan_text(text, rel_posix)


def run_full_gate(
    repo_root: Path | None = None,
    *,
    extra_prefixes: tuple[str, ...] = (),
    _git_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[Violation]:
    """Run a comprehensive privacy surface scan of all tracked repository files.

    Scans the Git index for every tracked text file, including staged content.
    When a working-tree file differs from the index, ``git show :<path>`` is
    used so clean working bytes cannot hide staged private content. It then
    scans the complete working source/test/docs/package roots, including
    untracked files that setuptools may package.

    This replaces the prior prefix allowlist approach (``_DEFAULT_SCAN_PREFIXES``)
    with full-repo enumeration, ensuring that root-level files
    (README.md, fabric-kg.yaml, pyproject.toml, .env.example, chainlit.md, etc.),
    fixture/, data/, example/, config/, app/, and infra/ directories are all
    covered without requiring explicit enumeration.

    The ``extra_prefixes`` parameter is retained for backward compatibility;
    when provided, files under those prefixes are scanned in addition to the
    full-repo scan (deduplication by path is applied).

    Raises ``PrivacyGateError`` on any fail-closed condition (git failure,
    path escape, read error).  Fails closed on all errors — never silently
    swallows git/read/path/symlink errors.

    Returns an empty list when no violations are found.
    """
    effective_root = (repo_root or _REPO_ROOT).resolve()
    all_violations: list[Violation] = []
    scanned_working: set[Path] = set()
    runner = _git_runner or subprocess.run

    def _run_git(args: list[str], *, operation: str) -> str:
        try:
            result: subprocess.CompletedProcess[str] = runner(
                args,
                cwd=str(effective_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=15,
            )
        except FileNotFoundError as exc:
            raise PrivacyGateError(
                f"git not found; cannot {operation}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise PrivacyGateError(
                f"git command failed while attempting to {operation}: "
                f"exit {exc.returncode}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PrivacyGateError(
                f"git command timed out while attempting to {operation}"
            ) from exc
        if result.stdout is None:
            raise PrivacyGateError(
                f"git returned no readable output while attempting to {operation}"
            )
        return result.stdout

    # Enumerate all tracked files from the repo root
    tracked = tracked_surface_files(".", repo_root, _git_runner=_git_runner)
    unstaged_output = _run_git(
        ["git", "diff", "--name-only", "--", "."],
        operation="enumerate unstaged paths",
    )
    unstaged_paths = {
        line.strip().replace("\\", "/")
        for line in unstaged_output.splitlines()
        if line.strip()
    }

    for file_path in tracked:
        try:
            rel_posix = file_path.resolve().relative_to(effective_root).as_posix()
        except (ValueError, OSError) as exc:
            raise PrivacyGateError(
                f"Cannot resolve tracked privacy surface: {str(file_path)!r}"
            ) from exc

        if file_path.suffix.lower() in _BINARY_EXTENSIONS:
            continue
        if rel_posix in unstaged_paths or not file_path.is_file():
            index_text = _run_git(
                ["git", "show", f":{rel_posix}"],
                operation=f"read index blob {rel_posix!r}",
            )
            all_violations.extend(_scan_text(index_text, rel_posix))
        else:
            scanned_working.add(file_path.resolve())
            all_violations.extend(scan_file(file_path, repo_root))

    working_roots = tuple(dict.fromkeys(
        _WORKING_SCAN_ROOTS + tuple(extra_prefixes)
    ))
    for relative_root in working_roots:
        scan_root = effective_root / relative_root
        if not scan_root.exists():
            continue
        for file_path in scan_root.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            resolved = file_path.resolve()
            if resolved in scanned_working:
                continue
            scanned_working.add(resolved)
            all_violations.extend(scan_file(file_path, repo_root))

    for relative_path in _PACKAGE_METADATA_FILES:
        file_path = effective_root / relative_path
        if (
            file_path.is_file()
            and file_path.suffix.lower() not in _BINARY_EXTENSIONS
            and file_path.resolve() not in scanned_working
        ):
            scanned_working.add(file_path.resolve())
            all_violations.extend(scan_file(file_path, repo_root))

    return sorted(
        set(all_violations),
        key=lambda item: (
            item.rel_path,
            item.line,
            item.pattern_name,
            item.excerpt,
        ),
    )
