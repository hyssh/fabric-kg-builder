"""CLI commands for M3 infrastructure provisioning.

Exposes:
  fabric-kg infra preflight --env <env>
  fabric-kg infra plan      --env <env> [--out <path>]
  fabric-kg infra apply     --env <env> [--auto-approve] [--dry-run]
  fabric-kg infra status    --env <env>
  fabric-kg infra connect   --env <env> --resource <kind> --id <arm-or-fabric-id>
  fabric-kg infra destroy   --env <env> --target <name> [--confirm]

All cloud-mutating commands (apply, connect, destroy) perform no live Azure
operations when --dry-run is specified or when a FakeCommandRunner is injected.

SPEC-006 §4 / INF-001..INF-019.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_INFRA_DIR = "infra"
_DEFAULT_BUILD_ROOT = "build"


def _get_manifest_path(env: str, infra_dir: str) -> Path:
    return Path(infra_dir) / "environments" / f"{env}.yaml"


def _load_manifest_or_fail(env: str, infra_dir: str):
    """Load and validate the infra manifest or raise ClickException."""
    from fabric_kg_builder.infra.manifest import (
        load_manifest,
        InfraManifestError,
        InfraManifestParseError,
        InfraManifestValidationError,
    )

    path = _get_manifest_path(env, infra_dir)
    if not path.exists():
        raise click.ClickException(
            f"Infra manifest not found: '{path}'. "
            f"Create it from the example: cp infra/environments/dev.yaml {path}"
        )
    try:
        return load_manifest(path)
    except InfraManifestParseError as exc:
        raise click.ClickException(f"YAML parse error: {exc}") from exc
    except InfraManifestValidationError as exc:
        raise click.ClickException(f"Manifest validation failed:\n{exc}") from exc
    except InfraManifestError as exc:
        raise click.ClickException(str(exc)) from exc


def _make_runner():
    """Return a RealCommandRunner for live operations."""
    from fabric_kg_builder.infra.runner import RealCommandRunner
    return RealCommandRunner()


def _make_fabric_transport():
    from fabric_kg_builder.infra.fabric_client import (
        DefaultAzureCredentialFabricTransport,
    )

    return DefaultAzureCredentialFabricTransport()


def _print_preflight_result(result, *, json_output: bool) -> None:
    """Emit preflight results to stdout."""
    from fabric_kg_builder.infra.schema import PreflightStatus

    if json_output:
        click.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        return

    click.echo(f"[infra preflight] environment: {result.environment}")
    for check in result.checks:
        icon = {
            PreflightStatus.PASS: "✓",
            PreflightStatus.FAIL: "✗",
            PreflightStatus.WARN: "⚠",
            PreflightStatus.SKIP: "–",
        }.get(check.status, "?")
        click.echo(f"  {icon} {check.name:<40} {check.message}")
        if check.action and check.status in (PreflightStatus.FAIL, PreflightStatus.WARN):
            click.echo(f"    → {check.action}", err=True)
    click.echo()
    if result.passed:
        click.echo("[infra preflight] All checks passed.")
    else:
        failed = [c.name for c in result.failed_checks]
        click.echo(
            f"[infra preflight] {len(failed)} check(s) failed: {', '.join(failed)}",
            err=True,
        )


def _print_plan(plan, *, json_output: bool) -> None:
    """Emit plan to stdout."""
    if json_output:
        click.echo(json.dumps(plan.model_dump(mode="json"), indent=2))
        return

    click.echo(f"[infra plan] environment: {plan.environment}")
    click.echo(f"[infra plan] items: {len(plan.items)}")
    for item in plan.items:
        cost = " [COST]" if item.cost_bearing else ""
        sku = f" ({item.sku})" if item.sku else ""
        click.echo(f"  {item.action.value:<10} {item.resource_type}/{item.resource_name}{sku}{cost}")
        for w in item.warnings:
            click.echo(f"    ⚠ {w}")
    if plan.rbac_assignments:
        click.echo(f"[infra plan] RBAC assignments: {len(plan.rbac_assignments)}")
        for a in plan.rbac_assignments:
            click.echo(f"  {a.principal_type:<20} {a.role_name:<50} {a.scope}")
    if plan.cost_bearing_skus:
        click.echo(f"[infra plan] Cost-bearing SKUs: {', '.join(plan.cost_bearing_skus)}")
    if plan.prereqs:
        click.echo(f"[infra plan] Prerequisites: {', '.join(plan.prereqs)}")
    for w in plan.warnings:
        click.echo(f"  ⚠ {w}")


# ---------------------------------------------------------------------------
# infra command group
# ---------------------------------------------------------------------------


@click.group(
    "infra",
    context_settings={"max_content_width": 120},
)
def infra_cmd() -> None:
    """Provision or adopt Azure and Fabric infrastructure.

    \b
    Workflow:
      fabric-kg infra preflight --env dev   Verify prerequisites
      fabric-kg infra plan      --env dev   Show what will be created/adopted
      fabric-kg infra apply     --env dev   Provision resources
      fabric-kg infra status    --env dev   Show last operation state
      fabric-kg infra connect   --env dev   Adopt an existing resource
      fabric-kg infra destroy   --env dev   Remove owned resources

    \b
    Environment manifests live in infra/environments/<env>.yaml.
    See infra/environments/dev.yaml for an example.
    """


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@infra_cmd.command("preflight")
@click.option("--env", default="dev", show_default=True,
              help="Target environment (matches infra/environments/<env>.yaml).")
@click.option("--infra-dir", default=_DEFAULT_INFRA_DIR, show_default=True,
              type=click.Path(), help="Path to the infra/ directory.")
@click.option("--skip-fabric", is_flag=True, default=False,
              help="Skip Fabric capacity and API checks.")
@click.option("--json", "--json-output", "json_output", is_flag=True, default=False,
              help="Emit JSON output.")
def preflight_cmd(env: str, infra_dir: str, skip_fabric: bool, json_output: bool) -> None:
    """Run infrastructure preflight checks.

    Verifies Azure CLI, azd, login, subscription, resource group role,
    resource provider registration, region/SKU availability, and Fabric
    prerequisites.

    \b
    Example:
      fabric-kg infra preflight --env dev
      fabric-kg infra preflight --env dev --json
    """
    from fabric_kg_builder.infra.preflight import run_preflight

    manifest = _load_manifest_or_fail(env, infra_dir)
    runner = _make_runner()
    result = run_preflight(manifest, runner, skip_fabric=skip_fabric)
    _print_preflight_result(result, json_output=json_output)
    if not result.passed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@infra_cmd.command("plan")
@click.option("--env", default="dev", show_default=True,
              help="Target environment.")
@click.option("--infra-dir", default=_DEFAULT_INFRA_DIR, show_default=True,
              type=click.Path(), help="Path to the infra/ directory.")
@click.option("--build-root", default=_DEFAULT_BUILD_ROOT, show_default=True,
              type=click.Path(), help="Build output root directory.")
@click.option("--out", default=None, type=click.Path(),
              help="Write the plan JSON to this file (default: build/infra/<env>/plan.json).")
@click.option("--json", "--json-output", "json_output", is_flag=True, default=False,
              help="Emit JSON to stdout.")
def plan_cmd(
    env: str,
    infra_dir: str,
    build_root: str,
    out: str | None,
    json_output: bool,
) -> None:
    """Generate a machine-readable infrastructure plan.

    The plan lists creates, adopts, updates, replacements, RBAC assignments,
    cost-bearing SKUs, and prerequisites.  No cloud operations are performed.

    \b
    Example:
      fabric-kg infra plan --env dev
      fabric-kg infra plan --env dev --out build/infra/plan.json
    """
    from fabric_kg_builder.infra.plan import build_plan, save_plan
    from fabric_kg_builder.infra.apply import load_state

    manifest = _load_manifest_or_fail(env, infra_dir)
    existing_state = load_state(Path(build_root), env)
    plan = build_plan(manifest, existing_state=existing_state)

    out_path = Path(out) if out else Path(build_root) / "infra" / env / "plan.json"
    save_plan(plan, out_path)
    click.echo(f"[infra plan] Written to: {out_path}")

    _print_plan(plan, json_output=json_output)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@infra_cmd.command("apply")
@click.option("--env", default="dev", show_default=True,
              help="Target environment.")
@click.option("--infra-dir", default=_DEFAULT_INFRA_DIR, show_default=True,
              type=click.Path(), help="Path to the infra/ directory.")
@click.option("--build-root", default=_DEFAULT_BUILD_ROOT, show_default=True,
              type=click.Path(), help="Build output root directory.")
@click.option("--auto-approve", is_flag=True, default=False,
              help="Skip interactive approval prompt.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Run what-if analysis without applying changes.")
@click.option("--json", "--json-output", "json_output", is_flag=True, default=False,
              help="Emit JSON output.")
def apply_cmd(
    env: str,
    infra_dir: str,
    build_root: str,
    auto_approve: bool,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Provision infrastructure resources.

    Runs Bicep deployment for Azure resources and calls Fabric REST APIs for
    workspace/Lakehouse/Ontology items.  Persists operation state without
    secrets to build/infra/<env>/.

    Second apply is idempotent (no-op for already-provisioned resources).

    \b
    Example:
      fabric-kg infra apply --env dev --dry-run
      fabric-kg infra apply --env dev --auto-approve
    """
    from fabric_kg_builder.infra.plan import build_plan
    from fabric_kg_builder.infra.apply import apply_plan, load_state

    manifest = _load_manifest_or_fail(env, infra_dir)
    existing_state = load_state(Path(build_root), env)
    plan = build_plan(manifest, existing_state=existing_state)

    if plan.warnings and not auto_approve:
        for w in plan.warnings:
            click.echo(f"  ⚠ {w}", err=True)

    if not auto_approve and not dry_run:
        _print_plan(plan, json_output=False)
        click.confirm(
            f"\nApply {len([i for i in plan.items if i.action.value != 'no-op'])} "
            f"item(s) to environment '{env}'?",
            abort=True,
        )

    runner = _make_runner()
    status = apply_plan(
        manifest,
        plan,
        runner,
        dry_run=dry_run,
        build_root=Path(build_root),
        infra_dir=Path(infra_dir),
    )

    if json_output:
        click.echo(json.dumps(status.as_dict(), indent=2))
    else:
        prefix = "[infra apply dry-run]" if dry_run else "[infra apply]"
        click.echo(f"{prefix} environment: {status.environment}")
        click.echo(f"{prefix} operation_id: {status.operation_id}")
        click.echo(f"{prefix} items_attempted: {status.items_attempted}")
        click.echo(f"{prefix} items_succeeded: {status.items_succeeded}")
        click.echo(f"{prefix} items_skipped: {status.items_skipped}")
        if status.errors:
            for err in status.errors:
                click.echo(f"  ✗ {err}", err=True)
        if status.state_path:
            click.echo(f"{prefix} state: {status.state_path}")
        if status.outputs_path:
            click.echo(f"{prefix} outputs: {status.outputs_path}")

    if not status.succeeded:
        sys.exit(1)

    if not dry_run:
        from fabric_kg_builder.infra.apply import load_outputs
        from fabric_kg_builder.infra.runtime_sync import sync_runtime_configuration

        outputs = load_outputs(Path(build_root), env)
        synced = sync_runtime_configuration(
            environment=env,
            manifest=manifest,
            outputs=outputs,
            fabric_environment_path=(
                Path("ontology") / "environments" / f"{env}.json"
            ),
            agent_metadata_path=Path(".foundry") / "agent-metadata.yaml",
        )
        if not json_output:
            click.echo(
                "[infra apply] runtime configuration: "
                + ", ".join(synced.values())
            )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@infra_cmd.command("status")
@click.option("--env", default="dev", show_default=True,
              help="Target environment.")
@click.option("--build-root", default=_DEFAULT_BUILD_ROOT, show_default=True,
              type=click.Path(), help="Build output root directory.")
@click.option("--json", "--json-output", "json_output", is_flag=True, default=False,
              help="Emit JSON output.")
def status_cmd(env: str, build_root: str, json_output: bool) -> None:
    """Show last apply operation status.

    Reads build/infra/<env>/state.json (no cloud calls).

    \b
    Example:
      fabric-kg infra status --env dev
      fabric-kg infra status --env dev --json
    """
    from fabric_kg_builder.infra.apply import get_apply_status

    summary = get_apply_status(Path(build_root), env)
    if json_output:
        click.echo(json.dumps(summary, indent=2))
        return
    click.echo(f"[infra status] environment            : {summary['environment']}")
    click.echo(f"[infra status] last_operation         : {summary['last_operation']}")
    click.echo(f"[infra status] last_operation_id      : {summary['last_operation_id']}")
    click.echo(f"[infra status] last_operation_status  : {summary['last_operation_status']}")
    click.echo(f"[infra status] managed_resources      : {len(summary['managed_resources'])}")
    click.echo(f"[infra status] adopted_resources      : {len(summary['adopted_resources'])}")
    click.echo(f"[infra status] outputs                : {summary['output_count']} values")


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


@infra_cmd.command("connect")
@click.option("--env", default="dev", show_default=True,
              help="Target environment.")
@click.option("--infra-dir", default=_DEFAULT_INFRA_DIR, show_default=True,
              type=click.Path(), help="Path to the infra/ directory.")
@click.option("--build-root", default=_DEFAULT_BUILD_ROOT, show_default=True,
              type=click.Path(), help="Build output root directory.")
@click.option(
    "--resource",
    required=True,
    type=click.Choice(
        ["storage", "document_intelligence", "foundry", "search",
         "workspace", "lakehouse", "ontology", "graph_model"],
        case_sensitive=False,
    ),
    help="Resource kind to connect.",
)
@click.option("--id", "resource_id", required=True,
              help="ARM resource ID or Fabric item ID.")
@click.option("--json", "--json-output", "json_output", is_flag=True, default=False,
              help="Emit JSON output.")
def connect_cmd(
    env: str,
    infra_dir: str,
    build_root: str,
    resource: str,
    resource_id: str,
    json_output: bool,
) -> None:
    """Adopt an existing resource into the environment.

    Validates compatibility and access; does not merely save an ID.

    \b
    Example:
      fabric-kg infra connect --env dev --resource search --id /subscriptions/...
      fabric-kg infra connect --env dev --resource graph_model --id <fabric-item-id>
    """
    from fabric_kg_builder.infra.apply import load_state, save_state
    from fabric_kg_builder.infra.schema import CompatibilityProbeResult, ResourceMode

    state = load_state(Path(build_root), env)
    azure_types = {
        "storage": "Microsoft.Storage/storageAccounts",
        "document_intelligence": (
            "Microsoft.CognitiveServices/accounts/document-intelligence"
        ),
        "foundry": "Microsoft.CognitiveServices/accounts/foundry",
        "search": "Microsoft.Search/searchServices",
    }
    fabric_types = {
        "workspace": "Fabric/Workspace",
        "lakehouse": "Fabric/Lakehouse",
        "ontology": "Fabric/Ontology",
        "graph_model": "Fabric/GraphModel",
    }

    try:
        if resource in azure_types:
            runner = _make_runner()
            probe = runner.run([
                "az", "resource", "show",
                "--ids", resource_id,
                "--output", "json",
            ])
            if not probe.succeeded:
                raise click.ClickException(
                    f"Cannot access Azure resource '{resource_id}': "
                    f"{probe.stderr}"
                )
            payload = json.loads(probe.stdout or "{}")
            kind = str(payload.get("kind", "")).lower()
            sku = str((payload.get("sku") or {}).get("name", "")).lower()
            errors: list[str] = []
            warnings: list[str] = []
            if resource == "storage" and payload.get("type") not in (
                None,
                "Microsoft.Storage/storageAccounts",
            ):
                errors.append("Resource is not an Azure Storage account.")
            elif resource == "document_intelligence" and kind not in (
                "",
                "formrecognizer",
            ):
                errors.append(
                    "Resource kind must be FormRecognizer for Document "
                    "Intelligence."
                )
            elif resource == "foundry" and kind not in ("", "aiservices"):
                errors.append("Foundry resource kind must be AIServices.")
            elif resource == "search" and sku not in (
                "",
                "standard",
                "standard2",
                "standard3",
            ):
                errors.append(
                    "AI Search must use Standard or higher for vector and "
                    "semantic search."
                )
            if errors:
                raise click.ClickException(" ".join(errors))
            warnings.append(
                "Control-plane access and resource compatibility passed. "
                "Run 'infra preflight' to verify runtime identity and quota."
            )
            result = CompatibilityProbeResult(
                resource_type=azure_types[resource],
                resource_name=str(payload.get("name") or resource_id),
                mode=ResourceMode.CONNECT,
                identity_ok=True,
                sku_ok=True,
                network_ok=True,
                rbac_ok=True,
                data_plane_ok=False,
                warnings=warnings,
            )
            state_key = azure_types[resource]
        else:
            from fabric_kg_builder.infra.fabric_client import (
                FabricGraphModelClient,
                FabricLakehouseClient,
                FabricOntologyClient,
                FabricWorkspaceClient,
            )

            transport = _make_fabric_transport()
            workspace_id = (
                state.managed_resource_ids.get("Fabric/Workspace")
                or state.adopted_resource_ids.get("Fabric/Workspace")
            )
            if resource == "workspace":
                payload = FabricWorkspaceClient(transport).get_workspace(
                    resource_id
                )
            else:
                if not workspace_id:
                    raise click.ClickException(
                        "Connect the Fabric workspace before connecting child "
                        f"item '{resource}'."
                    )
                if resource == "lakehouse":
                    payload = FabricLakehouseClient(
                        transport, workspace_id
                    ).get_lakehouse(resource_id)
                elif resource == "ontology":
                    payload = FabricOntologyClient(
                        transport, workspace_id
                    ).connect(item_id=resource_id)
                else:
                    payload = FabricGraphModelClient(
                        transport, workspace_id
                    ).connect(item_id=resource_id)
            result = CompatibilityProbeResult(
                resource_type=fabric_types[resource],
                resource_name=str(
                    payload.get("displayName")
                    or payload.get("name")
                    or resource_id
                ),
                mode=ResourceMode.CONNECT,
                identity_ok=True,
                sku_ok=True,
                network_ok=True,
                rbac_ok=True,
                data_plane_ok=True,
            )
            state_key = fabric_types[resource]
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"Compatibility probe failed for {resource}: {exc}"
        ) from exc

    adopted = dict(state.adopted_resource_ids)
    adopted[state_key] = resource_id
    state = state.model_copy(update={"adopted_resource_ids": adopted})
    save_state(state, Path(build_root))

    if json_output:
        click.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        return
    click.echo(f"[infra connect] resource : {result.resource_type}")
    click.echo(f"[infra connect] id       : {resource_id}")
    click.echo(f"[infra connect] adopted  : True")
    for w in result.warnings:
        click.echo(f"  ⚠ {w}")


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


@infra_cmd.command("destroy")
@click.option("--env", default="dev", show_default=True,
              help="Target environment.")
@click.option("--infra-dir", default=_DEFAULT_INFRA_DIR, show_default=True,
              type=click.Path(), help="Path to the infra/ directory.")
@click.option("--build-root", default=_DEFAULT_BUILD_ROOT, show_default=True,
              type=click.Path(), help="Build output root directory.")
@click.option("--target", multiple=True,
              help="Resource name to destroy (repeat for multiple targets).")
@click.option("--all-managed", is_flag=True, default=False,
              help="Destroy every managed resource in this environment; adopted resources remain protected.")
@click.option("--confirm", "confirmed", is_flag=True, default=False,
              help="Confirm destruction of managed resources.")
@click.option("--json", "--json-output", "json_output", is_flag=True, default=False,
              help="Emit JSON output.")
def destroy_cmd(
    env: str,
    infra_dir: str,
    build_root: str,
    target: tuple[str, ...],
    all_managed: bool,
    confirmed: bool,
    json_output: bool,
) -> None:
    """Destroy owned (managed) infrastructure resources.

    Only resources created by fabric-kg-builder can be destroyed.
    Adopted resources are never deleted.

    \b
    Example:
      fabric-kg infra destroy --env dev --target kg-storage-abc123 --confirm
      fabric-kg infra destroy --env dev --all-managed --confirm
    """
    from fabric_kg_builder.infra.apply import load_state
    from fabric_kg_builder.infra.destroy import build_destroy_plan, execute_destroy

    if all_managed and target:
        raise click.UsageError("Use either --all-managed or one or more --target options, not both.")
    if not all_managed and not target:
        raise click.UsageError("Select resources with --target or explicitly use --all-managed.")

    state = load_state(Path(build_root), env)
    plan = build_destroy_plan(
        state,
        target_names=None if all_managed else list(target),
    )

    if plan.blocked_adopted:
        click.echo(
            f"[infra destroy] ERROR: Cannot destroy adopted resources: "
            f"{', '.join(plan.blocked_adopted)}",
            err=True,
        )
        sys.exit(1)

    if not plan.has_destroyable_targets:
        selection = "all managed resources" if all_managed else list(target)
        click.echo(
            f"[infra destroy] No managed resources matched selection: {selection}"
        )
        return

    if not confirmed:
        click.echo("[infra destroy] Add --confirm to proceed with destruction.", err=True)
        _print_destroy_plan(plan, json_output=json_output)
        sys.exit(1)

    runner = _make_runner()
    try:
        status = execute_destroy(
            state, plan, runner,
            build_root=Path(build_root),
            confirmed=confirmed,
        )
    except (PermissionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        click.echo(json.dumps({
            "environment": status.environment,
            "items_destroyed": status.items_destroyed,
            "items_skipped": status.items_skipped,
            "errors": status.errors,
        }, indent=2))
    else:
        click.echo(f"[infra destroy] destroyed : {status.items_destroyed}")
        click.echo(f"[infra destroy] skipped   : {status.items_skipped}")
        if status.errors:
            for err in status.errors:
                click.echo(f"  ✗ {err}", err=True)
            sys.exit(1)


def _print_destroy_plan(plan, *, json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(plan.as_dict(), indent=2))
        return
    click.echo(f"[infra destroy plan] environment: {plan.environment}")
    for t in plan.targets:
        icon = "✗" if t.will_destroy else "–"
        click.echo(f"  {icon} {t.resource_name}  ({t.reason})")
