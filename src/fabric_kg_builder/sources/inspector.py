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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile

from pdfplumber.utils.exceptions import PdfminerException
from pydantic import BaseModel, Field, model_validator

from fabric_kg_builder.model.ids import content_hash as compute_content_hash
from fabric_kg_builder.model.ids import make_id
from fabric_kg_builder.model.schemas import DocumentElementRow
from fabric_kg_builder.release.redact import redact_secret_text
from fabric_kg_builder.sources import router
from fabric_kg_builder.sources.adapter import AdapterError


PROFILE_SCHEMA_VERSION = "1.0"

MAX_PROPOSAL_SAMPLES = 12
MAX_PROPOSAL_SAMPLES_PER_KIND = 4
MAX_PROPOSAL_SAMPLES_PER_FILE = 3
MAX_SAMPLE_EXCERPT_CHARS = 240
MAX_SAMPLE_EXCERPT_TOTAL_CHARS = 1_800
MIN_SAMPLE_EXCERPT_CHARS = 32

_SAMPLE_KIND_ORDER = ("heading", "text", "table", "visual")
_SAMPLE_KIND_BY_ELEMENT_TYPE: dict[str, str] = {
    "section": "heading",
    "paragraph": "text",
    "ocr_text": "text",
    "table": "table",
    "table_row": "table",
    "vision_description": "visual",
}
_PROFILE_HASH_EXCLUDED_KEYS = frozenset(
    {"approved", "approved_at_utc", "approved_by", "inspected_at_utc", "profile_hash"}
)

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


class SourceProposalSample(BaseModel):
    """Bounded, cited source excerpt used for domain proposal review."""

    sample_id: str
    sample_kind: str
    element_type: str
    source_file_id: str
    citation_path: str
    page_number: int | None = None
    section_path: str | None = None
    row_index: int | None = None
    col_index: int | None = None
    sort_order: int | None = None
    excerpt: str
    content_hash: str


class SourceSamplingWarning(BaseModel):
    """Visible sampling warning preserving typed extraction failures."""

    warning_id: str
    warning_type: str
    citation_path: str
    source_file_id: str | None = None
    message: str


class SourceProfile(BaseModel):
    """Approved source profile persisted before domain contract generation."""

    schema_version: str = PROFILE_SCHEMA_VERSION
    observed: ObservedFacts = Field(default_factory=ObservedFacts)
    inferred: InferredSuggestions = Field(default_factory=InferredSuggestions)
    proposal_samples: list[SourceProposalSample] = Field(default_factory=list)
    sampling_warnings: list[SourceSamplingWarning] = Field(default_factory=list)
    domain_description: str | None = None
    domain_hash: str | None = None  # contract_hash of domain.yaml if incorporated
    source_hash: str = ""          # SHA-256 over (name, size, mtime) of all files
    inspected_at_utc: str = ""
    approved: bool = False
    approved_at_utc: str | None = None
    approved_by: str | None = None
    user_corrected: bool = False   # True when user explicitly corrected inferred fields
    profile_hash: str = ""

    @model_validator(mode="after")
    def _refresh_profile_hash(self) -> SourceProfile:
        self.profile_hash = compute_source_profile_hash(self)
        return self


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


def _canonicalize_profile_hash_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _canonicalize_profile_hash_value(val)
            for key, val in sorted(value.items())
            if key not in _PROFILE_HASH_EXCLUDED_KEYS
        }
    if isinstance(value, list):
        return [_canonicalize_profile_hash_value(item) for item in value]
    return value


def compute_source_profile_hash(profile: SourceProfile | dict[str, object]) -> str:
    """Return a canonical, approval-stable hash for a source profile."""
    raw = profile.model_dump(mode="json") if isinstance(profile, SourceProfile) else dict(profile)
    canonical = _canonicalize_profile_hash_value(raw)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _SampleCandidate:
    sample_id: str
    sample_kind: str
    element_type: str
    source_file_id: str
    citation_path: str
    page_number: int | None
    section_path: str | None
    row_index: int | None
    col_index: int | None
    sort_order: int | None
    excerpt: str
    content_hash: str

    def to_model(self) -> SourceProposalSample:
        return SourceProposalSample(
            sample_id=self.sample_id,
            sample_kind=self.sample_kind,
            element_type=self.element_type,
            source_file_id=self.source_file_id,
            citation_path=self.citation_path,
            page_number=self.page_number,
            section_path=self.section_path,
            row_index=self.row_index,
            col_index=self.col_index,
            sort_order=self.sort_order,
            excerpt=self.excerpt,
            content_hash=self.content_hash,
        )


def _safe_citation_path(file_path: Path, source_root: Path) -> str:
    try:
        citation = file_path.resolve().relative_to(source_root.resolve()).as_posix()
    except ValueError:
        citation = file_path.name
    return redact_secret_text(citation)


def _excerpt_text(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", redact_secret_text(text)).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(limit - 1, 0)].rstrip() + "…"


def _candidate_from_element(
    element: DocumentElementRow,
    citation_path: str,
) -> _SampleCandidate | None:
    sample_kind = _SAMPLE_KIND_BY_ELEMENT_TYPE.get(element.element_type)
    if sample_kind is None:
        return None
    raw_text = (element.content or element.title or "").strip()
    if not raw_text:
        return None
    excerpt = _excerpt_text(raw_text, MAX_SAMPLE_EXCERPT_CHARS)
    if not excerpt:
        return None
    excerpt_hash = compute_content_hash(excerpt)
    return _SampleCandidate(
        sample_id=make_id("sample", f"{sample_kind}:{element.document_element_id}"),
        sample_kind=sample_kind,
        element_type=element.element_type,
        source_file_id=element.source_file_id,
        citation_path=citation_path,
        page_number=element.page_number,
        section_path=(
            redact_secret_text(element.section_path)
            if element.section_path
            else None
        ),
        row_index=element.row_index,
        col_index=element.col_index,
        sort_order=element.sort_order,
        excerpt=excerpt,
        content_hash=excerpt_hash,
    )


def _candidate_sort_key(candidate: _SampleCandidate) -> tuple[object, ...]:
    return (
        candidate.citation_path,
        _SAMPLE_KIND_ORDER.index(candidate.sample_kind),
        candidate.page_number if candidate.page_number is not None else -1,
        candidate.sort_order if candidate.sort_order is not None else -1,
        candidate.row_index if candidate.row_index is not None else -1,
        candidate.col_index if candidate.col_index is not None else -1,
        candidate.section_path or "",
        candidate.sample_id,
    )


def _warning_type(exc: Exception) -> str:
    if isinstance(exc, AdapterError):
        return exc.failure_type.value
    if isinstance(exc, BadZipFile):
        return "bad_zip_file"
    if isinstance(exc, PdfminerException):
        return "pdf_parse_error"
    if isinstance(exc, UnicodeDecodeError):
        return "unicode_decode_error"
    if isinstance(exc, csv.Error):
        return "csv_error"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, OSError):
        return "os_error"
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return "import_error"
    return "value_error"


def _sampling_warning(citation_path: str, exc: Exception) -> SourceSamplingWarning:
    safe_message = redact_secret_text(str(exc))
    return SourceSamplingWarning(
        warning_id=make_id("samplewarn", f"{citation_path}:{_warning_type(exc)}:{exc}"),
        warning_type=_warning_type(exc),
        citation_path=citation_path,
        message=safe_message,
    )


def _sample_source_file(
    file_path: Path,
    source_root: Path,
) -> tuple[list[_SampleCandidate], SourceSamplingWarning | None]:
    citation_path = _safe_citation_path(file_path, source_root)
    try:
        result = router.extract(file_path)
    except (
        AdapterError,
        BadZipFile,
        PdfminerException,
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        csv.Error,
        ImportError,
        ValueError,
    ) as exc:
        return [], _sampling_warning(citation_path, exc)

    elements = sorted(
        getattr(result, "document_elements", []),
        key=lambda element: (
            element.sort_order if element.sort_order is not None else -1,
            element.page_number if element.page_number is not None else -1,
            element.row_index if element.row_index is not None else -1,
            element.col_index if element.col_index is not None else -1,
            element.document_element_id,
        ),
    )
    candidates = [
        candidate
        for element in elements
        if (candidate := _candidate_from_element(element, citation_path)) is not None
    ]
    return sorted(candidates, key=_candidate_sort_key), None


def _try_add_candidate(
    candidate: _SampleCandidate,
    *,
    selected: list[_SampleCandidate],
    selected_ids: set[str],
    per_kind: Counter[str],
    per_file: Counter[str],
    total_chars: int,
) -> int:
    if candidate.sample_id in selected_ids:
        return total_chars
    if len(selected) >= MAX_PROPOSAL_SAMPLES:
        return total_chars
    if per_kind[candidate.sample_kind] >= MAX_PROPOSAL_SAMPLES_PER_KIND:
        return total_chars
    if per_file[candidate.citation_path] >= MAX_PROPOSAL_SAMPLES_PER_FILE:
        return total_chars
    remaining = MAX_SAMPLE_EXCERPT_TOTAL_CHARS - total_chars
    if remaining < MIN_SAMPLE_EXCERPT_CHARS:
        return total_chars

    excerpt = candidate.excerpt
    if len(excerpt) > remaining:
        excerpt = _excerpt_text(excerpt, remaining)
        if len(excerpt) < MIN_SAMPLE_EXCERPT_CHARS:
            return total_chars
        candidate = _SampleCandidate(
            sample_id=candidate.sample_id,
            sample_kind=candidate.sample_kind,
            element_type=candidate.element_type,
            source_file_id=candidate.source_file_id,
            citation_path=candidate.citation_path,
            page_number=candidate.page_number,
            section_path=candidate.section_path,
            row_index=candidate.row_index,
            col_index=candidate.col_index,
            sort_order=candidate.sort_order,
            excerpt=excerpt,
            content_hash=compute_content_hash(excerpt),
        )

    selected.append(candidate)
    selected_ids.add(candidate.sample_id)
    per_kind[candidate.sample_kind] += 1
    per_file[candidate.citation_path] += 1
    return total_chars + len(candidate.excerpt)


def _select_proposal_samples(candidates: list[_SampleCandidate]) -> list[SourceProposalSample]:
    grouped: dict[str, dict[str, list[_SampleCandidate]]] = {
        kind: {} for kind in _SAMPLE_KIND_ORDER
    }
    for candidate in sorted(candidates, key=_candidate_sort_key):
        grouped[candidate.sample_kind].setdefault(candidate.citation_path, []).append(candidate)

    selected: list[_SampleCandidate] = []
    selected_ids: set[str] = set()
    per_kind: Counter[str] = Counter()
    per_file: Counter[str] = Counter()
    total_chars = 0

    for kind in _SAMPLE_KIND_ORDER:
        for citation_path in sorted(grouped[kind]):
            if grouped[kind][citation_path]:
                total_chars = _try_add_candidate(
                    grouped[kind][citation_path].pop(0),
                    selected=selected,
                    selected_ids=selected_ids,
                    per_kind=per_kind,
                    per_file=per_file,
                    total_chars=total_chars,
                )

    made_progress = True
    while made_progress and len(selected) < MAX_PROPOSAL_SAMPLES and total_chars < MAX_SAMPLE_EXCERPT_TOTAL_CHARS:
        made_progress = False
        for kind in _SAMPLE_KIND_ORDER:
            for citation_path in sorted(grouped[kind]):
                bucket = grouped[kind][citation_path]
                if not bucket:
                    continue
                before = len(selected)
                total_chars = _try_add_candidate(
                    bucket.pop(0),
                    selected=selected,
                    selected_ids=selected_ids,
                    per_kind=per_kind,
                    per_file=per_file,
                    total_chars=total_chars,
                )
                made_progress = made_progress or len(selected) > before

    return [candidate.to_model() for candidate in selected]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_source_profile(
    source_path: Path,
    domain_description: str | None = None,
    *,
    include_proposal_samples: bool = False,
) -> SourceProfile:
    """Inspect *source_path* and build a SourceProfile.

    Parameters
    ----------
    source_path:
        A file or directory containing source documents.
    domain_description:
        Optional existing domain description to incorporate.
    include_proposal_samples:
        Run source adapters and persist bounded excerpts for schema-2.0 domain
        proposal generation. The default remains metadata-only for schema-1.0
        compatibility.

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

    sample_candidates: list[_SampleCandidate] = []
    sampling_warnings: list[SourceSamplingWarning] = []
    if include_proposal_samples:
        source_root = source_path if source_path.is_dir() else source_path.parent
        for file_path in files:
            candidates, warning = _sample_source_file(file_path, source_root)
            sample_candidates.extend(candidates)
            if warning is not None:
                sampling_warnings.append(warning)

    return SourceProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        observed=observed,
        inferred=inferred,
        proposal_samples=_select_proposal_samples(sample_candidates),
        sampling_warnings=sampling_warnings,
        domain_description=domain_description,
        source_hash=_compute_source_hash(files),
        inspected_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        approved=False,
    )


def save_source_profile(profile: SourceProfile, path: Path) -> None:
    """Persist *profile* as JSON to *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile.profile_hash = compute_source_profile_hash(profile)
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
    profile = SourceProfile.model_validate(raw)
    profile.profile_hash = compute_source_profile_hash(profile)
    return profile


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

    if profile.proposal_samples:
        lines.append(f"  Proposal samples: {len(profile.proposal_samples)} bounded excerpt(s)")

    if profile.sampling_warnings:
        lines.append("  Sampling warnings:")
        for warning in profile.sampling_warnings:
            lines.append(f"    - [{warning.warning_type}] {warning.citation_path}: {warning.message}")

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
