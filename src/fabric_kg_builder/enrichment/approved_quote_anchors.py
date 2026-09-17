"""Exact quote and source-segment adapters; canonical L2/L3 stay offset-based."""

from __future__ import annotations

from copy import deepcopy
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import re

from pydantic import ValidationError

from .schema2_extraction import RawCandidateResponse, raw_candidate_response_schema

OFFSET_MODE = "offsets-v1"
QUOTE_MODE = "quote-first-v1"
SOURCE_SPANS_MODE = "source-spans-v1"
SOURCE_SPANS_V2_MODE = "source-spans-v2"
ADAPTER_VERSION = "unique-exact-slice-codepoints/1.0.0"
SOURCE_SPANS_ADAPTER_VERSION = "source-bound-lines-html-table-codepoints/1.2.0"
SOURCE_SPANS_V2_ADAPTER_VERSION = "request-bound-local-lines-html-table-codepoints/2.0.0"
SOURCE_SPAN_MODES = (SOURCE_SPANS_MODE, SOURCE_SPANS_V2_MODE)
ANCHOR_MODES = (OFFSET_MODE, QUOTE_MODE, *SOURCE_SPAN_MODES)
SOURCE_SPANS_RULE = (
    'Every anchor is exactly {"source_unit_id": "<source_identity.source_unit_id>", '
    '"slice_id": "<source_segments.slice_id>", "start_segment_id": "<segment_id>", '
    '"end_segment_id": "<segment_id>"}. '
    "Select IDs from this request's source_segments only; never output quotes, numeric "
    "offsets or evidence IDs in anchors. Segments partition nonblank source lines "
    "and HTML table/section/row/cell tag boundaries, even when a whole table is on one line. "
    "Choose the smallest contiguous structural owning context for each entity, "
    "normally its single HTML row from row_open through row_close, not the whole "
    "table or page and not multiple unrelated owner rows. For a property choose "
    "the smallest supporting cell/content range inside that entity's owning context. "
    "A relationship range must support the relationship and include both endpoints' "
    "supporting contexts; do not weaken relationship evidence to mere co-occurrence. "
    "Ranges may span multiple segments and lines when needed. Both endpoints "
    "are inclusive and may be the same ID. Local code copies the exact substring "
    "from the start segment's first non-whitespace character through the end segment's last "
    "non-whitespace character. Interior whitespace, line endings, blank lines, Unicode "
    "and HTML remain unchanged; blank-only pieces have no selectable IDs. "
    "Values, labels, ownership and semantic IDs remain your source-grounded observations, "
    "not code-generated facts. Segment selection is not proof of semantic support; "
    "canonical L2/L3 validation still decides admissibility."
)
SOURCE_SPANS_V2_RULE = (
    'Every anchor is exactly {"start_segment_id": "s1", "end_segment_id": "s1"}, '
    "replacing s1 with selected local IDs from this request's source_segments. "
    "Return ONLY these two fields per anchor. Do not copy source identity, source "
    "hashes, slice IDs, quotes, numeric offsets or evidence IDs into anchors. "
    "Local IDs are meaningful only in this request: verified request code supplies "
    "the source identity and authorized slice, never model-authored metadata. "
    "Segments partition nonblank source lines and HTML table/section/row/cell tag "
    "boundaries, even when a whole table is on one line. Choose the smallest "
    "contiguous structural owning context for each entity, normally its single "
    "HTML row from row_open through row_close, not the whole table or page and "
    "not multiple unrelated owner rows. For a property choose the smallest "
    "supporting cell/content range inside that entity's owning context. "
    "A relationship range must support the relationship and include both endpoints' "
    "supporting contexts; do not weaken relationship evidence to mere co-occurrence. "
    "Ranges may span multiple segments and lines. Both endpoints are inclusive "
    "and may be the same ID. Local code copies one exact source substring from "
    "the first selected segment's trimmed start to the last selected segment's "
    "trimmed end. Interior whitespace, line endings, blank lines, Unicode and HTML "
    "remain unchanged; blank-only pieces have no selectable IDs. Values, labels, "
    "ownership and semantic IDs remain your source-grounded observations, not "
    "code-generated facts. Selection is a semantic proposal, not proof of support; "
    "canonical L2/L3 validation still decides admissibility."
)
QUOTE_RULE = (
    'Every anchor is exactly {"quote": "<exact source substring>"}. '
    "Do not output or calculate character offsets or evidence IDs. Copy a nonempty, "
    "field-specific quote occurring exactly once within source_text. If a short quote "
    "repeats, copy a longer exact contiguous quote that disambiguates it. Do not "
    "normalize whitespace, Unicode, punctuation or spelling. Choose quote boundaries "
    "without leading or trailing whitespace; preserve all interior whitespace. "
    "Local code resolves unique exact quotes within this authorized slice and "
    "canonical L2/L3 validation still decides admissibility."
)


class QuoteAnchorResolutionError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("APPROVED_QUOTE_ANCHOR_UNRESOLVED: see retained anchor diagnostics")


def quote_response_schema():
    schema = deepcopy(raw_candidate_response_schema())
    definitions = schema["$defs"]
    definitions["ProposedAnchor"] = {
        "title": "ExactQuoteAnchor", "type": "object", "additionalProperties": False,
        "properties": {"quote": {"type": "string", "minLength": 1}},
        "required": ["quote"],
    }
    for name in ("RawEntityCandidate", "RawRelationshipCandidate", "RawPropertyCandidate"):
        definition = definitions[name]
        key = "anchors" if name == "RawEntityCandidate" else "anchor"
        if key not in definition["required"]:
            definition["required"].append(key)
        definition["properties"][key] = (
            {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/ProposedAnchor"}}
            if key == "anchors" else {"$ref": "#/$defs/ProposedAnchor"}
        )
    return schema


def resolve_quote_response(raw, *, source_text, slice_start, slice_end):
    """Return a separate enriched copy, or fail the whole leaf without dropping items.

    Python string positions are Unicode codepoints. Never search the complete
    SourceUnit or mutate/trim quotes: ProposedAnchor would strip their boundaries.
    """
    diagnostics = []

    def fail(path, reason):
        diagnostics.append({"path": path, "reason": reason})

    if slice_start < 0 or slice_end != slice_start + len(source_text):
        raise ValueError("APPROVED_QUOTE_SLICE_INVALID")
    if not isinstance(raw, dict) or set(raw) != {"candidates"} or not isinstance(raw["candidates"], list):
        raise QuoteAnchorResolutionError([{"path": "$", "reason": "RESPONSE_ENVELOPE_INVALID"}])
    enriched = deepcopy(raw)

    def anchor(value, path):
        if not isinstance(value, dict) or set(value) != {"quote"}:
            fail(path, "QUOTE_ANCHOR_SCHEMA_INVALID")
            return
        quote = value["quote"]
        if not isinstance(quote, str) or not quote:
            fail(path, "QUOTE_EMPTY_OR_INVALID")
            return
        if quote != quote.strip():
            fail(path, "QUOTE_BOUNDARY_WHITESPACE_UNSUPPORTED")
            return
        position = source_text.find(quote)
        if position < 0:
            fail(path, "QUOTE_NOT_FOUND_IN_AUTHORIZED_SLICE")
            return
        if source_text.find(quote, position + 1) >= 0:
            fail(path, "QUOTE_AMBIGUOUS_IN_AUTHORIZED_SLICE")
            return
        value.update(span_start=slice_start + position,
                     span_end=slice_start + position + len(quote),
                     model_authored_evidence_id=None)

    for index, candidate in enumerate(enriched["candidates"]):
        path = f"$.candidates[{index}]"
        if not isinstance(candidate, dict):
            fail(path, "CANDIDATE_SCHEMA_INVALID")
            continue
        kind = candidate.get("candidate_kind")
        if kind == "entity":
            anchors = candidate.get("anchors")
            if not isinstance(anchors, list) or not anchors:
                fail(path + ".anchors", "ENTITY_QUOTES_REQUIRED")
                continue
            for number, value in enumerate(anchors):
                anchor(value, f"{path}.anchors[{number}]")
        elif kind in ("property", "relationship"):
            anchor(candidate.get("anchor"), path + ".anchor")
        else:
            fail(path, "CANDIDATE_KIND_INVALID")
    if diagnostics:
        raise QuoteAnchorResolutionError(diagnostics)
    return enriched


def quote_only_examples(examples):
    """Project already validated canonical examples; reject ambiguous quotations."""
    rendered = deepcopy(examples)
    for example in rendered:
        for candidate in example["response"]["candidates"]:
            anchors = candidate["anchors"] if candidate["candidate_kind"] == "entity" else [candidate["anchor"]]
            for anchor in anchors:
                quote = anchor["quote"]
                anchor.clear()
                anchor["quote"] = quote
        resolve_quote_response(
            example["response"], source_text=example["source_text"],
            slice_start=example["slice_start"], slice_end=example["slice_end"],
        )
    return rendered


class SourceSpanResolutionError(QuoteAnchorResolutionError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        ValueError.__init__(self, "APPROVED_SOURCE_SPAN_UNRESOLVED: see retained anchor diagnostics")


def source_span_response_schema():
    schema = quote_response_schema()
    fields = ("source_unit_id", "slice_id", "start_segment_id", "end_segment_id")
    schema["$defs"]["ProposedAnchor"] = {
        "title": "SourceSegmentRangeAnchor", "type": "object", "additionalProperties": False,
        "properties": {key: {"type": "string", "minLength": 1} for key in fields},
        "required": list(fields),
    }
    return schema


def source_span_v2_response_schema():
    schema = quote_response_schema()
    fields = ("start_segment_id", "end_segment_id")
    schema["$defs"]["ProposedAnchor"] = {
        "title": "RequestLocalSourceSegmentRangeAnchor",
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": "string", "pattern": "^s[1-9][0-9]*$"} for key in fields},
        "required": list(fields),
    }
    return schema


def _exact_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


_HTML_TOKEN = re.compile(
    r"<!--[\s\S]*?(?:-->|$)|<!\[CDATA\[[\s\S]*?(?:\]\]>|$)|"
    r"""<[A-Za-z/!?](?:[^'">]|"[^"]*"|'[^']*')*>"""
)
_TABLE_TAG = re.compile(r"<\s*(/?)\s*(table|thead|tbody|tfoot|tr|td|th)(?=[\s/>])", re.IGNORECASE)


def source_segments(*, source_unit_id, source_text_hash, source_text, slice_start, slice_end):
    """Partition at exact line and lexical HTML table-tag boundaries.

    Bounds are absolute Unicode codepoints. Blank lines are not selectable but
    remain inside any selected range. IDs bind the complete source authority AND
    lossless slice bytes, bounds, ordinal and the versioned segmentation policy.
    Tags are never decoded/repaired; comments and quoted attributes stay opaque.
    """
    if (
        not isinstance(source_unit_id, str) or not source_unit_id
        or not isinstance(source_text_hash, str) or not source_text_hash
        or not isinstance(source_text, str)
        or type(slice_start) is not int or type(slice_end) is not int
        or slice_start < 0 or slice_end != slice_start + len(source_text)
    ):
        raise ValueError("APPROVED_SOURCE_SPAN_SLICE_INVALID")
    binding = {
        "adapter_version": SOURCE_SPANS_ADAPTER_VERSION,
        "source_unit_id": source_unit_id, "source_text_hash": source_text_hash,
        "slice_start": slice_start, "slice_end": slice_end,
        "slice_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
    }
    slice_id = _exact_hash(binding)
    boundaries = {0, len(source_text)}
    line_starts = []
    position = 0
    for line in source_text.splitlines(keepends=True):
        line_starts.append(position)
        position += len(line)
        boundaries.add(position)
    tags = []
    for token in _HTML_TOKEN.finditer(source_text):
        match = _TABLE_TAG.match(token.group())
        if match is None:
            continue
        closing, name = match.groups()
        name = name.lower()
        kind = ("row" if name == "tr" else "cell" if name in ("td", "th")
                else "table" if name == "table" else "table_section")
        kind += "_close" if closing else "_open"
        boundaries.update(token.span())
        tags.append((token.start(), token.end(), kind))
    tag_starts = [start for start, _, _ in tags]
    segments = []
    ordered = sorted(boundaries)
    for left, right in zip(ordered, ordered[1:]):
        piece = source_text[left:right]
        text = piece.strip()
        if text:
            start = slice_start + left + len(piece) - len(piece.lstrip())
            end = slice_start + left + len(piece.rstrip())
            line_number = bisect_right(line_starts, start - slice_start)
            tag_index = bisect_right(tag_starts, left) - 1
            kind = tags[tag_index][2] if tag_index >= 0 and right <= tags[tag_index][1] else "content"
            ordinal = len(segments) + 1
            segment_id = f"seg:{_exact_hash([slice_id, ordinal, start, end])[:16]}:{ordinal}"
            segments.append({
                "segment_id": segment_id, "line_number": line_number,
                "segment_kind": kind, "start": start, "end": end, "text": text,
            })
    return {**binding, "slice_id": slice_id, "segments": segments}


def resolve_source_span_response(raw, **source):
    """Validate the entire response and build exact canonical anchors without search."""
    catalog = source_segments(**source)
    return _resolve_source_span_catalog(raw, catalog=catalog, source=source, model_authority=True)


def source_segments_v2(**source):
    """Reuse exact v1 partitions, but bind a new catalog with request-local IDs."""
    catalog = source_segments(**source)
    catalog["adapter_version"] = SOURCE_SPANS_V2_ADAPTER_VERSION
    catalog["slice_id"] = _exact_hash({
        key: value for key, value in catalog.items() if key not in ("slice_id", "segments")
    })
    for ordinal, segment in enumerate(catalog["segments"], 1):
        segment["segment_id"] = f"s{ordinal}"
    return catalog


def resolve_source_span_v2_response(raw, **source):
    """Resolve local selections exclusively against verified caller-supplied context."""
    catalog = source_segments_v2(**source)
    return _resolve_source_span_catalog(raw, catalog=catalog, source=source, model_authority=False)


def _resolve_source_span_catalog(raw, *, catalog, source, model_authority):
    if not isinstance(raw, dict) or set(raw) != {"candidates"} or not isinstance(raw["candidates"], list):
        raise SourceSpanResolutionError([{"path": "$", "reason": "RESPONSE_ENVELOPE_INVALID"}])
    enriched = deepcopy(raw)
    diagnostics = []
    by_id = {item["segment_id"]: item for item in catalog["segments"]}
    fields = {"start_segment_id", "end_segment_id"}
    if model_authority:
        fields |= {"source_unit_id", "slice_id"}

    def fail(path, reason):
        diagnostics.append({"path": path, "reason": reason})

    def anchor(value, path):
        if not isinstance(value, dict) or set(value) != fields or any(
            not isinstance(item, str) or not item for item in value.values()
        ):
            fail(path, "SOURCE_SPAN_ANCHOR_SCHEMA_INVALID")
            return
        if model_authority and (
            value["source_unit_id"] != catalog["source_unit_id"] or value["slice_id"] != catalog["slice_id"]
        ):
            fail(path, "SOURCE_SPAN_AUTHORITY_MISMATCH")
            return
        first, last = by_id.get(value["start_segment_id"]), by_id.get(value["end_segment_id"])
        if first is None or last is None:
            fail(path, "SOURCE_SPAN_ID_OUTSIDE_AUTHORIZED_SLICE")
            return
        if first["start"] > last["start"]:
            fail(path, "SOURCE_SPAN_REVERSED")
            return
        start, end = first["start"], last["end"]
        quote = source["source_text"][start - source["slice_start"]:end - source["slice_start"]]
        value.clear()
        value.update(quote=quote, span_start=start, span_end=end, model_authored_evidence_id=None)

    for index, candidate in enumerate(enriched["candidates"]):
        path = f"$.candidates[{index}]"
        if not isinstance(candidate, dict):
            fail(path, "CANDIDATE_SCHEMA_INVALID")
            continue
        kind = candidate.get("candidate_kind")
        if kind == "entity":
            anchors = candidate.get("anchors")
            if not isinstance(anchors, list) or not anchors:
                fail(path + ".anchors", "ENTITY_SOURCE_SPANS_REQUIRED")
                continue
            for number, value in enumerate(anchors):
                anchor(value, f"{path}.anchors[{number}]")
        elif kind in ("property", "relationship"):
            anchor(candidate.get("anchor"), path + ".anchor")
        else:
            fail(path, "CANDIDATE_KIND_INVALID")
    if diagnostics:
        raise SourceSpanResolutionError(diagnostics)
    try:
        # Validate without using the parsed result: semantic values must not be
        # rewritten by proposal validators or silently dropped at this boundary.
        json.dumps(enriched, allow_nan=False)
        RawCandidateResponse.model_validate(enriched)
    except ValidationError as exc:
        raise SourceSpanResolutionError([
            {"path": "$." + ".".join(map(str, error["loc"])), "reason": "CANDIDATE_SCHEMA_INVALID"}
            for error in exc.errors(include_input=False, include_context=False)
        ]) from None
    except (ValueError, TypeError):
        raise SourceSpanResolutionError([{"path": "$", "reason": "RESPONSE_JSON_INVALID"}]) from None
    return enriched


def source_span_examples(examples, *, anchor_mode=SOURCE_SPANS_MODE):
    """Project canonical examples only when their exact bounds are representable."""
    adapter = source_span_adapter(anchor_mode)
    rendered = deepcopy(examples)
    for number, example in enumerate(rendered):
        source = {
            "source_unit_id": f"synthetic-source-{number}",
            "source_text_hash": hashlib.sha256(example["source_text"].encode("utf-8")).hexdigest(),
            **{key: example[key] for key in ("source_text", "slice_start", "slice_end")},
        }
        catalog = adapter.catalog(**source)
        example["source_identity"] = {key: source[key] for key in (
            "source_unit_id", "source_text_hash", "slice_start", "slice_end",
        )}
        example["source_segments"] = catalog
        starts = {item["start"]: item["segment_id"] for item in catalog["segments"]}
        ends = {item["end"]: item["segment_id"] for item in catalog["segments"]}
        for candidate in example["response"]["candidates"]:
            anchors = candidate["anchors"] if candidate["candidate_kind"] == "entity" else [candidate["anchor"]]
            for anchor in anchors:
                start, end = starts.get(anchor["span_start"]), ends.get(anchor["span_end"])
                if start is None or end is None:
                    raise ValueError("APPROVED_FEW_SHOT_INVALID: anchor is not a complete source segment range")
                anchor.clear()
                if anchor_mode == SOURCE_SPANS_MODE:
                    anchor.update(source_unit_id=source["source_unit_id"], slice_id=catalog["slice_id"])
                anchor.update(start_segment_id=start, end_segment_id=end)
        resolved = adapter.resolve_response(example["response"], **source)
        if resolved != examples[number]["response"]:
            raise ValueError("APPROVED_FEW_SHOT_INVALID: source segment projection changed response")
    return rendered


@dataclass(frozen=True)
class SourceSpanAdapter:
    version: str
    rule: str
    response_schema: Callable
    catalog: Callable
    resolve_response: Callable


def source_span_adapter(mode):
    if mode == SOURCE_SPANS_MODE:
        return SourceSpanAdapter(
            SOURCE_SPANS_ADAPTER_VERSION, SOURCE_SPANS_RULE, source_span_response_schema,
            source_segments, resolve_source_span_response,
        )
    if mode == SOURCE_SPANS_V2_MODE:
        return SourceSpanAdapter(
            SOURCE_SPANS_V2_ADAPTER_VERSION, SOURCE_SPANS_V2_RULE, source_span_v2_response_schema,
            source_segments_v2, resolve_source_span_v2_response,
        )
    raise ValueError("APPROVED_REEXTRACTION_ANCHOR_MODE_INVALID")
