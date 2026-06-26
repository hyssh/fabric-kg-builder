"""inspect-source command — analyze source files and report schema profile."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import click

from fabric_kg_builder.sources.csv_loader import CsvLoaderError, CsvLoadResult, load_csv
from fabric_kg_builder.sources.pdf_extractor import PdfExtractResult
from fabric_kg_builder.sources.docx_extractor import DocxExtractResult
from fabric_kg_builder.sources.html_extractor import HtmlExtractResult

_CSV_EXTS = {".csv", ".tsv", ".xlsx"}
_DOC_EXTS = {".pdf", ".docx", ".html", ".htm", ".md"}
_SUPPORTED_EXTS = _CSV_EXTS | _DOC_EXTS


def _collect_files(p: Path) -> list[Path]:
    """Return supported source files from a file or directory path."""
    if p.is_file():
        return [p] if p.suffix.lower() in _SUPPORTED_EXTS else []
    if p.is_dir():
        return sorted(
            f for f in p.iterdir()
            if f.is_file() and f.suffix.lower() in _SUPPORTED_EXTS
        )
    return []


def _print_table_summary(result: CsvLoadResult, file_path: Path) -> None:
    sp = result.schema_profile
    sf = result.source_file
    click.echo(f"\n{'=' * 62}")
    click.echo(f"  Source:       {file_path.name}")
    click.echo(f"  Type:         {sp['source_type']}")
    click.echo(f"  Row count:    {sp['row_count']}")
    click.echo(f"  Column count: {sp['column_count']}")
    click.echo(f"  Content hash: {sf.content_hash[:16]}...")
    click.echo(f"  Inspected at: {sp['inspected_at']}")
    click.echo(f"{'=' * 62}")
    click.echo(f"  {'#':<4} {'Column':<30} {'Type':<10} {'Nulls':<6} Unique")
    click.echo(f"  {'-' * 58}")
    for col in sp["columns"]:
        click.echo(
            f"  {col['index']:<4} {col['name']:<30} {col['inferred_type']:<10} "
            f"{col['null_count']:<6} {col['unique_count']}"
        )
    click.echo("")


def _extract_doc(file_path: Path) -> PdfExtractResult | DocxExtractResult | HtmlExtractResult | None:
    """Extract document elements from a doc/PDF/HTML file. Returns None on error."""
    from fabric_kg_builder.sources.router import extract as router_extract
    try:
        return router_extract(file_path)
    except Exception as exc:
        click.echo(f"Warning: failed to extract {file_path.name}: {exc}", err=True)
        return None


def _doc_inventory(result: PdfExtractResult | DocxExtractResult | HtmlExtractResult) -> dict:
    """Build an inventory dict from a doc extract result."""
    elements = result.document_elements
    sf = result.source_file
    type_counts = dict(Counter(e.element_type for e in elements))
    page_count = getattr(result, "page_count", None)
    return {
        "source_type": sf.source_type,
        "filename": sf.filename,
        "content_hash": sf.content_hash,
        "byte_size": sf.byte_size,
        "page_count": page_count,
        "element_count": len(elements),
        "element_type_counts": type_counts,
    }


def _print_doc_summary(inventory: dict, file_path: Path) -> None:
    click.echo(f"\n{'=' * 62}")
    click.echo(f"  Source:        {file_path.name}")
    click.echo(f"  Type:          {inventory['source_type']}")
    if inventory["page_count"] is not None:
        click.echo(f"  Pages:         {inventory['page_count']}")
    click.echo(f"  Elements:      {inventory['element_count']}")
    click.echo(f"  Content hash:  {inventory['content_hash'][:16]}...")
    if inventory["byte_size"] is not None:
        click.echo(f"  File size:     {inventory['byte_size']:,} bytes")
    click.echo(f"{'=' * 62}")
    click.echo(f"  {'Element type':<28} {'Count':>6}")
    click.echo(f"  {'-' * 36}")
    for etype, cnt in sorted(inventory["element_type_counts"].items()):
        click.echo(f"  {etype:<28} {cnt:>6}")
    click.echo("")


_INSPECT_SOURCE_EPILOG = """\b
Example:
  fabric-kg inspect-source --input sample_data\\Surface_Troubleshootings
  fabric-kg inspect-source --input data\\devices.csv --format json

Questions? hyssh@microsoft.com
"""


@click.command("inspect-source", epilog=_INSPECT_SOURCE_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--input", "input_path", required=True, type=click.Path(),
              help="Path to a source file or directory to inspect.")
@click.option("--format", "output_format", default="table", show_default=True,
              type=click.Choice(["table", "json"]),
              help="Report output format: 'table' (human-readable) or 'json' (machine-readable).")
@click.option("--out", "out_dir", default=None, type=click.Path(),
              help="Directory to write schema-profile.json and/or doc-inventory.json.")
def inspect_source_cmd(input_path: str, output_format: str, out_dir: str | None) -> None:
    """Analyze source files and report column schema, element counts, and file metadata.

    For CSV/TSV/XLSX files: reports column names, inferred types, null counts,
    and unique-value counts.

    For PDF/DOCX/HTML/MD files: reports page count, element type breakdown
    (heading, paragraph, table, figure, etc.).

    Supports single files and directories (all supported files in the directory).
    Supported extensions: .csv .tsv .xlsx .pdf .docx .html .htm .md

    Exit codes: 0 success · 1 error · 3 unsupported source type.
    """
    p = Path(input_path)

    if not p.exists():
        click.echo(f"Error: path not found: {input_path}", err=True)
        sys.exit(1)

    files = _collect_files(p)

    if not files:
        if p.is_file():
            click.echo(
                f"Error: unsupported file type '{p.suffix.lower()}'. "
                f"Supported: .csv, .tsv, .xlsx, .pdf, .docx, .html, .htm, .md",
                err=True,
            )
            sys.exit(3)
        click.echo(
            f"Error: no supported source files (.csv/.tsv/.xlsx/.pdf/.docx/.html) found in '{input_path}'.",
            err=True,
        )
        sys.exit(1)

    profiles: list[dict] = []
    inventories: list[dict] = []
    load_errors: list[str] = []

    for file_path in files:
        suffix = file_path.suffix.lower()
        if suffix in _CSV_EXTS:
            try:
                result = load_csv(file_path)
            except CsvLoaderError as exc:
                load_errors.append(f"{file_path.name}: {exc}")
                continue
            except FileNotFoundError as exc:
                load_errors.append(f"{file_path.name}: {exc}")
                continue
            profiles.append(result.schema_profile)
            if output_format == "table":
                _print_table_summary(result, file_path)
        else:
            # PDF / DOCX / HTML path via router
            extract_result = _extract_doc(file_path)
            if extract_result is None:
                load_errors.append(f"{file_path.name}: extraction failed")
                continue
            inv = _doc_inventory(extract_result)
            inventories.append(inv)
            if output_format == "table":
                _print_doc_summary(inv, file_path)

    # Combined inventory summary for doc files
    if inventories and output_format == "table":
        total_pages = sum(i["page_count"] or 0 for i in inventories)
        total_elements = sum(i["element_count"] for i in inventories)
        click.echo(f"\n--- Combined document inventory ({len(inventories)} file(s)) ---")
        click.echo(f"  Total pages:    {total_pages}")
        click.echo(f"  Total elements: {total_elements}")
        click.echo("")

    if output_format == "json":
        all_output: list[dict] | dict = []
        if profiles and inventories:
            all_output = {"csv_profiles": profiles, "doc_inventories": inventories}
        elif profiles:
            all_output = profiles[0] if len(profiles) == 1 else profiles
        elif inventories:
            all_output = inventories[0] if len(inventories) == 1 else inventories
        if all_output:
            click.echo(json.dumps(all_output, indent=2, default=str))

    if out_dir is not None and (profiles or inventories):
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        if profiles:
            profile_file = out_path / "schema-profile.json"
            payload = profiles[0] if len(profiles) == 1 else profiles
            profile_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            click.echo(f"Schema profile written to {profile_file}")
        if inventories:
            inv_file = out_path / "doc-inventory.json"
            payload_inv: list[dict] | dict = inventories[0] if len(inventories) == 1 else inventories
            inv_file.write_text(json.dumps(payload_inv, indent=2, default=str), encoding="utf-8")
            click.echo(f"Document inventory written to {inv_file}")

    for err in load_errors:
        click.echo(f"Warning: {err}", err=True)

    if load_errors and not profiles and not inventories:
        sys.exit(1)
