"""YAML manifest loading and validation for infra environments.

Loads infra/environments/<env>.yaml, resolves ${ENV_VAR} references, and
validates with Pydantic.  Secrets are never written to disk.

SPEC-006 §5.1 / INF-001.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from .schema import InfraManifest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_INFRA_DIR = Path("infra")


def default_manifest_path(environment: str) -> Path:
    return _DEFAULT_INFRA_DIR / "environments" / f"{environment}.yaml"


# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env_vars(text: str) -> str:
    """Replace ${VAR_NAME} placeholders with their environment variable values.

    Unresolved placeholders are left intact (Pydantic will validate them).
    """
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is not None:
            return value
        return match.group(0)  # Leave unresolved placeholder intact.

    return _ENV_VAR_PATTERN.sub(replacer, text)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InfraManifestError(Exception):
    """Base error for infra manifest loading failures."""


class InfraManifestParseError(InfraManifestError):
    """Raised when the manifest YAML cannot be parsed."""


class InfraManifestValidationError(InfraManifestError):
    """Raised when the manifest fails Pydantic validation."""


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_manifest(path: Path | str) -> InfraManifest:
    """Load, interpolate, and validate an infra manifest YAML.

    Args:
        path: Path to the infra environment YAML file.

    Returns:
        Validated ``InfraManifest``.

    Raises:
        InfraManifestParseError: On YAML syntax errors.
        InfraManifestValidationError: On Pydantic validation failures.
        InfraManifestError: On I/O errors.
    """
    manifest_path = Path(path)
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InfraManifestError(
            f"Cannot read infra manifest '{manifest_path}': {exc}"
        ) from exc

    interpolated = _resolve_env_vars(raw_text)

    try:
        loaded = yaml.safe_load(interpolated)
    except yaml.MarkedYAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        col = getattr(mark, "column", None)
        loc = f" at line {line + 1}, column {col + 1}" if line is not None else ""
        raise InfraManifestParseError(
            f"YAML syntax error in '{manifest_path}'{loc}: {exc.problem or exc}"
        ) from exc

    if loaded is None:
        raise InfraManifestValidationError(
            f"Infra manifest '{manifest_path}' is empty."
        )
    if not isinstance(loaded, dict):
        raise InfraManifestValidationError(
            f"Infra manifest '{manifest_path}' must be a YAML mapping."
        )

    try:
        return InfraManifest.model_validate(loaded)
    except ValidationError as exc:
        errors = [
            f"  {'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        raise InfraManifestValidationError(
            f"Infra manifest '{manifest_path}' failed validation:\n"
            + "\n".join(errors)
        ) from exc
