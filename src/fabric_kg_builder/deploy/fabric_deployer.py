"""FabricDeployer — assembles and (mock-)deploys Fabric Ontology REST parts.

Primary path (not yet implemented): fabric-cicd SDK.
Fallback path (TODO): Fabric REST API ``POST /workspaces/{id}/items``.

In mock mode the deployer logs the exact payload that *would* be sent and
returns without making any network call.  This makes the deploy command
fully testable offline.

Per SPEC-003 §8/§9.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FabricDeployer:
    """Assembles InlineBase64 REST parts and deploys (or mocks) to Fabric Ontology.

    Parameters
    ----------
    workspace_id:
        Fabric workspace GUID — read from ``ontology/environments/{env}.json``.
    parts:
        List of InlineBase64 part dicts as returned by
        ``OntologyCompiler.get_rest_parts()`` or loaded from
        ``build/ontology/definition.json``.
    mock:
        When ``True`` (default) no network call is made; the deployer logs
        what *would* be sent and returns immediately.

    Usage::

        deployer = FabricDeployer(
            workspace_id="9802a28a-...",
            parts=compiler.get_rest_parts(),
            mock=True,
        )
        deployer.deploy()

    TODO (fabric-cicd primary path):
        Replace the mock body with::

            from fabric_cicd import FabricWorkspace, publish_all_items
            ws = FabricWorkspace(workspace_id=self.workspace_id, ...)
            publish_all_items(ws)

        Until that integration is complete, the REST fallback path is the
        intended real deployment mechanism. See SPEC-003 §9 for the full
        endpoint contract.
    """

    def __init__(
        self,
        workspace_id: str,
        parts: list[dict[str, Any]],
        mock: bool = True,
    ) -> None:
        self.workspace_id = workspace_id
        self.parts = parts
        self.mock = mock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deploy(self) -> bool:
        """Deploy (or mock-deploy) the ontology parts to Fabric.

        Returns ``True`` on success. Raises on non-mock failure.
        """
        if self.mock:
            return self._deploy_mock()

        # TODO: fabric-cicd primary path (SPEC-003 §9.1–§9.4)
        # TODO: REST fallback — POST /v1/workspaces/{workspace_id}/items
        raise NotImplementedError(
            "Live deployment not yet implemented. "
            "Use mock=True for offline testing, or implement the fabric-cicd path."
        )

    def _deploy_mock(self) -> bool:
        """Log the deployment payload without making any network call."""
        logger.info(
            "[deploy-ontology] MOCK: would deploy %d parts to workspace %s",
            len(self.parts),
            self.workspace_id,
        )

        payload_summary: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "parts_count": len(self.parts),
            "part_paths": [p["path"] for p in self.parts],
        }
        logger.debug(
            "[deploy-ontology] MOCK payload summary:\n%s",
            json.dumps(payload_summary, indent=2),
        )
        return True

    # ------------------------------------------------------------------
    # Class method: load parts from a compiled build directory
    # ------------------------------------------------------------------

    @classmethod
    def from_build_dir(
        cls,
        build_dir: Path | str,
        workspace_id: str,
        mock: bool = True,
    ) -> "FabricDeployer":
        """Construct a deployer by reading parts from a compiled build directory.

        Reads ``{build_dir}/definition.json`` which contains all parts with
        their InlineBase64 payloads.

        Parameters
        ----------
        build_dir:
            Path to the compiled ontology directory (e.g. ``build/ontology``).
        workspace_id:
            Target Fabric workspace GUID.
        mock:
            See class docstring.

        Raises
        ------
        FileNotFoundError
            When ``{build_dir}/definition.json`` does not exist.
        """
        manifest = Path(build_dir) / "definition.json"
        if not manifest.exists():
            raise FileNotFoundError(
                f"definition.json not found in {build_dir}. "
                "Run `compile-ontology` first."
            )
        definition = json.loads(manifest.read_text(encoding="utf-8"))
        parts: list[dict[str, Any]] = definition.get("parts", [])
        return cls(workspace_id=workspace_id, parts=parts, mock=mock)
