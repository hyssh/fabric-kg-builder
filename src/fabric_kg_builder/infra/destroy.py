"""Ownership-safe targeted infrastructure destroy.

``destroy`` only targets resources marked as managed in persisted state.
Adopted (connected) resources are NEVER deleted.

Key correctness invariants:
  - Azure values are full ARM IDs; Fabric values are exact item IDs.
  - Each Azure resource is deleted via ``az resource delete --ids <arm_id>``.
    Fabric items are deleted through their typed REST endpoints.
    A 404 response is treated as success (already deleted).
  - State entries are removed one-by-one only after Azure confirms deletion.
  - If delete fails, state entry is preserved so the next attempt retries.
  - Adopted resources raise ``ValueError`` and are never targeted.
  - Explicit ``confirmed=True`` is required; raises ``PermissionError`` otherwise.

SPEC-006 §4.1 / INF-018.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .apply import load_state, save_state
from .runner import CommandRunner, CommandError
from .schema import InfraState


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------


def _arm_id_to_name(arm_id: str) -> str:
    """Return the last path segment of an ARM resource ID as a friendly name."""
    if "/" in arm_id:
        return arm_id.rstrip("/").rsplit("/", 1)[-1]
    return arm_id


# ---------------------------------------------------------------------------
# Destroy plan
# ---------------------------------------------------------------------------


@dataclass
class DestroyItem:
    resource_type: str
    resource_name: str    # Friendly name (last ARM ID segment, or raw value if not an ID)
    resource_id: Optional[str]  # Full ARM ID or exact Fabric item ID
    is_managed: bool
    will_destroy: bool
    reason: str


@dataclass
class DestroyPlan:
    environment: str
    targets: list[DestroyItem] = field(default_factory=list)
    blocked_adopted: list[str] = field(default_factory=list)

    @property
    def has_destroyable_targets(self) -> bool:
        return any(t.will_destroy for t in self.targets)

    def as_dict(self) -> dict:
        return {
            "environment": self.environment,
            "targets": [
                {
                    "resource_type": t.resource_type,
                    "resource_name": t.resource_name,
                    "resource_id": t.resource_id,
                    "is_managed": t.is_managed,
                    "will_destroy": t.will_destroy,
                    "reason": t.reason,
                }
                for t in self.targets
            ],
            "blocked_adopted": self.blocked_adopted,
        }


def build_destroy_plan(
    state: InfraState,
    target_names: list[str] | None = None,
) -> DestroyPlan:
    """Build a destroy plan from persisted state.

    Only resources in ``managed_resource_ids`` can be destroyed.
    Resources in ``adopted_resource_ids`` are always blocked.

    ``target_names`` filters by the friendly name (last ARM ID segment) OR
    the full ARM ID — so both ``"teststorage"`` and the full ID are accepted.

    SPEC-006 §4.1 / INF-018.
    """
    plan = DestroyPlan(environment=state.environment)

    def _name_matches(arm_id_or_name: str) -> bool:
        if not target_names:
            return True
        friendly = _arm_id_to_name(arm_id_or_name)
        return friendly in target_names or arm_id_or_name in target_names

    # Check for attempt to destroy adopted resources
    for name in (target_names or []):
        for _rtype, arm_id in state.adopted_resource_ids.items():
            friendly = _arm_id_to_name(arm_id)
            if name in (arm_id, friendly):
                plan.blocked_adopted.append(name)
                break

    # Managed resources: only destroyable ones
    for resource_type, arm_id in state.managed_resource_ids.items():
        if not _name_matches(arm_id):
            continue
        friendly = _arm_id_to_name(arm_id)
        is_fabric = resource_type.startswith("Fabric/")
        has_provider_id = arm_id.startswith("/") or (
            is_fabric and bool(arm_id.strip())
        )
        adopted_in_managed_rg = (
            resource_type == "Microsoft.Resources/resourceGroups"
            and any(
                adopted_id.startswith(f"{arm_id.rstrip('/')}/")
                for adopted_id in state.adopted_resource_ids.values()
                if adopted_id.startswith("/")
            )
        )
        plan.targets.append(DestroyItem(
            resource_type=resource_type,
            resource_name=friendly,
            resource_id=arm_id if has_provider_id else None,
            is_managed=True,
            will_destroy=has_provider_id and not adopted_in_managed_rg,
            reason=(
                "Managed resource group contains adopted resources; deleting "
                "the group would violate ownership safety."
                if adopted_in_managed_rg
                else (
                    "Resource is managed by fabric-kg-builder and selected for destroy."
                    if has_provider_id
                    else f"No provider resource ID recorded for '{friendly}' — "
                    "cannot delete. Re-apply to persist the resource ID before "
                    "destroying."
                )
            ),
        ))

    # Adopted resources: always shown as blocked
    for resource_type, arm_id in state.adopted_resource_ids.items():
        if not _name_matches(arm_id):
            continue
        plan.targets.append(DestroyItem(
            resource_type=resource_type,
            resource_name=_arm_id_to_name(arm_id),
            resource_id=arm_id,
            is_managed=False,
            will_destroy=False,
            reason=(
                "Resource was adopted (not created by fabric-kg-builder). "
                "Destroy refused to protect externally-owned resources."
            ),
        ))

    return plan


# ---------------------------------------------------------------------------
# Execute destroy
# ---------------------------------------------------------------------------


@dataclass
class DestroyStatus:
    environment: str
    items_destroyed: int
    items_skipped: int
    items_blocked: int
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors


def execute_destroy(
    state: InfraState,
    plan: DestroyPlan,
    runner: CommandRunner,
    *,
    build_root: Path = Path("build"),
    confirmed: bool = False,
    fabric_transport: Any | None = None,
) -> DestroyStatus:
    """Execute the destroy plan using real ``az resource delete`` calls.

    Raises ``PermissionError`` if *confirmed* is False.
    Raises ``ValueError`` if the plan targets any adopted resource.

    For each destroyable item:
    1. Calls ``az resource delete --ids <arm_id> --yes``.
    2. A 404/ResourceNotFound is treated as success (already deleted).
    3. Removes the entry from ``managed_resource_ids`` only after Azure confirms.
    4. Saves state after each individual successful deletion.

    SPEC-006 §4.1 / INF-018.
    """
    if plan.blocked_adopted:
        raise ValueError(
            f"Destroy refused: adopted (externally-owned) resources cannot be deleted "
            f"by fabric-kg-builder: {plan.blocked_adopted}. "
            "Remove them from the target list."
        )

    if not confirmed:
        raise PermissionError(
            "Destroy requires explicit confirmation (--confirm flag). "
            "Review the destroy plan before proceeding."
        )

    destroyable = [t for t in plan.targets if t.will_destroy]
    priority = {
        "Fabric/GraphModel": 10,
        "Fabric/Ontology": 11,
        "Fabric/Lakehouse": 12,
        "Fabric/Workspace": 19,
        "Microsoft.CognitiveServices/accounts/deployments/chat": 20,
        "Microsoft.CognitiveServices/accounts/deployments/embedding": 20,
        "Microsoft.CognitiveServices/accounts/projects": 21,
        "Microsoft.Resources/resourceGroups": 100,
    }
    destroyable.sort(key=lambda item: priority.get(item.resource_type, 50))
    blocked = [t for t in plan.targets if not t.will_destroy]
    errors: list[str] = []
    destroyed = 0

    # Work on a mutable copy of managed_resource_ids
    managed = dict(state.managed_resource_ids)
    workspace_id = (
        state.managed_resource_ids.get("Fabric/Workspace")
        or state.adopted_resource_ids.get("Fabric/Workspace")
    )

    if any(item.resource_type.startswith("Fabric/") for item in destroyable):
        if fabric_transport is None:
            from .runner import RealCommandRunner

            if isinstance(runner, RealCommandRunner):
                from .fabric_client import (
                    DefaultAzureCredentialFabricTransport,
                )

                fabric_transport = DefaultAzureCredentialFabricTransport()

    for item in destroyable:
        if not item.resource_id:
            errors.append(
                f"Cannot delete '{item.resource_name}': no ARM resource ID in state. "
                "Re-apply to record the ARM ID before destroying."
            )
            continue

        if item.resource_type.startswith("Fabric/"):
            if fabric_transport is None:
                errors.append(
                    f"Cannot delete '{item.resource_name}': authenticated "
                    "Fabric transport is unavailable."
                )
                continue
            try:
                _delete_fabric_item(
                    fabric_transport,
                    item.resource_type,
                    item.resource_id,
                    workspace_id,
                )
                destroyed += 1
            except Exception as exc:
                errors.append(
                    f"Delete failed for '{item.resource_name}': {exc}"
                )
                continue
        else:
            try:
                result = runner.run([
                    "az", "resource", "delete",
                    "--ids", item.resource_id,
                ])
            except CommandError as exc:
                errors.append(
                    f"Delete failed for '{item.resource_name}': {exc}"
                )
                continue

            if result.succeeded or _is_not_found(result.stderr):
                destroyed += 1
            else:
                errors.append(
                    f"Delete failed for '{item.resource_name}' "
                    f"(ARM ID: {item.resource_id}): {result.stderr}"
                )
                continue

        # Remove from managed state only after confirmed deletion
        managed.pop(item.resource_type, None)
        updated_state = state.model_copy(update={"managed_resource_ids": managed})
        save_state(updated_state, build_root)
        # Keep local `state` reference for adopted_resource_ids
        state = updated_state

    return DestroyStatus(
        environment=state.environment,
        items_destroyed=destroyed,
        items_skipped=len(blocked),
        items_blocked=len(plan.blocked_adopted),
        errors=errors,
    )


def _delete_fabric_item(
    transport: Any,
    resource_type: str,
    item_id: str,
    workspace_id: str | None,
) -> None:
    from .fabric_client import (
        FabricGraphModelClient,
        FabricLakehouseClient,
        FabricOntologyClient,
        FabricWorkspaceClient,
    )

    if resource_type == "Fabric/Workspace":
        FabricWorkspaceClient(transport).delete_workspace(item_id)
        return
    if not workspace_id:
        raise ValueError(
            f"Cannot delete {resource_type}: Fabric workspace ID is missing."
        )
    if resource_type == "Fabric/Lakehouse":
        FabricLakehouseClient(transport, workspace_id).delete_lakehouse(item_id)
    elif resource_type == "Fabric/Ontology":
        FabricOntologyClient(transport, workspace_id).delete_ontology(item_id)
    elif resource_type == "Fabric/GraphModel":
        FabricGraphModelClient(
            transport, workspace_id
        ).delete_graph_model(item_id)
    else:
        raise ValueError(f"Unsupported Fabric resource type: {resource_type}")


def _is_not_found(stderr: str) -> bool:
    """Return True if the error indicates the resource is already gone."""
    not_found_phrases = (
        "ResourceNotFound",
        "404",
        "does not exist",
        "was not found",
        "not found",
    )
    lower = stderr.lower()
    return any(p.lower() in lower for p in not_found_phrases)
