"""Copilot-assisted schema-2.0 domain proposal initialization.

The new-project default collects unresolved intake, inspects bounded source
samples, generates candidates, applies local N/K authority, and presents one
approve/correct/abort summary. YAML/JSON automation generates draft artifacts
only. The previous schema-1.0 profile workflow remains available explicitly
through ``--legacy-schema-1`` and the legacy ``--approve`` alias.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import click

from fabric_kg_builder.domain import (
    ApprovalMetadata,
    compute_contract_hash,
    default_domain_contract,
    load_domain_contract,
    save_domain_contract,
    utc_now_text,
)
from fabric_kg_builder.sources.inspector import (
    SourceProfile,
    build_source_profile,
    load_source_profile,
    render_profile_text,
    save_source_profile,
)


_DEFAULT_PROFILE_PATH = Path(".fkg") / "source-profile.json"
_DEFAULT_CONTRACT_PATH = Path("domain.yaml")

_UNRESOLVED_QUESTIONS: list[tuple[str, str, str]] = [
    # (field_path, prompt_text, default)
    (
        "domain_name",
        "Domain name",
        "My Domain",
    ),
    (
        "domain_description",
        "Domain description (brief statement of purpose)",
        "",
    ),
    (
        "temporal_constraints",
        "Temporal constraints (e.g. 'historical records back to 2001', or 'none')",
        "none",
    ),
    (
        "relationship_approval",
        "Which relationship types require human approval? (comma-separated, or 'none')",
        "none",
    ),
    (
        "historical_separation",
        "Should historical records be kept as separate entities? [y/N]",
        "N",
    ),
]


def _resolve_domain_context(
    domain_description_opt: str | None,
    domain_file: Path | None,
) -> tuple[str | None, str | None]:
    """Return (effective_description, domain_hash) from option or existing contract.

    domain_hash is the contract_hash of the loaded domain.yaml, or None.
    """
    if domain_description_opt:
        return domain_description_opt.strip() or None, None
    if domain_file and domain_file.exists():
        try:
            contract = load_domain_contract(str(domain_file))
            description = contract.domain.description or None
            domain_hash = compute_contract_hash(contract)
            return description, domain_hash
        except Exception:
            pass
    return None, None


def _check_legacy_domain_json(cwd: Path) -> None:
    """Warn if a legacy domain.json is found alongside the expected domain.yaml."""
    legacy_candidates = [
        cwd / "domain.json",
        cwd / "build" / "enriched" / "domain.json",
    ]
    for candidate in legacy_candidates:
        if candidate.exists():
            click.echo(
                f"[init-domain] Warning: legacy domain.json found at '{candidate}'. "
                "Run 'fabric-kg domain convert-legacy' to migrate it first.",
                err=True,
            )
            break


def _approve_interactively(profile: SourceProfile) -> tuple[bool, SourceProfile]:
    """Present the profile and prompt for approval, correction, or abort.

    The user may:
    - Enter ``y`` to approve.
    - Enter ``c`` to enter correction mode (edit categories, entities, risks, dates),
      then be re-presented with the updated profile for approval.
    - Enter ``n`` (or press Enter) to abort.

    Returns ``(approved, corrected_profile)``.
    """
    while True:
        click.echo("")
        click.echo(render_profile_text(profile))
        click.echo("")
        click.echo(
            "Inferred suggestions (categories, entity candidates, extraction risks) are "
            "heuristic guesses from filenames and schema — verify before accepting."
        )
        click.echo("")

        answer = click.prompt(
            "Approve [y], Correct [c], Abort [n]",
            default="n",
            show_default=False,
        ).strip().lower()

        if answer == "y":
            return True, profile

        if answer == "c":
            profile = _apply_corrections(profile)
            continue  # re-render updated profile and ask again

        if answer in {"n", ""}:
            click.echo(
                "[init-domain] Profile not approved. "
                "Re-run with corrected source files or --domain-description."
            )
            return False, profile

        click.echo("Please enter 'y' to approve, 'c' to correct, or 'n' (default) to abort.")


def _apply_corrections(profile: SourceProfile) -> SourceProfile:
    """Interactively correct inferred fields and observed date range.

    Covers the fields most likely to be wrong: document categories, entity
    candidates, extraction risks, and the observed date range.  Pressing Enter
    at any prompt keeps the current value.  Returns a new :class:`SourceProfile`
    with ``user_corrected=True`` when any field was changed.
    """
    click.echo("")
    click.echo("Correction mode — press Enter at any prompt to keep the current value.")

    inf = profile.inferred.model_copy(deep=True)
    obs = profile.observed.model_copy(deep=True)
    any_change = False

    # 1. Document categories
    current_cats = (
        ", ".join(inf.document_categories) if inf.document_categories else "(none)"
    )
    click.echo(f"\n  Current inferred document categories: {current_cats}")
    new_cats_raw = click.prompt(
        "  New categories (comma-separated, or Enter to keep)",
        default="",
        show_default=False,
    ).strip()
    if new_cats_raw:
        new_cats = [c.strip() for c in new_cats_raw.split(",") if c.strip()]
        if new_cats != inf.document_categories:
            inf = inf.model_copy(update={"document_categories": new_cats})
            any_change = True

    # 2. Entity candidates
    current_ents = (
        ", ".join(inf.entity_candidates) if inf.entity_candidates else "(none)"
    )
    click.echo(f"\n  Current inferred entity candidates: {current_ents}")
    new_ents_raw = click.prompt(
        "  New entity candidates (comma-separated, or Enter to keep)",
        default="",
        show_default=False,
    ).strip()
    if new_ents_raw:
        new_ents = [e.strip() for e in new_ents_raw.split(",") if e.strip()]
        if new_ents != inf.entity_candidates:
            inf = inf.model_copy(update={"entity_candidates": new_ents})
            any_change = True

    # 3. Extraction risks
    if inf.extraction_risks:
        click.echo("\n  Current extraction risks:")
        for r in inf.extraction_risks:
            click.echo(f"    - {r}")
    else:
        click.echo("\n  No extraction risks detected.")
    clear_risks = click.prompt(
        "  Clear all extraction risks? [y/N]",
        default="N",
        show_default=False,
    ).strip().lower()
    if clear_risks == "y" and inf.extraction_risks:
        inf = inf.model_copy(update={"extraction_risks": []})
        any_change = True

    # 4. Date range
    current_dr = (
        f"{obs.date_range[0]}–{obs.date_range[1]}" if obs.date_range else "(none)"
    )
    click.echo(f"\n  Current observed date range: {current_dr}")
    new_dr_raw = click.prompt(
        "  New date range (e.g. 2001,2024 or just 2024, or Enter to keep)",
        default="",
        show_default=False,
    ).strip()
    if new_dr_raw and new_dr_raw.lower() not in {"none", ""}:
        # Accept "2001,2024", "2001–2024", "2001-2024", or a single year
        sep_char = "," if "," in new_dr_raw else ("–" if "–" in new_dr_raw else "-")
        parts = [p.strip() for p in new_dr_raw.split(sep_char) if p.strip()]
        if len(parts) == 1:
            parts = [parts[0], parts[0]]
        if len(parts) >= 2:
            new_dr = [parts[0], parts[-1]]
            if new_dr != obs.date_range:
                obs = obs.model_copy(update={"date_range": new_dr})
                any_change = True

    return profile.model_copy(
        update={
            "inferred": inf,
            "observed": obs,
            "user_corrected": profile.user_corrected or any_change,
        }
    )


def _ask_unresolved_questions(profile: SourceProfile) -> dict[str, str]:
    """Ask only questions not already resolved by the profile."""
    answers: dict[str, str] = {}
    click.echo("")
    click.echo("Unresolved questions:")

    q_num = 0
    for field_path, prompt_text, default in _UNRESOLVED_QUESTIONS:
        # Skip domain description if already in profile
        if field_path == "domain_description" and profile.domain_description:
            answers[field_path] = profile.domain_description
            continue
        # Skip temporal constraints if date range already observed
        if field_path == "temporal_constraints" and profile.observed.date_range:
            dr = profile.observed.date_range
            answers[field_path] = f"date range {dr[0]}–{dr[1]}"
            continue
        q_num += 1
        answers[field_path] = click.prompt(
            f"  {q_num}. {prompt_text}",
            default=default,
            show_default=bool(default),
        )

    if q_num == 0:
        click.echo("  (no unresolved questions — profile provides sufficient context)")

    return answers


def _build_contract_from_profile(
    profile: SourceProfile,
    answers: dict[str, str],
) -> "object":  # DomainContract
    """Build a draft DomainContract from an approved profile and answered questions.

    Provenance rule: inferred suggestions (entity candidates, document categories)
    are only carried into the contract when the user explicitly confirmed them via
    the correction flow (``profile.user_corrected=True``).  Auto-approved profiles
    leave those fields as TODO placeholders so domain review fills them in with
    ground truth rather than heuristic guesses.
    """
    contract = default_domain_contract()

    # Domain name
    domain_name = answers.get("domain_name", "").strip() or "My Domain"
    contract.domain.name = domain_name

    # Domain description — prefer existing over answered
    desc = profile.domain_description or answers.get("domain_description", "").strip()
    if desc:
        contract.domain.description = desc

    # Inferred document categories — only use when user explicitly confirmed them.
    # Without correction, leave as TODO to prevent mislabelling inference as fact.
    if profile.user_corrected and profile.inferred.document_categories:
        contract.domain.subdomains = list(profile.inferred.document_categories)

    # Inferred entity candidates — same provenance rule as document categories.
    if profile.user_corrected and profile.inferred.entity_candidates:
        contract.candidate_model.entity_categories = list(profile.inferred.entity_candidates)

    # Temporal constraints
    temporal_answer = answers.get("temporal_constraints", "none").strip()
    if temporal_answer and temporal_answer.lower() != "none":
        contract.constraints.temporal = [temporal_answer]

    contract.approval = ApprovalMetadata(status="draft")
    return contract


_INIT_DOMAIN_EPILOG = """\b
Example:
  fabric-kg init-domain --input ./facility-records
  fabric-kg init-domain --input ./data --approve --out domain.yaml
  fabric-kg init-domain --input ./docs --domain-description "Facility asset management"

Exit codes: 0 success · 1 error · 4 user rejected profile (interactive only).

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


def _run_legacy_init_domain(
    input_path: str | None,
    output_path: str,
    profile_path: str,
    domain_description: str | None,
    domain_file: str | None,
    force: bool,
    approve: bool,
    force_interactive: bool,
) -> None:
    """Inspect source files then guide domain contract authoring.

    Inspects source files first, presents a profile of observed facts and
    inferred suggestions, asks only unresolved questions, then writes a
    draft domain.yaml contract.

    Use --approve (or redirect stdin) for noninteractive CI/CD execution.
    Use --interactive to force the approval prompt regardless of TTY detection.
    Use --domain-description or --domain-file to incorporate an existing
    domain context before generating questions.

    The approved profile is persisted to .fkg/source-profile.json so that
    later commands (enrich, compile-data) can reuse the same context.

    Exit codes: 0 success · 1 error · 4 user rejected profile.
    """
    import sys

    out_path = Path(output_path)
    prof_path = Path(profile_path)
    df_path = Path(domain_file) if domain_file else _DEFAULT_CONTRACT_PATH

    # Guard: do not overwrite existing domain contract without --force
    if out_path.exists() and not force:
        raise click.ClickException(
            f"Domain contract already exists at '{out_path}'. "
            "Re-run with --force to overwrite, or choose a different --out path."
        )

    # Warn about legacy domain.json (R6)
    _check_legacy_domain_json(Path.cwd())

    # Resolve domain description and hash
    effective_description, domain_hash = _resolve_domain_context(
        domain_description, df_path
    )

    # --- Step 1: Inspect source files ---
    if input_path is not None:
        src = Path(input_path)
        if not src.exists():
            raise click.ClickException(f"Source path not found: {input_path}")
        click.echo(f"[init-domain] inspecting source path: {src}")
        profile = build_source_profile(src, domain_description=effective_description)
        profile.domain_hash = domain_hash
        click.echo(
            f"[init-domain] found {profile.observed.total_file_count} supported file(s)"
        )
    else:
        # No source path — create an empty profile with domain description only
        profile = SourceProfile(
            domain_description=effective_description,
            domain_hash=domain_hash,
            inspected_at_utc=utc_now_text(),
        )
        click.echo(
            "[init-domain] no --input path provided; skipping source inspection"
        )

    # --- Step 2: Detect interactive vs noninteractive mode ---
    # --approve always wins; --interactive forces interactive even without TTY
    is_interactive = force_interactive or (not approve and sys.stdin.isatty())

    if is_interactive:
        # Present profile and ask for approval
        approved, profile = _approve_interactively(profile)
        if not approved:
            click.echo("[init-domain] aborted — profile not approved.", err=True)
            sys.exit(4)
    else:
        # Noninteractive: auto-approve and echo profile summary
        click.echo("")
        click.echo(render_profile_text(profile))
        click.echo("")
        click.echo("[init-domain] noninteractive mode — auto-approving source profile")

    # --- Step 3: Mark profile approved and persist ---
    approver = (
        os.environ.get("FABRIC_KG_APPROVER")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "unknown"
    )
    profile.approved = True
    profile.approved_at_utc = utc_now_text()
    profile.approved_by = approver

    save_source_profile(profile, prof_path)
    click.echo(f"[init-domain] source profile persisted → {prof_path}")

    # --- Step 4: Collect unresolved answers ---
    if is_interactive:
        answers = _ask_unresolved_questions(profile)
    else:
        answers = _noninteractive_defaults(profile)

    # --- Step 5: Build and save domain contract ---
    contract = _build_contract_from_profile(profile, answers)
    save_domain_contract(contract, out_path)

    click.echo(f"[init-domain] draft domain contract written → {out_path}")
    click.echo(
        "[init-domain] next steps: 'fabric-kg domain validate' → "
        "'domain review' → 'domain approve'"
    )


def _noninteractive_defaults(profile: SourceProfile) -> dict[str, str]:
    """Return deterministic default answers for all unresolved questions."""
    answers: dict[str, str] = {}
    for field_path, _prompt, default in _UNRESOLVED_QUESTIONS:
        if field_path == "domain_description" and profile.domain_description:
            answers[field_path] = profile.domain_description
        elif field_path == "temporal_constraints" and profile.observed.date_range:
            dr = profile.observed.date_range
            answers[field_path] = f"date range {dr[0]}–{dr[1]}"
        else:
            answers[field_path] = default
    return answers


def _slug(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:48] or fallback


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _collect_v2_intake(profile: SourceProfile):
    from fabric_kg_builder.domain import DomainIntake

    goal = profile.domain_description or click.prompt(
        "What business outcome should this graph support?"
    )
    users = _split_list(
        click.prompt("Who will use it? (comma-separated)")
    )
    decisions = _split_list(
        click.prompt("What decisions will they make? (comma-separated)")
    )
    in_scope = _split_list(
        click.prompt("In-scope concepts (comma-separated)")
    )
    out_of_scope = _split_list(
        click.prompt("Explicitly out-of-scope concepts (comma-separated)", default="none")
    )
    question_count = click.prompt(
        "How many competency questions?",
        default=5,
        type=click.IntRange(5, 10),
    )
    questions = []
    used_ids: set[str] = set()
    for index in range(question_count):
        question = click.prompt(f"Competency question {index + 1}")
        base = _slug(question, f"question-{index + 1}")
        question_id = f"cq:{base}"
        suffix = 2
        while question_id in used_ids:
            question_id = f"cq:{base}-{suffix}"
            suffix += 1
        used_ids.add(question_id)
        questions.append(
            {
                "id": question_id,
                "question": question,
                "business_critical": True,
            }
        )
    sensitive = _split_list(
        click.prompt(
            "Safety-, legal-, or policy-sensitive predicates (comma-separated)",
            default="none",
        )
    )
    return DomainIntake.model_validate(
        {
            "schema_version": "2.0",
            "business_goal": goal,
            "organization_context": goal,
            "users": users,
            "decisions": decisions,
            "desired_outcomes": [goal],
            "in_scope": in_scope,
            "out_of_scope": [
                item for item in out_of_scope if item.casefold() != "none"
            ],
            "competency_questions": questions,
            "sensitive_predicates": [
                item for item in sensitive if item.casefold() != "none"
            ],
        }
    )


def _build_proposal_client(ctx_obj: dict):
    from fabric_kg_builder.config.loader import load_config
    from fabric_kg_builder.enrichment.foundry_client import FoundryClient

    config = load_config(env=ctx_obj.get("env", "dev"))
    return FoundryClient(config.foundry)


def _proposal_model_version(client, ctx_obj: dict) -> str:
    injected = ctx_obj.get("_foundry_model_version")
    if isinstance(injected, str) and injected.strip():
        return injected.strip()
    config = getattr(client, "_config", None)
    deployment = getattr(config, "chat_deployment", "")
    return deployment.strip() if isinstance(deployment, str) and deployment.strip() else "unknown"


def _render_proposal_summary(proposal, profile: SourceProfile, findings) -> str:
    contract = proposal.contract
    lines = [
        "Domain proposal summary",
        f"  Domain: {contract.domain.name}",
        f"  Description: {contract.domain.description}",
        f"  Scope: {', '.join(contract.problem.in_scope)}",
        f"  Out of scope: {', '.join(contract.problem.out_of_scope) or '(none)'}",
        f"  Users: {', '.join(contract.business.users)}",
        f"  Decisions: {', '.join(contract.business.decisions)}",
        f"  Outcomes: {', '.join(contract.problem.desired_outcomes)}",
        "  Competency questions:",
    ]
    plans = {item.question_id: item for item in contract.question_plans}
    for question in contract.competency_questions:
        plan = plans[question.id]
        status = (
            f"covered in {plan.hop_count} hop(s)"
            if plan.covered
            else f"UNSUPPORTED: {plan.unsupported_reason}"
        )
        lines.append(f"    - {question.id}: {question.question} [{status}]")
    lines.append("  Entity types:")
    for entity in contract.candidate_model.entity_types:
        lines.append(f"    - {entity.id}: {entity.name}")
    lines.append("  Relationship types:")
    for relationship in contract.candidate_model.relationship_types:
        lines.append(
            "    - "
            f"{relationship.id}: {relationship.source_types} -> "
            f"{relationship.target_types}"
        )
    lines.extend(
        [
            f"  N: {contract.reasoning_policy.relationship_type_count} "
            "(deterministic minimum; advisory 8-20, no padding)",
            f"  K: {contract.reasoning_policy.max_hops} "
            "(maximum question-scoped shortest path)",
            "  Evidence examples:",
        ]
    )
    for item in proposal.evidence[:8]:
        lines.append(
            f"    - {item.id} [{item.sample_kind}] {item.citation}: {item.excerpt}"
        )
    if not proposal.evidence:
        lines.append("    - (no representative source excerpts available)")
    risks = list(profile.inferred.extraction_risks)
    risks.extend(
        item.message for item in getattr(profile, "sampling_warnings", [])
    )
    warnings = [item.message for item in findings if item.severity == "warning"]
    warnings.extend(proposal.warnings)
    if risks or warnings:
        lines.append("  Warnings and extraction risks:")
        lines.extend(f"    - {item}" for item in [*risks, *warnings])
    lines.extend(
        [
            "  Policies: closed vocabulary; exact evidence span required; "
            "abstain without evidence; asserted-only publication",
            f"  Schema: {contract.schema_version}",
            f"  Source-profile hash: {proposal.source_profile_hash}",
            f"  Proposal hash: {proposal.proposal_hash}",
            f"  Prompt: {proposal.prompt_version} ({proposal.prompt_hash})",
            f"  Model: {proposal.model_version} ({proposal.model_hash})",
            f"  Contract hash: {proposal.contract_hash}",
        ]
    )
    return "\n".join(lines)


def _resolve_interactive_approver(explicit: str | None) -> str:
    return (
        explicit
        or os.environ.get("FABRIC_KG_APPROVER")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "local-user"
    )


def _run_v2_init_domain(
    ctx: click.Context,
    *,
    input_path: str | None,
    output_path: str,
    profile_path: str,
    proposal_path: str,
    intake_path: str | None,
    domain_description: str | None,
    force: bool,
    non_interactive: bool,
    approved_by: str | None,
) -> None:
    from pydantic import ValidationError

    from fabric_kg_builder.domain import (
        ProposalArtifactError,
        ProposalSelectionError,
        approve_domain_proposal,
        generate_domain_proposal,
        load_domain_intake,
        run_deterministic_validation,
        save_domain_proposal,
    )

    out_path = Path(output_path)
    prof_path = Path(profile_path)
    prop_path = Path(proposal_path)
    for artifact in (out_path, prof_path, prop_path):
        if artifact.exists() and not force:
            raise click.ClickException(
                f"Refusing to overwrite existing artifact '{artifact}'. Re-run with --force."
            )

    if input_path:
        source_path = Path(input_path)
        if not source_path.exists():
            raise click.ClickException(f"Source path not found: {input_path}")
        profile = build_source_profile(
            source_path,
            domain_description=domain_description,
        )
    else:
        profile = SourceProfile(
            domain_description=domain_description,
            inspected_at_utc=utc_now_text(),
        )
    save_source_profile(profile, prof_path)

    if intake_path:
        try:
            intake = load_domain_intake(intake_path)
        except (ProposalArtifactError, ValidationError) as exc:
            raise click.ClickException(str(exc)) from exc
    elif non_interactive:
        raise click.ClickException(
            "--non-interactive requires a complete YAML or JSON --intake file."
        )
    else:
        try:
            intake = _collect_v2_intake(profile)
        except ValidationError as exc:
            raise click.ClickException(f"Interactive intake is invalid: {exc}") from exc

    ctx.ensure_object(dict)
    client = ctx.obj.get("_foundry_client")
    if client is None:
        try:
            client = _build_proposal_client(ctx.obj)
        except (EnvironmentError, ImportError, OSError, ValidationError) as exc:
            raise click.ClickException(
                f"Could not build Foundry client for domain proposal: {exc}"
            ) from exc
    model_version = _proposal_model_version(client, ctx.obj)
    correction: str | None = None

    while True:
        try:
            proposal = generate_domain_proposal(
                intake,
                profile,
                client=client,
                model_version=model_version,
                correction_instruction=correction,
            )
        except (ProposalArtifactError, ProposalSelectionError, ValidationError, ValueError) as exc:
            raise click.ClickException(f"Domain proposal generation failed: {exc}") from exc

        findings, _coverage = run_deterministic_validation(proposal.contract)
        errors = [item for item in findings if item.severity == "error"]
        save_domain_proposal(proposal, prop_path)
        save_domain_contract(proposal.contract, out_path)

        if non_interactive:
            click.echo(f"[init-domain] source profile written → {prof_path}")
            click.echo(f"[init-domain] draft proposal written → {prop_path}")
            click.echo(f"[init-domain] draft schema-2.0 contract written → {out_path}")
            click.echo(
                "[init-domain] approval is still required: fabric-kg domain approve "
                f"--file {out_path} --proposal {prop_path} --source-profile {prof_path} "
                '--approved-by "$OPERATOR"'
            )
            return

        click.echo("")
        click.echo(_render_proposal_summary(proposal, profile, findings))
        click.echo("")
        action = click.prompt(
            "Approve, Correct, or Abort",
            type=click.Choice(["approve", "correct", "abort"], case_sensitive=False),
            show_choices=True,
        ).casefold()
        if action == "abort":
            click.echo("[init-domain] aborted; draft artifacts remain unapproved.", err=True)
            raise click.exceptions.Exit(4)
        if action == "correct":
            correction = click.prompt("Correction instruction").strip()
            if not correction:
                raise click.ClickException("Correction instruction cannot be empty.")
            continue
        if errors:
            click.echo(
                f"[init-domain] approval blocked by {len(errors)} deterministic error(s); "
                "choose correct or abort.",
                err=True,
            )
            correction = click.prompt("Correction instruction").strip()
            if not correction:
                raise click.ClickException("Correction instruction cannot be empty.")
            continue

        approver = _resolve_interactive_approver(approved_by)
        try:
            approved = approve_domain_proposal(
                proposal.contract,
                proposal,
                profile,
                approved_by=approver,
                approved_at_utc=utc_now_text(),
            )
        except ProposalArtifactError as exc:
            raise click.ClickException(str(exc)) from exc
        profile.approved = True
        profile.approved_at_utc = utc_now_text()
        profile.approved_by = approver
        save_source_profile(profile, prof_path)
        save_domain_contract(approved, out_path)
        click.echo(f"[init-domain] approved schema-2.0 contract written → {out_path}")
        click.echo(f"[init-domain] cited proposal written → {prop_path}")
        return


_INIT_DOMAIN_V2_EPILOG = """\b
Examples:
  fabric-kg init-domain --input ./sources
  fabric-kg init-domain --input ./sources --intake domain-intake.yaml --non-interactive
  fabric-kg init-domain --legacy-schema-1 --input ./sources --approve

Noninteractive generation never approves. Use `fabric-kg domain approve` as a
separate explicit action.
"""


@click.command(
    "init-domain",
    epilog=_INIT_DOMAIN_V2_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option("--input", "input_path", type=click.Path(), help="Source file or directory.")
@click.option(
    "--out",
    "output_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(),
)
@click.option(
    "--profile-out",
    "profile_path",
    default=str(_DEFAULT_PROFILE_PATH),
    show_default=True,
    type=click.Path(),
)
@click.option(
    "--proposal-out",
    "proposal_path",
    default=str(Path(".fkg") / "domain-proposal.json"),
    show_default=True,
    type=click.Path(),
)
@click.option("--intake", "intake_path", type=click.Path(exists=True))
@click.option("--domain-description", default=None)
@click.option("--domain-file", default=None, type=click.Path())
@click.option("--force", is_flag=True, default=False)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Generate draft artifacts from --intake without approval.",
)
@click.option(
    "--approved-by",
    default=None,
    help="Identity recorded when an interactive user chooses approve.",
)
@click.option(
    "--legacy-schema-1",
    is_flag=True,
    default=False,
    help="Run the unchanged schema-1.0 source-profile workflow.",
)
@click.option(
    "--approve",
    is_flag=True,
    default=False,
    help="Legacy schema-1.0 compatibility alias; never approves schema 2.0.",
)
@click.option(
    "--interactive",
    "force_interactive",
    is_flag=True,
    default=False,
    help="Force terminal interaction (schema 2.0 is interactive by default).",
)
@click.pass_context
def init_domain_cmd(
    ctx: click.Context,
    input_path: str | None,
    output_path: str,
    profile_path: str,
    proposal_path: str,
    intake_path: str | None,
    domain_description: str | None,
    domain_file: str | None,
    force: bool,
    non_interactive: bool,
    approved_by: str | None,
    legacy_schema_1: bool,
    approve: bool,
    force_interactive: bool,
) -> None:
    """Generate and explicitly approve a cited schema-2.0 domain proposal."""

    if legacy_schema_1 or approve:
        if non_interactive or intake_path:
            raise click.ClickException(
                "Schema-1.0 compatibility mode cannot be combined with "
                "--non-interactive or --intake."
            )
        if approve and not legacy_schema_1:
            click.echo(
                "[init-domain] --approve selects legacy schema-1.0 compatibility; "
                "schema 2.0 is never auto-approved.",
                err=True,
            )
        _run_legacy_init_domain(
            input_path=input_path,
            output_path=output_path,
            profile_path=profile_path,
            domain_description=domain_description,
            domain_file=domain_file,
            force=force,
            approve=approve,
            force_interactive=force_interactive,
        )
        return

    _run_v2_init_domain(
        ctx,
        input_path=input_path,
        output_path=output_path,
        profile_path=profile_path,
        proposal_path=proposal_path,
        intake_path=intake_path,
        domain_description=domain_description,
        force=force,
        non_interactive=non_interactive,
        approved_by=approved_by,
    )
