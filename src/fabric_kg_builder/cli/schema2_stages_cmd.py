"""validate-evidence and project-serving commands — schema-2 L3 and L4.

These activate the existing local-only schema-2 stages. ``validate-evidence``
runs L3, which verifies every candidate L2 proposed against its source text and
mints evidence spans. ``project-serving`` runs L4, which turns a validated L3
result into the canonical audit and asserted-only serving Parquet tables.

Both stages are local: they make no LLM, Foundry, Document Intelligence,
embedding, Search, or Fabric call. Publication to a live target is L5a, which
is deployed separately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from fabric_kg_builder.enrichment.schema2_evidence import L3StageError
from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
from fabric_kg_builder.serving.lifecycle_projection import L4ProjectionError, run_l4

#: Exit code for a fail-closed stage error that names a stable audit code.
_STAGE_FAILURE_EXIT = 5


def _echo_receipt(prefix: str, receipt) -> None:
    click.echo(f"  status: {receipt.status}")
    click.echo(f"  {prefix} receipt: {receipt.stage_receipt_id}")
    click.echo(f"  receipt hash: {receipt.receipt_hash}")


def _fail(stage: str, exc: Exception, code: str | None = None) -> None:
    """Report a fail-closed stage error with its audit code and stop."""
    label = f"{code}: " if code else ""
    click.echo(f"[{stage}] FAILED: {label}{exc}", err=True)
    reasons = getattr(exc, "reason_codes", ())
    for reason in reasons:
        click.echo(f"  reason: {reason}", err=True)
    sys.exit(_STAGE_FAILURE_EXIT)


def _run_l3_stage(
    *,
    state_root: str,
    l2_state_root: str,
    l1_state_root: str,
    domain_path: str,
):
    return run_l3(
        state_root=Path(state_root),
        l2_state_root=Path(l2_state_root),
        l1_state_root=Path(l1_state_root),
        domain_path=Path(domain_path),
    )


_VALIDATE_EVIDENCE_EPILOG = """\b
Example:
  fabric-kg validate-evidence
  fabric-kg validate-evidence --l2-state .fkg/l2 --domain domain.yaml

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("validate-evidence", epilog=_VALIDATE_EVIDENCE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--state", "state_root", default=".fkg/l3", show_default=True,
              type=click.Path(),
              help="L3 state directory holding run and leaf checkpoints.")
@click.option("--l2-state", "l2_state_root", default=".fkg/l2", show_default=True,
              type=click.Path(),
              help="Completed L2 state directory to consume.")
@click.option("--l1-state", "l1_state_root", default=".fkg/l1", show_default=True,
              type=click.Path(),
              help="Approved L1 state directory holding the domain contract.")
@click.option("--domain", "domain_path", default="domain.yaml", show_default=True,
              type=click.Path(),
              help="Approved domain contract.")
def validate_evidence_cmd(
    state_root: str,
    l2_state_root: str,
    l1_state_root: str,
    domain_path: str,
) -> None:
    """Validate schema-2 L2 proposals locally and mint evidence spans (L3).

    Consumes an intact succeeded L2 handoff and verifies every proposed
    candidate against its recorded source text, minting evidence spans and
    required-member manifests. Completed leaves are checkpointed, so re-running
    reuses prior work instead of revalidating.

    Makes no remote call of any kind.

    Exit codes: 0 success · 1 I/O or unexpected error · 5 stage failure.
    """
    click.echo(f"[validate-evidence] Validating L2 proposals from {l2_state_root} ...")
    try:
        result = _run_l3_stage(
            state_root=state_root,
            l2_state_root=l2_state_root,
            l1_state_root=l1_state_root,
            domain_path=domain_path,
        )
    except L3StageError as exc:
        _fail("validate-evidence", exc, exc.code)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise click.ClickException(f"L3 input is unavailable: {exc}") from exc

    click.echo(
        f"  leaves: {len(result.leaves)} "
        f"(reused={result.reused_leaf_count}, "
        f"recomputed={result.recomputed_leaf_count})"
    )
    click.echo(
        f"  candidates: {len(result.candidate_results)}, "
        f"evidence spans: {len(result.evidence_spans)}, "
        f"required-member manifests: {len(result.required_member_manifests)}"
    )
    _echo_receipt("L3", result.receipt)
    click.echo(f"  run root: {result.run_root}")
    if result.receipt.status != "succeeded":
        click.echo(
            "  [FAIL] L3 did not succeed; project-serving requires a succeeded "
            "L3 receipt.",
            err=True,
        )
        sys.exit(_STAGE_FAILURE_EXIT)


_PROJECT_SERVING_EPILOG = """\b
Example:
  fabric-kg project-serving
  fabric-kg project-serving --state .fkg/l4 --l3-state .fkg/l3

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("project-serving", epilog=_PROJECT_SERVING_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--state", "state_root", default=".fkg/l4", show_default=True,
              type=click.Path(),
              help="L4 state directory holding the projection run.")
@click.option("--l3-state", "l3_state_root", default=".fkg/l3", show_default=True,
              type=click.Path(),
              help="L3 state directory to resolve the validated source from.")
@click.option("--l2-state", "l2_state_root", default=".fkg/l2", show_default=True,
              type=click.Path(),
              help="Completed L2 state directory backing L3.")
@click.option("--l1-state", "l1_state_root", default=".fkg/l1", show_default=True,
              type=click.Path(),
              help="Approved L1 state directory holding the domain contract.")
@click.option("--domain", "domain_path", default="domain.yaml", show_default=True,
              type=click.Path(),
              help="Approved domain contract.")
def project_serving_cmd(
    state_root: str,
    l3_state_root: str,
    l2_state_root: str,
    l1_state_root: str,
    domain_path: str,
) -> None:
    """Project a validated L3 result into canonical serving tables (L4).

    Writes the audit and asserted-only semantic serving Parquet tables that
    L5a publishes. The validated L3 source is resolved by re-running L3, which
    reuses its leaf checkpoints and so does not revalidate completed work.

    Makes no remote call of any kind.

    Exit codes: 0 success · 1 I/O or unexpected error · 5 stage failure.
    """
    click.echo(f"[project-serving] Resolving validated L3 source from {l3_state_root} ...")
    try:
        source = _run_l3_stage(
            state_root=l3_state_root,
            l2_state_root=l2_state_root,
            l1_state_root=l1_state_root,
            domain_path=domain_path,
        )
    except L3StageError as exc:
        _fail("project-serving", exc, exc.code)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise click.ClickException(f"L3 input is unavailable: {exc}") from exc

    if source.receipt.status != "succeeded":
        click.echo(
            f"[project-serving] FAILED: L4 requires a succeeded L3 receipt, "
            f"found '{source.receipt.status}'.",
            err=True,
        )
        sys.exit(_STAGE_FAILURE_EXIT)

    click.echo(
        f"  L3 leaves: {len(source.leaves)} "
        f"(reused={source.reused_leaf_count}, "
        f"recomputed={source.recomputed_leaf_count})"
    )
    click.echo("[project-serving] Projecting canonical serving tables ...")
    try:
        result = run_l4(source, state_root=Path(state_root))
    except L4ProjectionError as exc:
        _fail("project-serving", exc)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise click.ClickException(f"L4 input is unavailable: {exc}") from exc

    click.echo(f"  reused prior projection: {result.reused}")
    for name, rows in result.rows.tables().items():
        click.echo(f"    {name}: {len(rows)}")
    _echo_receipt("L4", result.receipt)
    click.echo(f"  run root: {result.run_root}")
    if result.receipt.status != "succeeded":
        click.echo("  [FAIL] L4 did not succeed.", err=True)
        sys.exit(_STAGE_FAILURE_EXIT)
