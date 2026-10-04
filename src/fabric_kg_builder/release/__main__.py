"""Command-line entry point for the release package.

Usage::

    python -m fabric_kg_builder.release [--manifest <path>] [--ledger <path>]
        [--report-out <path>] [--require-live-smoke] [--check]

Generates or checks a release evidence report from a supplied manifest JSON
and optional ledger JSON.  Does not require cloud credentials.  Exits with
code 1 when readiness is NOT_READY.

This module does NOT alter pyproject.toml or the main CLI entry points.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fabric_kg_builder.release.ledger import ResourceLedger
from fabric_kg_builder.release.manifest import ReleaseManifest, build_empty_manifest
from fabric_kg_builder.release.redact import redact_evidence_manifest, redact_ledger
from fabric_kg_builder.release.report import ReadinessStatus, generate_report


def _load_manifest(path: Path) -> ReleaseManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReleaseManifest.model_validate(data)


def _load_ledger(path: Path) -> ResourceLedger:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ResourceLedger.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fabric_kg_builder.release",
        description="Generate or check the release evidence report (offline, no cloud credentials).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to a release manifest JSON file.  If omitted, an empty manifest is generated.",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Path to a resource ledger JSON file.  Optional.",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=None,
        help="Write the readiness report JSON to this path.",
    )
    parser.add_argument(
        "--require-live-smoke",
        action="store_true",
        default=False,
        help="Treat REQUIRES_LIVE_SMOKE evidence as blocking.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Exit 1 if readiness is NOT_READY.",
    )
    parser.add_argument(
        "--show-empty",
        action="store_true",
        default=False,
        help="Print an empty manifest template and exit.",
    )

    args = parser.parse_args(argv)

    if args.show_empty:
        manifest = build_empty_manifest()
        safe = redact_evidence_manifest(manifest.to_safe_dict())
        print(json.dumps(safe, indent=2))
        return 0

    if args.manifest:
        if not args.manifest.exists():
            print(f"ERROR: manifest file not found: {args.manifest}", file=sys.stderr)
            return 2
        manifest = _load_manifest(args.manifest)
    else:
        manifest = build_empty_manifest()
        print(
            "NOTE: No manifest supplied; using empty template. "
            "All evidence will be at offline classification status.",
            file=sys.stderr,
        )

    ledger: ResourceLedger | None = None
    if args.ledger:
        if not args.ledger.exists():
            print(f"ERROR: ledger file not found: {args.ledger}", file=sys.stderr)
            return 2
        ledger = _load_ledger(args.ledger)

    report = generate_report(
        manifest=manifest,
        ledger=ledger,
        require_live_smoke=args.require_live_smoke,
    )

    safe_report = json.loads(report.to_json(indent=2))

    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(json.dumps(safe_report, indent=2), encoding="utf-8")
        print(f"Report written to: {args.report_out}", file=sys.stderr)

    print(json.dumps(safe_report, indent=2))

    if args.check and report.readiness == ReadinessStatus.NOT_READY:
        print(
            f"\nREADINESS: NOT_READY — {report.blocking_summary}",
            file=sys.stderr,
        )
        return 1

    print(f"\nREADINESS: {report.readiness.value}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
