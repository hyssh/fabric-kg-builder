"""Explicit reviewed quote projections; never modify or impersonate provider output."""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import re

from fabric_kg_builder.contracts.base import canonical_sha256

from . import approved_reextraction as core
from .approved_quote_anchors import QUOTE_MODE, QuoteAnchorResolutionError, resolve_quote_response
from .schema2_extraction import parse_candidate_array

VERSION = "approved-quote-correction-review/1.0.0"
ANCESTRY_VERSION = "approved-quote-correction-review/2.0.0"
CODE_PATH = "enrichment/approved_quote_review.py"
# Exact core producer used by the retained l2-v2 run; no code-drift waiver.
KNOWN_PRODUCER = "be1e3eca73f49c95043f55d19524b3edaf3795dcd5847aca02331dfcad0469ed"
REVIEW_FIELDS = {
    "version", "actor", "rationale", "reviewed_at", "donor_fingerprint",
    "donor_authority_sha256", "donor_integrity_sha256",
    "producer_code_identity_sha256", "corrections",
}
ANCESTRY_FIELDS = REVIEW_FIELDS | {"donor_code_identity_sha256"}
# Exact reviewed-child producer, inspected before modifying either helper.
LEGACY_HELPERS = {
    "enrichment/approved_donor_continuation.py": "abb4b606a69dde9ad5b194253d666a23c5c35496386b68936bb1cf4730f7cbcf",
    CODE_PATH: "14d0f0c8a1d2b9db2152a67435c14ea820cde2cac9770c5b48d4fea1c6274484",
}
CORRECTION_FIELDS = {
    "request_hash", "response_hash", "source_unit_id", "source_text_hash",
    "slice_start", "slice_end", "candidate_path", "old_quote_sha256", "new_quote",
}
PATH = re.compile(r"candidates\[(0|[1-9][0-9]*)\]\.(anchor|anchors\[(0|[1-9][0-9]*)\])")
HASH = re.compile(r"[0-9a-f]{64}")


def _fail(reason):
    raise ValueError(f"APPROVED_QUOTE_REVIEW_{reason}")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(value):
    _fail("NON_JSON_CONSTANT")


def _binding(unit):
    return {
        "source_unit_id": unit.source_unit_id, "source_text_hash": unit.source_text_hash,
        "slice_start": unit.slice_start, "slice_end": unit.slice_end,
    }


def verify_helper_identity(identity, *, current_helpers, review_version, has_chain):
    producer = core._code_identity()
    if canonical_sha256(producer) != KNOWN_PRODUCER:
        _fail("PRODUCER_CODE_DRIFT")
    if identity == {**producer, **LEGACY_HELPERS} and review_version == VERSION and not has_chain:
        return "legacy"
    if identity == {**producer, **current_helpers} and has_chain:
        return "current"
    _fail("HELPER_CODE_DRIFT")


class QuoteReview:
    def __init__(self, path):
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            _fail("UNSAFE_FILE")
        self._load(path.read_bytes())

    def _load(self, original_bytes):
        self.original_bytes = original_bytes
        self.document = json.loads(
            self.original_bytes.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=_constant,
        )
        doc = self.document
        if not isinstance(doc, dict) or doc.get("version") not in (VERSION, ANCESTRY_VERSION):
            _fail("SCHEMA_INVALID")
        self.version = doc["version"]
        fields = ANCESTRY_FIELDS if self.version == ANCESTRY_VERSION else REVIEW_FIELDS
        if set(doc) != fields:
            _fail("SCHEMA_INVALID")
        for field in fields - {"corrections"}:
            if not isinstance(doc[field], str) or not doc[field].strip():
                _fail("SCHEMA_INVALID")
        for field in ("donor_fingerprint", "donor_authority_sha256", "donor_integrity_sha256",
                      "producer_code_identity_sha256", *(
                          ("donor_code_identity_sha256",) if self.version == ANCESTRY_VERSION else ()
                      )):
            if not HASH.fullmatch(doc[field]):
                _fail("SCHEMA_INVALID")
        try:
            stamp = datetime.fromisoformat(doc["reviewed_at"].replace("Z", "+00:00"))
        except ValueError:
            _fail("REVIEWED_AT_INVALID")
        if stamp.tzinfo is None:
            _fail("REVIEWED_AT_INVALID")
        if doc["producer_code_identity_sha256"] != KNOWN_PRODUCER:
            _fail("PRODUCER_NOT_APPROVED")
        corrections = doc["corrections"]
        if not isinstance(corrections, list) or not corrections:
            _fail("SCHEMA_INVALID")
        self.by_request = {}
        paths = set()
        for correction in corrections:
            if not isinstance(correction, dict) or set(correction) != CORRECTION_FIELDS:
                _fail("SCHEMA_INVALID")
            for field in CORRECTION_FIELDS - {"slice_start", "slice_end"}:
                if not isinstance(correction[field], str) or not correction[field]:
                    _fail("SCHEMA_INVALID")
            for field in ("request_hash", "response_hash", "source_text_hash", "old_quote_sha256"):
                if not HASH.fullmatch(correction[field]):
                    _fail("SCHEMA_INVALID")
            if (
                any(type(correction[k]) is not int for k in ("slice_start", "slice_end"))
                or not 0 <= correction["slice_start"] < correction["slice_end"]
                or not PATH.fullmatch(correction["candidate_path"])
            ):
                _fail("SCHEMA_INVALID")
            key = (correction["request_hash"], correction["candidate_path"])
            if key in paths:
                _fail("DUPLICATE_CORRECTION")
            paths.add(key)
            self.by_request.setdefault(correction["request_hash"], []).append(correction)
        self.projections = {}

    @classmethod
    def from_record(cls, record):
        try:
            original = base64.b64decode(record["review_file_utf8_base64"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ValueError("APPROVED_QUOTE_REVIEW_RETAINED_RECORD_INVALID") from exc
        instance = cls.__new__(cls)
        try:
            instance._load(original)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("APPROVED_QUOTE_REVIEW_RETAINED_RECORD_INVALID") from exc
        return instance

    def verify_donor(self, authority, *, authority_sha256, integrity_sha256):
        if self.version == VERSION and ("continuation" in authority or "quote_review" in authority):
            _fail("DIRECT_ORIGINAL_DONOR_REQUIRED")
        if authority.get("anchor_mode") != QUOTE_MODE:
            _fail("QUOTE_MODE_REQUIRED")
        producer = {k: v for k, v in authority["code_identity"].items() if k not in LEGACY_HELPERS}
        if canonical_sha256(producer) != KNOWN_PRODUCER or producer != core._code_identity():
            _fail("PRODUCER_CODE_DRIFT")
        expected = {
            "donor_fingerprint": authority["fingerprint"],
            "donor_authority_sha256": authority_sha256,
            "donor_integrity_sha256": integrity_sha256,
        }
        if any(self.document[key] != value for key, value in expected.items()):
            _fail("DONOR_BINDING_DRIFT")
        if self.version == ANCESTRY_VERSION and (
            self.document["donor_code_identity_sha256"] != canonical_sha256(authority["code_identity"])
        ):
            _fail("DONOR_CODE_BINDING_DRIFT")

    def verify_response(self, paid, unit, provider_output):
        corrections = self.by_request.get(paid.request_hash)
        if corrections is None:
            return
        if paid.request_hash in self.projections:
            _fail("DUPLICATE_REQUEST")
        if paid.review_projection is not None:
            _fail("PREVIOUSLY_REVIEWED_REQUEST")
        binding = _binding(unit)
        try:
            resolve_quote_response(
                paid.response, source_text=unit.text,
                slice_start=unit.slice_start, slice_end=unit.slice_end,
            )
        except QuoteAnchorResolutionError as exc:
            unresolved = {item["path"] for item in exc.diagnostics}
        else:
            _fail("ORIGINAL_ALREADY_RESOLVABLE")
        projected = deepcopy(paid.response)
        for correction in corrections:
            if (
                correction["response_hash"] != paid.response_hash
                or any(correction[key] != value for key, value in binding.items())
                or "$." + correction["candidate_path"] not in unresolved
            ):
                _fail("RESPONSE_OR_SOURCE_BINDING_DRIFT")
            match = PATH.fullmatch(correction["candidate_path"])
            try:
                candidate = projected["candidates"][int(match[1])]
                anchor = candidate["anchor"] if match[2] == "anchor" else candidate["anchors"][int(match[3])]
            except (IndexError, KeyError, TypeError):
                _fail("CANDIDATE_PATH_INVALID")
            if (
                not isinstance(anchor, dict) or set(anchor) != {"quote"}
                or not isinstance(anchor["quote"], str)
                or _sha(anchor["quote"].encode("utf-8")) != correction["old_quote_sha256"]
            ):
                _fail("OLD_QUOTE_DRIFT")
            anchor["quote"] = correction["new_quote"]
        # Resolve the WHOLE leaf. A review cannot hide a second invalid anchor.
        resolved = resolve_quote_response(
            projected, source_text=unit.text, slice_start=unit.slice_start, slice_end=unit.slice_end,
        )
        parse_candidate_array(resolved["candidates"])
        if provider_output is None:
            _fail("ORIGINAL_PROVIDER_OUTPUT_MISSING")
        self.projections[paid.request_hash] = {
            **binding, "original_response_hash": paid.response_hash,
            "imported_from": paid.provenance(), "corrections": corrections,
            "reviewed_response": projected,
            "reviewed_response_hash": core.response_hash(projected, QUOTE_MODE),
            "resolved_response": resolved,
            "resolved_response_hash": core.response_hash(resolved, QUOTE_MODE),
            "original_provider_output": provider_output,
        }

    def finish(self):
        if set(self.projections) != set(self.by_request):
            _fail("REQUEST_NOT_FOUND")

    def authority(self):
        return {
            "version": self.version, "review_file_sha256": _sha(self.original_bytes),
            "review_document_sha256": core.response_hash(self.document, QUOTE_MODE),
            "producer_code_identity_sha256": KNOWN_PRODUCER,
            "actor": self.document["actor"], "rationale": self.document["rationale"],
            "reviewed_at": self.document["reviewed_at"],
            "corrected_requests": len(self.projections),
            "correction_count": len(self.document["corrections"]),
        }

    def record(self):
        return {
            **self.authority(), "review": self.document,
            "review_file_utf8_base64": base64.b64encode(self.original_bytes).decode("ascii"),
        }

    def persist(self, state):
        core.persist_response(state / "approved-quote-correction-review.json", self.record(), QUOTE_MODE)

    def project(self, paid):
        projection = self.projections.get(paid.request_hash)
        return replace(paid, review_projection=projection, review_record=self.record()) if projection else paid

def review_chain(responses):
    records = {}
    for paid in responses.values():
        if paid.review_record is not None:
            record = paid.review_record
            key = record["review_file_sha256"]
            if key in records and records[key] != record:
                _fail("REVIEW_CHAIN_DRIFT")
            records[key] = record
    return {"version": ANCESTRY_VERSION, "reviews": [records[key] for key in sorted(records)]}


def chain_authority(responses):
    chain = review_chain(responses)
    return {
        "version": ANCESTRY_VERSION, "review_count": len(chain["reviews"]),
        "chain_sha256": core.response_hash(chain, QUOTE_MODE),
    }


def reviewed_artifact(paid, unit, request_hash):
    projection = paid.review_projection
    if (
        projection is None or paid.review_record is None
        or projection["original_response_hash"] != paid.response_hash
        or any(projection[key] != value for key, value in _binding(unit).items())
    ):
        _fail("PROJECTION_DRIFT")
    record = paid.review_record
    summary = {k: v for k, v in record.items() if k not in {"review", "review_file_utf8_base64"}}
    return {
        **projection, "version": summary["version"], "review": summary,
        "request_hash": request_hash, "work_unit_id": unit.work_unit_id,
        "status": "reviewed_quote_projection_pending_canonical_validation",
    }


def adapt_inherited(raw, *, paid, work_unit, state, request_hash):
    if core.response_hash(raw, QUOTE_MODE) != paid.response_hash:
        _fail("PROJECTION_DRIFT")
    artifact = reviewed_artifact(paid, work_unit, request_hash)
    core.persist_response(
        state / "reextraction-reviewed-responses" / f"{request_hash}.json", artifact, QUOTE_MODE,
    )
    return deepcopy(artifact["resolved_response"])
