"""Strict non-secret schemas for persisted infrastructure authority."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fabric_kg_builder.release.redact import (
    canonicalize_https_authority,
    looks_like_secret,
    redact_secret_text,
)

INFRA_OUTPUT_AUTHORITY_KEYS = frozenset({
    "blobendpoint",
    "chatdeploymentid",
    "chatdeploymentname",
    "chatmodelname",
    "containername",
    "containerregistryid",
    "containerregistryloginserver",
    "containerregistryname",
    "documentintelligenceendpoint",
    "documentintelligenceid",
    "documentintelligencename",
    "embeddingdeploymentid",
    "embeddingdeploymentname",
    "embeddingmodelname",
    "fabricgraphmodelguidance",
    "fabricgraphmodelid",
    "fabricgraphmodelstate",
    "fabriclakehouseid",
    "fabricontologyid",
    "fabricworkspaceid",
    "foundryaccountid",
    "foundryaccountname",
    "foundryendpoint",
    "foundryopenaiendpoint",
    "foundryprojectendpoint",
    "foundryprojectid",
    "foundryprojectname",
    "foundrysearchconnectionid",
    "foundrysearchconnectionname",
    "identityclientid",
    "identityid",
    "identityname",
    "identityprincipalid",
    "searchendpoint",
    "searchindexname",
    "searchserviceid",
    "searchservicename",
    "storageaccountid",
    "storageaccountname",
    "visualindexname",
})
_ARM_ID_KEYS = frozenset({
    "chatdeploymentid",
    "containerregistryid",
    "documentintelligenceid",
    "embeddingdeploymentid",
    "foundryaccountid",
    "foundryprojectid",
    "foundrysearchconnectionid",
    "identityid",
    "searchserviceid",
    "storageaccountid",
})
_CREDENTIAL_MARKERS = (
    "accesskey",
    "accesstoken",
    "accountkey",
    "adminkey",
    "apikey",
    "authorization",
    "certificate",
    "clientsecret",
    "connectionstring",
    "credential",
    "functionkey",
    "hostkey",
    "masterkey",
    "password",
    "primarykey",
    "privatekey",
    "refreshtoken",
    "sas",
    "secondarykey",
    "secret",
    "sharedaccesskey",
    "sharedkey",
    "signingkey",
    "subscriptionkey",
    "token",
)
_ARM_RESOURCE_ID = re.compile(
    r"^/subscriptions/[^/?#\s]+/resourceGroups/[^/?#\s]+"
    r"/providers/[^/?#\s]+(?:/[^/?#\s]+/[^/?#\s]+)+/?$",
    re.IGNORECASE,
)
_RESOURCE_GROUP_ID = re.compile(
    r"^/subscriptions/[^/?#\s]+/resourceGroups/[^/?#\s]+/?$",
    re.IGNORECASE,
)
_STATE_KEYS = frozenset({
    "adoptedresourceids",
    "environment",
    "lastoperation",
    "lastoperationid",
    "lastoperationstatus",
    "managedresourceids",
    "outputs",
    "schemaversion",
})


def canonical_key(value: str) -> str:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value))
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return "".join(re.findall(r"[A-Za-z0-9]+", separated)).lower()


def is_credential_key(value: str) -> bool:
    canonical = canonical_key(value)
    return canonical in {"auth", "credentials", "keys", "secrets", "tokens"} or any(
        marker in canonical for marker in _CREDENTIAL_MARKERS
    )


def sanitize_infrastructure_outputs(outputs: Any) -> dict[str, Any]:
    """Return strict flat output authority; reject unknown or nested values."""
    if not isinstance(outputs, dict):
        raise ValueError("Infrastructure outputs must be a JSON object.")
    safe: dict[str, Any] = {}
    for key, value in sorted(outputs.items(), key=lambda item: str(item[0])):
        key_text = str(key)
        canonical = canonical_key(key_text)
        if is_credential_key(key_text):
            continue
        if canonical not in INFRA_OUTPUT_AUTHORITY_KEYS:
            raise ValueError(
                f"Unknown infrastructure output authority field: {key_text}"
            )
        if isinstance(value, (dict, list, tuple)):
            raise ValueError(
                f"Infrastructure output '{key_text}' must be a scalar value."
            )
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"Infrastructure output '{key_text}' must be a string."
            )
        if not value.strip():
            continue
        if canonical.endswith("endpoint"):
            endpoint = canonicalize_https_authority(value)
            if endpoint is None:
                raise ValueError(
                    f"Infrastructure output '{key_text}' is not a safe HTTPS endpoint."
                )
            safe[key_text] = endpoint
            continue
        if canonical in _ARM_ID_KEYS:
            if _ARM_RESOURCE_ID.fullmatch(value.strip()) is None:
                raise ValueError(
                    f"Infrastructure output '{key_text}' is not a strict ARM ID."
                )
            safe[key_text] = value.strip().rstrip("/")
            continue
        if (
            len(value) > 2048
            or any(ord(character) < 32 for character in value)
            or looks_like_secret(value)
            or redact_secret_text(value) != value
        ):
            raise ValueError(
                f"Infrastructure output '{key_text}' is unsafe."
            )
        safe[key_text] = value
    return safe


def sanitize_infrastructure_state(
    payload: Any,
    *,
    environment: str,
) -> dict[str, Any]:
    """Validate the complete persisted infra state without retaining secrets."""
    if not isinstance(payload, dict):
        raise ValueError("Infrastructure state must be a JSON object.")
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        canonical = canonical_key(key_text)
        if is_credential_key(key_text):
            continue
        if canonical not in _STATE_KEYS:
            raise ValueError(
                f"Unknown infrastructure state authority field: {key_text}"
            )
        if canonical == "outputs":
            safe[key_text] = sanitize_infrastructure_outputs(value)
            continue
        if canonical in {"managedresourceids", "adoptedresourceids"}:
            if not isinstance(value, dict):
                raise ValueError(
                    f"Infrastructure state '{key_text}' must be an ID mapping."
                )
            ids: dict[str, str] = {}
            for resource_type, resource_id in value.items():
                if is_credential_key(str(resource_type)):
                    continue
                if not isinstance(resource_id, str):
                    raise ValueError(
                        f"Infrastructure state '{key_text}' contains a non-string ID."
                    )
                candidate = resource_id.strip().rstrip("/")
                is_arm_id = bool(
                    _ARM_RESOURCE_ID.fullmatch(candidate)
                    or _RESOURCE_GROUP_ID.fullmatch(candidate)
                )
                is_fabric_id = False
                if str(resource_type).startswith("Fabric/"):
                    try:
                        canonical_id = str(uuid.UUID(candidate))
                        is_fabric_id = canonical_id == candidate.lower()
                        candidate = canonical_id
                    except ValueError:
                        is_fabric_id = False
                if not (is_arm_id or is_fabric_id):
                    raise ValueError(
                        f"Infrastructure state '{key_text}' contains an invalid ID."
                    )
                ids[str(resource_type)] = candidate
            safe[key_text] = ids
            continue
        if value is None:
            safe[key_text] = None
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"Infrastructure state '{key_text}' is invalid or unsafe."
            )
        if canonical == "lastoperationid":
            try:
                canonical_operation_id = str(uuid.UUID(value))
            except ValueError as exc:
                raise ValueError(
                    "Infrastructure state last_operation_id must be a UUID."
                ) from exc
            if canonical_operation_id != value.lower():
                raise ValueError(
                    "Infrastructure state last_operation_id must be a "
                    "canonical UUID."
                )
            value = canonical_operation_id
        elif canonical == "lastoperation" and value not in {"", "apply"}:
            raise ValueError(
                "Infrastructure state last_operation is unsupported."
            )
        elif canonical == "lastoperationstatus" and value not in {
            "",
            "failed",
            "in_progress",
            "succeeded",
        }:
            raise ValueError(
                "Infrastructure state last_operation_status is unsupported."
            )
        elif canonical == "schemaversion" and not re.fullmatch(
            r"\d+\.\d+",
            value,
        ):
            raise ValueError(
                "Infrastructure state schema_version is invalid."
            )
        if (
            len(value) > 512
            or any(ord(character) < 32 for character in value)
            or looks_like_secret(value)
            or redact_secret_text(value) != value
        ):
            raise ValueError(
                f"Infrastructure state '{key_text}' is invalid or unsafe."
            )
        safe[key_text] = value
    if safe.get("environment") not in (None, environment):
        raise ValueError(
            "Infrastructure state environment differs from the target run."
        )
    safe["environment"] = environment
    return safe
