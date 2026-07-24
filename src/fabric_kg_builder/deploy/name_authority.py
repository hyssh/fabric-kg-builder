"""Name resolution authority for Fabric item display names.

The deployment manifest (:class:`~fabric_kg_builder.deploy.manifest.DeploymentManifest`)
is the **single** naming authority.  All standalone and orchestrated commands
resolve names through :func:`resolve_item_name`.  Conflicting names from
generated metadata or command flags raise :class:`NameAuthorityConflict`.

Issue #6 / ADR: keyser-deployment-manifest.md.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Literal

from .manifest import DeploymentManifest, ManifestItemSpec


# ---------------------------------------------------------------------------
# Item type ↔ manifest field mapping
# ---------------------------------------------------------------------------

# Maps both CamelCase (public API used internally) and snake_case (accepted
# from callers / tests) to the manifest field name.
_ITEM_TYPE_FIELD: dict[str, str] = {
    # CamelCase (used in error messages and render output)
    "Ontology": "ontology",
    "Lakehouse": "lakehouse",
    "SemanticModel": "semantic_model",
    "GraphModel": "graph_model",
    "DataAgent": "data_agent",
    "SearchIndex": "search_index",
    # snake_case (accepted from callers / tests)
    "ontology": "ontology",
    "lakehouse": "lakehouse",
    "semantic_model": "semantic_model",
    "graph_model": "graph_model",
    "data_agent": "data_agent",
    "search_index": "search_index",
}

# Canonical CamelCase display form for each field.
_FIELD_DISPLAY_TYPE: dict[str, str] = {
    "ontology": "Ontology",
    "lakehouse": "Lakehouse",
    "semantic_model": "SemanticModel",
    "graph_model": "GraphModel",
    "data_agent": "DataAgent",
    "search_index": "SearchIndex",
}

# Field names that require Fabric identifier validation (no spaces/hyphens).
_IDENTIFIER_FIELDS = {"ontology", "lakehouse", "semantic_model"}
# Field names that require graph model name validation.
_GRAPH_MODEL_FIELDS = {"graph_model"}


def _item_spec(manifest: DeploymentManifest, item_type: str) -> ManifestItemSpec:
    """Return the ``ManifestItemSpec`` for *item_type*.

    Accepts both CamelCase (``"Ontology"``) and snake_case (``"ontology"``).
    """
    field = _ITEM_TYPE_FIELD.get(item_type)
    if field is None:
        raise ValueError(
            f"Unknown item type '{item_type}'. "
            f"Expected one of: {', '.join(sorted(set(_ITEM_TYPE_FIELD.values())))}"
        )
    return getattr(manifest.items, field)


# ---------------------------------------------------------------------------
# Resolved name
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedName:
    """Name resolved from the manifest — the authoritative Fabric item name.

    Attributes:
        item_type:          Human-readable item type (e.g. ``"Ontology"``).
        display_name:       The manifest-authoritative display name.
        authority:          Source path of the manifest (e.g. ``"deployment.yaml"``).
        generated_metadata: ``"compatible"`` | ``"conflict"`` | ``"absent"``.
    """

    item_type: str
    display_name: str
    authority: str
    generated_metadata: Literal["compatible", "conflict", "absent"]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class NameAuthorityConflict(Exception):
    """A generated-metadata or command-supplied name conflicts with the manifest.

    Error code: ``NAME_AUTHORITY_CONFLICT``.

    The ``str()`` representation matches the project's established
    ``ERROR <CODE>:\\n<message>\\n<remediation>`` format.
    """

    code: str = "NAME_AUTHORITY_CONFLICT"

    def __init__(
        self,
        item_type: str,
        manifest_name: str,
        conflicting_name: str,
        source: str,
    ) -> None:
        self.item_type = item_type
        self.manifest_name = manifest_name
        self.conflicting_name = conflicting_name
        self.source = source  # "generated_metadata" | "command" | "read_back"

        # Resolve canonical field name for the remediation message.
        field = _ITEM_TYPE_FIELD.get(item_type, item_type.lower().replace(" ", "_"))
        source_label = {
            "generated_metadata": "Generated display name",
            "command": "Command-supplied name",
            "read_back": "Deployed display name",
        }.get(source, "Supplied name")

        remediation = (
            f"Update deployment.yaml items.{field}.display_name "
            f"or remove the conflicting source; names must be defined once "
            f"in the manifest."
        )
        super().__init__(
            f"ERROR NAME_AUTHORITY_CONFLICT:\n"
            f'{source_label} "{conflicting_name}" conflicts with\n'
            f'manifest display name "{manifest_name}".\n'
            f"{remediation}"
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_item_name(
    manifest: DeploymentManifest,
    item_type: str,
    *,
    generated_metadata_name: str | None = None,
    command_name: str | None = None,
) -> ResolvedName:
    """Resolve a Fabric item display name from the manifest (single authority).

    The manifest is always authoritative.  If ``generated_metadata_name`` or
    ``command_name`` is truthy and differs from the manifest name,
    :class:`NameAuthorityConflict` is raised.

    Names are validated with the existing ``infra/names.py`` helpers where
    applicable (Ontology/Lakehouse/SemanticModel use
    :func:`~fabric_kg_builder.infra.names.validate_fabric_identifier_name`;
    GraphModel uses
    :func:`~fabric_kg_builder.infra.names.validate_fabric_graph_model_name`).

    Args:
        manifest:               The loaded deployment manifest.
        item_type:              One of ``"Ontology"``/``"ontology"``,
                                ``"Lakehouse"``/``"lakehouse"``,
                                ``"SemanticModel"``/``"semantic_model"``,
                                ``"GraphModel"``/``"graph_model"``,
                                ``"DataAgent"``/``"data_agent"``,
                                ``"SearchIndex"``/``"search_index"``.
                                Both CamelCase and snake_case are accepted.
        generated_metadata_name: Name from a generated artifact (e.g. a
                                  ``.platform`` ``displayName``, or a semantic
                                  model title).  Checked only when truthy.
        command_name:           Name supplied via a CLI ``--*-name`` flag.
                                Checked only when truthy.

    Returns:
        :class:`ResolvedName` with the manifest-authoritative display name.

    Raises:
        NameAuthorityConflict: When a provided name differs from the manifest.
        ValueError:            When *item_type* is unrecognised.
    """
    field = _ITEM_TYPE_FIELD.get(item_type)
    if field is None:
        raise ValueError(
            f"Unknown item type '{item_type}'. "
            f"Expected one of: {', '.join(sorted(set(_ITEM_TYPE_FIELD.values())))}"
        )
    spec = getattr(manifest.items, field)
    manifest_name = spec.display_name
    authority = manifest._source_path or "deployment.yaml"

    # Canonical display form for error messages and ResolvedName.item_type.
    display_item_type = _FIELD_DISPLAY_TYPE.get(field, item_type)

    # Validate with existing infra/names.py helpers where applicable.
    if manifest_name:
        from fabric_kg_builder.infra.names import (
            validate_fabric_graph_model_name,
            validate_fabric_identifier_name,
        )

        if field in _IDENTIFIER_FIELDS:
            validate_fabric_identifier_name(manifest_name, display_item_type)
        elif field in _GRAPH_MODEL_FIELDS:
            validate_fabric_graph_model_name(manifest_name)

    # Determine generated_metadata status.
    generated_metadata: Literal["compatible", "conflict", "absent"] = "absent"
    if generated_metadata_name:
        if generated_metadata_name == manifest_name:
            generated_metadata = "compatible"
        else:
            generated_metadata = "conflict"
            raise NameAuthorityConflict(
                item_type=display_item_type,
                manifest_name=manifest_name,
                conflicting_name=generated_metadata_name,
                source="generated_metadata",
            )

    # Check command-supplied name.
    if command_name and command_name != manifest_name:
        raise NameAuthorityConflict(
            item_type=display_item_type,
            manifest_name=manifest_name,
            conflicting_name=command_name,
            source="command",
        )

    return ResolvedName(
        item_type=display_item_type,
        display_name=manifest_name,
        authority=authority,
        generated_metadata=generated_metadata,
    )


# ---------------------------------------------------------------------------
# Read-back validation
# ---------------------------------------------------------------------------


def validate_readback_name(
    item_type: str,
    deployed_display_name: str,
    manifest: DeploymentManifest,
) -> None:
    """Verify that a deployed item's display name matches the manifest.

    Args:
        item_type:             One of ``"Ontology"``/``"ontology"``, etc.
        deployed_display_name: The display name returned from the Fabric API.
        manifest:              The loaded deployment manifest.

    Raises:
        NameAuthorityConflict: When the deployed name differs from the manifest.
    """
    field = _ITEM_TYPE_FIELD.get(item_type, "")
    spec = getattr(manifest.items, field, None) if field else None
    manifest_name = spec.display_name if spec else ""
    display_item_type = _FIELD_DISPLAY_TYPE.get(field, item_type)
    if manifest_name and deployed_display_name and deployed_display_name != manifest_name:
        raise NameAuthorityConflict(
            item_type=display_item_type,
            manifest_name=manifest_name,
            conflicting_name=deployed_display_name,
            source="read_back",
        )


# ---------------------------------------------------------------------------
# Dry-run rendering
# ---------------------------------------------------------------------------


def render_name_resolution(resolved: ResolvedName) -> str:
    """Render a resolved name block for CLI dry-run output.

    Output format (matches Issue #6 success format exactly)::

        Resolved item:
          type: Ontology
          display name: demo-ontology
          name authority: deployment.yaml
          generated metadata: compatible

        No naming conflicts detected.

    Args:
        resolved: The resolved name produced by :func:`resolve_item_name`.

    Returns:
        Multi-line string suitable for :func:`click.echo`.
    """
    lines = [
        "Resolved item:",
        f"  type: {resolved.item_type}",
        f"  display name: {resolved.display_name}",
        f"  name authority: {resolved.authority}",
        f"  generated metadata: {resolved.generated_metadata}",
        "",
        "No naming conflicts detected.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legacy env config → in-memory manifest (migration adapter)
# ---------------------------------------------------------------------------


def manifest_from_env_config(env_config: dict) -> DeploymentManifest:
    """Build an in-memory :class:`DeploymentManifest` from a legacy env JSON.

    Used when ``--manifest`` is not supplied so existing env configs keep
    working.  The returned manifest's ``_source_path`` is set to
    ``"env-config (legacy)"`` to distinguish it from a file-loaded manifest.

    When both ``--manifest`` *and* a legacy env config are present (in the CLI
    callers), the caller is responsible for emitting a migration warning when the
    names differ and letting the manifest win.

    Args:
        env_config: Either the full env JSON dict (with a ``"fabric"`` top-level
                    key) **or** the already-extracted fabric section dict as
                    returned by ``_read_fabric_env_config()``. Both forms are
                    accepted for backward compatibility.

    Returns:
        In-memory :class:`DeploymentManifest` synthesised from the env config.
    """
    from .manifest import ManifestDependency, ManifestItemSpec, ManifestItems

    # Accept both the full env JSON and the already-extracted fabric section.
    if "fabric" in env_config and isinstance(env_config["fabric"], dict):
        fabric = env_config["fabric"]
    else:
        fabric = env_config

    items = ManifestItems(
        ontology=ManifestItemSpec(
            display_name=fabric.get("ontology_display_name") or "",
            configured_id=(fabric.get("ontology_item_id") or "") if (fabric.get("ontology_display_name") or "") else "",
        ),
        lakehouse=ManifestItemSpec(
            display_name=fabric.get("lakehouse_display_name") or "",
            configured_id=(fabric.get("lakehouse_item_id") or "") if (fabric.get("lakehouse_display_name") or "") else "",
        ),
        semantic_model=ManifestItemSpec(
            display_name="",
            configured_id="",
        ),
        graph_model=ManifestItemSpec(
            display_name=fabric.get("graph_model_display_name") or "",
            configured_id=(fabric.get("graph_model_id") or fabric.get("graph_model_item_id") or "") if (fabric.get("graph_model_display_name") or "") else "",
        ),
        data_agent=ManifestItemSpec(
            display_name=fabric.get("data_agent_display_name") or "",
            configured_id=(fabric.get("data_agent_item_id") or "") if (fabric.get("data_agent_display_name") or "") else "",
        ),
        search_index=ManifestItemSpec(
            display_name="",
            configured_id="",
        ),
    )
    manifest = DeploymentManifest(
        workspace=fabric.get("workspace_id") or "",
        items=items,
        dependencies=[
            ManifestDependency(item="data_agent", depends_on=["ontology", "graph_model"]),
            ManifestDependency(item="graph_model", depends_on=["ontology"]),
        ],
    )
    manifest._source_path = "env-config (legacy)"
    return manifest
