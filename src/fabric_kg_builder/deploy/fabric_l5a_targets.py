"""Live Fabric adapter for the four L5a structured publication targets.

Read paths are real: ``inspect`` and ``read_back`` resolve the release-owned
item by display name, call ``getDefinition``, and reconstruct the canonical
L5a state from the embedded state part.

Mutating paths fail closed. Fabric's item control plane exposes no
compare-and-swap authority — ``GET`` on an item returns an *empty* ``etag``
header, and ``DELETE`` with ``If-Match`` is silently ignored (it answers
``404 ItemNotFound`` rather than ``412 Precondition Failed``). Creating or
deleting a release-owned item therefore cannot be fenced, and an unfenced
mutation on a shared workspace is not a risk this release takes. The
mutating methods raise an explicit capability NO-GO instead of attempting
it, so a blocked publication is reported as a capability gap rather than
mistaken for success.

The OneLake data plane is different: it returns real ADLS Gen2 ETags and
honours conditional writes. Parquet upload is therefore fenceable, but it
still requires a Lakehouse *item* to exist, so it inherits the same gate.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fabric_kg_builder.contracts.publication import AccessPolicy
from fabric_kg_builder.serving.structured_publication import (
    L5aPublishOperation,
    L5aRemoteAccounting,
    L5aStateOperation,
    L5aTableSnapshot,
    L5aTargetState,
)

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_TOKEN_SCOPE = "https://api.fabric.microsoft.com/.default"

#: Fabric collection that hosts each L5a target kind.
TARGET_COLLECTIONS: Mapping[str, str] = {
    "parquet": "lakehouses",
    "semantic_model": "semanticModels",
    "ontology": "ontologies",
    "graph": "graphModels",
}

#: Definition part carrying the canonical L5a state for a published target.
L5A_STATE_PART = "l5a-state.json"


class FabricCapabilityUnavailable(RuntimeError):
    """A required Fabric capability is absent, so the operation is refused.

    Carries a stable ``code`` so a release plan can record an explicit
    capability NO-GO rather than an opaque transport failure.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FabricTargetCapabilities:
    """Capability verdict for one Fabric item lifecycle.

    ``create`` and ``delete`` are ``False`` because Fabric exposes no ETag or
    conditional-delete authority for items, not because the endpoints are
    missing. Recording the reason keeps the release proof honest about which
    kind of blocker this is.
    """

    read: bool
    read_definition: bool
    create: bool
    update_definition: bool
    delete: bool
    reason: str


#: Verdict established by live probing of workspace 570d838d-… on the 0.2.4
#: release line. ``etag: ""`` on item GET and a 404 (not 412) answer to
#: ``DELETE`` with ``If-Match`` were both observed directly.
FABRIC_ITEM_CAPABILITIES = FabricTargetCapabilities(
    read=True,
    read_definition=True,
    create=False,
    update_definition=True,
    delete=False,
    reason=(
        "Fabric item GET returns an empty ETag and DELETE ignores If-Match "
        "(404 ItemNotFound rather than 412 Precondition Failed), so item "
        "create and delete cannot be fenced by compare-and-swap"
    ),
)


def _accounting(
    sequence: int,
    verb: str,
    target_kind: str,
    *,
    request_bytes: int = 0,
    response_bytes: int = 0,
    errors: tuple[str, ...] = (),
) -> L5aRemoteAccounting:
    return L5aRemoteAccounting(
        operation_refs=(f"fabric:{sequence}:{verb}:{target_kind}",),
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        retry_count=0,
        retry_wait_ms=0,
        error_codes=errors,
    )


def _decode_state_part(payload: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(payload).decode("utf-8"))


def _state_from_definition(
    target_kind: str,
    target_id: str,
    definition: Mapping[str, Any],
) -> L5aTargetState | None:
    """Reconstruct the canonical L5a state from a Fabric item definition.

    Returns ``None`` when the item carries no L5a state part, which is the
    correct answer for an item that exists but was never published by this
    release: it has no prior publication to preserve or restore.
    """

    parts = definition.get("parts") or ()
    part = next(
        (
            item
            for item in parts
            if str(item.get("path") or "") == L5A_STATE_PART
        ),
        None,
    )
    if part is None:
        return None
    envelope = _decode_state_part(str(part.get("payload") or ""))
    return L5aTargetState(
        target_kind=envelope["target_kind"],
        target_id=target_id,
        target_version=envelope["target_version"],
        definition=envelope["definition"],
        table_snapshots=tuple(
            L5aTableSnapshot(**snapshot)
            for snapshot in envelope["table_snapshots"]
        ),
        access_policy_id=envelope["access_policy_id"],
        access_policy_hash=envelope["access_policy_hash"],
        publication_token=envelope["publication_token"],
        required_member_manifest_rows=tuple(
            dict(row) for row in envelope["required_member_manifest_rows"]
        ),
        required_member_rows=tuple(
            dict(row) for row in envelope["required_member_rows"]
        ),
    )


class FabricL5aTargetClient:
    """Bounded live adapter over the four L5a Fabric targets.

    Constructed with a transport so the read paths stay testable without a
    live workspace. ``requests`` is used when no transport is supplied.
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        token: str,
        transport: Any | None = None,
        capabilities: FabricTargetCapabilities = FABRIC_ITEM_CAPABILITIES,
    ) -> None:
        self.workspace_id = workspace_id
        self._token = token
        self._transport = transport
        self._capabilities = capabilities
        self._sequence = 0
        self._item_ids: dict[tuple[str, str], str] = {}

    # -- transport ---------------------------------------------------------

    def _requests(self) -> Any:
        if self._transport is not None:
            return self._transport
        import requests  # noqa: PLC0415

        return requests

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _get(self, url: str) -> Any:
        try:
            return self._requests().get(
                url,
                headers=self._headers(),
                timeout=120,
            )
        except Exception as exc:  # normalize transport faults
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_TRANSPORT_FAILED",
                f"GET {url} failed: {type(exc).__name__}",
            ) from exc

    # -- resolution --------------------------------------------------------

    def _collection(self, target_kind: str) -> str:
        try:
            return TARGET_COLLECTIONS[target_kind]
        except KeyError as exc:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_TARGET_KIND_UNSUPPORTED",
                f"no Fabric collection hosts target kind {target_kind!r}",
            ) from exc

    def _resolve_item_id(self, target_kind: str, target_id: str) -> str | None:
        """Resolve a release-owned display name to its Fabric item id.

        Fabric addresses items by GUID, and the GUID of an item this release
        has not created yet is unknowable, so the release-owned *name* is the
        stable handle across dry-run and live.
        """

        cached = self._item_ids.get((target_kind, target_id))
        if cached is not None:
            return cached
        collection = self._collection(target_kind)
        name = target_id.split(":", 1)[1] if ":" in target_id else target_id
        response = self._get(
            f"{FABRIC_API_BASE}/workspaces/{self.workspace_id}/{collection}"
        )
        if response.status_code != 200:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_LIST_FAILED",
                f"listing {collection} answered {response.status_code}",
            )
        for item in response.json().get("value") or ():
            if str(item.get("displayName") or "") == name:
                item_id = str(item.get("id") or "")
                self._item_ids[(target_kind, target_id)] = item_id
                return item_id
        return None

    def _definition(self, target_kind: str, item_id: str) -> Mapping[str, Any]:
        collection = self._collection(target_kind)
        url = (
            f"{FABRIC_API_BASE}/workspaces/{self.workspace_id}/"
            f"{collection}/{item_id}/getDefinition"
        )
        try:
            response = self._requests().post(
                url,
                headers=self._headers(),
                timeout=300,
            )
        except Exception as exc:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_TRANSPORT_FAILED",
                f"getDefinition failed: {type(exc).__name__}",
            ) from exc
        if response.status_code == 202:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_DEFINITION_ASYNC_UNSUPPORTED",
                f"{target_kind} getDefinition returned a long-running "
                "operation that this release does not yet poll",
            )
        if response.status_code == 403:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_DEFINITION_FORBIDDEN",
                f"{target_kind} getDefinition was refused, commonly because "
                "the item carries a protected sensitivity label",
            )
        if response.status_code != 200:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_DEFINITION_FAILED",
                f"{target_kind} getDefinition answered {response.status_code}",
            )
        body = response.json()
        return body.get("definition") or body

    # -- read paths --------------------------------------------------------

    def inspect(self, target_kind: str, target_id: str) -> L5aStateOperation:
        self._sequence += 1
        item_id = self._resolve_item_id(target_kind, target_id)
        if item_id is None:
            return L5aStateOperation(
                state=None,
                accounting=_accounting(self._sequence, "inspect", target_kind),
            )
        definition = self._definition(target_kind, item_id)
        return L5aStateOperation(
            state=_state_from_definition(target_kind, target_id, definition),
            accounting=_accounting(self._sequence, "inspect", target_kind),
        )

    def read_back(self, target_kind: str, target_id: str) -> L5aStateOperation:
        self._sequence += 1
        item_id = self._resolve_item_id(target_kind, target_id)
        if item_id is None:
            return L5aStateOperation(
                state=None,
                accounting=_accounting(
                    self._sequence,
                    "read-back",
                    target_kind,
                ),
            )
        definition = self._definition(target_kind, item_id)
        return L5aStateOperation(
            state=_state_from_definition(target_kind, target_id, definition),
            accounting=_accounting(self._sequence, "read-back", target_kind),
        )

    # -- mutating paths ----------------------------------------------------

    def publish(
        self,
        target_kind: str,
        target_id: str,
        *,
        definition_path: Path,
        table_paths: Mapping[str, Path],
        access_policy: AccessPolicy,
        expected_state: L5aTargetState | None,
        publication_token: str,
    ) -> L5aPublishOperation:
        """Refuse publication that would require an unfenced item create."""

        item_id = self._resolve_item_id(target_kind, target_id)
        if item_id is None and not self._capabilities.create:
            raise FabricCapabilityUnavailable(
                "L5A_FABRIC_CREATE_UNFENCED",
                f"publishing {target_kind} requires creating "
                f"{target_id!r}, and {self._capabilities.reason}",
            )
        raise FabricCapabilityUnavailable(
            "L5A_FABRIC_PUBLISH_UNAVAILABLE",
            f"{target_kind} publication is not enabled on this release line",
        )

    def cleanup(
        self,
        target_kind: str,
        target_id: str,
        *,
        publication_token: str,
    ) -> L5aPublishOperation:
        """Refuse rollback-by-delete, which cannot be fenced.

        Rolling a created target back means deleting it, and an unconditional
        delete would remove an item that another writer may have taken over
        since the publish. Failing here is louder than a silent unfenced
        delete, and the caller records it as a cleanup error.
        """

        raise FabricCapabilityUnavailable(
            "L5A_FABRIC_DELETE_UNFENCED",
            f"rolling back {target_kind} requires deleting {target_id!r}, "
            f"and {self._capabilities.reason}",
        )

    def restore(
        self,
        target_kind: str,
        target_id: str,
        *,
        prior_state: L5aTargetState,
        publication_token: str,
    ) -> L5aPublishOperation:
        """Refuse restore while publication itself is disabled.

        ``updateDefinition`` *is* fenceable through definition read-back, so
        this path is reachable in principle. It stays closed on this release
        line because nothing can reach it without a publish first.
        """

        raise FabricCapabilityUnavailable(
            "L5A_FABRIC_RESTORE_UNAVAILABLE",
            f"{target_kind} restore is not enabled on this release line",
        )

    # -- reporting ---------------------------------------------------------

    def capability_report(self) -> dict[str, object]:
        """Capability verdict for every L5a target, for the release plan."""

        report: dict[str, object] = {}
        for kind in sorted(TARGET_COLLECTIONS):
            report[f"fabric.{kind}.read"] = self._capabilities.read
            report[f"fabric.{kind}.read_definition"] = (
                self._capabilities.read_definition
            )
            report[f"fabric.{kind}.create"] = self._capabilities.create
            report[f"fabric.{kind}.update_definition"] = (
                self._capabilities.update_definition
            )
            report[f"fabric.{kind}.delete"] = self._capabilities.delete
        report["fabric.capability_reason"] = self._capabilities.reason
        return report
