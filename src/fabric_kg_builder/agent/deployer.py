"""agent/deployer.py — Foundry prompt-agent deployment lifecycle.

Mandatory lifecycle contracts:
  1. Load & validate agent-metadata.yaml (source of truth).
  2. Validate the current agent schema via the live client.
  3. Build versioned routing instructions (hash stored for audit).
  4. Create/update the prompt-agent definition; persist the returned agent_id.
  5. Verify agent readiness (check_ready).
  6. Run smoke prompt; raise on failure.
  6.5. Run the declared testCases regression battery N times each and gate
       on required cases (issue #138) — see agent/regression_battery.py.
       A single successful run is not sound evidence for this agent's
       multi-step tool-invocation behavior; only a repeat-N pass counts.
  7. Persist deploymentContext ONLY on live success — merge selected env,
     never overwrite other envs or testCases.
  8. Dry-run: validate only, NEVER persist deploymentContext (also skips the
     regression battery, since dry-run never reaches a live client).

No real cloud calls during tests — inject a FakeAgentTransport.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from fabric_kg_builder.agent.instructions import build_routing_instructions, INSTRUCTIONS_VERSION
from fabric_kg_builder.agent.metadata import AgentMetadata, load_agent_metadata
from fabric_kg_builder.agent.regression_battery import (
    DEFAULT_REPEAT,
    RegressionBatteryError,
    TestCaseBatteryResult,
    enforce_battery_gate,
    run_test_case_battery,
)

_METADATA_PATH = Path(".foundry") / "agent-metadata.yaml"

# Azure AI Search query modes accepted for the grounding tool. The modes in
# _VECTOR_QUERY_TYPES need an integrated vectorizer on the index; when no
# override is given they are selected only after a live probe confirms one
# (issue #121). The allowed set exists so that a typo in an explicit override
# is rejected up front instead of reaching the service.
_VECTOR_QUERY_TYPES = frozenset({"vector", "vector_simple_hybrid", "vector_semantic_hybrid"})
_ALLOWED_QUERY_TYPES = frozenset({"simple", "full", "semantic"}) | _VECTOR_QUERY_TYPES
_DEFAULT_QUERY_TYPE = "semantic"
_SMOKE_PROMPT = (
    "Hello, are you available? "
    "Reply with route_type: search and confirm you are ready."
)


class DeploymentError(Exception):
    """Raised when agent deployment fails at any step."""


class DeploymentContext:
    """Deployment result persisted to agent-metadata.yaml.deploymentContext.<env>."""

    def __init__(
        self,
        *,
        environment: str,
        agent_name: str,
        deployed_at: str,
        model_deployment: str,
        instructions_version: str,
        instructions_hash: str,
        schema_valid: bool,
        agent_ready: bool,
        smoke_passed: bool,
        agent_version_id: str = "",
        agent_version: str = "",
        image_tag: str = "",
        test_battery: list[TestCaseBatteryResult] | None = None,
    ) -> None:
        self.environment = environment
        self.agent_name = agent_name
        self.deployed_at = deployed_at
        self.model_deployment = model_deployment
        self.instructions_version = instructions_version
        self.instructions_hash = instructions_hash
        self.schema_valid = schema_valid
        self.agent_ready = agent_ready
        self.smoke_passed = smoke_passed
        self.agent_version_id = agent_version_id
        self.agent_version = agent_version
        self.image_tag = image_tag
        # Regression-battery results (issue #138) — reported via CLI but
        # intentionally NOT included in to_dict()/persisted YAML, so the
        # deploymentContext schema on disk is unchanged by this feature.
        self.test_battery: list[TestCaseBatteryResult] = test_battery or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "agent_name": self.agent_name,
            "deployed_at": self.deployed_at,
            "model_deployment": self.model_deployment,
            "instructions_version": self.instructions_version,
            "instructions_hash": self.instructions_hash,
            "schema_valid": self.schema_valid,
            "agent_ready": self.agent_ready,
            "smoke_passed": self.smoke_passed,
            "agent_version_id": self.agent_version_id,
            "agent_version": self.agent_version,
            "image_tag": self.image_tag,
        }


def _timestamp_tag() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")


def _hash_instructions(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _merge_deployment_context(
    metadata_path: Path,
    environment: str,
    context: DeploymentContext,
) -> None:
    """Merge deploymentContext.<environment> into agent-metadata.yaml.

    Contract:
      - Only updates deploymentContext.<environment>.
      - Never overwrites other environments, testCases, or environments blocks.
      - Never called from dry-run paths.
    """
    if not metadata_path.exists():
        return
    raw_text = metadata_path.read_text(encoding="utf-8")
    raw = yaml.safe_load(raw_text) or {}

    deploy_ctx = raw.get("deploymentContext") or {}
    if not isinstance(deploy_ctx, dict):
        deploy_ctx = {}
    deploy_ctx[environment] = context.to_dict()
    raw["deploymentContext"] = deploy_ctx

    metadata_path.write_text(
        yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def deploy_agent(
    *,
    environment: str | None = None,
    _client: Any | None = None,
    metadata_path: str | Path | None = None,
    entity_types: list[str] | None = None,
    relationship_types: list[str] | None = None,
    domain_context: str | None = None,
    dry_run: bool = False,
    smoke_timeout_s: int = 60,
    require_grounding_tools: bool = False,
    regression_repeat: int = DEFAULT_REPEAT,
    skip_regression_battery: bool = False,
) -> DeploymentContext:
    """Deploy the Foundry prompt-agent for the given environment.

    Steps (live mode, not dry-run):
      1. Load agent-metadata.yaml.
      2. Build live FoundryAgentClient (via DefaultAzureCredential) unless
         ``_client`` is injected.
      3. Validate current agent schema (GET existing).
      4. Build versioned routing instructions and hash.
      5. Create/update the agent definition; capture returned agent_id.
      6. Verify agent readiness.
      7. Invoke smoke prompt; fail on error/timeout.
      8. Persist deploymentContext by MERGING into the selected environment.

    Dry-run:
      - Steps 1, 3 (schema fetch), 4 only.
      - NEVER persists deploymentContext.
      - Returns DeploymentContext with schema_valid but agent_ready=False,
        smoke_passed=False, agent_version_id="" to signal nothing was deployed.

    Args:
        environment:    Target environment (default: metadata.defaultEnvironment).
        _client:        Pre-built FoundryAgentClient or FakeAgentTransport-based
                        client. When None (live mode), built from metadata.
        metadata_path:  Override path to agent-metadata.yaml.
        entity_types:   Optional entity types for instruction grounding.
        relationship_types: Optional relationship types for instruction
            grounding, so the agent traverses real edge names.
        dry_run:        Validate and plan only; do not deploy or persist.
        smoke_timeout_s: Seconds to wait for smoke run.
        regression_repeat: Times to repeat EACH declared testCase (issue
            #138 — a single run is not sound evidence; default 3). A
            testCase's own ``repeat`` field, if set, overrides this.
        skip_regression_battery: Skip the testCase battery entirely (e.g.
            for fast local iteration). Never set true for a real live
            deploy — this is the gate that catches known-regression classes
            like the unsupported-gate routing bug before they ship.

    Returns:
        DeploymentContext (non-empty agent_version_id only after live success).

    Raises:
        DeploymentError: On any step failure.
    """
    from fabric_kg_builder.agent.foundry_agent_client import (
        FoundryAgentClient,
        build_client_from_metadata,
    )

    md_path = Path(metadata_path) if metadata_path else _METADATA_PATH
    metadata: AgentMetadata = load_agent_metadata(md_path)
    env = environment or metadata.defaultEnvironment
    env_cfg = metadata.env_config(env)
    model_deployment = env_cfg.deployments.get("chat", metadata.model.deploymentName)
    search_connection_id = env_cfg.connections.get("search", "")
    fabric_connection_id = env_cfg.connections.get("fabricDataAgent", "")
    knowledge_connection_id = env_cfg.connections.get("knowledgeBase", "")
    search_index_name = str(env_cfg.knowledge.get("searchIndexName", ""))
    # v1.8: an optional second Azure AI Search index on the SAME connection,
    # e.g. an image/visual-assets index searched alongside the primary
    # evidence index so the agent can surface image citations.
    visual_assets_index_name = str(
        env_cfg.knowledge.get("visualAssetsIndexName", "")
    ).strip()
    knowledge_base_name = str(env_cfg.knowledge.get("knowledgeBaseName", ""))
    knowledge_mcp_endpoint = str(
        env_cfg.knowledge.get("knowledgeBaseMcpEndpoint", "")
    )

    # query_type is resolved after the live client is available (see Step 2.5
    # below) so it can be auto-detected against the real index schema; the
    # placeholder here is only used if that resolution step never runs
    # (e.g. dry-run short-circuit).
    search_query_type_override = str(
        env_cfg.knowledge.get("searchQueryType", "")
    ).strip()
    if search_query_type_override and search_query_type_override not in _ALLOWED_QUERY_TYPES:
        raise DeploymentError(
            "environments.<env>.knowledge.searchQueryType is "
            f"{search_query_type_override!r}. Allowed: "
            f"{', '.join(sorted(_ALLOWED_QUERY_TYPES))}."
        )

    tool_specs: list[dict[str, Any]] = []
    if search_connection_id and search_index_name:
        tool_specs.append({
            "type": "azure_ai_search",
            "tool_name": "azure_ai_search_evidence",
            "project_connection_id": search_connection_id,
            "index_name": search_index_name,
            "query_type": search_query_type_override or _DEFAULT_QUERY_TYPE,
            "top_k": 5,
        })
    if search_connection_id and visual_assets_index_name:
        # v1.8: a second, independent azure_ai_search tool for the
        # visual-assets (image) index. The live Foundry service rejects more
        # than one entry in a single AzureAISearchToolResource.indexes list
        # ("Array length 2 exceeds maximum 1"), so this MUST be a separate
        # tool_spec/tool object rather than a second index under the primary
        # search tool. Each azure_ai_search tool object must also have a
        # UNIQUE tool_name — without it, both tools default to the same
        # underlying "azure_ai_search" tool-call argument name and the model
        # invocation fails with "Duplicate tool argument name: 'azure_ai_search'".
        tool_specs.append({
            "type": "azure_ai_search",
            "tool_name": "azure_ai_search_visual_assets",
            "project_connection_id": search_connection_id,
            "index_name": visual_assets_index_name,
            "query_type": search_query_type_override or _DEFAULT_QUERY_TYPE,
            "top_k": 5,
        })
    if fabric_connection_id:
        tool_specs.append({
            "type": "fabric_data_agent",
            "project_connection_id": fabric_connection_id,
        })
    if (
        knowledge_connection_id
        and knowledge_base_name
        and knowledge_mcp_endpoint
    ):
        tool_specs.append({
            "type": "mcp",
            "server_label": "knowledge-base",
            "server_url": knowledge_mcp_endpoint,
            "project_connection_id": knowledge_connection_id,
            "require_approval": "never",
            "allowed_tools": ["knowledge_base_retrieve"],
        })
    if require_grounding_tools:
        missing: list[str] = []
        # A search/knowledge grounding leg is required, but it may be
        # satisfied by EITHER the plain azure_ai_search tool (connections.search
        # + knowledge.searchIndexName) OR the Foundry IQ Knowledge Base MCP
        # tool (connections.knowledgeBase + knowledge.knowledgeBaseName +
        # knowledge.knowledgeBaseMcpEndpoint), which supersedes it — the KB
        # tool spans multiple indexes (evidence + visual-assets) via agentic
        # retrieval, so it is not a downgrade from the single-index tool.
        has_plain_search = bool(search_connection_id and search_index_name)
        has_knowledge_base = bool(
            knowledge_connection_id and knowledge_base_name and knowledge_mcp_endpoint
        )
        if not has_plain_search and not has_knowledge_base:
            missing.append(
                "either (environments.<env>.connections.search and "
                "knowledge.searchIndexName) or (connections.knowledgeBase and "
                "knowledge.knowledgeBaseName and knowledge.knowledgeBaseMcpEndpoint)"
            )
        if not fabric_connection_id:
            missing.append(
                "environments.<env>.connections.fabricDataAgent "
                "(create the Microsoft Fabric project connection in Foundry)"
            )
        if missing:
            raise DeploymentError(
                "Grounded agent deployment is blocked because required tool "
                f"configuration is missing: {'; '.join(missing)}"
            )

    instructions = build_routing_instructions(
        version=INSTRUCTIONS_VERSION,
        entity_types=entity_types,
        relationship_types=relationship_types,
        domain_context=domain_context,
    )
    instructions_hash = _hash_instructions(instructions)
    image_tag = _timestamp_tag()

    # -- Step 2: build client --------------------------------------------------
    if _client is None:
        if dry_run:
            # Dry-run with no injected client: validate metadata only.
            return DeploymentContext(
                environment=env,
                agent_name=metadata.agentName,
                deployed_at="",
                model_deployment=model_deployment,
                instructions_version=INSTRUCTIONS_VERSION,
                instructions_hash=instructions_hash,
                schema_valid=True,
                agent_ready=False,
                smoke_passed=False,
                agent_version_id="",
                image_tag=image_tag,
            )
        # Live mode: build from metadata + DefaultAzureCredential.
        try:
            client = build_client_from_metadata(metadata, env)
        except Exception as exc:
            raise DeploymentError(f"Cannot build Foundry client: {exc}") from exc
    else:
        client = _client

    # -- Step 3: validate schema -----------------------------------------------
    try:
        schema_resp = client.validate_schema(metadata.agentName)
        schema_valid = bool(schema_resp.get("valid", True))
        if not schema_valid:
            raise DeploymentError(
                f"Agent schema validation failed: {schema_resp}"
            )
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError(f"Schema validation error: {exc}") from exc

    if dry_run:
        # Schema fetched; do NOT create/update or persist.
        return DeploymentContext(
            environment=env,
            agent_name=metadata.agentName,
            deployed_at="",
            model_deployment=model_deployment,
            instructions_version=INSTRUCTIONS_VERSION,
            instructions_hash=instructions_hash,
            schema_valid=schema_valid,
            agent_ready=False,
            smoke_passed=False,
            agent_version_id="",
            image_tag=image_tag,
        )

    # -- Step 3.5: resolve AI Search query_type (auto-detect vs override) ------
    # vector_semantic_hybrid/vector_simple_hybrid require an integrated
    # vectorizer on the index; without one they fail at invocation time with
    # a 400 error even though deployment succeeds (issue #121). Precedence:
    #   1. explicit env_cfg.knowledge.searchQueryType override
    #   2. live probe of the actual index schema (best-effort)
    #   3. safe default "semantic" (works without a vectorizer)
    # v1.8: there may be MORE THAN ONE azure_ai_search tool_spec (the primary
    # evidence index plus an optional visual-assets index) — each is probed
    # and resolved independently by its own index_name, since a different
    # index on the same connection may or may not have its own integrated
    # vectorizer; one index's probe result must never be assumed for another.
    if not search_query_type_override and search_connection_id:
        probe = getattr(client, "index_has_integrated_vectorizer", None)
        for spec in tool_specs:
            if spec.get("type") != "azure_ai_search":
                continue
            spec_conn = spec.get("project_connection_id", search_connection_id)
            spec_index_name = spec.get("index_name", "")
            if not spec_index_name:
                continue
            detected = (
                probe(spec_conn, spec_index_name) if callable(probe) else None
            )
            spec["query_type"] = (
                "vector_semantic_hybrid" if detected else _DEFAULT_QUERY_TYPE
            )

    # -- Step 4: agent definition ----------------------------------------------
    agent_definition = {
        "name": metadata.agentName,
        "description": f"Fabric KG grounded agent — {env}",
        "model": model_deployment,
        "system_prompt": instructions,
        "system_prompt_version": INSTRUCTIONS_VERSION,
        "temperature": metadata.promptAgent.temperature,
        "max_tokens": metadata.promptAgent.maxTokens,
        "top_p": metadata.promptAgent.topP,
        "seed": metadata.promptAgent.seed,
        "instructions_hash": instructions_hash,
        "image_tag": image_tag,
        "tools": tool_specs,
    }

    # -- Step 5: create/update (create_version) --------------------------------
    try:
        create_resp = client.create_or_update_agent(agent_definition)
        # 2.x API returns id / name / version from create_version
        agent_version_id = create_resp.get("version_id") or create_resp.get("id") or ""
        agent_version = str(create_resp.get("version", ""))
        if not agent_version_id:
            raise DeploymentError(
                "create_or_update_agent returned no agent id. "
                f"Response: {create_resp}"
            )
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError(f"Agent create_version failed: {exc}") from exc

    # -- Step 6: readiness check -----------------------------------------------
    try:
        agent_ready = client.check_ready(metadata.agentName)
        if not agent_ready:
            raise DeploymentError(
                f"Agent '{metadata.agentName}' is not ready after deployment. "
                "Check the Foundry project for errors."
            )
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError(f"Readiness check failed: {exc}") from exc

    # -- Step 7: smoke prompt (2.x: response.output_text) ─────────────────────
    try:
        smoke_resp = client.invoke(metadata.agentName, _SMOKE_PROMPT)
        # 2.x SDK path returns output_text; fallback to answer for fake transport
        answer = (
            smoke_resp.get("output_text")
            or smoke_resp.get("answer")
            or smoke_resp.get("content")
            or ""
        )
        if not answer:
            raise DeploymentError(
                "Smoke prompt returned empty answer. Agent may not be functional."
            )
        smoke_passed = True
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError(f"Smoke prompt failed: {exc}") from exc

    # -- Step 7.5: regression battery (issue #138) ─────────────────────────────
    # Runs every declared testCase `regression_repeat` times (default 3) and
    # classifies pass/fail/flaky. A required test case that is not N/N
    # aborts the deploy — this is the gate that would have caught the
    # unsupported-gate routing regression before it shipped, since a single
    # lucky run is not sound evidence of correct behavior (see issue #138:
    # one query observed a 1/5 pass rate across identical reruns).
    test_battery: list[TestCaseBatteryResult] = []
    if metadata.testCases and not skip_regression_battery:
        test_battery = run_test_case_battery(
            client,
            metadata.agentName,
            metadata.testCases,
            default_repeat=regression_repeat,
        )
        try:
            enforce_battery_gate(test_battery)
        except RegressionBatteryError as exc:
            raise DeploymentError(f"Regression battery gate failed: {exc}") from exc

    ctx = DeploymentContext(
        environment=env,
        agent_name=metadata.agentName,
        deployed_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        model_deployment=model_deployment,
        instructions_version=INSTRUCTIONS_VERSION,
        instructions_hash=instructions_hash,
        schema_valid=schema_valid,
        agent_ready=agent_ready,
        smoke_passed=smoke_passed,
        agent_version_id=agent_version_id,
        agent_version=agent_version,
        image_tag=image_tag,
        test_battery=test_battery,
    )

    # -- Step 8: persist (MERGE, never overwrite) ------------------------------
    try:
        _merge_deployment_context(md_path, env, ctx)
    except Exception as exc:
        # Log but do not fail the deployment — the agent IS deployed.
        import warnings
        warnings.warn(f"Failed to persist deploymentContext: {exc}", stacklevel=2)

    return ctx
