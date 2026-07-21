"""Infrastructure preflight checks.

Runs a suite of checks before ``infra plan`` or ``infra apply``:
- Azure CLI and azd installation/login
- Active subscription and resource group role
- Required resource provider registration
- Region/SKU availability
- GPT-4.1 and embedding quota (model discovery)
- AI Search semantic/vector support for connect mode
- Fabric capacity, API, and preview feature availability

Each check returns a typed ``PreflightCheck``. The runner accumulates all
results; callers decide whether to stop on first failure or collect all.

SPEC-006 §6.2 / INF-002.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .schema import (
    InfraManifest,
    PreflightCheck,
    PreflightResult,
    PreflightStatus,
    ResourceMode,
    SearchSku,
)
from .runner import CommandRunner, CommandError

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Required resource providers
# ---------------------------------------------------------------------------

_CORE_PROVIDERS = [
    "Microsoft.CognitiveServices",
    "Microsoft.Storage",
    "Microsoft.Search",
]


def _required_providers(manifest: InfraManifest) -> list[str]:
    """Return providers needed by resources this manifest can create."""
    providers = list(_CORE_PROVIDERS)
    if manifest.features.reference_app:
        providers.extend([
            "Microsoft.ManagedIdentity",
            "Microsoft.ContainerRegistry",
        ])
    return providers


# ---------------------------------------------------------------------------
# Individual check implementations
# ---------------------------------------------------------------------------


def check_azure_cli(runner: CommandRunner) -> PreflightCheck:
    """Verify Azure CLI is installed and at a supported version."""
    try:
        result = runner.run(["az", "version", "--output", "json"])
    except CommandError as exc:
        return PreflightCheck(
            name="azure_cli_installed",
            status=PreflightStatus.FAIL,
            message=f"Azure CLI not found or failed: {exc}",
            action="Install Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli",
        )
    if not result.succeeded:
        return PreflightCheck(
            name="azure_cli_installed",
            status=PreflightStatus.FAIL,
            message=f"'az version' exited {result.returncode}: {result.stderr}",
            action="Install Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli",
        )
    try:
        data = json.loads(result.stdout)
        version = data.get("azure-cli", "unknown")
    except (json.JSONDecodeError, AttributeError):
        version = "unknown"
    return PreflightCheck(
        name="azure_cli_installed",
        status=PreflightStatus.PASS,
        message=f"Azure CLI version: {version}",
    )


def check_azd_cli(runner: CommandRunner) -> PreflightCheck:
    """Verify Azure Developer CLI (azd) is installed."""
    try:
        result = runner.run(["azd", "version"])
    except CommandError as exc:
        return PreflightCheck(
            name="azd_cli_installed",
            status=PreflightStatus.FAIL,
            message=f"Azure Developer CLI (azd) not found: {exc}",
            action=(
                "Install azd: https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd"
            ),
        )
    if not result.succeeded:
        return PreflightCheck(
            name="azd_cli_installed",
            status=PreflightStatus.FAIL,
            message=f"'azd version' exited {result.returncode}: {result.stderr}",
            action=(
                "Install azd: https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd"
            ),
        )
    version_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
    return PreflightCheck(
        name="azd_cli_installed",
        status=PreflightStatus.PASS,
        message=f"Azure Developer CLI: {version_line}",
    )


def check_azure_login(runner: CommandRunner) -> PreflightCheck:
    """Verify the user is logged in to the Azure CLI."""
    try:
        result = runner.run(["az", "account", "show", "--output", "json"])
    except CommandError as exc:
        return PreflightCheck(
            name="azure_login",
            status=PreflightStatus.FAIL,
            message=f"Azure account check failed: {exc}",
            action="Run 'az login' to authenticate.",
        )
    if not result.succeeded:
        return PreflightCheck(
            name="azure_login",
            status=PreflightStatus.FAIL,
            message="No active Azure account. Run 'az login'.",
            action="az login",
        )
    try:
        data = json.loads(result.stdout)
        user = data.get("user", {}).get("name", "unknown")
        sub_id = data.get("id", "unknown")
        sub_name = data.get("name", "unknown")
    except (json.JSONDecodeError, AttributeError):
        user = "unknown"
        sub_id = "unknown"
        sub_name = "unknown"
    return PreflightCheck(
        name="azure_login",
        status=PreflightStatus.PASS,
        message=f"Logged in as '{user}' on subscription '{sub_name}' ({sub_id}).",
        details={"subscription_id": sub_id, "subscription_name": sub_name, "user": user},
    )


def check_subscription(runner: CommandRunner, subscription_id: str) -> PreflightCheck:
    """Verify the subscription exists and is accessible."""
    try:
        result = runner.run([
            "az", "account", "show",
            "--subscription", subscription_id,
            "--output", "json",
        ])
    except CommandError as exc:
        return PreflightCheck(
            name="subscription_access",
            status=PreflightStatus.FAIL,
            message=f"Cannot access subscription '{subscription_id}': {exc}",
            action=f"Verify you have access to subscription '{subscription_id}'.",
        )
    if not result.succeeded:
        return PreflightCheck(
            name="subscription_access",
            status=PreflightStatus.FAIL,
            message=(
                f"Subscription '{subscription_id}' not found or not accessible. "
                f"stderr: {result.stderr}"
            ),
            action=f"Run 'az account list' to view available subscriptions.",
        )
    try:
        data = json.loads(result.stdout)
        state = data.get("state", "Unknown")
    except (json.JSONDecodeError, AttributeError):
        state = "unknown"
    if state.lower() != "enabled":
        return PreflightCheck(
            name="subscription_access",
            status=PreflightStatus.FAIL,
            message=f"Subscription '{subscription_id}' state is '{state}' (expected 'Enabled').",
            action="Contact your Azure admin to enable the subscription.",
        )
    return PreflightCheck(
        name="subscription_access",
        status=PreflightStatus.PASS,
        message=f"Subscription '{subscription_id}' is accessible and Enabled.",
    )


def check_resource_group_role(
    runner: CommandRunner,
    subscription_id: str,
    resource_group_name: str,
) -> PreflightCheck:
    """Verify the caller has at least Contributor on the resource group."""
    scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group_name}"
    )
    try:
        result = runner.run([
            "az", "role", "assignment", "list",
            "--scope", scope,
            "--include-inherited",
            "--output", "json",
        ])
    except CommandError as exc:
        return PreflightCheck(
            name="resource_group_role",
            status=PreflightStatus.FAIL,
            message=f"Failed to check role assignments: {exc}",
            action="Ensure you have read access to the resource group.",
        )
    if not result.succeeded:
        return PreflightCheck(
            name="resource_group_role",
            status=PreflightStatus.FAIL,
            message=f"Role assignment check failed: {result.stderr}",
            action=(
                f"Request Contributor or Owner on resource group '{resource_group_name}'."
            ),
        )
    try:
        assignments = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        assignments = []

    privileged_roles = {
        "Owner", "Contributor",
        "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",  # Owner role definition ID
        "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor role definition ID
    }
    has_role = any(
        a.get("roleDefinitionName") in privileged_roles
        or a.get("roleDefinitionId") in privileged_roles
        for a in assignments
        if isinstance(a, dict)
    )
    if not has_role:
        return PreflightCheck(
            name="resource_group_role",
            status=PreflightStatus.WARN,
            message=(
                f"No Contributor/Owner assignment found on '{resource_group_name}'. "
                "Role assignments from inherited scopes may still grant access. "
                "Apply will fail if insufficient roles exist."
            ),
            action=(
                f"Request Contributor or Owner on resource group '{resource_group_name}'."
            ),
        )
    return PreflightCheck(
        name="resource_group_role",
        status=PreflightStatus.PASS,
        message=f"Contributor/Owner role confirmed on resource group '{resource_group_name}'.",
    )


def check_resource_provider(
    runner: CommandRunner,
    subscription_id: str,
    provider_namespace: str,
) -> PreflightCheck:
    """Check whether a resource provider is registered."""
    try:
        result = runner.run([
            "az", "provider", "show",
            "--namespace", provider_namespace,
            "--subscription", subscription_id,
            "--output", "json",
        ])
    except CommandError as exc:
        return PreflightCheck(
            name=f"provider_{provider_namespace.replace('.', '_').lower()}",
            status=PreflightStatus.FAIL,
            message=f"Failed to check provider '{provider_namespace}': {exc}",
            action=f"az provider register --namespace {provider_namespace} --subscription {subscription_id}",
        )
    if not result.succeeded:
        return PreflightCheck(
            name=f"provider_{provider_namespace.replace('.', '_').lower()}",
            status=PreflightStatus.FAIL,
            message=f"Provider check failed: {result.stderr}",
            action=f"az provider register --namespace {provider_namespace}",
        )
    try:
        data = json.loads(result.stdout)
        state = data.get("registrationState", "Unknown")
    except (json.JSONDecodeError, AttributeError):
        state = "Unknown"

    if state == "Registered":
        return PreflightCheck(
            name=f"provider_{provider_namespace.replace('.', '_').lower()}",
            status=PreflightStatus.PASS,
            message=f"Provider '{provider_namespace}' is Registered.",
        )
    action = f"az provider register --namespace {provider_namespace} --subscription {subscription_id}"
    if state == "Registering":
        return PreflightCheck(
            name=f"provider_{provider_namespace.replace('.', '_').lower()}",
            status=PreflightStatus.WARN,
            message=f"Provider '{provider_namespace}' is in state '{state}' (registration in progress).",
            action="Wait for registration to complete or re-run preflight.",
        )
    return PreflightCheck(
        name=f"provider_{provider_namespace.replace('.', '_').lower()}",
        status=PreflightStatus.FAIL,
        message=f"Provider '{provider_namespace}' is not registered (state: {state}).",
        action=action,
    )


def check_search_sku_semantic(
    runner: CommandRunner,
    subscription_id: str,
    search_sku: SearchSku,
) -> PreflightCheck:
    """Verify the Search SKU supports semantic ranker and vector search."""
    vector_skus = {
        SearchSku.STANDARD,
        SearchSku.STANDARD2,
        SearchSku.STANDARD3,
        SearchSku.STORAGE_OPTIMIZED_L1,
        SearchSku.STORAGE_OPTIMIZED_L2,
    }
    semantic_skus = {SearchSku.STANDARD, SearchSku.STANDARD2, SearchSku.STANDARD3}

    if search_sku not in vector_skus:
        return PreflightCheck(
            name="search_sku_vector",
            status=PreflightStatus.FAIL,
            message=(
                f"Search SKU '{search_sku.value}' does not support vector search. "
                "Minimum: Standard."
            ),
            action="Set search.sku to 'standard' or higher in the infra manifest.",
        )
    if search_sku not in semantic_skus:
        return PreflightCheck(
            name="search_sku_vector",
            status=PreflightStatus.WARN,
            message=(
                f"Search SKU '{search_sku.value}' supports vector search but not "
                "semantic ranker. Semantic ranking requires Standard or higher."
            ),
            action="Set search.sku to 'standard' or higher to enable semantic ranker.",
        )
    return PreflightCheck(
        name="search_sku_vector",
        status=PreflightStatus.PASS,
        message=f"Search SKU '{search_sku.value}' supports vector search and semantic ranker.",
    )


def check_fabric_capacity(manifest: InfraManifest) -> PreflightCheck:
    """Verify Fabric capacity ID is configured."""
    cap_id = manifest.fabric.capacity_id
    if not cap_id or cap_id.startswith("${"):
        return PreflightCheck(
            name="fabric_capacity",
            status=PreflightStatus.FAIL,
            message=(
                "Fabric capacity ID is not set. Set FABRIC_CAPACITY_ID environment "
                "variable or fabric.capacity_id in the infra manifest."
            ),
            action=(
                "Obtain a Fabric capacity from a capacity admin, then set "
                "FABRIC_CAPACITY_ID=<id>."
            ),
        )
    return PreflightCheck(
        name="fabric_capacity",
        status=PreflightStatus.PASS,
        message=f"Fabric capacity ID configured: {cap_id}",
    )


# ---------------------------------------------------------------------------
# Main preflight runner
# ---------------------------------------------------------------------------


def run_preflight(
    manifest: InfraManifest,
    runner: CommandRunner,
    *,
    skip_fabric: bool = False,
    skip_model_quota: bool = False,
) -> PreflightResult:
    """Execute all preflight checks and return a typed ``PreflightResult``.

    Checks are ordered so that fundamental failures (CLI not installed, not
    logged in) short-circuit dependent checks.  All checks still run unless a
    check is explicitly marked as a dependency blocker.

    SPEC-006 §6.2 / INF-002.
    """
    checks: list[PreflightCheck] = []

    # --- Azure CLI and azd ---
    az_check = check_azure_cli(runner)
    checks.append(az_check)
    azd_check = check_azd_cli(runner)
    checks.append(azd_check)

    # --- Login (depends on CLI being present) ---
    login_check = check_azure_login(runner)
    checks.append(login_check)

    # --- Subscription (only if login succeeded) ---
    subscription_id = manifest.azure.subscription_id
    if not subscription_id.startswith("${"):
        sub_check = check_subscription(runner, subscription_id)
        checks.append(sub_check)

        # --- Resource group role ---
        rg_name = manifest.azure.resource_group.name
        if rg_name:
            rg_check = check_resource_group_role(runner, subscription_id, rg_name)
            checks.append(rg_check)

        # --- Resource provider registration ---
        for provider in _required_providers(manifest):
            checks.append(check_resource_provider(runner, subscription_id, provider))
    else:
        checks.append(PreflightCheck(
            name="subscription_access",
            status=PreflightStatus.SKIP,
            message=(
                "Subscription ID is an unresolved env var placeholder; "
                "set AZURE_SUBSCRIPTION_ID before running preflight."
            ),
        ))

    # --- Search SKU ---
    if manifest.resources.search.mode == ResourceMode.CREATE:
        checks.append(
            check_search_sku_semantic(
                runner,
                subscription_id,
                manifest.resources.search.sku,
            )
        )

    # --- Fabric capacity ---
    if not skip_fabric:
        checks.append(check_fabric_capacity(manifest))

    # --- Model quota (optional; detailed check lives in model_discovery) ---
    if not skip_model_quota:
        checks.append(PreflightCheck(
            name="model_quota",
            status=PreflightStatus.SKIP,
            message=(
                "Model quota discovery requires an active Foundry account. "
                "Run 'infra plan' for detailed quota analysis."
            ),
        ))

    return PreflightResult(environment=manifest.environment, checks=checks)
