"""Infrastructure plan generation.

Produces a machine-readable ``InfraPlan`` from an ``InfraManifest``:
- Per-resource create/adopt/update/replace actions
- RBAC assignments required
- Prerequisites (provider registrations, capacity, etc.)
- Cost-bearing SKUs
- Warnings

SPEC-006 §4.1 / INF-012.
"""

from __future__ import annotations

import json
from pathlib import Path

from .names import resolve_resource_name
from .schema import (
    InfraManifest,
    InfraPlan,
    InfraState,
    PlanAction,
    PlanItem,
    RBACAssignment,
    IdentityMode,
    ResourceMode,
)


# ---------------------------------------------------------------------------
# RBAC role definitions (minimum privilege per SPEC-006 §6.3)
# ---------------------------------------------------------------------------

# Built-in role names and definition IDs.
_ROLES = {
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "Search Service Contributor": "7ca78c08-252a-4471-8644-bb5ff32d4ba0",
    "Search Index Data Contributor": "8ebe5a00-799e-43f5-93ac-243d3dce84a7",
    "Search Index Data Reader": "1407120a-92aa-4202-b7e9-c0e197c71c8f",
    "Cognitive Services OpenAI User": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
    "Cognitive Services OpenAI Contributor": "a001fd3d-188f-4b5d-821b-7da978bf7442",
}


def _state_resource_name(resource_id_or_name: str) -> str:
    """Return the terminal resource name from an ARM ID or stored name."""
    normalized = resource_id_or_name.rstrip("/")
    return normalized.rsplit("/", 1)[-1]


def _configured_resource_name(
    configured_name: str | None,
    resource_id: str | None,
    resource_type: str,
    environment: str,
) -> str:
    """Use the terminal ARM-ID name when a connected resource has no name."""
    return configured_name or (
        _state_resource_name(resource_id)
        if resource_id
        else resolve_resource_name(None, resource_type, environment)
    )


def _role_assignment(
    principal_type: str,
    role_name: str,
    scope: str,
    description: str,
    principal_id: str | None = None,
) -> RBACAssignment:
    return RBACAssignment(
        principal_type=principal_type,
        principal_id=principal_id,
        role_name=role_name,
        scope=scope,
        description=description,
    )


# ---------------------------------------------------------------------------
# Plan item builders
# ---------------------------------------------------------------------------


def _plan_resource_group(manifest: InfraManifest) -> PlanItem:
    rg = manifest.azure.resource_group
    name = rg.name or f"rg-{manifest.environment}"
    action = PlanAction.ADOPT if rg.mode == ResourceMode.CONNECT else PlanAction.CREATE
    return PlanItem(
        resource_type="Microsoft.Resources/resourceGroups",
        resource_name=name,
        action=action,
        cost_bearing=False,
        location=rg.location or manifest.azure.default_location,
    )


def _plan_identity(manifest: InfraManifest) -> PlanItem:
    name = resolve_resource_name(
        manifest.identity.name,
        "identity",
        manifest.environment,
    )
    return PlanItem(
        resource_type="Microsoft.ManagedIdentity/userAssignedIdentities",
        resource_name=name,
        action=PlanAction.CREATE,
        cost_bearing=False,
        location=manifest.azure.default_location,
        prereqs=["provider_microsoft_managedidentity"],
    )


def _reference_app_requires_identity(manifest: InfraManifest) -> bool:
    """A user-assigned identity is only provisioned for the reference app."""
    return (
        manifest.features.reference_app
        and manifest.identity.mode == IdentityMode.USER_ASSIGNED
    )


def _plan_storage(manifest: InfraManifest) -> PlanItem:
    storage = manifest.resources.storage
    name = _configured_resource_name(
        storage.name, storage.resource_id, "storage", manifest.environment
    )
    action = PlanAction.ADOPT if storage.mode == ResourceMode.CONNECT else PlanAction.CREATE
    return PlanItem(
        resource_type="Microsoft.Storage/storageAccounts",
        resource_name=name,
        action=action,
        cost_bearing=True,
        sku=storage.sku.value,
        location=manifest.azure.default_location,
        prereqs=["provider_microsoft_storage"],
        details={
            "hierarchical_namespace": storage.hierarchical_namespace,
            "container": storage.container,
            "retention_days": storage.retention_days,
        },
    )


def _plan_document_intelligence(manifest: InfraManifest) -> PlanItem:
    di = manifest.resources.document_intelligence
    name = _configured_resource_name(
        di.name, di.resource_id, "document_intelligence", manifest.environment
    )
    action = PlanAction.ADOPT if di.mode == ResourceMode.CONNECT else PlanAction.CREATE
    return PlanItem(
        resource_type="Microsoft.CognitiveServices/accounts",
        resource_name=name,
        action=action,
        cost_bearing=True,
        sku=di.sku.value,
        location=manifest.azure.default_location,
        prereqs=["provider_microsoft_cognitiveservices"],
        details={"kind": "FormRecognizer"},
    )


def _plan_foundry(manifest: InfraManifest) -> list[PlanItem]:
    foundry = manifest.resources.foundry
    name = _configured_resource_name(
        foundry.name, foundry.resource_id, "foundry", manifest.environment
    )
    action = PlanAction.ADOPT if foundry.mode == ResourceMode.CONNECT else PlanAction.CREATE
    nested_action = action
    items = [
        PlanItem(
            resource_type="Microsoft.CognitiveServices/accounts",
            resource_name=name,
            action=action,
            cost_bearing=True,
            sku="S0",
            location=manifest.azure.default_location,
            prereqs=[
                "provider_microsoft_cognitiveservices",
                "model_capacity_verified",
            ],
            details={"kind": "AIServices"},
            warnings=[
                "Foundry resource is kind=AIServices (not OpenAI). "
                "Model deployments require separate quota checks."
            ],
        ),
        PlanItem(
            resource_type="Microsoft.CognitiveServices/accounts/projects",
            resource_name=f"{name}/{foundry.project_name}",
            action=nested_action,
            cost_bearing=False,
            prereqs=[
                f"foundry_account:{name}",
                "rbac_azure_ai_user_assigned",
            ],
            warnings=(
                [
                    "Project creation requires the caller to have Azure AI roles. "
                    "The CLI does not auto-assign Azure AI User."
                ]
                if foundry.mode == ResourceMode.CREATE
                else []
            ),
        ),
        PlanItem(
            resource_type="Microsoft.CognitiveServices/accounts/deployments",
            resource_name=(
                f"{name}/"
                f"{foundry.models.chat.deployment_name or foundry.models.chat.model}"
            ),
            action=nested_action,
            cost_bearing=True,
            sku=foundry.models.chat.sku.value,
            prereqs=[
                f"foundry_project:{foundry.project_name}",
                "model_capacity_verified",
            ],
            details={
                "model": foundry.models.chat.model,
                "target_tpm": foundry.models.chat.target_tpm,
            },
        ),
        PlanItem(
            resource_type="Microsoft.CognitiveServices/accounts/deployments",
            resource_name=(
                f"{name}/"
                f"{foundry.models.embedding.deployment_name or foundry.models.embedding.model}"
            ),
            action=nested_action,
            cost_bearing=True,
            sku="GlobalStandard",
            prereqs=[
                f"foundry_project:{foundry.project_name}",
                "model_capacity_verified",
            ],
            details={
                "model": foundry.models.embedding.model,
                "dimensions": foundry.models.embedding.dimensions,
            },
        ),
    ]
    return items


def _plan_search(manifest: InfraManifest) -> PlanItem:
    search = manifest.resources.search
    name = _configured_resource_name(
        search.name, search.resource_id, "search", manifest.environment
    )
    action = PlanAction.ADOPT if search.mode == ResourceMode.CONNECT else PlanAction.CREATE
    incompatible = False
    warnings: list[str] = []
    if search.mode == ResourceMode.CONNECT:
        from .schema import SearchSku
        if search.sku not in {SearchSku.STANDARD, SearchSku.STANDARD2, SearchSku.STANDARD3}:
            warnings.append(
                f"Adopted Search service SKU '{search.sku.value}' may not support "
                "semantic ranker. Validate before applying."
            )
    return PlanItem(
        resource_type="Microsoft.Search/searchServices",
        resource_name=name,
        action=action if not incompatible else PlanAction.REPLACE,
        cost_bearing=True,
        sku=search.sku.value,
        location=manifest.azure.default_location,
        prereqs=["provider_microsoft_search"],
        details={
            "semantic_ranker": search.semantic_ranker.value,
        },
        warnings=warnings,
    )


def _plan_container_registry(manifest: InfraManifest) -> PlanItem:
    registry = manifest.resources.container_registry
    name = _configured_resource_name(
        registry.name,
        registry.resource_id,
        "container_registry",
        manifest.environment,
    )
    action = (
        PlanAction.ADOPT
        if registry.mode == ResourceMode.CONNECT
        else PlanAction.CREATE
    )
    return PlanItem(
        resource_type="Microsoft.ContainerRegistry/registries",
        resource_name=name,
        action=action,
        cost_bearing=True,
        sku=registry.sku,
        location=manifest.azure.default_location,
        prereqs=["provider_microsoft_containerregistry"],
        details={"admin_user_enabled": False},
    )


def _plan_fabric_items(manifest: InfraManifest) -> list[PlanItem]:
    """Return plan items for Fabric workspace, lakehouse, ontology, graph."""
    items = []
    fb = manifest.fabric

    workspace_action = (
        PlanAction.ADOPT if fb.workspace.mode == ResourceMode.CONNECT
        else PlanAction.CREATE
    )
    workspace_name = fb.workspace.name or fb.workspace.display_name or "kg-workspace"
    items.append(PlanItem(
        resource_type="Fabric/Workspace",
        resource_name=workspace_name,
        action=workspace_action,
        cost_bearing=False,
        prereqs=["fabric_capacity_configured"],
    ))

    lh_action = (
        PlanAction.ADOPT if fb.lakehouse.mode == ResourceMode.CONNECT
        else PlanAction.CREATE
    )
    lh_name = fb.lakehouse.name or fb.lakehouse.display_name or "kg"
    items.append(PlanItem(
        resource_type="Fabric/Lakehouse",
        resource_name=lh_name,
        action=lh_action,
        cost_bearing=False,
        prereqs=[f"fabric_workspace:{workspace_name}"],
        details={"enable_schemas": fb.lakehouse.enable_schemas},
    ))

    if manifest.features.graph or fb.ontology.mode == ResourceMode.CREATE:
        ont_action = (
            PlanAction.ADOPT if fb.ontology.mode == ResourceMode.CONNECT
            else PlanAction.CREATE
        )
        ont_name = fb.ontology.display_name or fb.ontology.name or "KG Ontology"
        items.append(PlanItem(
            resource_type="Fabric/Ontology",
            resource_name=ont_name,
            action=ont_action,
            cost_bearing=False,
            prereqs=[f"fabric_workspace:{workspace_name}"],
            warnings=["Ontology capability is preview and capacity-gated."],
        ))

    if manifest.features.graph or fb.graph_model.mode == ResourceMode.CREATE:
        gm_action = (
            PlanAction.ADOPT if fb.graph_model.mode == ResourceMode.CONNECT
            else PlanAction.CREATE
        )
        gm_name = fb.graph_model.display_name or fb.graph_model.name or "KG Graph"
        items.append(PlanItem(
            resource_type="Fabric/GraphModel",
            resource_name=gm_name,
            action=gm_action,
            cost_bearing=False,
            prereqs=[f"fabric_workspace:{workspace_name}"],
            warnings=[
                "Graph model capability is preview. "
                "When automated API create is unavailable, the CLI will guide manual creation."
            ],
        ))

    return items


def _plan_rbac(manifest: InfraManifest) -> list[RBACAssignment]:
    """Compute minimum RBAC assignments for the deployment identity."""
    if not _reference_app_requires_identity(manifest):
        return []
    assignments: list[RBACAssignment] = []
    identity_name = resolve_resource_name(
        manifest.identity.name,
        "identity",
        manifest.environment,
    )
    rg_name = manifest.azure.resource_group.name or f"rg-{manifest.environment}"
    sub_id = manifest.azure.subscription_id
    rg_scope = (
        f"/subscriptions/{sub_id}/resourceGroups/{rg_name}"
    )

    # Storage: ingestion runtime reads/writes blobs
    storage_name = resolve_resource_name(
        manifest.resources.storage.name, "storage", manifest.environment
    )
    assignments.append(_role_assignment(
        principal_type="ManagedIdentity",
        role_name="Storage Blob Data Contributor",
        scope=f"{rg_scope}/providers/Microsoft.Storage/storageAccounts/{storage_name}",
        description=(
            f"Grant '{identity_name}' blob read/write on storage '{storage_name}'. "
            "Required for ingestion runtime."
        ),
    ))

    # Search: deployment identity needs service + index data roles
    search_name = resolve_resource_name(
        manifest.resources.search.name, "search", manifest.environment
    )
    for role in ("Search Index Data Contributor", "Search Index Data Reader"):
        assignments.append(_role_assignment(
            principal_type="ManagedIdentity",
            role_name=role,
            scope=f"{rg_scope}/providers/Microsoft.Search/searchServices/{search_name}",
            description=(
                f"Grant '{identity_name}' {role} on search service '{search_name}'."
            ),
        ))

    # Foundry: model and agent operations
    foundry_name = resolve_resource_name(
        manifest.resources.foundry.name, "foundry", manifest.environment
    )
    assignments.append(_role_assignment(
        principal_type="ManagedIdentity",
        role_name="Cognitive Services OpenAI User",
        scope=(
            f"{rg_scope}/providers/Microsoft.CognitiveServices/accounts/{foundry_name}"
        ),
        description=(
            f"Grant '{identity_name}' Cognitive Services OpenAI User on Foundry account. "
            "Note: Azure AI User role for Foundry projects is separate and must be "
            "assigned by a user with Owner or User Access Administrator."
        ),
    ))

    return assignments


def _collect_cost_bearing_skus(items: list[PlanItem]) -> list[str]:
    return [
        f"{item.resource_type} ({item.sku})"
        for item in items
        if item.cost_bearing and item.sku
    ]


def _collect_prereqs(items: list[PlanItem]) -> list[str]:
    seen: set[str] = set()
    prereqs: list[str] = []
    for item in items:
        for p in item.prereqs:
            if p not in seen:
                seen.add(p)
                prereqs.append(p)
    return prereqs


# ---------------------------------------------------------------------------
# Main plan builder
# ---------------------------------------------------------------------------


def build_plan(
    manifest: InfraManifest,
    existing_state: InfraState | None = None,
) -> InfraPlan:
    """Build a deterministic infrastructure plan from the manifest.

    SPEC-006 §4.1 / INF-012.

    Args:
        manifest: Validated InfraManifest.
        existing_state: Current persisted state (used for adopt/no-op detection).
    """
    items: list[PlanItem] = []
    warnings: list[str] = []

    items.append(_plan_resource_group(manifest))
    if _reference_app_requires_identity(manifest):
        items.append(_plan_identity(manifest))
    items.append(_plan_storage(manifest))
    items.append(_plan_document_intelligence(manifest))
    items.extend(_plan_foundry(manifest))
    items.append(_plan_search(manifest))
    if manifest.features.reference_app:
        items.append(_plan_container_registry(manifest))
    items.extend(_plan_fabric_items(manifest))

    # Mark items as no-op if they are already in state
    if existing_state:
        managed_types = set(existing_state.managed_resource_ids)
        adopted_types = set(existing_state.adopted_resource_ids)
        managed = {
            _state_resource_name(value)
            for value in existing_state.managed_resource_ids.values()
        }
        adopted = {
            _state_resource_name(value)
            for value in existing_state.adopted_resource_ids.values()
        }
        items = [
            (
                item.model_copy(update={"action": PlanAction.NO_OP})
                if (
                    item.action in (PlanAction.CREATE, PlanAction.ADOPT)
                    and (
                        item.resource_name in managed
                        or item.resource_name in adopted
                        or item.resource_name.rsplit("/", 1)[-1] in managed
                        or item.resource_name.rsplit("/", 1)[-1] in adopted
                        or item.resource_type in managed_types
                        or item.resource_type in adopted_types
                    )
                )
                else item
            )
            for item in items
        ]

    rbac_assignments = _plan_rbac(manifest)
    prereqs = _collect_prereqs(items)
    cost_bearing = _collect_cost_bearing_skus(items)

    # Add top-level warnings
    if manifest.resources.foundry.mode == ResourceMode.CREATE:
        warnings.append(
            "Foundry model deployment requires model capacity verification "
            "(infra preflight or infra plan --check-quota). "
            "GPT-4.1 target: 200,000 TPM GlobalStandard."
        )
    if manifest.features.graph:
        warnings.append(
            "Graph model and Ontology features are preview. "
            "Verify tenant settings with a Fabric admin before applying."
        )

    return InfraPlan(
        environment=manifest.environment,
        items=items,
        rbac_assignments=rbac_assignments,
        prereqs=prereqs,
        warnings=warnings,
        cost_bearing_skus=cost_bearing,
    )


def save_plan(plan: InfraPlan, path: Path) -> None:
    """Write the plan to a JSON file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_plan(path: Path) -> InfraPlan:
    """Read a plan from a JSON file at *path*."""
    if not path.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return InfraPlan.model_validate(raw)
