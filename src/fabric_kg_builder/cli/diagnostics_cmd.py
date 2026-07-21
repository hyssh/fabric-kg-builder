"""inspect-diagnostics command — SPEC-008A §11.2 local diagnostic inspection.

Reads local Fabric Data Agent diagnostic JSON/NDJSON exports and produces a
redacted, aggregate-by-default completeness/gap report. Implements
S8A-DIA-001, S8A-DIA-002, S8A-DIA-003, and VAL-087.

This command never uploads or transmits diagnostic content: it only reads
local files named on the command line and writes a redacted report to a
local path (default under the git-ignored ``build/`` directory).

Exit codes:
  0  diagnostics are complete (no gaps, no duplicate/overlap/partial/stale
     snapshots, full required-field coverage)
  1  invalid input (missing file, unparseable JSON/NDJSON, zero records)
  2  privacy violation detected by the redaction canary (report withheld)
  3  diagnostics are incomplete (gaps found and/or duplicate, overlapping,
     partial, or stale snapshots detected, and/or required-field coverage
     is below 100%)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from fabric_kg_builder.release.diagnostics import (
    DiagnosticsInspectionError,
    DiagnosticsPrivacyViolation,
    inspect_files,
)

_EPILOG = """\b
Example:
  fabric-kg inspect-diagnostics .\\exports\\agent-diagnostics.json
  fabric-kg inspect-diagnostics .\\exports\\run1.json .\\exports\\run2.json --detail
  fabric-kg inspect-diagnostics .\\exports\\*.json --out build\\diagnostics\\report.json

Exit codes: 0 complete · 1 invalid input · 2 privacy violation · 3 incomplete diagnostics.

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("inspect-diagnostics", epilog=_EPILOG,
                context_settings={"max_content_width": 120})
@click.argument("files", nargs=-1, required=True, type=click.Path())
@click.option(
    "--out",
    default="build/diagnostics/inspection-report.json",
    show_default=True,
    type=click.Path(),
    help="Where to write the redacted, sealed inspection report.",
)
@click.option(
    "--max-age-hours",
    default=24.0,
    show_default=True,
    type=click.FloatRange(min=0.0),
    help="Maximum export freshness watermark age before a snapshot is stale.",
)
@click.option(
    "--reference-time",
    default=None,
    type=str,
    help="ISO 8601 reference timestamp for staleness checks (default: latest "
         "watermark found, or current UTC time if none are present).",
)
@click.option(
    "--detail",
    is_flag=True,
    default=False,
    help="Include a per-record redacted breakdown (default: aggregate only).",
)
def inspect_diagnostics_cmd(
    files: tuple[str, ...],
    out: str,
    max_age_hours: float,
    reference_time: str | None,
    detail: bool,
) -> None:
    """Locally inspect generic Fabric Data Agent diagnostic JSON/NDJSON exports.

    Robustly parses varying export shapes (arbitrary wrapper keys, list or
    single-record JSON, or NDJSON) without assuming one exact structure.
    Every raw identifier, free-form value, question, answer, prompt, entity
    value, document name, URL, path, or token is redacted to a deterministic
    one-way fingerprint or dropped before it can appear anywhere in the
    report -- only aggregate counts, coverage ratios, hashes, fingerprints,
    booleans, numbers, and known-safe enum tokens are ever emitted.

    Classifies gaps into schema / planning / query / runtime / evidence /
    latency categories, and deterministically detects exact duplicate,
    overlapping, partial, and stale diagnostic snapshots using whatever
    run/request/correlation/thread identities and freshness watermarks are
    present in the input. Raw diagnostic files are never copied into the
    report or into any build/package artifact.
    """
    paths: list[Path] = []
    for raw_path in files:
        p = Path(raw_path)
        if not p.is_file():
            click.echo(f"Error: file not found: {raw_path}", err=True)
            sys.exit(1)
        paths.append(p)

    try:
        report = inspect_files(
            paths,
            max_age_hours=max_age_hours,
            reference_time=reference_time,
            detail=detail,
        )
    except DiagnosticsInspectionError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except DiagnosticsPrivacyViolation as exc:
        click.echo(f"Privacy violation: {exc}", err=True)
        sys.exit(2)

    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(target)

    coverage = report["completeness"]["overall_coverage"]
    click.echo(
        f"[inspect-diagnostics] {report['status'].upper()} "
        f"files={report['inputs']['file_count']} "
        f"records={report['inputs']['record_count']} "
        f"coverage={coverage:.1%} -> {target}"
    )
    if report["status"] != "complete":
        sys.exit(3)
