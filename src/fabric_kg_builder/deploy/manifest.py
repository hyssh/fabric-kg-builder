"""Deployment manifest — single naming authority for Fabric item display names.

Distinct from ``InfraManifest`` (Azure resource provisioning). This manifest
governs Fabric item identity: display names, prefixes, configured IDs, target
workspace, and item dependencies.

Loaded from ``deployment.yaml`` (passed via ``--manifest`` CLI option). Supports
``${ENV_VAR}`` interpolation throughout the YAML.

Issue #6 / ADR: keyser-deployment-manifest.md.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator


# ---------------------------------------------------------------------------
# ENV-VAR interpolation (mirrors infra/manifest.py pattern)
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env_vars(text: str) -> str:
    """Replace ``${VAR_NAME}`` placeholders with environment variable values.

    Unresolved placeholders are left intact for Pydantic to validate.
    """
    def replacer(match: re.Match) -> str:
        value = os.environ.get(match.group(1))
        return value if value is not None else match.group(0)

    return _ENV_VAR_PATTERN.sub(replacer, text)


# ---------------------------------------------------------------------------
# Exceptions (mirror InfraManifest*Error shape)
# ---------------------------------------------------------------------------


class DeploymentManifestError(Exception):
    """Base error for deployment manifest loading failures."""


class DeploymentManifestParseError(DeploymentManifestError):
    """Raised when the manifest YAML cannot be parsed."""


class DeploymentManifestValidationError(DeploymentManifestError):
    """Raised when the manifest fails Pydantic validation."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ManifestItemSpec(BaseModel):
    """Display name and identity fields for one Fabric item type."""

    display_name: str = ""
    prefix: str = ""
    configured_id: str = ""

    @model_validator(mode="after")
    def require_display_name_when_configured_id_set(self) -> "ManifestItemSpec":
        """display_name is required when configured_id is non-empty."""
        if self.configured_id and not self.display_name:
            raise ValueError(
                "display_name is required when configured_id is set. "
                "An item with a configured ID must have a known display name."
            )
        return self


class ManifestItems(BaseModel):
    """One ``ManifestItemSpec`` per deployable Fabric item type."""

    ontology: ManifestItemSpec = Field(default_factory=ManifestItemSpec)
    lakehouse: ManifestItemSpec = Field(default_factory=ManifestItemSpec)
    semantic_model: ManifestItemSpec = Field(default_factory=ManifestItemSpec)
    graph_model: ManifestItemSpec = Field(default_factory=ManifestItemSpec)
    data_agent: ManifestItemSpec = Field(default_factory=ManifestItemSpec)
    search_index: ManifestItemSpec = Field(default_factory=ManifestItemSpec)


class ManifestDependency(BaseModel):
    """Directed deployment dependency between two Fabric item types."""

    item: str
    depends_on: list[str] = Field(default_factory=list)


class DeploymentManifest(BaseModel):
    """Authoritative deployment manifest for Fabric item display names.

    Distinct from ``InfraManifest`` (Azure resource provisioning). This model
    governs Fabric item identity only.

    ``_source_path`` is a private attribute set by the loader — it is not part
    of the YAML schema and is not validated from input data.
    """

    workspace: str = ""
    items: ManifestItems = Field(default_factory=ManifestItems)
    dependencies: list[ManifestDependency] = Field(default_factory=list)

    # Set by the loader after successful validation; not part of the YAML schema.
    _source_path: str = PrivateAttr(default="")


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_deployment_manifest(path: Path | str) -> DeploymentManifest:
    """Load, interpolate, and validate a deployment manifest YAML.

    Args:
        path: Path to ``deployment.yaml``.

    Returns:
        Validated :class:`DeploymentManifest`.

    Raises:
        DeploymentManifestParseError: On YAML syntax errors.
        DeploymentManifestValidationError: On Pydantic validation failures.
        DeploymentManifestError: On I/O errors.
    """
    manifest_path = Path(path)
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentManifestError(
            f"Cannot read deployment manifest '{manifest_path}': {exc}"
        ) from exc

    interpolated = _resolve_env_vars(raw_text)

    try:
        loaded = yaml.safe_load(interpolated)
    except yaml.MarkedYAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        col = getattr(mark, "column", None)
        loc = f" at line {line + 1}, column {col + 1}" if line is not None else ""
        raise DeploymentManifestParseError(
            f"YAML syntax error in '{manifest_path}'{loc}: {exc.problem or exc}"
        ) from exc

    if loaded is None:
        raise DeploymentManifestValidationError(
            f"Deployment manifest '{manifest_path}' is empty."
        )
    if not isinstance(loaded, dict):
        raise DeploymentManifestValidationError(
            f"Deployment manifest '{manifest_path}' must be a YAML mapping."
        )

    try:
        manifest = DeploymentManifest.model_validate(loaded)
    except ValidationError as exc:
        errors = [
            f"  {'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        raise DeploymentManifestValidationError(
            f"Deployment manifest '{manifest_path}' failed validation:\n"
            + "\n".join(errors)
        ) from exc

    if not manifest.workspace:
        raise DeploymentManifestValidationError(
            f"Deployment manifest '{manifest_path}' requires a non-empty 'workspace' field. "
            "Set it to a Fabric workspace ID or a ${ENV_VAR} placeholder."
        )

    manifest._source_path = str(manifest_path)
    return manifest
