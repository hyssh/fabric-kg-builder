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

import os
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
def init_domain_cmd(
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
