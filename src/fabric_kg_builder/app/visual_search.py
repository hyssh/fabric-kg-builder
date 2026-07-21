"""Azure AI Search visual-asset retrieval for the reference application."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import unquote, urlparse


class VisualSearchError(RuntimeError):
    """Raised when a visual search or protected image read fails."""


class VisualSearchClient(Protocol):
    """Subset of Azure AI Search used by visual search."""

    def search(self, search_text: str, **kwargs: Any) -> Any: ...



@dataclass(frozen=True)
class VisualSearchResult:
    """A visual asset returned by Azure AI Search."""

    visual_id: str
    image_id: str
    description: str
    source_path: str
    asset_type: str
    score: float


class VisualSearchTool:
    """Search the visual-assets index and proxy only approved Blob locations."""

    _SELECT_FIELDS = [
        "visual_id",
        "image_id",
        "content",
        "source_path",
        "asset_type",
        "blob_url",
    ]

    def __init__(
        self,
        index_name: str,
        blob_account_url: str,
        blob_container: str,
        *,
        _client: VisualSearchClient | None = None,
        _blob_service_client: Any = None,
        fail_on_error: bool = False,
    ) -> None:
        self.index_name = index_name
        self.blob_account_url = blob_account_url.rstrip("/")
        self.blob_container = blob_container
        self._client = _client
        self._blob_service_client = _blob_service_client
        self.fail_on_error = fail_on_error

    @property
    def is_available(self) -> bool:
        return self._client is not None

    def search(self, query: str, *, top_k: int = 8) -> list[VisualSearchResult]:
        """Return matching images using the visual index semantic configuration."""
        if self._client is None:
            return []
        try:
            items = self._client.search(
                query,
                top=top_k,
                select=self._SELECT_FIELDS,
                query_type="semantic",
                semantic_configuration_name="kg-visual-assets-semantic",
            )
            results: list[VisualSearchResult] = []
            for item in items:
                value = dict(item) if not isinstance(item, dict) else item
                visual_id = str(value.get("visual_id") or "")
                if not visual_id or not value.get("blob_url"):
                    continue
                results.append(
                    VisualSearchResult(
                        visual_id=visual_id,
                        image_id=str(value.get("image_id") or visual_id),
                        description=str(value.get("content") or ""),
                        source_path=str(value.get("source_path") or ""),
                        asset_type=str(value.get("asset_type") or ""),
                        score=float(value.get("@search.score", value.get("score", 0.0))),
                    )
                )
            return results
        except Exception as exc:
            if self.fail_on_error:
                raise VisualSearchError("Azure AI Search visual retrieval failed.") from exc
            return []

    def read_image(self, visual_id: str) -> tuple[bytes, str]:
        """Read one indexed image from its configured private Blob container."""
        if self._client is None or self._blob_service_client is None:
            raise VisualSearchError("Visual image retrieval is not configured.")
        try:
            escaped_visual_id = visual_id.replace("'", "''")
            values = list(self._client.search(
                "*",
                top=1,
                select=["visual_id", "blob_url"],
                filter=f"visual_id eq '{escaped_visual_id}'",
            ))
            if not values:
                raise VisualSearchError("The requested image is not indexed.")
            value = values[0]
            blob_url = str(
                (
                    dict(value).get("blob_url")
                    if not isinstance(value, dict)
                    else value.get("blob_url")
                )
                or ""
            )
            blob_name = self._validated_blob_name(blob_url)
            blob_client = self._blob_service_client.get_blob_client(
                container=self.blob_container,
                blob=blob_name,
            )
            properties = blob_client.get_blob_properties()
            if int(properties.size) > 10 * 1024 * 1024:
                raise VisualSearchError("The requested image exceeds the 10 MB display limit.")
            content_type = str(
                getattr(getattr(properties, "content_settings", None), "content_type", "")
                or "application/octet-stream"
            )
            if content_type == "application/octet-stream":
                content_type = mimetypes.guess_type(blob_name)[0] or content_type
            if not content_type.startswith("image/"):
                raise VisualSearchError("The indexed Blob is not an image.")
            return blob_client.download_blob().readall(), content_type
        except VisualSearchError:
            raise
        except Exception as exc:
            raise VisualSearchError("The protected image could not be retrieved.") from exc

    def _validated_blob_name(self, blob_url: str) -> str:
        parsed = urlparse(blob_url)
        account = urlparse(self.blob_account_url)
        expected_prefix = f"/{self.blob_container}/"
        if (
            parsed.scheme != "https"
            or parsed.netloc != account.netloc
            or not parsed.path.startswith(expected_prefix)
        ):
            raise VisualSearchError("The indexed image location is outside the configured Blob container.")
        return unquote(parsed.path[len(expected_prefix):])
