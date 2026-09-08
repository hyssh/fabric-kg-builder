"""Local schema-2 document assessment and immutable review commands."""

from __future__ import annotations

import json
from pathlib import Path

import click
from azure.core.exceptions import ClientAuthenticationError
from openai import APIError
from pydantic import TypeAdapter, ValidationError

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.assessment import (
    AssessmentReport,
    AssessmentReview,
    CachedResponse,
    Decision,
    ModelAssessment,
    RevisionRequest,
    assess_documents,
    review_assessment,
    save_new_artifact,
)
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract


def _model_client(ctx: click.Context):
    from fabric_kg_builder.config.loader import load_config
    from fabric_kg_builder.enrichment.foundry_client import FoundryClient

    settings = ctx.obj or {}
    injected = settings.get("_assessment_client")
    if injected is not None:
        return injected
    config = load_config(
        env=settings.get("env", "dev"),
        yaml_path=Path(settings.get("config", "./fabric-kg.yaml")),
    )
    return FoundryClient(config.foundry)


def _model_failure(exc: APIError | ClientAuthenticationError) -> click.ClickException:
    status = getattr(exc, "status_code", None)
    if isinstance(exc, ClientAuthenticationError) or status in {401, 403}:
        return click.ClickException(
            "MODEL_ACCESS_DENIED: authentication or inference data-plane permission "
            "failed for the configured resource. No key/identity fallback is attempted."
        )
    return click.ClickException(
        f"MODEL_REQUEST_FAILED: {type(exc).__name__}, status={status}. "
        "Completed response-cache entries remain available for replay."
    )


@click.command("assessment-schema")
def domain_assessment_schema_cmd() -> None:
    """Print exact versioned assessment/review schemas without files or model calls."""
    from fabric_kg_builder.sources.docintel_cache import CachedLayout
    click.echo(canonical_json({
        "contract_version": "1.0.0",
        "operation": "domain.assessment-schema",
        "schemas": {
            model.__name__: model.model_json_schema()
            for model in (
                ModelAssessment, CachedResponse, CachedLayout, AssessmentReport,
                Decision, AssessmentReview, RevisionRequest,
            )
        },
    }))


@click.command("assess")
@click.option("--file", "domain_file", default="domain.yaml", show_default=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--input", "source", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--out", default=".fkg/assessment/report.json", show_default=True,
              type=click.Path(dir_okay=False, path_type=Path))
@click.option("--responses", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Offline JSON object mapping planned window IDs to model responses.")
@click.option("--live", is_flag=True,
              help="Explicitly allow bounded calls to the configured model.")
@click.option("--dry-run", is_flag=True, help="Plan windows without calls or writes (default mode).")
@click.option("--max-calls", default=4, show_default=True, type=click.IntRange(0, 1000))
@click.option("--window-chars", default=8000, show_default=True, type=click.IntRange(256, 32000))
@click.option("--max-output-tokens", default=1600, show_default=True, type=click.IntRange(256, 8000))
@click.option("--max-prompt-chars", default=64000, show_default=True, type=click.IntRange(256))
@click.option("--checkpoint", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Prior assessment with exactly matching contract/corpus/model inputs.")
@click.option("--response-cache", type=click.Path(file_okay=False, path_type=Path),
              help="Immutable model response cache (default: responses/ beside output).")
@click.option("--ocr-cache", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Read exact cached DI responses for PDF/images; never calls DI.")
@click.option("--ocr-identity", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Nonsecret extractor identity JSON matching the OCR cache request.")
@click.pass_context
def domain_assess_cmd(
    ctx: click.Context, domain_file: Path, source: Path, out: Path,
    responses: Path | None, live: bool, dry_run: bool, max_calls: int,
    window_chars: int, max_output_tokens: int, max_prompt_chars: int,
    checkpoint: Path | None, response_cache: Path | None,
    ocr_cache: Path | None, ocr_identity: Path | None,
) -> None:
    """Find grounded document challenges without approving or changing an ontology.

    Defaults to a read-only window plan. Use --responses for offline execution
    or --live for bounded model calls. Partial coverage is explicitly reported;
    a complete report does not prove semantic recall or approve a domain.
    """
    global_dry_run = bool((ctx.obj or {}).get("dry_run"))
    if live and responses is not None:
        raise click.UsageError("--live and --responses are mutually exclusive")
    if (dry_run or global_dry_run) and live:
        raise click.UsageError("--dry-run cannot be combined with --live")
    planning = dry_run or global_dry_run or (not live and responses is None)
    if not planning and out.exists():
        raise click.ClickException(f"Refusing to overwrite assessment: {out}")
    try:
        contract = load_domain_contract(domain_file)
        if not isinstance(contract, DomainContractV2):
            raise ValueError("domain assess requires a schema-2 contract")
        client = None
        model_identity = "offline"
        if live:
            client = _model_client(ctx)
            model_identity = canonical_sha256(client.execution_identity())
        fixtures = json.loads(responses.read_text(encoding="utf-8")) if responses else None
        if fixtures is not None and not isinstance(fixtures, dict):
            raise ValueError("responses must be an object keyed by window ID")
        prior = (
            AssessmentReport.model_validate_json(checkpoint.read_text(encoding="utf-8"))
            if checkpoint else None
        )
        extraction_identity = json.loads(ocr_identity.read_text(encoding="utf-8")) if ocr_identity else None
        if extraction_identity is not None and not isinstance(extraction_identity, dict):
            raise ValueError("OCR identity must be a JSON object")
        report = assess_documents(
            source, contract, client=client, responses=fixtures,
            model_identity=model_identity, window_chars=window_chars,
            max_calls=max_calls, max_output_tokens=max_output_tokens,
            max_prompt_chars=max_prompt_chars, dry_run=planning,
            checkpoint=prior,
            response_cache=(response_cache or out.parent / "responses") if live else None,
            ocr_cache=ocr_cache, ocr_identity=extraction_identity,
        )
        if not planning:
            save_new_artifact(out, report)
    except (APIError, ClientAuthenticationError) as exc:
        raise _model_failure(exc) from exc
    except (OSError, ValueError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "contract_version": "1.0.0",
        "operation": "domain.assess",
        "status": "planned" if planning else report.coverage,
        "artifact": None if planning else str(out),
        "report_hash": report.report_hash,
        "model_calls": report.model_calls,
        "files": len(report.files),
        "windows": len(report.windows),
        "assessed_windows": sum(w.status == "assessed" for w in report.windows),
        "findings": len(report.findings),
        "planned_windows": [
            {
                "window_id": w.window_id,
                "source_ref": w.source_ref,
                "span_start": w.span_start,
                "span_end": w.span_end,
                "status": w.status,
                "reason": w.reason,
            } for w in report.windows
        ],
        "file_dispositions": [f.model_dump(mode="json") for f in report.files],
    }))


@click.command("review-assessment")
@click.option("--file", "domain_file", default="domain.yaml", show_default=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--assessment", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--decisions", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="JSON array: finding_id, disposition and rationale for every finding.")
@click.option("--actor", required=True, help="Named reviewer; not automatic domain approval.")
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--revision-out", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.pass_context
def domain_review_assessment_cmd(
    ctx: click.Context, domain_file: Path, assessment: Path, decisions: Path,
    actor: str, out: Path, revision_out: Path,
) -> None:
    """Persist review decisions and a revision request without changing the domain."""
    planning = bool((ctx.obj or {}).get("dry_run"))
    if out.resolve() == revision_out.resolve():
        raise click.UsageError("review and revision output paths must differ")
    if not planning and (out.exists() or revision_out.exists()):
        raise click.ClickException("Refusing to overwrite an existing review/revision")
    try:
        contract = load_domain_contract(domain_file)
        if not isinstance(contract, DomainContractV2):
            raise ValueError("review-assessment requires a schema-2 contract")
        report = AssessmentReport.model_validate_json(assessment.read_text(encoding="utf-8"))
        if report.domain_contract_hash != compute_contract_hash(contract):
            raise ValueError("assessment belongs to a different domain contract")
        items = TypeAdapter(tuple[Decision, ...]).validate_json(
            decisions.read_text(encoding="utf-8")
        )
        review, request = review_assessment(report, actor=actor, decisions=items)
        if not planning:
            save_new_artifact(out, review)
            save_new_artifact(revision_out, request)
    except (OSError, ValueError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "contract_version": "1.0.0",
        "operation": "domain.review-assessment",
        "status": "planned" if planning else "reviewed",
        "review_hash": review.review_hash,
        "revision_request_hash": request.request_hash,
        "accepted": len(request.accepted_findings),
        "deferred": len(request.deferred_finding_ids),
        "domain_modified": False,
        "approved": False,
    }))


@click.command("revise")
@click.option("--parent-state", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--input", "source", required=True,
              type=click.Path(exists=True, path_type=Path))
@click.option("--assessment", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--review", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--request", "request_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out-state", required=True,
              type=click.Path(file_okay=False, path_type=Path))
@click.option("--candidates", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Offline L1 candidate fixture for the revised proposal.")
@click.option("--live", is_flag=True, help="Allow bounded L1 model calls.")
@click.option("--max-calls", default=2, show_default=True, type=click.IntRange(1, 6))
@click.option("--dry-run", is_flag=True, help="Plan without calls/writes (default mode).")
@click.option("--ocr-cache", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Exact source OCR cache used by the assessment, if any.")
@click.option("--response-cache", type=click.Path(file_okay=False, path_type=Path),
              help="Revision response cache outside parent/child state (default: beside child state).")
@click.pass_context
def domain_revise_cmd(
    ctx: click.Context, parent_state: Path, source: Path, assessment: Path,
    review: Path, request_path: Path, out_state: Path, candidates: Path | None,
    live: bool, max_calls: int, dry_run: bool, ocr_cache: Path | None,
    response_cache: Path | None,
) -> None:
    """Create a new unapproved L1 draft; preserve the parent and require reapproval."""
    from fabric_kg_builder.domain.revision import BoundedRevisionClient, create_revision

    global_dry_run = bool((ctx.obj or {}).get("dry_run"))
    if live and (candidates is not None or dry_run or global_dry_run):
        raise click.UsageError("--live conflicts with offline/dry-run mode")
    planning = dry_run or global_dry_run or (not live and candidates is None)
    try:
        report = AssessmentReport.model_validate_json(assessment.read_text(encoding="utf-8"))
        decisions = AssessmentReview.model_validate_json(review.read_text(encoding="utf-8"))
        request = RevisionRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        fixture = json.loads(candidates.read_text(encoding="utf-8")) if candidates else None
        if fixture is not None and not isinstance(fixture, dict):
            raise ValueError("revision candidates must be a JSON object")
        client = BoundedRevisionClient(
            _model_client(ctx), max_calls=max_calls,
            response_cache=response_cache or out_state.with_name(f"{out_state.name}-responses"),
        ) if live else None
        result = create_revision(
            parent_state=parent_state, source=source, report=report,
            review=decisions, request=request, state_root=out_state,
            domain_path=out_state / "domain.yaml", candidates=fixture,
            client=client, dry_run=planning,
            ocr_cache=ocr_cache,
        )
    except (APIError, ClientAuthenticationError) as exc:
        raise _model_failure(exc) from exc
    except (OSError, ValueError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({"contract_version": "1.0.0", **result}))
