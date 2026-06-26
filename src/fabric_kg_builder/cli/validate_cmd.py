"""validate command — validate build artifacts, ontology, and AI Search.

Exit codes
----------
0  All gates passed (warnings are shown but do not fail)
8  One or more FAIL-severity gates tripped
1  Unexpected error during validation
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from fabric_kg_builder.validate.suite import ValidationViolation, validate_all


def _load_config_from_yaml(config_path: str) -> dict[str, Any]:
    """Load fabric-kg.yaml and return a plain dict (best-effort)."""
    try:
        import yaml  # noqa: PLC0415
        p = Path(config_path)
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:  # noqa: BLE001
        pass
    return {}


_VALIDATE_EPILOG = """\b
Example:
  fabric-kg validate --env dev
  fabric-kg validate --env prod --rules VAL-008,BRG-001 --report build\\validation.txt

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("validate", epilog=_VALIDATE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option(
    "--env",
    required=True,
    type=click.Choice(["dev", "test", "prod"]),
    help="Target deployment environment to validate.",
)
@click.option(
    "--build",
    "build_dir",
    default="build",
    show_default=True,
    type=click.Path(),
    help="Build directory containing parquet/, ontology/, and search/ artifacts.",
)
@click.option(
    "--rules",
    default=None,
    show_default=True,
    help="Comma-separated subset of rule IDs to run, e.g. VAL-008,BRG-001. "
         "Default: run the full catalog.",
)
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(),
    help="Write the validation report to this file path (default: stdout only).",
)
@click.option(
    "--skip-env-check",
    is_flag=True,
    default=False,
    help="Skip VAL-025 (env-var presence) and VAL-026 (secret scan) — useful in CI.",
)
@click.pass_context
def validate_cmd(
    ctx: click.Context,
    env: str,
    build_dir: str,
    rules: str | None,
    report_path: str | None,
    skip_env_check: bool,
) -> None:
    """Validate build artifacts, ontology, and AI Search against SPEC-005 gates.

    Runs the full VAL + BRG gate catalog against the build directory and prints
    a report grouped by severity (FAIL / WARN).

    Exit codes: 0 all gates passed · 8 one or more FAIL-severity violations · 1 unexpected error.
    """
    config_path = (ctx.obj or {}).get("config", "fabric-kg.yaml") if ctx.obj else "fabric-kg.yaml"
    config = _load_config_from_yaml(config_path)

    try:
        violations = validate_all(
            build_dir=build_dir,
            config=config,
            skip_env_check=skip_env_check,
        )
    except Exception as exc:  # noqa: BLE001
        click.echo(f"[validate] ERROR: {exc}", err=True)
        sys.exit(1)

    # Apply rule filter if --rules given
    if rules:
        rule_set = {r.strip().upper() for r in rules.split(",")}
        violations = [v for v in violations if v.rule_id.upper() in rule_set]

    fails = [v for v in violations if v.severity == "fail"]
    warns = [v for v in violations if v.severity == "warn"]

    lines: list[str] = []
    lines.append(f"\n{'='*60}")
    lines.append(f"  fabric-kg validate  |  env={env}  |  build={build_dir}")
    lines.append(f"{'='*60}")

    if fails:
        lines.append(f"\n  FAILURES ({len(fails)})")
        lines.append("  " + "-" * 50)
        for v in fails:
            lines.append(f"  FAIL  [{v.rule_id}] {v.message}")

    if warns:
        lines.append(f"\n  WARNINGS ({len(warns)})")
        lines.append("  " + "-" * 50)
        for v in warns:
            lines.append(f"  WARN  [{v.rule_id}] {v.message}")

    if not violations:
        lines.append("\n  PASS: All gates passed -- no violations found.")

    lines.append(f"\n  Summary: {len(fails)} FAIL, {len(warns)} WARN")
    lines.append("=" * 60 + "\n")

    report_text = "\n".join(lines)
    click.echo(report_text)

    if report_path:
        try:
            Path(report_path).write_text(report_text, encoding="utf-8")
            click.echo(f"[validate] Report written to: {report_path}")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"[validate] WARNING: could not write report: {exc}", err=True)

    if fails:
        sys.exit(8)
