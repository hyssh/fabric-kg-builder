"""Explicitly bounded Azure CLI-authenticated DI analysis into an immutable cache."""

from __future__ import annotations

import io
from pathlib import Path

import click
from azure.core.exceptions import HttpResponseError

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.stage import make_l1_identity
from fabric_kg_builder.sources.corpus import (
    build_source_corpus_manifest, open_verified_source_snapshot,
)
from fabric_kg_builder.sources.docintel_cache import (
    load_cached_layout, make_cached_layout, store_cached_layout,
    validate_extractor_identity,
)


def _page_count(data: bytes, media_type: str) -> int:
    if media_type == "application/pdf":
        import fitz
        with fitz.open(stream=data, filetype="pdf") as document:
            return document.page_count
    if media_type.startswith("image/"):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            return getattr(image, "n_frames", 1)
    raise ValueError("analyze-layout accepts a PDF or supported image")


@click.command("analyze-layout")
@click.option("--input", "source", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--endpoint", required=True, help="Existing authorized DI endpoint; Azure CLI identity only.")
@click.option("--cache-dir", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--model-id", default="prebuilt-layout", show_default=True)
@click.option("--api-version", default="2024-11-30", show_default=True)
@click.option("--max-pages", default=1, show_default=True, type=click.IntRange(1, 2000),
              help="Explicit whole-document page cap; S0 supports up to 2000 pages.")
@click.option("--max-bytes", default=10_000_000, show_default=True, type=click.IntRange(1, 50_000_000))
@click.option("--poll-timeout", default=90, show_default=True, type=click.IntRange(1, 1800),
              help="Maximum wait in seconds for an already submitted analysis.")
@click.option("--live", is_flag=True, help="Permit exactly one new analysis POST; otherwise plan only.")
@click.option("--dry-run", is_flag=True, help="No calls or writes (default mode).")
@click.pass_context
def domain_analyze_layout_cmd(
    ctx: click.Context, source: Path, endpoint: str, cache_dir: Path,
    model_id: str, api_version: str, max_pages: int, max_bytes: int,
    live: bool, dry_run: bool, poll_timeout: int,
) -> None:
    """Cache the full DI response; never silently reanalyze or use API keys.

    Reject oversized documents instead of analyzing only some pages and later
    implying full coverage. A pending attempt blocks another POST after an
    ambiguous failure. This command does not create or configure Azure resources.
    """
    planning = dry_run or bool((ctx.obj or {}).get("dry_run")) or not live
    if live and planning:
        raise click.UsageError("--live conflicts with --dry-run")
    identity = {
        "endpoint": endpoint.rstrip("/"),
        "api_version": api_version, "model_id": model_id,
        "options": {
            "output_content_format": "markdown",
            "string_index_type": "unicodeCodePoint",
            "pages": "all",
        },
    }
    try:
        if not endpoint.startswith("https://"):
            raise ValueError("DI endpoint must use HTTPS")
        validate_extractor_identity(identity)
        corpus = build_source_corpus_manifest(
            source, corpus_root_id="corpus-root:layout-cache",
            identity=make_l1_identity(project_id="project:layout-cache", run_id="run:layout-cache"),
        )
        entry = corpus.entries[0]
        if entry.byte_count > max_bytes:
            raise ValueError("document exceeds max-bytes; no analysis requested")
        with open_verified_source_snapshot(
            source, entry=entry, corpus_root_id=corpus.corpus_root_id
        ) as snapshot:
            data = snapshot.path.read_bytes()
        pages = _page_count(data, entry.media_type)
        if not 1 <= pages <= max_pages:
            raise ValueError(f"document has {pages} pages; max-pages={max_pages}; no analysis requested")
        key = canonical_sha256({
            "input_sha256": entry.original_byte_hash,
            "extractor_identity": identity,
        })
        cached = load_cached_layout(
            cache_dir, input_sha256=entry.original_byte_hash,
            extractor_identity=identity,
        )
        status = "cached" if cached is not None else "planned"
        calls = 0
        if live and cached is None:
            pending = cache_dir / f"{key}.pending.json"
            cache_dir.mkdir(parents=True, exist_ok=True)
            with pending.open("x", encoding="utf-8") as stream:
                stream.write(canonical_json({
                    "cache_key": key, "input_sha256": entry.original_byte_hash,
                    "extractor_identity": identity, "status": "analysis_reserved",
                }) + "\n")
            client = (ctx.obj or {}).get("_docintel_client")
            if client is None:
                from azure.ai.documentintelligence import DocumentIntelligenceClient
                from azure.identity import AzureCliCredential
                client = DocumentIntelligenceClient(
                    endpoint=endpoint, credential=AzureCliCredential(),
                    api_version=api_version, retry_total=0,
                )
            submitted = False
            try:
                poller = client.begin_analyze_document(
                    model_id=model_id, body=data,
                    content_type="application/octet-stream",
                    output_content_format="markdown",
                    string_index_type="unicodeCodePoint",
                )
                submitted = True
                calls = 1
                raw = poller.result(timeout=poll_timeout)
                if not poller.done():
                    raise TimeoutError(
                        f"analysis still pending; do not retry POST; inspect reservation {pending.name}"
                    )
            except HttpResponseError as exc:
                if exc.status_code in {401, 403}:
                    request = getattr(getattr(exc, "response", None), "request", None)
                    rejected_post = (
                        not submitted and getattr(request, "method", None) == "POST"
                    )
                    if rejected_post:
                        pending.unlink()
                    raise click.ClickException(
                        "OCR_ACCESS_DENIED: configured resource denies Azure CLI "
                        "identity; no key/identity fallback is attempted. "
                        + (
                            "Submission was rejected."
                            if rejected_post else
                            "Analysis may already be submitted; reservation retained."
                        )
                    ) from exc
                raise
            cached = make_cached_layout(data, raw, identity)
            store_cached_layout(cache_dir, cached)
            pending.unlink()
            status = "analyzed"
    except FileExistsError as exc:
        raise click.ClickException(
            "An earlier analysis is pending; reconcile it before another POST."
        ) from exc
    except HttpResponseError as exc:
        raise click.ClickException(
            f"OCR_REQUEST_FAILED: status={exc.status_code}; reservation retained."
        ) from exc
    except (OSError, ValueError, TimeoutError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({
        "contract_version": "1.0.0",
        "operation": "domain.analyze-layout",
        "status": status, "pages": pages, "analysis_posts": calls,
        "cache_key": key, "extractor_identity": identity,
        "artifact": str(cache_dir / f"{key}.json") if cached is not None else None,
    }))
