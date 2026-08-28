"""Grouped CLI commands for domain contract authoring, review, and approval."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from pydantic import ValidationError

from fabric_kg_builder.domain import (
    ApprovalMetadata,
    DomainContract,
    DomainContractV2,
    DomainReview,
    DomainReviewError,
    compute_contract_hash,
    convert_legacy_brief_to_contract,
    default_domain_contract,
    evaluate_domain_guard_status,
    load_domain_contract,
    load_domain_review_file,
    load_legacy_domain_brief,
    proposal_path_for_contract,
    render_review_diff,
    require_ready_domain_contract,
    review_path_for_contract,
    run_deterministic_validation,
    run_structured_review,
    save_domain_contract,
    save_json_document,
    utc_now_text,
)


def _build_foundry_client(ctx_obj: dict):
    """Build the Foundry client used by domain review."""
    from ..config.loader import load_config
    from ..enrichment.foundry_client import FoundryClient

    env = (ctx_obj or {}).get("env", "dev")
    config = load_config(env=env)
    return FoundryClient(config.foundry)


def _resolve_model_version(client, ctx_obj: dict | None) -> str:
    """Resolve the effective review model version for metadata."""
    injected = (ctx_obj or {}).get("_foundry_model_version")
    if isinstance(injected, str) and injected.strip():
        return injected.strip()
    config = getattr(client, "_config", None)
    deployment = getattr(config, "chat_deployment", "")
    if isinstance(deployment, str) and deployment.strip():
        return deployment.strip()
    return "unknown"


def _comma_list_prompt(text: str, default: str = "") -> list[str]:
    """Prompt for a comma-separated list and normalize it."""
    raw = click.prompt(text, default=default, show_default=bool(default))
    return [item.strip() for item in raw.split(",") if item.strip()]


def _question_list_prompt() -> list[str]:
    """Prompt for one or more semicolon-separated competency questions."""
    raw = click.prompt(
        "Competency questions (semicolon-separated)",
        default="What question should the graph answer?",
        show_default=True,
    )
    return [item.strip() for item in raw.split(";") if item.strip()]


def _build_interactive_contract() -> DomainContract:
    """Collect guided domain authoring input from the terminal."""
    contract = default_domain_contract()
    contract.domain.name = click.prompt("Domain name")
    contract.domain.description = click.prompt("Domain description")
    contract.domain.subdomains = _comma_list_prompt(
        "Subdomains (comma-separated)", "operations, analytics"
    )
    contract.business.organization_context = click.prompt("Organization context")
    contract.business.users = _comma_list_prompt(
        "Primary users (comma-separated)", "analyst, manager"
    )
    contract.business.decisions = _comma_list_prompt(
        "Supported decisions (comma-separated)",
        "Prioritize investigations, approve actions",
    )
    contract.problem.statement = click.prompt("Problem statement")
    contract.problem.desired_outcomes = _comma_list_prompt(
        "Desired outcomes (comma-separated)",
        "Trace impacts, provide evidence-backed answers",
    )
    contract.problem.in_scope = _comma_list_prompt(
        "In-scope concepts (comma-separated)"
    )
    contract.problem.out_of_scope = _comma_list_prompt(
        "Out-of-scope concepts (comma-separated)", "none"
    )
    contract.competency_questions = _question_list_prompt()
    contract.candidate_model.entity_categories = _comma_list_prompt(
        "Candidate entity categories (comma-separated)"
    )
    contract.candidate_model.relationship_categories = _comma_list_prompt(
        "Candidate relationship categories (comma-separated)"
    )
    contract.constraints.temporal = _comma_list_prompt(
        "Temporal constraints (comma-separated)", "none"
    )
    contract.constraints.regulatory = _comma_list_prompt(
        "Regulatory constraints (comma-separated)", "none"
    )
    contract.constraints.privacy = _comma_list_prompt(
        "Privacy constraints (comma-separated)", "none"
    )
    contract.constraints.safety = _comma_list_prompt(
        "Safety constraints (comma-separated)", "no automated external action"
    )
    contract.examples.positive[0].text = click.prompt("Positive example text")
    contract.examples.positive[0].expected = _comma_list_prompt(
        "Positive example expected facts (comma-separated)"
    )
    contract.examples.negative[0].text = click.prompt("Negative example text")
    contract.examples.negative[0].reason = click.prompt("Negative example reason")
    contract.approval = ApprovalMetadata(status="draft")
    return contract


def _echo_findings(findings) -> None:
    """Print review findings in a stable structured format."""
    if not findings:
        click.echo("[domain] no findings")
        return
    for finding in findings:
        click.echo(
            f"[domain] {finding.severity.upper()} {finding.code} {finding.path}: {finding.message}"
        )


@click.group(
    "domain",
    context_settings={"max_content_width": 120},
)
def domain_cmd() -> None:
    """Author, validate, review, approve, and inspect domain.yaml contracts."""


@domain_cmd.command("init")
@click.option("--interactive", is_flag=True, default=False, help="Prompt for contract values interactively.")
@click.option(
    "--out",
    "output_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(),
    help="Path to the draft domain contract YAML.",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing output file.")
def domain_init_cmd(interactive: bool, output_path: str, force: bool) -> None:
    """Create a schema-valid draft domain.yaml scaffold."""
    out_path = Path(output_path)
    if out_path.exists() and not force:
        raise click.ClickException(
            f"Refusing to overwrite existing contract '{out_path}'. Re-run with --force."
        )

    contract = _build_interactive_contract() if interactive else default_domain_contract()
    save_domain_contract(contract, out_path)
    click.echo(f"[domain init] wrote draft contract → {out_path}")
    click.echo(
        "[domain init] next steps: run 'fabric-kg domain validate', 'domain review', and 'domain approve'."
    )


@domain_cmd.command("validate")
@click.option(
    "--file",
    "contract_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(exists=True),
    help="Path to the domain contract YAML file.",
)
def domain_validate_cmd(contract_path: str) -> None:
    """Validate YAML syntax, schema conformance, and deterministic quality gates."""
    contract = load_domain_contract(contract_path)
    findings, coverage = run_deterministic_validation(contract)
    error_count = sum(1 for finding in findings if finding.severity == "error")
    warning_count = sum(1 for finding in findings if finding.severity == "warning")
    click.echo(f"[domain validate] contract hash : {compute_contract_hash(contract)}")
    click.echo(f"[domain validate] error count   : {error_count}")
    click.echo(f"[domain validate] warning count : {warning_count}")
    _echo_findings(findings)
    for item in coverage:
        click.echo(
            f"[domain validate] coverage supported={item.supported} question={item.question}"
        )
    if error_count:
        raise click.ClickException(
            f"Domain validation failed with {error_count} error(s)."
        )
    click.echo("[domain validate] domain contract is schema-valid and passed deterministic checks.")


@domain_cmd.command("review")
@click.option(
    "--file",
    "contract_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(exists=True),
    help="Path to the domain contract YAML file.",
)
@click.option(
    "--apply-proposals",
    is_flag=True,
    default=False,
    help="Write the proposed YAML to a sidecar file instead of overwriting the source contract.",
)
@click.pass_context
def domain_review_cmd(
    ctx: click.Context,
    contract_path: str,
    apply_proposals: bool,
) -> None:
    """Run deterministic and LLM review, then persist domain.review.json."""
    ctx.ensure_object(dict)
    contract = load_domain_contract(contract_path)
    if isinstance(contract, DomainContractV2):
        raise click.ClickException(
            "Schema-2.0 proposal review is not enabled in the schema foundation "
            "layer. Use 'fabric-kg domain validate' until the one-summary approval "
            "workflow is installed."
        )
    client = ctx.obj.get("_foundry_client") if ctx.obj else None
    if client is None:
        try:
            client = _build_foundry_client(ctx.obj or {})
        except (EnvironmentError, ImportError, OSError, ValidationError) as exc:
            raise click.ClickException(
                f"Could not build Foundry client for domain review: {exc}"
            ) from exc
    model_version = _resolve_model_version(client, ctx.obj or {})
    try:
        review = run_structured_review(
            contract,
            client=client,
            model_version=model_version,
        )
    except DomainReviewError as exc:
        raise click.ClickException(str(exc)) from exc

    review_path = review_path_for_contract(contract_path)
    save_json_document(review.model_dump(mode="json"), review_path)
    click.echo(f"[domain review] review written → {review_path}")
    click.echo(f"[domain review] quality score : {review.quality_score:.2f}")
    _echo_findings(review.findings)

    diff = render_review_diff(contract, review.proposed_contract)
    if diff:
        click.echo("[domain review] proposed diff:")
        click.echo(diff)
        if apply_proposals and review.proposed_contract is not None:
            proposal_path = proposal_path_for_contract(contract_path)
            save_domain_contract(review.proposed_contract, proposal_path)
            click.echo(
                f"[domain review] wrote candidate proposal without overwriting source → {proposal_path}"
            )
    else:
        click.echo("[domain review] no proposed YAML changes")


@domain_cmd.command("approve")
@click.option(
    "--file",
    "contract_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(exists=True),
    help="Path to the domain contract YAML file.",
)
@click.option(
    "--approved-by",
    default=None,
    help="Identity to record in approval metadata (default: env-driven local identity).",
)
@click.option(
    "--min-quality-score",
    default=0.70,
    show_default=True,
    type=float,
    help="Minimum review quality score required for approval.",
)
@click.option(
    "--proposal",
    "proposal_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 immutable domain proposal JSON.",
)
@click.option(
    "--design-context",
    "design_context_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 immutable design context JSON.",
)
@click.option(
    "--source-profile",
    "source_profile_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 immutable source profile JSON.",
)
@click.option(
    "--source-corpus-manifest",
    "source_corpus_manifest_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 complete source corpus manifest JSON.",
)
@click.option(
    "--design-sample-manifest",
    "design_sample_manifest_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 bounded design sample manifest JSON.",
)
@click.option(
    "--project-id",
    envvar="FABRIC_KG_PROJECT_ID",
    default=None,
    help="Expected schema-2 project ID (or FABRIC_KG_PROJECT_ID).",
)
@click.option(
    "--run-id",
    default=None,
    help="Expected immutable schema-2 run ID.",
)
@click.option(
    "--proposal-hash",
    default=None,
    help="Expected immutable proposal hash printed by init-domain.",
)
@click.option(
    "--state-dir",
    default=str(Path(".fkg") / "l1"),
    show_default=True,
    type=click.Path(file_okay=False),
    help="Schema-2 L1 state directory.",
)
def domain_approve_cmd(
    contract_path: str,
    approved_by: str | None,
    min_quality_score: float,
    proposal_path: str | None,
    design_context_path: str | None,
    source_profile_path: str | None,
    source_corpus_manifest_path: str | None,
    design_sample_manifest_path: str | None,
    project_id: str | None,
    run_id: str | None,
    proposal_hash: str | None,
    state_dir: str,
) -> None:
    """Record explicit approval metadata after a current passing review."""
    contract = load_domain_contract(contract_path)
    if isinstance(contract, DomainContractV2):
        if not approved_by or not approved_by.strip():
            raise click.ClickException(
                "Schema-2 approval requires explicit --approved-by."
            )
        from fabric_kg_builder.contracts.base import canonical_json
        from fabric_kg_builder.domain.stage import (
            L1StageError,
            approve_persisted_l1_draft,
        )

        state_root = Path(state_dir)
        explicit_bindings = {
            "domain-proposal.json": proposal_path,
            "domain-design-context.json": design_context_path,
            "source-profile.json": source_profile_path,
            "source-corpus-manifest.json": source_corpus_manifest_path,
            "design-sample-manifest.json": design_sample_manifest_path,
        }
        for default_name, explicit in explicit_bindings.items():
            if explicit is None:
                continue
            default_path = state_root / default_name
            try:
                explicit_data = json.loads(Path(explicit).read_text(encoding="utf-8"))
                default_data = json.loads(default_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise click.ClickException(
                    f"Could not reconcile schema-2 approval artifact: {exc}"
                ) from exc
            if canonical_json(explicit_data) != canonical_json(default_data):
                raise click.ClickException(
                    f"Explicit {default_name} does not match the current L1 state."
                )
        try:
            result = approve_persisted_l1_draft(
                actor=approved_by.strip(),
                state_root=state_root,
                domain_path=Path(contract_path),
                reviewed_contract=contract,
                expected_project_id=project_id,
                expected_run_id=run_id,
                expected_proposal_hash=proposal_hash,
            )
        except L1StageError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"[domain approve] approved schema-2 contract → {contract_path}")
        click.echo(
            f"[domain approve] approval context : "
            f"{result.approval_context.domain_approval_context_id}"
        )
        click.echo(
            f"[domain approve] receipt          : {result.receipt.stage_receipt_id}"
        )
        return
    review_path = review_path_for_contract(contract_path)
    if not review_path.exists():
        raise click.ClickException(
            f"Domain review file not found: {review_path}. Run 'fabric-kg domain review' first."
        )
    review = DomainReview.model_validate(load_domain_review_file(review_path))
    contract_hash = compute_contract_hash(contract)
    if review.contract_hash != contract_hash:
        raise click.ClickException(
            "Domain review is stale. Re-run 'fabric-kg domain review' before approval."
        )
    if review.quality_score < min_quality_score:
        raise click.ClickException(
            f"Review quality score {review.quality_score:.2f} is below the required minimum of {min_quality_score:.2f}."
        )
    review_errors = [finding for finding in review.findings if finding.severity == "error"]
    if review_errors:
        raise click.ClickException(
            f"Domain review still contains {len(review_errors)} error finding(s); approval is blocked."
        )
    approver = (
        approved_by
        or os.environ.get("FABRIC_KG_APPROVER")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "unknown"
    )
    contract.approval = ApprovalMetadata(
        status="approved",
        approved_by=approver,
        approved_at_utc=utc_now_text(),
        contract_hash=contract_hash,
        schema_version=contract.schema_version,
        prompt_version=review.prompt_version,
        model_version=review.model_version,
        notes=contract.approval.notes,
    )
    save_domain_contract(contract, contract_path)
    click.echo(f"[domain approve] approved contract → {contract_path}")
    click.echo(f"[domain approve] approved_by   : {approver}")
    click.echo(f"[domain approve] contract_hash : {contract_hash}")


@domain_cmd.command("status")
@click.option(
    "--file",
    "contract_path",
    default=None,
    type=click.Path(),
    help="Optional explicit path to domain.yaml (or legacy domain.json for diagnostics).",
)
@click.option(
    "--state-dir",
    default=str(Path(".fkg") / "l1"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Schema-2 L1 state directory containing approval artifacts.",
)
def domain_status_cmd(contract_path: str | None, state_dir: Path) -> None:
    """Report contract, review, approval, and enrichment readiness status."""
    status = evaluate_domain_guard_status(
        contract_path,
        l1_state_root=state_dir,
    )
    click.echo(f"[domain status] contract path         : {status.contract_path}")
    click.echo(f"[domain status] review path           : {status.review_path}")
    click.echo(f"[domain status] legacy path           : {status.legacy_path}")
    click.echo(f"[domain status] contract hash         : {status.contract_hash}")
    click.echo(
        f"[domain status] deterministic errors : {status.deterministic_error_count}"
    )
    click.echo(
        f"[domain status] deterministic warnings: {status.deterministic_warning_count}"
    )
    click.echo(
        f"[domain status] ready for enrichment : {status.ready_for_enrichment}"
    )
    if status.messages:
        for message in status.messages:
            click.echo(f"[domain status] note: {message}")


@domain_cmd.command("convert-legacy")
@click.option(
    "--legacy-file",
    default="build\\enriched\\domain.json",
    show_default=True,
    type=click.Path(exists=True),
    help="Path to the legacy domain.json file created by set-domain.",
)
@click.option(
    "--out",
    "output_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(),
    help="Path to write the converted v1 domain contract.",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite the output file if it already exists.")
def domain_convert_legacy_cmd(
    legacy_file: str,
    output_path: str,
    force: bool,
) -> None:
    """Convert legacy domain.json into a review-required v1 domain.yaml."""
    out_path = Path(output_path)
    if out_path.exists() and not force:
        raise click.ClickException(
            f"Refusing to overwrite existing contract '{out_path}'. Re-run with --force."
        )
    brief = load_legacy_domain_brief(legacy_file)
    contract = convert_legacy_brief_to_contract(brief)
    save_domain_contract(contract, out_path)
    click.echo(f"[domain convert-legacy] wrote converted contract → {out_path}")
    click.echo(
        "[domain convert-legacy] next steps: fill in TODO fields, run 'domain review', then 'domain approve'."
    )
