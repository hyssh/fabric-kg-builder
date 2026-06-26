"""Unified validation suite — SPEC-005 VAL + BRG + D gates.

Composes the low-level data_gates (Fenster's VAL-001..012) and bridge_validation
(McManus's BRG-001..010) into a single callable that returns
:class:`ValidationViolation` objects keyed by SPEC-005 rule IDs.

New gates added here
--------------------
VAL-008   chunks.document_element_id FK → document_elements
VAL-010   visual_assets.blob_url non-null after upload
VAL-013   AI Search docs blob_url → visual_assets.blob_url consistency
VAL-014   Ontology visual/image entity types declare blob_url property
VAL-019   AI Search schema field alignment with generated index documents
VAL-023   chunk/visual AI Search indexes contain no structured Parquet rows
VAL-024   Domain text appears only in user role, never in system prompt
VAL-025   Required environment variables are non-empty at startup
VAL-026   No secret values in committed YAML/JSON config files
VAL-027   Foundry chat deployment config is non-empty / resolvable
VAL-028   visual_region.polygon_json populated for document_intelligence source
D-31      Chunks with related_entity_ids must have entity_search_keys
D-32      Non-placeholder entities must have search_aliases

Public API
----------
    from fabric_kg_builder.validate.suite import ValidationViolation, validate_all

    violations = validate_all(build_dir="build", config={...}, env_vars=os.environ)
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fabric_kg_builder.ontology.bridge_validation import (
    BridgeViolation,
    validate_bridge,
)
from fabric_kg_builder.validate.data_gates import Violation as _DGViolation
from fabric_kg_builder.validate.data_gates import run_gates as _run_data_gates


# ---------------------------------------------------------------------------
# Canonical violation type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationViolation:
    """A single validation gate result (SPEC-005 IDs).

    Attributes:
        rule_id:  SPEC-005 rule identifier, e.g. ``"VAL-008"``.
        severity: ``"fail"`` (pipeline must stop) or ``"warn"`` (log only).
        message:  Human-readable description of the violation.
    """

    rule_id: str
    severity: str  # "fail" | "warn"
    message: str

    def __str__(self) -> str:
        tag = "FAIL" if self.severity == "fail" else "WARN"
        return f"[{self.rule_id}] {tag}: {self.message}"


# ---------------------------------------------------------------------------
# Required env vars (VAL-025)
# ---------------------------------------------------------------------------

_REQUIRED_ENV_VARS = (
    "AZURE_AI_FOUNDRY_ENDPOINT",
    "AZURE_AI_FOUNDRY_API_KEY",
    "FABRIC_WORKSPACE_ID",
    "AZURE_BLOB_CONNECTION_STRING",
)

# Secret-like patterns for VAL-026 (intentionally imprecise — false positives
# are investigated; false negatives are the real risk)
_SECRET_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S{8,}"),
    re.compile(r"(?i)secret\s*[=:]\s*\S{8,}"),
    re.compile(r"(?i)password\s*[=:]\s*\S{8,}"),
    re.compile(r"(?i)token\s*[=:]\s*\S{20,}"),
    re.compile(r"(?i)connection[_-]?string\s*[=:]\s*\S{20,}"),
    # Bearer token / base64-blob that is NOT an env-var placeholder
    re.compile(r"(?<!\$\{)[A-Za-z0-9+/]{40,}={0,2}(?!\})"),
]

# Structural entity/relationship fields that must NOT appear in chunk/visual
# AI Search index documents (VAL-023)
_STRUCTURED_FIELDS = frozenset(
    {"entity_id", "entity_type", "relationship_id", "relationship_type",
     "source_entity_id", "target_entity_id"}
)


# ---------------------------------------------------------------------------
# Adapter: data_gates Violation → ValidationViolation
# ---------------------------------------------------------------------------


def _adapt_data_gate(v: _DGViolation) -> ValidationViolation:
    """Map a data_gates Violation to a ValidationViolation with SPEC-005 severity."""
    return ValidationViolation(
        rule_id=v.gate,
        severity="fail",
        message=f"[{v.table}] {v.message}",
    )


def _adapt_brg(v: BridgeViolation) -> ValidationViolation:
    return ValidationViolation(
        rule_id=v.gate_id,
        severity="fail" if v.severity == "error" else "warn",
        message=v.message,
    )


# ---------------------------------------------------------------------------
# Parquet reader (optional dep — graceful no-op when pyarrow not installed)
# ---------------------------------------------------------------------------


def _read_parquet_tables(build_dir: Path) -> dict[str, list[dict]]:
    """Read all canonical Parquet tables from *build_dir*/<name>.parquet.

    Returns a dict mapping table name → list of row dicts.
    Silently skips missing files.  Requires pyarrow.

    Reads via pyarrow directly (a hard dependency) rather than going through a
    pandas round-trip.  pyarrow's ``to_pylist`` is version-stable across the
    pyarrow releases CI pins, whereas ``pandas.read_parquet`` round-tripping a
    pyarrow-written table can raise on some pyarrow/pandas version combinations
    (e.g. tz-aware timestamp columns) — which previously caused tables to be
    silently dropped and validators to no-op.
    """
    table_rows: dict[str, list[dict]] = {}
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return table_rows

    parquet_dir = Path(build_dir)
    for parquet_path in parquet_dir.glob("*.parquet"):
        try:
            table = pq.read_table(parquet_path)
            table_rows[parquet_path.stem] = table.to_pylist()
        except Exception:  # noqa: BLE001 — skip unreadable tables
            pass
    return table_rows


# ---------------------------------------------------------------------------
# VAL-008 — chunk.document_element_id → document_elements
# ---------------------------------------------------------------------------


def _val008_chunk_docelem_fk(table_rows: dict[str, list[dict]]) -> list[ValidationViolation]:
    """VAL-008: Any document_element_id in chunks not in document_elements."""
    de_ids = {
        r["document_element_id"]
        for r in table_rows.get("document_elements", [])
        if r.get("document_element_id") is not None
    }
    violations: list[ValidationViolation] = []
    for chunk in table_rows.get("chunks", []):
        deid = chunk.get("document_element_id")
        cid = chunk.get("chunk_id", "<unknown>")
        if deid is not None and deid not in de_ids:
            violations.append(ValidationViolation(
                "VAL-008", "fail",
                f"Chunk {cid}: document_element_id '{deid}' not found in document_elements",
            ))
    return violations


# ---------------------------------------------------------------------------
# VAL-010 — visual_assets.blob_url non-null after upload
# ---------------------------------------------------------------------------


def _val010_blob_url_present(table_rows: dict[str, list[dict]]) -> list[ValidationViolation]:
    """VAL-010: Every non-placeholder visual asset must have a blob_url."""
    violations: list[ValidationViolation] = []
    for va in table_rows.get("visual_assets", []):
        if va.get("is_placeholder"):
            continue
        if not va.get("blob_url"):
            violations.append(ValidationViolation(
                "VAL-010", "fail",
                f"Visual asset '{va.get('image_id', '<unknown>')}' has no blob_url after upload",
            ))
    return violations


# ---------------------------------------------------------------------------
# VAL-013 — AI Search blob_url reference consistency
# ---------------------------------------------------------------------------


def _val013_search_blob_url(
    table_rows: dict[str, list[dict]],
    search_dir: Path,
) -> list[ValidationViolation]:
    """VAL-013: Search doc blob_url must match the uploaded blob_url in visual_assets."""
    if not search_dir.exists():
        return []

    va_by_id: dict[str, str | None] = {
        r["image_id"]: r.get("blob_url")
        for r in table_rows.get("visual_assets", [])
        if r.get("image_id")
    }

    violations: list[ValidationViolation] = []
    for json_path in search_dir.rglob("*.json"):
        try:
            doc = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(doc, dict):
            continue
        blob_url = doc.get("blob_url")
        image_id = doc.get("image_id") or doc.get("figure_id")
        if blob_url and image_id:
            expected = va_by_id.get(image_id)
            if expected is not None and blob_url != expected:
                violations.append(ValidationViolation(
                    "VAL-013", "fail",
                    f"Search document '{doc.get('chunk_id', json_path.name)}': "
                    f"blob_url mismatch for asset '{image_id}' "
                    f"(search={blob_url!r}, parquet={expected!r})",
                ))
    return violations


# ---------------------------------------------------------------------------
# VAL-014 — Ontology visual/image nodes declare blob_url property
# ---------------------------------------------------------------------------

_VISUAL_ENTITY_TYPES = frozenset({"ImageAsset", "Figure", "VisualRegion"})


def _val014_ontology_blob_url_property(model: dict[str, Any]) -> list[ValidationViolation]:
    """VAL-014: ImageAsset, Figure, VisualRegion entity types must have blob_url."""
    violations: list[ValidationViolation] = []
    for et in model.get("entityTypes", []):
        name = et.get("name", "")
        if name not in _VISUAL_ENTITY_TYPES:
            continue
        props = {p["name"] for p in et.get("properties", [])}
        if "blob_url" not in props:
            violations.append(ValidationViolation(
                "VAL-014", "fail",
                f"Ontology entity type '{name}' is missing required blob_url property",
            ))
    return violations


# ---------------------------------------------------------------------------
# VAL-019 — AI Search schema field alignment with index documents
# ---------------------------------------------------------------------------


def _val019_search_schema_alignment(search_dir: Path) -> list[ValidationViolation]:
    """VAL-019: Every index document field must be declared in the schema.

    Expects *search_dir*/<index-name>/schema.json and <index-name>/documents/*.json.
    Skips indices or schemas that cannot be read.
    """
    if not search_dir.exists():
        return []

    violations: list[ValidationViolation] = []
    for index_dir in search_dir.iterdir():
        if not index_dir.is_dir():
            continue
        schema_path = index_dir / "schema.json"
        if not schema_path.exists():
            continue
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue

        declared_fields: set[str] = {
            f["name"]
            for f in schema.get("fields", [])
            if isinstance(f, dict) and "name" in f
        }
        if not declared_fields:
            continue

        docs_dir = index_dir / "documents"
        if not docs_dir.exists():
            continue
        for doc_path in docs_dir.glob("*.json"):
            try:
                doc = json.loads(doc_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            doc_fields = set(doc.keys())
            extra = doc_fields - declared_fields
            missing = declared_fields - doc_fields
            if extra or missing:
                violations.append(ValidationViolation(
                    "VAL-019", "fail",
                    f"Search index '{index_dir.name}' document '{doc_path.stem}' "
                    f"has field mismatch — extra: {sorted(extra)}, missing: {sorted(missing)}",
                ))
    return violations


# ---------------------------------------------------------------------------
# VAL-023 — No structured Parquet rows in chunk/visual AI Search indexes
# ---------------------------------------------------------------------------


def _val023_no_structured_rows_in_search(search_dir: Path) -> list[ValidationViolation]:
    """VAL-023: chunk/visual AI Search indexes must not contain entity/relationship fields."""
    if not search_dir.exists():
        return []

    violations: list[ValidationViolation] = []
    for index_dir in search_dir.iterdir():
        if not index_dir.is_dir():
            continue
        docs_dir = index_dir / "documents"
        if not docs_dir.exists():
            continue
        for doc_path in docs_dir.glob("*.json"):
            try:
                doc = json.loads(doc_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            leaked = _STRUCTURED_FIELDS & set(doc.keys())
            if leaked:
                violations.append(ValidationViolation(
                    "VAL-023", "fail",
                    f"deploy-lakehouse: structured Parquet field(s) {sorted(leaked)} "
                    f"found in AI Search index '{index_dir.name}' document '{doc_path.stem}' "
                    f"— entity and relationship tables must be deployed to the Lakehouse, not AI Search",
                ))
    return violations


# ---------------------------------------------------------------------------
# VAL-024 — Domain text must appear in USER role only (prompt-builder boundary)
# ---------------------------------------------------------------------------


def _val024_domain_not_in_system_prompt(
    messages: list[dict[str, str]],
    domain_text: str,
    call_id: str = "<unknown>",
) -> list[ValidationViolation]:
    """VAL-024: Assert domain_text does not appear in any role:system message.

    Call this at the prompt-builder boundary before each LLM call.
    ``messages`` is the list of ``{role, content}`` dicts passed to the model.
    """
    violations: list[ValidationViolation] = []
    for msg in messages:
        if msg.get("role") == "system" and domain_text and domain_text in (msg.get("content") or ""):
            violations.append(ValidationViolation(
                "VAL-024", "fail",
                f"Domain text detected in LLM system prompt at call '{call_id}' — "
                f"domain text must be in user role only",
            ))
    return violations


# ---------------------------------------------------------------------------
# VAL-025 — Required env vars present
# ---------------------------------------------------------------------------


def _val025_required_env_vars(
    env_vars: dict[str, str] | None = None,
) -> list[ValidationViolation]:
    """VAL-025: AZURE_AI_FOUNDRY_ENDPOINT, _API_KEY, FABRIC_WORKSPACE_ID,
    AZURE_BLOB_CONNECTION_STRING must all be non-empty.
    """
    if env_vars is None:
        env_vars = dict(os.environ)
    violations: list[ValidationViolation] = []
    for var in _REQUIRED_ENV_VARS:
        if not env_vars.get(var, "").strip():
            violations.append(ValidationViolation(
                "VAL-025", "fail",
                f"Required env var '{var}' is missing or empty — "
                f"set it in .env or the environment before running",
            ))
    return violations


# ---------------------------------------------------------------------------
# VAL-026 — No secret values in committed config files
# ---------------------------------------------------------------------------


def _val026_no_secrets_in_config(
    config_paths: list[Path],
) -> list[ValidationViolation]:
    """VAL-026: Scan YAML/JSON config files for raw secret-like values."""
    violations: list[ValidationViolation] = []
    for path in config_paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                value = match.group()
                # Skip env-var placeholders like ${MY_SECRET}
                if "${" in value or value.startswith("$"):
                    continue
                violations.append(ValidationViolation(
                    "VAL-026", "fail",
                    f"Possible secret value in config file '{path}' "
                    f"matched pattern '{pattern.pattern[:40]}' — "
                    f"use ${{ENV_VAR}} interpolation instead",
                ))
                break  # one violation per file-per-pattern is enough
    return violations


# ---------------------------------------------------------------------------
# VAL-027 — Foundry chat deployment config non-empty
# ---------------------------------------------------------------------------


def _val027_foundry_config(config: dict[str, Any]) -> list[ValidationViolation]:
    """VAL-027: fabric-kg.yaml enrichment.chat_deployment and foundry.endpoint must be non-empty."""
    violations: list[ValidationViolation] = []
    foundry = config.get("foundry") or {}
    enrichment = config.get("enrichment") or {}

    # chat_deployment lives under enrichment.chat_deployment in fabric-kg.yaml
    # but may also be under foundry.chat_deployment in programmatic config dicts
    chat_deployment = (
        (enrichment.get("chat_deployment") if isinstance(enrichment, dict) else None)
        or (foundry.get("chat_deployment") if isinstance(foundry, dict) else None)
        or getattr(foundry, "chat_deployment", None)
    )
    endpoint = (
        foundry.get("endpoint")
        if isinstance(foundry, dict)
        else getattr(foundry, "endpoint", None)
    )
    if not chat_deployment:
        violations.append(ValidationViolation(
            "VAL-027", "fail",
            "Foundry deployment name is empty — check 'enrichment.chat_deployment' "
            "in fabric-kg.yaml and AZURE_AI_FOUNDRY_* credentials",
        ))
    if not endpoint:
        violations.append(ValidationViolation(
            "VAL-027", "fail",
            "Foundry endpoint is empty — set AZURE_AI_FOUNDRY_ENDPOINT or "
            "'foundry.endpoint' in fabric-kg.yaml",
        ))
    return violations


# ---------------------------------------------------------------------------
# VAL-028 — visual_region.polygon_json for document_intelligence source
# ---------------------------------------------------------------------------


def _val028_polygon_json(table_rows: dict[str, list[dict]]) -> list[ValidationViolation]:
    """VAL-028: visual_region rows with source_type=document_intelligence must have
    non-null, valid-JSON polygon_json.
    """
    violations: list[ValidationViolation] = []
    for vr in table_rows.get("visual_regions", []):
        if vr.get("source_type") != "document_intelligence":
            continue
        vrid = vr.get("visual_region_id", "<unknown>")
        polygon_json = vr.get("polygon_json")
        if polygon_json is None:
            violations.append(ValidationViolation(
                "VAL-028", "fail",
                f"VisualRegion '{vrid}': polygon_json is null for document_intelligence source",
            ))
            continue
        try:
            parsed = json.loads(polygon_json) if isinstance(polygon_json, str) else polygon_json
            if not isinstance(parsed, list) or len(parsed) == 0:
                raise ValueError("empty polygon")
        except Exception:  # noqa: BLE001
            violations.append(ValidationViolation(
                "VAL-028", "fail",
                f"VisualRegion '{vrid}': polygon_json is invalid or empty for "
                f"document_intelligence source",
            ))
    return violations


# ---------------------------------------------------------------------------
# D-31/D-32 alias gates (warn)
# ---------------------------------------------------------------------------


def _d31_chunk_entity_search_keys(table_rows: dict[str, list[dict]]) -> list[ValidationViolation]:
    """D-31: Chunks with related_entity_ids must also have entity_search_keys."""
    violations: list[ValidationViolation] = []
    for chunk in table_rows.get("chunks", []):
        cid = chunk.get("chunk_id", "<unknown>")
        related = chunk.get("related_entity_ids")
        has_related = related is not None and (
            (isinstance(related, list) and len(related) > 0)
            or (isinstance(related, str) and related not in ("", "[]", "null"))
        )
        search_keys = chunk.get("entity_search_keys")
        has_keys = search_keys is not None and (
            (isinstance(search_keys, list) and len(search_keys) > 0)
            or (isinstance(search_keys, str) and search_keys not in ("", "[]", "null"))
        )
        if has_related and not has_keys:
            violations.append(ValidationViolation(
                "D-31", "warn",
                f"Chunk '{cid}' has related_entity_ids but null entity_search_keys "
                f"— alias keyword boost will be degraded in AI Search",
            ))
    return violations


def _d32_entity_search_aliases(table_rows: dict[str, list[dict]]) -> list[ValidationViolation]:
    """D-32: Non-placeholder entities must have search_aliases."""
    violations: list[ValidationViolation] = []
    for ent in table_rows.get("entities", []):
        eid = ent.get("entity_id", "<unknown>")
        if ent.get("is_placeholder"):
            continue
        search_aliases = ent.get("search_aliases")
        has_aliases = search_aliases is not None and (
            (isinstance(search_aliases, list) and len(search_aliases) > 0)
            or (isinstance(search_aliases, str) and search_aliases not in ("", "[]", "null"))
        )
        if not has_aliases:
            violations.append(ValidationViolation(
                "D-32", "warn",
                f"Entity '{eid}' has is_placeholder=False but null search_aliases "
                f"— AI Search alias coverage will be incomplete",
            ))
    return violations


# ---------------------------------------------------------------------------
# Ontology loader helper
# ---------------------------------------------------------------------------


def _load_model(build_dir: Path) -> dict[str, Any] | None:
    """Try to load model.yaml from the repo root or build/ontology."""
    candidates = [
        Path("ontology") / "model.yaml",
        build_dir / "ontology" / "model.yaml",
        build_dir.parent / "ontology" / "model.yaml",
    ]
    for p in candidates:
        if p.exists():
            try:
                raw = yaml.safe_load(p.read_text(encoding="utf-8"))
                return raw.get("ontology", raw) if isinstance(raw, dict) else raw
            except Exception:  # noqa: BLE001
                return None
    return None


# ---------------------------------------------------------------------------
# Config file scanner helper
# ---------------------------------------------------------------------------


def _find_config_files(project_root: Path) -> list[Path]:
    """Return YAML/JSON config files that should be scanned for secrets."""
    paths: list[Path] = []
    fg = project_root / "fabric-kg.yaml"
    if fg.exists():
        paths.append(fg)
    env_dir = project_root / "ontology" / "environments"
    if env_dir.exists():
        paths.extend(env_dir.glob("*.json"))
    return paths


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_all(
    build_dir: str | Path = "build",
    config: dict[str, Any] | None = None,
    *,
    env_vars: dict[str, str] | None = None,
    skip_env_check: bool = False,
) -> list[ValidationViolation]:
    """Run the full SPEC-005 validation suite against *build_dir* artifacts.

    Parameters
    ----------
    build_dir:
        Root build output directory.  Parquet files are expected in
        ``<build_dir>/*.parquet``; search artifacts in ``<build_dir>/search/``;
        ontology compiled output in ``<build_dir>/ontology/``.
    config:
        Runtime configuration dict.  Keys: ``"foundry"`` (dict with
        ``endpoint`` and ``chat_deployment``).  May be None.
    env_vars:
        Environment variables to check.  Defaults to ``os.environ``.
        Pass an explicit dict in tests.
    skip_env_check:
        When True, skip VAL-025 (env-var presence) and VAL-026 (secret scan).
        Use in unit tests to avoid polluting CI with credential errors.

    Returns
    -------
    list[ValidationViolation]
        All violations found.  Empty means all gates passed.
    """
    build_dir = Path(build_dir)
    config = config or {}
    violations: list[ValidationViolation] = []

    # ---- Read Parquet tables ----
    parquet_dir = build_dir / "parquet" if (build_dir / "parquet").exists() else build_dir
    table_rows = _read_parquet_tables(parquet_dir)

    # ---- Data integrity gates (Fenster VAL-001..012 → keep their IDs) ----
    for dg_v in _run_data_gates(table_rows):
        violations.append(_adapt_data_gate(dg_v))

    # ---- New data gates ----
    violations.extend(_val008_chunk_docelem_fk(table_rows))
    violations.extend(_val010_blob_url_present(table_rows))
    violations.extend(_d31_chunk_entity_search_keys(table_rows))
    violations.extend(_d32_entity_search_aliases(table_rows))
    violations.extend(_val028_polygon_json(table_rows))

    # ---- Search directory gates ----
    search_dir = build_dir / "search"
    violations.extend(_val013_search_blob_url(table_rows, search_dir))
    violations.extend(_val019_search_schema_alignment(search_dir))
    violations.extend(_val023_no_structured_rows_in_search(search_dir))

    # ---- Ontology gates (BRG-001..010 + VAL-014) ----
    model = _load_model(build_dir)
    if model is not None:
        for brg_v in validate_bridge(model):
            violations.append(_adapt_brg(brg_v))
        violations.extend(_val014_ontology_blob_url_property(model))

    # ---- Config / environment gates ----
    if not skip_env_check:
        violations.extend(_val025_required_env_vars(env_vars))
        project_root = build_dir.parent
        config_files = _find_config_files(project_root)
        violations.extend(_val026_no_secrets_in_config(config_files))

    if config:
        violations.extend(_val027_foundry_config(config))

    return violations
