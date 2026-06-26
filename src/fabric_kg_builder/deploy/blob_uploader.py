"""Azure Blob Storage uploader for visual assets.

Uploads image bytes to Azure Blob Storage and returns a stable blob URL.
Deduplicates by asset_id: if a blob with the same name already exists,
the existing URL is returned without re-uploading.

Auth
----
``DefaultAzureCredential`` by default (managed identity / ``az login``).
If ``AZURE_STORAGE_KEY`` is present in the environment, a
``StorageSharedKeyCredential`` is used instead.
Secrets are **never** stored in code or config files.

Mockability
-----------
Inject ``_blob_service_client`` for tests — must satisfy::

    client.get_blob_client(container=..., blob=...) -> blob_client
    blob_client.get_blob_properties()  # raises on not-found
    blob_client.upload_blob(data, overwrite=False)
    blob_client.url  -> str

The ``make_blob_uploader`` factory in ``tests/conftest.py`` satisfies the higher-
level ``upload(asset_id, data, ext) -> url`` contract directly via ``side_effect``.
Use that mock for end-to-end tests; inject ``_blob_service_client`` when you need
to verify low-level call counts.
"""

from __future__ import annotations

import os
from typing import Any

from ..config.schema import BlobStorageConfig


class BlobUploader:
    """Upload image bytes to Azure Blob Storage with content-hash dedup.

    Parameters
    ----------
    config:
        :class:`~fabric_kg_builder.config.schema.BlobStorageConfig`
        (``account_name``, ``container``, ``path_prefix``).  Non-secret.
    _blob_service_client:
        Optional pre-built ``BlobServiceClient`` for testing.  Pass a
        ``MagicMock`` that satisfies the call chain documented in the module
        docstring.
    """

    def __init__(
        self,
        config: BlobStorageConfig,
        *,
        _blob_service_client: Any = None,
    ) -> None:
        self._config = config
        self._container_ready = False
        self._client = (
            _blob_service_client
            if _blob_service_client is not None
            else self._build_client(config)
        )

    # ------------------------------------------------------------------
    # SDK construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_client(config: BlobStorageConfig) -> Any:
        """Build a ``BlobServiceClient`` using DefaultAzureCredential or a storage key.

        The storage key (``AZURE_STORAGE_KEY``) is read from the environment at
        runtime — it is never stored in code or config files.
        """
        try:
            from azure.storage.blob import BlobServiceClient  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "azure-storage-blob is required for live Blob uploads. "
                "Install it with: pip install azure-storage-blob"
            ) from exc

        account_url = f"https://{config.account_name}.blob.core.windows.net"
        storage_key = os.environ.get("AZURE_STORAGE_KEY")

        if storage_key:
            from azure.storage.blob import (  # type: ignore[import]
                StorageSharedKeyCredential,
            )
            credential: Any = StorageSharedKeyCredential(config.account_name, storage_key)
        else:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            credential = DefaultAzureCredential()

        return BlobServiceClient(account_url=account_url, credential=credential)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _ensure_container(self) -> None:
        """Create the target container if it doesn't exist (idempotent, once per instance).

        Mirrors the reference ingestion pattern: lazily create the container so a
        first-time deploy/enrich doesn't fail with ContainerNotFound.  Silently
        ignores 'already exists' and missing-create-permission cases.
        """
        if self._container_ready:
            return
        try:
            cc = self._client.get_container_client(self._config.container)
            try:
                cc.create_container()
            except Exception:
                # Already exists, or no create permission — proceed either way.
                pass
        except Exception:
            # Client without get_container_client (e.g. some mocks) — skip.
            pass
        self._container_ready = True

    def upload(self, asset_id: str, data: bytes, ext: str) -> str:
        """Upload *data* to Blob Storage and return the blob URL.

        Deduplicates by ``asset_id``: if a blob with the same derived name
        already exists, returns its URL without re-uploading (idempotent).

        Parameters
        ----------
        asset_id:
            Stable identifier for this asset (typically the ``image_id`` from
            :func:`~fabric_kg_builder.model.ids.make_image_id`).  Used as the
            blob name so the same ``asset_id`` always maps to the same URL.
        data:
            Raw image bytes.
        ext:
            File extension without leading dot, e.g. ``"png"``, ``"jpg"``.

        Returns
        -------
        str
            Public or SAS-accessible blob URL.
        """
        self._ensure_container()
        prefix = self._config.path_prefix.rstrip("/")
        blob_name = f"{prefix}/{asset_id}.{ext}" if prefix else f"{asset_id}.{ext}"
        container = self._config.container

        blob_client = self._client.get_blob_client(container=container, blob=blob_name)

        try:
            # If the blob already exists, return its URL without re-uploading.
            blob_client.get_blob_properties()
            return blob_client.url
        except Exception:
            # Blob does not exist — upload now.
            blob_client.upload_blob(data, overwrite=False)
            return blob_client.url
