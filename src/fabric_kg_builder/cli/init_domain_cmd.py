"""init-domain command — inspect source files then guide domain contract authoring.

Workflow:
  1. Inspect source files and build a SourceProfile (observed facts + inferred suggestions).
  2. Present the profile, clearly separating observed facts from inferred suggestions.
  3. Incorporate an existing domain description if supplied or found.
  4. Ask for user approval (default N; --approve to auto-approve in CI/CD).
  5. Persist the approved profile to .fkg/source-profile.json.
  6. Ask only questions not already resolved by the profile.
  7. Generate and save a draft domain.yaml contract.

Existing commands (inspect-source, domain init, enrich) are unchanged.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

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
_L1_AUDIT_PATH_SEGMENTS = {
    "business_goal",
    "competency_questions",
    "contract_version",
    "decisions",
    "domain_boundary_candidates",
    "draft_contract",
    "domain_intake_id",
    "end_type_id",
    "generalization_candidates",
    "identity",
    "in_scope",
    "intake_hash",
    "organization_context",
    "out_of_scope",
    "proposal",
    "provider",
    "question_id",
    "question_routes",
    "relationship_candidates",
    "root",
    "score",
    "score_inputs",
    "semantic_type_candidates",
    "start_type_id",
    "unsupported_reason",
}

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


@click.command(
    "init-domain",
    epilog=_INIT_DOMAIN_EPILOG,
    context_settings={"max_content_width": 120},
)
@click.option(
    "--input",
    "input_path",
    default=None,
    type=click.Path(),
    help="Path to source file or directory to inspect before generating questions.",
)
@click.option(
    "--out",
    "output_path",
    default="domain.yaml",
    show_default=True,
    type=click.Path(),
    help="Path to write the draft domain contract YAML.",
)
@click.option(
    "--profile-out",
    "profile_path",
    default=str(_DEFAULT_PROFILE_PATH),
    show_default=True,
    type=click.Path(),
    help="Path to persist the approved source profile JSON.",
)
@click.option(
    "--domain-description",
    "domain_description",
    default=None,
    help="Existing domain description to incorporate before generating questions.",
)
@click.option(
    "--domain-file",
    "domain_file",
    default=None,
    type=click.Path(),
    help="Existing domain.yaml to read description from (alternative to --domain-description).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing domain contract and profile without prompting.",
)
@click.option(
    "--approve",
    is_flag=True,
    default=False,
    help="Auto-approve profile without interactive prompt (CI/CD noninteractive mode).",
)
@click.option(
    "--interactive",
    "force_interactive",
    is_flag=True,
    default=False,
    help="Force interactive approval prompt regardless of TTY detection.",
)
@click.option(
    "--legacy-schema-1",
    is_flag=True,
    default=False,
    help="Use the unchanged schema-1 source-profile and draft workflow.",
)
@click.option(
    "--intake",
    "intake_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 YAML/JSON intake with five to ten competency questions.",
)
@click.option(
    "--candidates",
    "candidates_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Optional schema-2 proposal-candidate fixture for offline automation.",
)
@click.option(
    "--source-corpus-manifest",
    "source_corpus_manifest_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Existing immutable corpus manifest to reconcile against --input.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Write a blocked schema-2 draft; explicit 'domain approve' is required.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Inventory and validate only; make no remote calls and write nothing.",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Reuse an intact succeeded L1 output when all skip bindings match.",
)
@click.option(
    "--state-dir",
    default=str(Path(".fkg") / "l1"),
    show_default=True,
    type=click.Path(file_okay=False),
    help="Schema-2 L1 runtime-state directory.",
)
@click.option(
    "--project-id",
    default=None,
    help="Stable project identity for schema-2 artifacts.",
)
@click.pass_context
def init_domain_cmd(
    ctx: click.Context,
    input_path: str | None,
    output_path: str,
    profile_path: str,
    domain_description: str | None,
    domain_file: str | None,
    force: bool,
    approve: bool,
    force_interactive: bool,
    legacy_schema_1: bool,
    intake_path: str | None,
    candidates_path: str | None,
    source_corpus_manifest_path: str | None,
    non_interactive: bool,
    dry_run: bool,
    resume: bool,
    state_dir: str,
    project_id: str | None,
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
    if not legacy_schema_1:
        _run_schema_2_l1(
            ctx=ctx,
            input_path=input_path,
            output_path=output_path,
            intake_path=intake_path,
            candidates_path=candidates_path,
            source_corpus_manifest_path=source_corpus_manifest_path,
            non_interactive=non_interactive,
            force_interactive=force_interactive,
            dry_run=dry_run,
            resume=resume,
            force=force,
            approve=approve,
            state_dir=state_dir,
            project_id=project_id,
        )
        return

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


def _load_mapping(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            loaded = json.loads(text)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            loaded = yaml.safe_load(text)
        else:
            raise click.ClickException("automation files must be YAML or JSON")
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not load '{path}': {exc}") from exc
    if not isinstance(loaded, dict):
        raise click.ClickException(f"'{path}' must contain a mapping")
    return loaded


def _collect_schema_2_intake() -> dict:
    questions = [
        item.strip()
        for item in click.prompt(
            "Competency questions (5-10, semicolon-separated)"
        ).split(";")
        if item.strip()
    ]
    return {
        "business_goal": click.prompt("Business goal"),
        "organization_context": click.prompt("Organization context"),
        "users": _comma_list_prompt("Primary users (comma-separated)"),
        "decisions": _comma_list_prompt("Supported decisions (comma-separated)"),
        "desired_outcomes": _comma_list_prompt(
            "Desired outcomes (comma-separated)"
        ),
        "in_scope": _comma_list_prompt("In-scope concepts (comma-separated)"),
        "out_of_scope": _comma_list_prompt(
            "Out-of-scope concepts (comma-separated)", "none"
        ),
        "competency_questions": questions,
    }


def _schema_2_actor() -> str:
    return (
        os.environ.get("FABRIC_KG_APPROVER")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "local-user"
    )


def _persist_early_l1_failure_audit(
    *,
    state_root: Path,
    project_id: str,
    run_id: str,
    path: str,
    code: str,
) -> Path:
    state_root.mkdir(parents=True, exist_ok=True)
    audit_path = state_root / "proposal-failure-audit.json"
    temporary = audit_path.with_name(
        f".{audit_path.name}.{os.getpid()}.tmp"
    )
    payload = {
        "schema_version": "1.0.0",
        "error_code": "L1_STAGE_FAILED",
        "run_id": run_id,
        "project_id": project_id,
        "model_version": "not-resolved",
        "model_hash": None,
        "intake_hash": None,
        "attempt_count": 0,
        "failures": [{"path": path, "code": code}],
    }
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(audit_path)
    return audit_path


def _build_schema_2_client(ctx: click.Context):
    from .domain_cmd import _build_foundry_client, _resolve_model_version

    ctx.ensure_object(dict)
    client = ctx.obj.get("_foundry_client") if ctx.obj else None
    if client is None:
        client = _build_foundry_client(ctx.obj or {})
    return client, _resolve_model_version(client, ctx.obj or {})


def _run_schema_2_l1(
    *,
    ctx: click.Context,
    input_path: str | None,
    output_path: str,
    intake_path: str | None,
    candidates_path: str | None,
    source_corpus_manifest_path: str | None,
    non_interactive: bool,
    force_interactive: bool,
    dry_run: bool,
    resume: bool,
    force: bool,
    approve: bool,
    state_dir: str,
    project_id: str | None,
) -> None:
    import sys

    from fabric_kg_builder.contracts.base import canonical_sha256
    from fabric_kg_builder.domain.proposal import compute_model_hash
    from fabric_kg_builder.domain.stage import (
        L1StageError,
        L1ProposalSchemaRepairError,
        L1ZeroSupportedRoutesError,
        dry_run_l1,
        finalize_l1_stage,
        load_prepared_l1_stage,
        preflight_l1_inputs,
        prepare_l1_stage,
        try_resume_l1,
    )

    state_root = Path(state_dir)
    provisional_source = Path(input_path) if input_path else Path("unknown")
    effective_project_id = (
        project_id
        or os.environ.get("FABRIC_KG_PROJECT_ID")
        or f"project:{provisional_source.resolve().name}"
    )
    run_id = f"run:{uuid.uuid4().hex}"

    def fail_precondition(path: str, code: str) -> None:
        audit_path = _persist_early_l1_failure_audit(
            state_root=state_root,
            project_id=effective_project_id,
            run_id=run_id,
            path=path,
            code=code,
        )
        raise click.ClickException(
            f"L1_STAGE_FAILED; audit={audit_path}"
        )

    if input_path is None:
        fail_precondition("preflight.source", "input_required")
        raise click.ClickException(
            "Schema-2 L1 requires --input for complete corpus inventory. "
            "Use --legacy-schema-1 for the prior workflow."
        )
    if force_interactive and non_interactive:
        fail_precondition("preflight.mode", "mode_conflict")
        raise click.ClickException(
            "--interactive and --non-interactive are mutually exclusive"
        )
    if approve:
        fail_precondition("preflight.approval", "approve_not_supported")
        raise click.ClickException(
            "--approve is schema-1-only. Schema-2 requires the one-summary "
            "interactive decision or explicit 'fabric-kg domain approve'."
        )
    source_path = Path(input_path)
    if not source_path.exists():
        audit_path = _persist_early_l1_failure_audit(
            state_root=state_root,
            project_id=effective_project_id,
            run_id=run_id,
            path="preflight.source",
            code="source_not_found",
        )
        raise click.ClickException(
            f"L1_STAGE_FAILED; audit={audit_path}"
        )
    out_path = Path(output_path)
    if out_path.exists() and not force and not resume:
        fail_precondition("preflight.output", "output_exists")
        raise click.ClickException(
            f"Domain contract already exists at '{out_path}'. "
            "Use --resume or --force."
        )
    if intake_path is not None:
        try:
            intake_raw = _load_mapping(Path(intake_path))
        except Exception as exc:
            audit_path = _persist_early_l1_failure_audit(
                state_root=state_root,
                project_id=effective_project_id,
                run_id=run_id,
                path="preflight.intake",
                code="intake_load_failed",
            )
            raise click.ClickException(
                f"L1_STAGE_FAILED; audit={audit_path}"
            ) from exc
    elif non_interactive or dry_run:
        fail_precondition("preflight.intake", "intake_required")
        raise click.ClickException(
            "--intake is required for schema-2 non-interactive and dry-run modes"
        )
    else:
        intake_raw = _collect_schema_2_intake()

    client = None
    if candidates_path is not None:
        try:
            candidates_raw = _load_mapping(Path(candidates_path))
            candidates = candidates_raw
            model_version = "offline-candidate-fixture/1.0.0"
            model_hash = canonical_sha256(
                {"model_version": model_version, "candidates": candidates_raw}
            )
        except Exception as exc:
            audit_path = _persist_early_l1_failure_audit(
                state_root=state_root,
                project_id=effective_project_id,
                run_id=run_id,
                path="proposal.candidates",
                code="candidate_load_failed",
            )
            raise click.ClickException(
                f"L1_STAGE_FAILED; audit={audit_path}"
            ) from exc
    else:
        candidates = None
        if dry_run:
            model_version = "planned-foundry-model"
            model_hash = canonical_sha256(
                {"model_version": model_version, "dry_run": True}
            )
        else:
            try:
                client, model_version = _build_schema_2_client(ctx)
                model_hash = compute_model_hash(client, model_version)
            except Exception as exc:
                audit_path = _persist_early_l1_failure_audit(
                    state_root=state_root,
                    project_id=effective_project_id,
                    run_id=run_id,
                    path="proposal.provider",
                    code="client_construction_failed",
                )
                raise click.ClickException(
                    f"L1_STAGE_FAILED; audit={audit_path}"
                ) from exc
    previous = None
    if resume and (state_root / "stage-receipt.json").exists():
        try:
            previous = load_prepared_l1_stage(state_root=state_root)
            run_id = previous.preflight.run_id
        except L1StageError:
            previous = None
    preflight = None
    proposal_started = False

    def failure_error_code(exc: Exception) -> str:
        if isinstance(exc, L1ZeroSupportedRoutesError):
            return "L1_ZERO_SUPPORTED_ROUTES"
        if isinstance(exc, L1ProposalSchemaRepairError):
            return "L1_PROPOSAL_SCHEMA_REPAIR_EXHAUSTED"
        return (
            "L1_PROPOSAL_FAILED"
            if proposal_started
            else "L1_STAGE_FAILED"
        )

    def persist_failure_audit(
        exc: Exception,
        *,
        details: dict | None = None,
    ) -> Path:
        def safe_path(parts: tuple[object, ...]) -> str:
            return ".".join(
                str(part)
                if isinstance(part, int)
                or str(part) == "[index]"
                or str(part) in _L1_AUDIT_PATH_SEGMENTS
                else "unknown_field"
                for part in parts
            )

        failures: list[dict[str, str]] = []
        if isinstance(exc, L1ZeroSupportedRoutesError):
            failures = [
                {
                    "path": "proposal.selection",
                    "code": exc.audit_payload.terminal_error_code,
                }
            ]
        elif isinstance(exc, L1ProposalSchemaRepairError):
            failures = [
                {
                    "path": safe_path(tuple(path.split("."))),
                    "code": code,
                }
                for path, code in exc.validation_failures
            ]
        elif isinstance(exc, ValidationError):
            failures = [
                {
                    "path": safe_path(tuple(item["loc"])),
                    "code": str(item["type"]),
                }
                for item in exc.errors(
                    include_url=False,
                    include_input=False,
                )
            ][:20]
        else:
            text = str(exc)
            candidate_code = next(
                (
                    token.strip("[]")
                    for token in text.split()
                    if token.startswith("[DOM-")
                ),
                "",
            )
            code = (
                candidate_code
                if candidate_code in {"DOM-101", "DOM-103", "DOM-104", "DOM-105"}
                else (
                    "provider_or_validation_failure"
                    if proposal_started
                    else "preflight_failure"
                )
            )
            failures = [
                {
                    "path": (
                        "proposal.selection"
                        if proposal_started
                        else "preflight"
                    ),
                    "code": code,
                }
            ]
        audit = {
            "schema_version": "1.0.0",
            "error_code": failure_error_code(exc),
            "run_id": run_id,
            "project_id": effective_project_id,
            "model_version": model_version,
            "model_hash": model_hash,
            "intake_hash": (
                preflight.intake.intake_hash
                if preflight is not None
                else None
            ),
            "attempt_count": getattr(
                exc,
                "attempt_count",
                1 if proposal_started else 0,
            ),
            "failures": failures,
        }
        candidate_attempts = getattr(exc, "candidate_attempts", ())
        if candidate_attempts:
            audit["candidate_attempts"] = [
                {
                    "attempt": int(item["attempt"]),
                    "candidate_hash": str(item["candidate_hash"]),
                    "proposed_type_count": int(
                        item["proposed_type_count"]
                    ),
                    "proposed_relationship_count": int(
                        item["proposed_relationship_count"]
                    ),
                    "failures": [
                        {
                            "path": safe_path(
                                tuple(str(failure["path"]).split("."))
                            ),
                            "code": str(failure["code"]),
                        }
                        for failure in item["failures"]
                    ],
                }
                for item in candidate_attempts
            ]
        if details:
            audit.update(details)
        state_root.mkdir(parents=True, exist_ok=True)
        audit_path = state_root / "proposal-failure-audit.json"
        temporary_audit = audit_path.with_name(
            f".{audit_path.name}.{os.getpid()}.tmp"
        )
        with temporary_audit.open("w", encoding="utf-8") as stream:
            json.dump(audit, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_audit.replace(audit_path)
        return audit_path

    try:
        preflight = preflight_l1_inputs(
            source_path=source_path,
            intake_raw=intake_raw,
            project_id=effective_project_id,
            run_id=run_id,
            model_version=model_version,
            model_hash=model_hash,
            source_corpus_manifest_path=(
                Path(source_corpus_manifest_path)
                if source_corpus_manifest_path is not None
                else None
            ),
        )
        if dry_run:
            result = dry_run_l1(
                preflight,
                state_root=state_root,
                domain_path=out_path,
            )
            click.echo(f"[init-domain] {result.summary}")
            for path in result.planned_paths:
                click.echo(f"[init-domain] planned artifact: {path}")
            return
        if previous is not None:
            current_prepared = replace(previous, preflight=preflight)
            resumed = try_resume_l1(
                current_prepared,
                state_root=state_root,
                domain_path=out_path,
            )
            if resumed is not None:
                click.echo("[init-domain] L1 skipped; prior output is intact.")
                return
        proposal_started = True
        prepared = prepare_l1_stage(
            preflight,
            candidates=candidates,
            client=client,
        )
        if non_interactive or not (force_interactive or sys.stdin.isatty()):
            result = finalize_l1_stage(
                prepared,
                decision=None,
                actor=None,
                state_root=state_root,
                domain_path=out_path,
            )
            click.echo(prepared.summary)
            click.echo(
                "[init-domain] schema-2 draft persisted with blocked receipt; "
                "run 'fabric-kg domain approve --approved-by ...'."
            )
            return

        while True:
            click.echo("")
            click.echo(prepared.summary)
            click.echo("")
            decision = click.prompt(
                "Decision",
                type=click.Choice(
                    ["approve", "correct", "abort"],
                    case_sensitive=False,
                ),
            ).lower()
            actor = _schema_2_actor()
            if decision == "correct":
                correction = click.prompt("Correction instruction").strip()
                correction_result = finalize_l1_stage(
                    prepared,
                    decision="correct",
                    actor=actor,
                    correction_text=correction,
                    state_root=state_root,
                    domain_path=out_path,
                )
                if candidates is not None:
                    raise click.ClickException(
                        "Offline candidate fixtures cannot regenerate after correction"
                    )
                prepared = prepare_l1_stage(
                    preflight,
                    client=client,
                    correction_instruction=correction,
                    parent_correction_context_id=(
                        correction_result.approval_context.domain_approval_context_id
                        if correction_result.approval_context is not None
                        else None
                    ),
                )
                continue
            result = finalize_l1_stage(
                prepared,
                decision=decision,
                actor=actor,
                state_root=state_root,
                domain_path=out_path,
            )
            if decision == "abort":
                click.echo("[init-domain] aborted with a blocked L1 receipt.", err=True)
                raise click.exceptions.Exit(4)
            click.echo(
                f"[init-domain] approved schema-2 domain → {out_path}; "
                f"receipt={result.receipt.stage_receipt_id}"
            )
            return
    except L1ZeroSupportedRoutesError as exc:
        audit_path = persist_failure_audit(
            exc,
            details={
                "zero_route_audit": exc.audit_payload.model_dump(
                    mode="json"
                )
            },
        )
        raise click.ClickException(
            f"{exc.error_code}; audit={audit_path}"
        ) from exc
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        audit_path = persist_failure_audit(exc)
        if proposal_started:
            raise click.ClickException(
                f"{failure_error_code(exc)}; "
                f"audit={audit_path}"
            ) from exc
        raise click.ClickException(
            f"L1_STAGE_FAILED; audit={audit_path}"
        ) from exc
