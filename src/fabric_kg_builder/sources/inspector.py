"""Source inspector — builds a SourceProfile from a file collection.

Observed facts are extracted directly from file metadata (counts, sizes, dates).
Inferred suggestions are derived from heuristics and clearly labeled as such.
No LLM calls are made — all inference is schema-driven and deterministic.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field


PROFILE_SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Extension sets
# ---------------------------------------------------------------------------

_SPREADSHEET_EXTS = frozenset({".csv", ".tsv", ".xls", ".xlsx"})
_DOCUMENT_EXTS = frozenset({".pdf", ".docx", ".doc", ".pptx"})
_WEB_EXTS = frozenset({".html", ".htm", ".md"})
_DATA_EXTS = frozenset({".parquet"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tiff", ".tif"})
_ALL_SUPPORTED = _SPREADSHEET_EXTS | _DOCUMENT_EXTS | _WEB_EXTS | _DATA_EXTS | _IMAGE_EXTS

_FORMAT_LABELS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "document",
    ".doc": "document",
    ".pptx": "presentation",
    ".csv": "spreadsheet",
    ".tsv": "spreadsheet",
    ".xls": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".parquet": "parquet",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tiff": "image",
    ".tif": "image",
}

# ---------------------------------------------------------------------------
# Heuristic keyword tables (deterministic — no LLM)
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "drawings": ["draw", "dwg", "plan", "diagram", "blueprint", "schematic", "layout"],
    "equipment schedules": ["equip", "schedule", "asset", "inventory"],
    "warranties": ["warrant", "guarantee"],
    "manuals": ["manual", "guide", "instruction", "procedure", "operation"],
    "project records": ["project", "proj"],
    "maintenance records": ["mainten", "repair", "service"],
    "reports": ["report", "summar", "analys", "review", "assessment"],
    "contracts": ["contract", "agreement", "specification", "sow", "scope"],
    "invoices": ["invoice", "billing", "receipt"],
}

_ENTITY_KEYWORDS: dict[str, list[str]] = {
    "Equipment": ["equip", "machine", "device", "asset", "instrument"],
    "Project": ["project", "proj"],
    "Location": ["locat", "site", "area", "zone", "room", "floor", "building"],
    "Person": ["person", "employee", "staff", "user", "contact", "owner", "operator"],
    "Organization": ["org", "company", "vendor", "supplier", "contractor", "manufacturer"],
    "Component": ["component", "part", "module", "assembly"],
    "Work Order": ["work_order", "workorder", "wo_"],
    "Maintenance": ["mainten", "maintenance", "repair"],
    "Document": ["document", "record", "certificate"],
    "Warranty": ["warrant", "guarantee"],
}

_YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ObservedFacts(BaseModel):
    """Facts directly observed from file metadata — no inference."""

    total_file_count: int = 0
    format_counts: dict[str, int] = Field(default_factory=dict)
    total_bytes: int = 0
    date_range: list[str] | None = None  # [min_year, max_year] or None
    csv_column_names: list[str] = Field(default_factory=list)


class InferredSuggestions(BaseModel):
    """Suggestions derived from heuristics — clearly labeled, require approval."""

    document_categories: list[str] = Field(default_factory=list)
    entity_candidates: list[str] = Field(default_factory=list)
    extraction_risks: list[str] = Field(default_factory=list)


class SourceProfile(BaseModel):
    """Approved source profile persisted before domain contract generation."""

    schema_version: str = PROFILE_SCHEMA_VERSION
    observed: ObservedFacts = Field(default_factory=ObservedFacts)
    inferred: InferredSuggestions = Field(default_factory=InferredSuggestions)
    domain_description: str | None = None
    domain_hash: str | None = None  # contract_hash of domain.yaml if incorporated
    source_hash: str = ""          # SHA-256 over (name, size, mtime) of all files
    inspected_at_utc: str = ""
    approved: bool = False
    approved_at_utc: str | None = None
    approved_by: str | None = None
    user_corrected: bool = False   # True when user explicitly corrected inferred fields


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


def collect_source_files(p: Path) -> list[Path]:
    """Return supported source files from a file or directory path."""
    if p.is_file():
        return [p] if p.suffix.lower() in _ALL_SUPPORTED else []
    if p.is_dir():
        return sorted(
            f for f in p.rglob("*")
            if f.is_file() and f.suffix.lower() in _ALL_SUPPORTED
        )
    return []


# ---------------------------------------------------------------------------
# Lightweight CSV column name reader (no full load)
# ---------------------------------------------------------------------------


def _read_csv_columns(path: Path) -> list[str]:
    """Return header column names from a CSV/TSV file (first row only)."""
    try:
        raw = path.read_bytes()[:4096].decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(raw))
        row = next(reader, [])
        return [c.strip() for c in row if c.strip()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Heuristic inference helpers
# ---------------------------------------------------------------------------


def _infer_categories(files: list[Path]) -> list[str]:
    """Infer document categories from filenames using keyword matching."""
    combined_text = " ".join(f.stem.lower() for f in files)
    categories: list[str] = []
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in combined_text for kw in keywords):
            categories.append(category)
    return sorted(categories)


def _infer_entity_candidates(files: list[Path], csv_columns: list[str]) -> list[str]:
    """Infer entity candidates from filenames and CSV column names (schema-driven only)."""
    combined_text = " ".join(
        [f.stem.lower() for f in files] + [c.lower() for c in csv_columns]
    )
    candidates: list[str] = []
    for entity, keywords in _ENTITY_KEYWORDS.items():
        if any(kw in combined_text for kw in keywords):
            candidates.append(entity)
    return sorted(candidates)


def _assess_extraction_risks(files: list[Path]) -> list[str]:
    """Identify potential extraction risks from file characteristics."""
    risks: list[str] = []
    image_count = sum(1 for f in files if f.suffix.lower() in _IMAGE_EXTS)
    if image_count:
        risks.append(f"{image_count} image file(s) require OCR for text extraction")

    zero_byte_count = sum(1 for f in files if f.stat().st_size == 0)
    if zero_byte_count:
        risks.append(f"{zero_byte_count} zero-byte file(s) have no extractable content")

    # Heuristic: small PDFs (< 20 KB each) likely contain only images/scans
    small_pdf_count = sum(
        1 for f in files
        if f.suffix.lower() == ".pdf" and f.stat().st_size < 20_000
    )
    if small_pdf_count:
        risks.append(
            f"{small_pdf_count} small PDF(s) (<20 KB) may be scanned images with no embedded text"
        )

    return risks


def _extract_years(files: list[Path]) -> list[int]:
    """Extract year integers from filenames and file modification times."""
    years: list[int] = []
    for f in files:
        # From filename
        for m in _YEAR_PATTERN.findall(f.name):
            years.append(int(m))
        # From mtime
        try:
            mtime = f.stat().st_mtime
            year = datetime.fromtimestamp(mtime, tz=timezone.utc).year
            years.append(year)
        except OSError:
            pass
    return years


def _compute_source_hash(files: list[Path]) -> str:
    """Compute a deterministic SHA-256 over (name, size, mtime) for all files."""
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.name):
        try:
            stat = f.stat()
            h.update(f.name.encode("utf-8"))
            h.update(str(stat.st_size).encode("utf-8"))
            h.update(str(int(stat.st_mtime)).encode("utf-8"))
        except OSError:
            h.update(f.name.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_source_profile(
    source_path: Path,
    domain_description: str | None = None,
) -> SourceProfile:
    """Inspect *source_path* and build a SourceProfile.

    Parameters
    ----------
    source_path:
        A file or directory containing source documents.
    domain_description:
        Optional existing domain description to incorporate.

    Returns
    -------
    SourceProfile
        An unapproved profile populated with observed facts and inferred
        suggestions.  Call ``save_source_profile`` after user approval.
    """
    files = collect_source_files(source_path)

    # --- Observed facts ---
    format_counter: Counter[str] = Counter()
    total_bytes = 0
    csv_columns: list[str] = []

    for f in files:
        ext = f.suffix.lower()
        label = _FORMAT_LABELS.get(ext, ext.lstrip(".") or "unknown")
        format_counter[label] += 1
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        total_bytes += size
        if ext in _SPREADSHEET_EXTS and ext in {".csv", ".tsv"}:
            csv_columns.extend(_read_csv_columns(f))

    # Deduplicate CSV column names preserving order
    seen: set[str] = set()
    unique_csv_columns: list[str] = []
    for col in csv_columns:
        if col not in seen:
            seen.add(col)
            unique_csv_columns.append(col)

    years = _extract_years(files)
    date_range: list[str] | None = None
    if years:
        date_range = [str(min(years)), str(max(years))]

    observed = ObservedFacts(
        total_file_count=len(files),
        format_counts=dict(sorted(format_counter.items())),
        total_bytes=total_bytes,
        date_range=date_range,
        csv_column_names=unique_csv_columns,
    )

    # --- Inferred suggestions ---
    inferred = InferredSuggestions(
        document_categories=_infer_categories(files),
        entity_candidates=_infer_entity_candidates(files, unique_csv_columns),
        extraction_risks=_assess_extraction_risks(files),
    )

    return SourceProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        observed=observed,
        inferred=inferred,
        domain_description=domain_description,
        source_hash=_compute_source_hash(files),
        inspected_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        approved=False,
    )


def save_source_profile(profile: SourceProfile, path: Path) -> None:
    """Persist *profile* as JSON to *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )


def load_source_profile(path: Path) -> SourceProfile:
    """Load a persisted source profile from *path*.

    Raises
    ------
    FileNotFoundError
        When *path* does not exist.
    ValueError
        When the JSON is malformed or fails schema validation.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SourceProfile.model_validate(raw)


def render_profile_text(profile: SourceProfile) -> str:
    """Render a human-readable source profile summary."""
    lines: list[str] = ["Source profile:"]

    obs = profile.observed
    # File counts (observed)
    if obs.total_file_count == 0:
        lines.append("  (no supported source files found)")
    else:
        format_parts = ", ".join(
            f"{count} {fmt}" for fmt, count in sorted(obs.format_counts.items())
        )
        lines.append(f"  {obs.total_file_count} file(s): {format_parts}")

    # Date range (observed)
    if obs.date_range:
        min_yr, max_yr = obs.date_range
        if min_yr == max_yr:
            lines.append(f"  Observed date range: {min_yr}")
        else:
            lines.append(f"  Observed date range: {min_yr}–{max_yr}")

    # CSV column names (observed schema sample)
    if obs.csv_column_names:
        sample_cols = obs.csv_column_names[:10]
        suffix = f" (+{len(obs.csv_column_names) - 10} more)" if len(obs.csv_column_names) > 10 else ""
        lines.append(f"  Observed columns: {', '.join(sample_cols)}{suffix}")

    # Domain description
    if profile.domain_description:
        lines.append(f"  Domain description: {profile.domain_description}")

    # Inferred suggestions (clearly labeled)
    inf = profile.inferred
    if inf.document_categories:
        lines.append(
            f"  Inferred categories: {', '.join(inf.document_categories)}"
        )
    if inf.entity_candidates:
        lines.append(
            f"  Inferred entity candidates: {', '.join(inf.entity_candidates)}"
        )
    if inf.extraction_risks:
        lines.append("  Extraction risks:")
        for risk in inf.extraction_risks:
            lines.append(f"    - {risk}")

    if profile.user_corrected:
        lines.append("  (profile contains user corrections)")

    return "\n".join(lines)


def check_source_profile_staleness(profile: SourceProfile, source_path: Path) -> str | None:
    """Return a warning message when the profile's source_hash does not match
    the current contents of *source_path*.

    Returns
    -------
    str | None
        A human-readable warning when files have changed since the profile was
        approved, or ``None`` when the hash matches (profile is current).

    Graceful: returns ``None`` for any OS error or an empty stored hash.
    """
    if not profile.source_hash:
        return None
    try:
        files = collect_source_files(source_path)
        current_hash = _compute_source_hash(files)
    except OSError:
        return None
    if profile.source_hash != current_hash:
        return (
            f"Source profile is stale: files in '{source_path}' have changed "
            f"since the profile was approved. "
            f"Re-run 'fabric-kg init-domain --input {source_path} --force' to refresh. "
            f"(approved hash: {profile.source_hash[:8]}…, current: {current_hash[:8]}…)"
        )
    return None
