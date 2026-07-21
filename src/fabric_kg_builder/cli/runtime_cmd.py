"""SPEC-008 deployment validation, evaluation, and reporting commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from fabric_kg_builder.runtime import (
    CompetencyContractError,
    RuntimeAcceptanceError,
    RuntimeCollectionError,
    build_live_collector,
    build_runtime_report,
    evaluate_runtime_evidence,
    load_competency_contract,
    load_runtime_evidence,
    load_runtime_config,
    validate_deployment_evidence,
)
from fabric_kg_builder.semantic import (
    SemanticArtifactValidationError,
    validate_compiled_semantic_artifacts,
)


def _write_json(path: str, payload: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def _load(path: str) -> dict:
    try:
        return load_runtime_evidence(path)
    except RuntimeAcceptanceError as exc:
        raise click.ClickException(str(exc)) from exc


_COLLECT_EVIDENCE_EPILOG = """\b
Receipt requirement:
  runtime-config.json deployment.receipt_path must reference the aggregate
  semantic deployment receipt with schema:
    fabric-kg.semantic-deployment-receipt.v1

  build-deploy writes this receipt only after artifact validation, persisted
  Ontology/Graph projection validation, Search deployment, and Data Agent
  publication. Surface-specific receipts are not accepted substitutes.

Rejected receipt schema example:
\b
  fabric-kg.serving-deployment.v1

Runtime config fragment:
\b
  {
    "deployment": {
      "receipt_path": "../build/runs/{{RUN_ID}}/release/deployment-receipt.json"
    }
  }

Replace {{RUN_ID}} with the completed build-deploy run ID. Keep the config
secret-free; authentication uses the configured token scopes and Azure identity.

PowerShell example:
\b
  fabric-kg collect-evidence --competency-contract build\\agents\\competency-contract.json --runtime-config evaluation\\runtime-config.json --out build\\evaluation\\runtime-evidence.json
"""


@click.command(
    "collect-evidence",
    epilog=_COLLECT_EVIDENCE_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option(
    "--competency-contract",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Compiled route-aware competency-contract.json.",
)
@click.option(
    "--runtime-config",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Secret-free runtime endpoints, IDs, and deployment receipt JSON.",
)
@click.option(
    "--out",
    default="build/evaluation/runtime-evidence.json",
    show_default=True,
    type=click.Path(),
)
def collect_evidence_cmd(
    competency_contract: str,
    runtime_config: str,
    out: str,
) -> None:
    """Run probes using an aggregate semantic deployment receipt."""
    try:
        contract = load_competency_contract(competency_contract)
        config = load_runtime_config(runtime_config)
        evidence = build_live_collector(
            contract=contract,
            config=config,
        ).collect()
    except (CompetencyContractError, RuntimeCollectionError) as exc:
        raise click.ClickException(str(exc)) from exc
    target = _write_json(out, evidence)
    click.echo(
        f"[collect-evidence] COMPLETE cases={len(evidence['cases'])} -> {target}"
    )


@click.command("validate-deployment")
@click.option(
    "--evidence",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Redacted Graph/Search/Data Agent runtime evidence JSON.",
)
@click.option(
    "--build-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Optional compiled build directory for cross-artifact validation.",
)
@click.option(
    "--out",
    default="build/validation/deployment.json",
    show_default=True,
    type=click.Path(),
)
def validate_deployment_cmd(
    evidence: str,
    build_dir: str | None,
    out: str,
) -> None:
    """Validate deployment integrity, authorization, publication, and telemetry."""
    payload = _load(evidence)
    result = validate_deployment_evidence(payload)
    if build_dir:
        try:
            artifact_report = validate_compiled_semantic_artifacts(
                build_dir,
                require_search=True,
                require_competency=True,
            )
        except SemanticArtifactValidationError as exc:
            result["status"] = "failed"
            result["artifact_validation"] = {
                "status": "failed",
                "findings": [
                    {"code": finding.code, "message": finding.message}
                    for finding in exc.findings
                ],
            }
        else:
            result["artifact_validation"] = artifact_report
    target = _write_json(out, result)
    click.echo(
        f"[validate-deployment] {result['status'].upper()} -> {target}"
    )
    if result["status"] != "passed":
        raise click.ClickException("Deployment validation failed.")


@click.command("evaluate")
@click.option(
    "--evidence",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Redacted competency route evidence JSON.",
)
@click.option(
    "--out",
    default="build/evaluation/runtime.json",
    show_default=True,
    type=click.Path(),
)
def evaluate_cmd(evidence: str, out: str) -> None:
    """Score direct Graph, Search, composed, citation, and MCP behavior."""
    result = evaluate_runtime_evidence(_load(evidence))
    target = _write_json(out, result)
    click.echo(f"[evaluate] {result['status'].upper()} -> {target}")
    if result["status"] != "passed":
        raise click.ClickException(
            f"Runtime evaluation status is {result['status']}."
        )


@click.command("report")
@click.option(
    "--evidence",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Redacted competency and deployment evidence JSON.",
)
@click.option(
    "--out",
    default="build/reports/runtime-report.json",
    show_default=True,
    type=click.Path(),
)
def report_cmd(evidence: str, out: str) -> None:
    """Generate a redacted release report or runtime-blocked support receipt."""
    result = build_runtime_report(_load(evidence))
    target = _write_json(out, result)
    click.echo(f"[report] {result['status'].upper()} -> {target}")
    if result["status"] != "passed":
        raise click.ClickException(
            f"Runtime report status is {result['status']}."
        )
