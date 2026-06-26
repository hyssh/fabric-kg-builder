"""set-domain command — intake a user-supplied domain prompt.

Security: domain/user text is NEVER placed in the LLM system prompt.
See SPEC-004 §2.3 and fabric_kg_builder.enrichment.domain for details.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..enrichment.domain import (
    DomainBrief,
    load_domain_brief,
    rephrase_domain,
    save_domain_brief,
)


# ---------------------------------------------------------------------------
# Internal helpers (isolated for test-patching)
# ---------------------------------------------------------------------------


def _build_foundry_client(ctx_obj: dict):
    """Build a FoundryClient from project config.

    Separated from the command function so tests can inject a mock by
    patching this function or by passing ``_foundry_client`` in ctx.obj.
    """
    from ..config.loader import load_config
    from ..enrichment.foundry_client import FoundryClient

    config = load_config()
    return FoundryClient(config.foundry)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


_SET_DOMAIN_EPILOG = """\b
A good domain brief names four things:
  1. INDUSTRY / business domain — pass via --industry and --business-domain
  2. ENTITY TYPES               — the node types you want in the graph
  3. RELATIONSHIPS              — typed verbs connecting entities (has_part, causes)
  4. SAMPLE QUESTIONS           — pass via --questions-file (one per line)

The sample questions are the single biggest lever on ontology quality: they tell
the model which entity types and relationships to create. See README 'Domain
Template Playbook' for guidance.

\b
Surface (field-service) template example:
  fabric-kg set-domain --industry manufacturing --business-domain field-service \
    --questions-file data\\surface_questions.txt --prompt \
    "Field-service hardware troubleshooting for Microsoft Surface devices. \
Entity types: Device, DeviceModel, Component, Part, PartNumber, Procedure, \
Step, Tool, Symptom, Cause, Resolution. Key relationships: has_component, \
has_part, has_part_number, has_step, uses_tool, causes, resolved_by, addressed_by."

\b
Other domains:
  fabric-kg set-domain --industry healthcare --business-domain clinical \
    --questions-file q.txt --prompt "Patient care: Patient, Condition, Symptom, \
Treatment, Medication, Provider."
  fabric-kg set-domain --industry legal --business-domain contracts \
    --domain-file data\\domain_brief.txt --force

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("set-domain", epilog=_SET_DOMAIN_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--prompt", default=None,
              help="Inline domain description text to rephrase and persist.")
@click.option(
    "--domain-file",
    default=None,
    type=click.Path(),
    help="Path to a text file whose content becomes the domain description.",
)
@click.option(
    "--industry",
    required=True,
    help="Your industry — shapes the graph model. "
         "E.g. manufacturing, healthcare, financial-services, retail, public-sector.",
)
@click.option(
    "--business-domain",
    "business_domain",
    required=True,
    help="Your business domain — the functional area the data serves. "
         "E.g. field-service, hr, legal, finance, supply-chain, customer-support.",
)
@click.option(
    "--questions-file",
    "questions_file",
    default=None,
    type=click.Path(),
    help="Path to a text file of sample/competency questions (one per line) the graph "
         "must answer. STRONGLY RECOMMENDED — the sample questions are the single biggest "
         "lever on ontology quality: they tell the model which entity types and "
         "relationships to create so every question is answerable.",
)
@click.option(
    "--out",
    "output_dir",
    default="build/enriched",
    show_default=True,
    type=click.Path(),
    help="Output directory for domain.json.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-run the LLM rephrase pass even if domain.json already exists.",
)
@click.pass_context
def set_domain_cmd(
    ctx: click.Context,
    prompt: str | None,
    domain_file: str | None,
    industry: str,
    business_domain: str,
    questions_file: str | None,
    output_dir: str,
    force: bool,
) -> None:
    """Persist a domain brief to build/enriched/domain.json via an LLM rephrase pass.

    Exactly one of --prompt or --domain-file must be provided.  --industry and
    --business-domain are required: they declare the domain template that shapes
    your graph model.  All user-supplied text is sent to Azure AI Foundry in the
    USER message (never the system prompt, SPEC-004 §2.3), rephrased into a
    structured DomainBrief, and saved as JSON.

    Subsequent 'enrich' runs pick up the saved brief automatically.

    For best results, also pass --questions-file with 3-5 sample questions the
    graph must answer.  The sample questions are the strongest signal for which
    entity types and relationships to model.  See README 'Domain Template
    Playbook'.
    """
    ctx.ensure_object(dict)

    # Validate mutual exclusion.
    if prompt is None and domain_file is None:
        raise click.UsageError("Provide exactly one of --prompt or --domain-file.")
    if prompt is not None and domain_file is not None:
        raise click.UsageError("Provide exactly one of --prompt or --domain-file, not both.")

    out_path = Path(output_dir) / "domain.json"

    # If domain.json already exists and --force is not set, skip.
    if out_path.exists() and not force:
        click.echo(
            f"[set-domain] domain.json already exists at {out_path}. "
            "Use --force to overwrite."
        )
        return

    # Load raw domain text.
    if domain_file is not None:
        raw_text = Path(domain_file).read_text(encoding="utf-8")
    else:
        raw_text = prompt  # type: ignore[assignment]

    # Load competency questions (one per line, blanks ignored).
    competency_questions: list[str] = []
    if questions_file is not None:
        competency_questions = [
            ln.strip()
            for ln in Path(questions_file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]

    # Get or build FoundryClient — test can inject via ctx.obj["_foundry_client"].
    client = ctx.obj.get("_foundry_client") if ctx.obj else None
    if client is None:
        client = _build_foundry_client(ctx.obj or {})

    # Rephrase: all user text goes to the USER message only (enforced in rephrase_domain).
    brief: DomainBrief = rephrase_domain(
        raw_text,
        client,
        industry=industry,
        business_domain=business_domain,
        competency_questions=competency_questions,
    )

    save_domain_brief(brief, out_path)
    click.echo(f"[set-domain] domain brief written to {out_path}")
    click.echo(
        f"[set-domain] industry={industry!r} business_domain={business_domain!r} "
        f"questions={len(competency_questions)}"
    )
    if not competency_questions:
        click.echo(
            "[set-domain] TIP: pass --questions-file with 3-5 sample questions — "
            "it greatly improves the entity/relationship types the model proposes."
        )
