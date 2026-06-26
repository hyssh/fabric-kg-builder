"""enrich command — run LLM extraction and enrichment on source files.

Security: domain text is NEVER placed in the LLM system prompt.
See SPEC-004 §2.3 and fabric_kg_builder.enrichment.domain / orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..enrichment.domain import (
    DomainBrief,
    load_domain_brief,
    rephrase_domain,
    save_domain_brief,
)
from ..enrichment.orchestrator import enrich_batch, enrich_documents, link_text_evidence
from ..sources.csv_loader import load_csv
from ..sources.chunker import Chunker
from ..sources.router import extract as router_extract

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CSV_EXTENSIONS: frozenset[str] = frozenset({".csv", ".tsv", ".xlsx"})
_DOC_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".html", ".htm", ".md"})


# ---------------------------------------------------------------------------
# Internal helpers (isolated for test-patching)
# ---------------------------------------------------------------------------


def _build_foundry_client(ctx_obj: dict):
    """Build a FoundryClient from project config.

    Separated for test-patching or ctx.obj injection.
    """
    from ..config.loader import load_config
    from ..enrichment.foundry_client import FoundryClient

    config = load_config()
    return FoundryClient(config.foundry)


def _build_di_layout_client(ctx_obj: dict):
    """Build a DocIntelClient for DI table extraction, or None if not configured.

    Returns None (never raises) when:
    - ``document_intelligence.endpoint`` is empty / unset.
    - The azure-ai-documentintelligence SDK is not installed.
    - Config cannot be loaded.

    Separated for test-patching or ctx.obj injection.
    """
    import logging

    _log = logging.getLogger(__name__)
    try:
        from ..config.loader import load_config
        from ..enrichment.docintel import DocIntelClient

        config = load_config()
        if not config.document_intelligence.endpoint:
            return None
        return DocIntelClient(config.document_intelligence)
    except Exception as exc:
        _log.debug("_build_di_layout_client: DI client not available (%s)", exc)
        return None


def _build_blob_uploader(ctx_obj: dict):
    """Build a BlobUploader from project config, or None if blob is not configured.

    Returns None (never raises) when:
    - ``blob.account_name`` is empty / unset.
    - The azure-storage-blob SDK is not installed.
    - Config cannot be loaded.

    Separated for test-patching or ctx.obj injection.
    """
    import logging

    _log = logging.getLogger(__name__)
    try:
        from ..config.loader import load_config
        from ..deploy.blob_uploader import BlobUploader

        config = load_config()
        if not config.blob.account_name:
            return None
        return BlobUploader(config.blob)
    except Exception as exc:
        _log.debug("_build_blob_uploader: blob uploader not available (%s)", exc)
        return None


def _resolve_domain_brief(
    domain_prompt: str | None,
    domain_file: str | None,
    output_dir: Path,
    client,
    force: bool,
) -> DomainBrief | None:
    """Load or generate the domain brief.

    Resolution order:
    1. --domain-file → load directly (no rephrase pass).
    2. build/enriched/domain.json exists → load (unless --force).
    3. --domain-prompt → run rephrase pass; persist to domain.json.
    4. None of the above → return None (enrichment runs without domain context).
    """
    if domain_file is not None:
        return load_domain_brief(domain_file)

    existing = output_dir / "domain.json"
    if existing.exists() and not force:
        return load_domain_brief(existing)

    if domain_prompt is not None:
        brief = rephrase_domain(domain_prompt, client)
        save_domain_brief(brief, existing)
        return brief

    return None


def _enrich_document_file(
    src_file: Path,
    client,
    domain_brief: DomainBrief | None,
    output_dir: Path,
    *,
    resume: bool = False,
    di_layout_client=None,
    blob_uploader=None,
) -> None:
    """Route a PDF/DOCX/HTML/MD file through the full document enrichment pipeline.

    Steps:
    1. Extract document elements via router.
    2. Produce structural chunks via Chunker.
    3. [Optional] Call Document Intelligence Layout to extract table document_elements
       and table_html chunks when ``di_layout_client`` is provided.  Falls back
       gracefully (no crash) when DI is unavailable.
    4. [Optional] Extract DI figure crops via PyMuPDF and upload to Blob Storage when
       both ``di_layout_client`` and ``blob_uploader`` are provided (PDF only).
       Populates ``visual_assets`` and ``visual_regions`` in the canonical output.
    5. Call enrich_documents() → LLM enrichment (entities, relationships, evidence).
    6. Link each structural chunk to its document element via link_text_evidence().
    7. Write a single canonical intermediate JSON file (all sections combined).

    Security: domain brief is forwarded to enrich_documents → build_user_message
    and placed in the USER message ONLY (never the system prompt).

    Parameters
    ----------
    di_layout_client:
        Optional :class:`~fabric_kg_builder.enrichment.docintel.DocIntelClient`.
        When provided, DI Layout is called to extract tables (chunk_type='table_html')
        and to obtain figure bounding regions for visual asset extraction.
        When None (DI not configured or creds absent), both steps are silently skipped.
    blob_uploader:
        Optional :class:`~fabric_kg_builder.deploy.blob_uploader.BlobUploader`.
        When provided alongside ``di_layout_client``, figure crops are uploaded and
        ``visual_assets``/``visual_regions`` rows are populated.  When None, visual
        extraction is silently skipped.
    """
    import logging

    _log = logging.getLogger(__name__)

    extract_result = router_extract(src_file)
    source_file_id: str = extract_result.source_file.source_file_id
    document_elements = extract_result.document_elements

    # Resume at the FILE level: if this document was already fully enriched
    # (its canonical JSON exists), skip it entirely — do not reprocess and do
    # not overwrite the good output with an empty result.
    canonical_out = output_dir / f"{source_file_id.replace(':', '_')}_canonical.json"
    if resume and canonical_out.exists():
        _log.info(
            "_enrich_document_file: skipping %s — already enriched (%s exists)",
            src_file.name,
            canonical_out.name,
        )
        return

    chunk_result = Chunker.extract(document_elements)

    # --- DI table extraction (when DI is configured) -------------------------
    # DI is the source of truth for table structure (SPEC-004 §8,
    # coordinator-tables-via-docintel.md 2026-06-24).  The LLM is NOT asked
    # to transcribe table cells — it only semantically enriches what DI found.
    di_table_elements: list = []
    di_table_chunks: list = []
    di_analyze_result = None  # shared between table + figure extraction below

    if di_layout_client is not None:
        try:
            from ..enrichment.docintel_tables import extract_tables

            raw_bytes = src_file.read_bytes()
            di_analyze_result = di_layout_client.layout_analyze_raw(raw_bytes)
            di_result = extract_tables(
                di_analyze_result,
                source_file_id,
                sort_order_start=len(document_elements),
            )
            di_table_elements = di_result.document_elements
            di_table_chunks = di_result.chunks
            if di_table_elements:
                _log.info(
                    "_enrich_document_file: DI extracted %d table(s) from %s",
                    len(di_table_elements),
                    src_file.name,
                )
        except Exception as exc:
            # Graceful fallback — DI table extraction is additive; failure must
            # not abort the main enrichment pass.
            _log.warning(
                "_enrich_document_file: DI table extraction failed for %s "
                "(continuing without DI tables): %s",
                src_file.name,
                exc,
            )

    # --- DI figure extraction (PDF only; reuses di_analyze_result from above) --
    # Produces visual_assets (one per figure crop) and visual_regions (polygon
    # rows linking back to visual_assets by image_id FK).
    visual_assets_rows: list = []
    visual_regions_rows: list = []

    if (
        di_analyze_result is not None
        and blob_uploader is not None
        and src_file.suffix.lower() == ".pdf"
    ):
        try:
            from ..enrichment.image_extractor import (
                extract_figures_from_di,
                make_visual_asset_row,
                make_visual_regions_for_figure,
            )
            from ..model.ids import make_image_id

            figure_candidates = extract_figures_from_di(
                src_file, di_analyze_result, source_file_id
            )
            for candidate in figure_candidates:
                image_id = make_image_id(source_file_id, candidate.image_hash)
                blob_url = blob_uploader.upload(image_id, candidate.image_bytes, "png")
                asset_row = make_visual_asset_row(
                    candidate, source_file_id, blob_url=blob_url
                )
                visual_assets_rows.append(asset_row)
                regions = make_visual_regions_for_figure(
                    image_id, candidate, di_analyze_result, blob_url=blob_url
                )
                visual_regions_rows.extend(regions)

            if visual_assets_rows:
                _log.info(
                    "_enrich_document_file: extracted %d figure(s) from %s",
                    len(visual_assets_rows),
                    src_file.name,
                )
        except Exception as exc:
            # Graceful fallback — figure extraction is additive.
            _log.warning(
                "_enrich_document_file: figure extraction failed for %s "
                "(continuing without visual assets): %s",
                src_file.name,
                exc,
            )

    records = enrich_documents(
        document_elements=document_elements,
        source_file_id=source_file_id,
        client=client,
        domain_brief=domain_brief,
        output_dir=output_dir,
        resume=resume,
    )

    # Link each structural chunk to its document element.
    linked_evidence = list(records.evidence)
    for chunk in chunk_result.chunks:
        ev = link_text_evidence(
            source_file_id=source_file_id,
            chunk_id=chunk.chunk_id,
            document_element_id=chunk.document_element_id,
            text=chunk.content,
            page_number=chunk.page_number,
            section_path=chunk.section_path,
        )
        linked_evidence.append(ev)

    # Merge DI table artifacts alongside the text-based results.
    all_document_elements = list(document_elements) + di_table_elements
    all_chunks = list(chunk_result.chunks) + di_table_chunks

    # Write canonical intermediate JSON with all sections.
    safe_id = source_file_id.replace(":", "_")
    out_file = output_dir / f"{safe_id}_canonical.json"
    out_file.write_text(
        json.dumps(
            {
                "source_file_id": source_file_id,
                "source_file": extract_result.source_file.model_dump(),
                "document_elements": [
                    e.model_dump() for e in all_document_elements
                ],
                "chunks": [c.model_dump() for c in all_chunks],
                "entities": [e.model_dump() for e in records.entities],
                "relationships": [r.model_dump() for r in records.relationships],
                "evidence": [ev.model_dump() for ev in linked_evidence],
                "visual_assets": [a.model_dump() for a in visual_assets_rows],
                "visual_regions": [r.model_dump() for r in visual_regions_rows],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


_ENRICH_EPILOG = """\b
Example:
  fabric-kg enrich --input sample_data\\Surface_Troubleshootings
  fabric-kg enrich --input sample_data\\Surface_Troubleshootings --resume
  fabric-kg enrich --input data\\devices.csv --domain-prompt "IoT device catalog"

Questions? hyssh@microsoft.com
"""


@click.command("enrich", epilog=_ENRICH_EPILOG,
               context_settings={"max_content_width": 120})
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(),
    help="Source file or directory (.csv/.tsv/.xlsx/.pdf/.docx/.html/.md) to enrich.",
)
@click.option(
    "--model",
    default=None,
    show_default=True,
    help="Override the Azure AI Foundry chat deployment name from config.",
)
@click.option(
    "--max-concurrent",
    default=None,
    type=int,
    show_default=True,
    help="Override the max number of concurrent LLM calls (default: from config).",
)
@click.option(
    "--domain-prompt",
    default=None,
    help="Inline domain description text; writes build/enriched/domain.json then enriches.",
)
@click.option(
    "--domain-file",
    default=None,
    type=click.Path(),
    help="Path to a pre-written domain brief JSON file (skips rephrase pass).",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Skip files whose canonical JSON already exists (continue from last checkpoint).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Ignore checkpoint and re-process all source files from scratch.",
)
@click.option(
    "--out",
    "output_path",
    default="build/enriched",
    show_default=True,
    type=click.Path(),
    help="Output directory for enriched canonical JSON files.",
)
@click.pass_context
def enrich_cmd(
    ctx: click.Context,
    input_path: str,
    model: str | None,
    max_concurrent: int | None,
    domain_prompt: str | None,
    domain_file: str | None,
    resume: bool,
    force: bool,
    output_path: str,
) -> None:
    """Run LLM extraction on source files and produce structured JSON in build/enriched/.

    Accepts CSV/TSV/XLSX (tabular rows) and PDF/DOCX/HTML/MD (document elements).
    For each file the pipeline calls Azure AI Foundry to extract entities,
    relationships, and evidence chunks, then writes a per-file canonical JSON.

    Domain context (--domain-prompt or --domain-file) is injected ONLY into
    the LLM user message — never the system prompt (SPEC-004 §2.3).

    Exit codes: 0 success · 1 error · 4 partial enrichment (checkpoint saved).
    """
    ctx.ensure_object(dict)

    out_dir = Path(output_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Get or build FoundryClient.
    client = ctx.obj.get("_foundry_client") if ctx.obj else None
    if client is None:
        try:
            client = _build_foundry_client(ctx.obj or {})
        except Exception as exc:
            click.echo(f"[enrich] ERROR: could not build Foundry client: {exc}", err=True)
            ctx.exit(1)
            return

    # Get or build DI layout client (optional — None when DI not configured).
    di_layout_client = ctx.obj.get("_di_layout_client") if ctx.obj else None
    if di_layout_client is None:
        di_layout_client = _build_di_layout_client(ctx.obj or {})

    # Get or build Blob uploader (optional — None when blob not configured).
    blob_uploader = ctx.obj.get("_blob_uploader") if ctx.obj else None
    if blob_uploader is None:
        blob_uploader = _build_blob_uploader(ctx.obj or {})

    # Resolve domain brief (may involve an LLM rephrase call).
    domain_brief = _resolve_domain_brief(
        domain_prompt=domain_prompt,
        domain_file=domain_file,
        output_dir=out_dir,
        client=client,
        force=force,
    )

    # Collect source files.
    input_p = Path(input_path)
    if input_p.is_dir():
        source_files = (
            list(input_p.glob("*.csv"))
            + list(input_p.glob("*.tsv"))
            + list(input_p.glob("*.xlsx"))
            + [f for f in input_p.iterdir() if f.suffix.lower() in _DOC_EXTENSIONS]
        )
    else:
        source_files = [input_p]

    if not source_files:
        click.echo(f"[enrich] No source files found at {input_path}", err=True)
        ctx.exit(1)
        return

    do_resume = resume and not force
    errors = 0
    for src_file in source_files:
        try:
            suffix = src_file.suffix.lower()
            if suffix in _DOC_EXTENSIONS:
                _enrich_document_file(
                    src_file,
                    client,
                    domain_brief,
                    out_dir,
                    resume=do_resume,
                    di_layout_client=di_layout_client,
                    blob_uploader=blob_uploader,
                )
            else:
                # CSV / tabular path (unchanged).
                result = load_csv(src_file)
                source_file_id = result.source_file.source_file_id
                rows = [
                    elem.content
                    for elem in result.document_elements
                    if elem.element_type == "table_row" and elem.content
                ]
                source_content = "\n".join(rows[:50])
                enrich_batch(
                    source_content=source_content,
                    source_file_id=source_file_id,
                    client=client,
                    domain_brief=domain_brief,
                    output_dir=out_dir,
                    resume=do_resume,
                    default_source_type="csv_row",
                )
            click.echo(f"[enrich] enriched {src_file.name} → {out_dir}")
        except Exception as exc:
            click.echo(f"[enrich] ERROR enriching {src_file}: {exc}", err=True)
            errors += 1

    if errors:
        ctx.exit(4)
    else:
        click.echo("[enrich] done.")
