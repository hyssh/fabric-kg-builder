"""Pure, lossless response-local reference qualification; never an identity waiver."""

from copy import deepcopy
import base64
import hashlib
import json
import unicodedata

VERSION = "response-slice-local-identifiers/1.1.0"
PROFILE_KEY = "local_identifier_qualification"
ARTIFACT_KEY = "local_identifier_projection"
CODE_PATH = "enrichment/approved_local_identifiers.py"
REFERENCE_FIELDS = {
    "entity": ("local_id",),
    "relationship": ("source_local_id", "target_local_id"),
    "property": ("owner_local_id",),
}


def profile():
    return {
        "version": VERSION,
        "qualify_local_identifiers": True,
        "scope": ["source_unit_id", "slice_start", "slice_end"],
        "reference_equivalence": "strip_nfc_casefold_nfc",
        "reference_encoding": "lq_colon_base32_sha256_scope_and_normalized_reference",
        "qualified_reference_length": 55,
        "reference_spelling": "case_only_digest_variant_preserves_payload_conflicts",
        "native_stable_identity": "preserve_entire_reference_equivalence_group",
        "non_reference_fields": "lossless_unchanged",
    }


def _fail(reason):
    raise ValueError(f"APPROVED_LOCAL_IDENTIFIERS_{reason}")


def _bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def lossless_hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def normalize_reference(value):
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value.strip()).casefold())


def _reference_digest(scope, key):
    digest = hashlib.sha256(_bytes([scope, key])).digest()
    return "lq:" + base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def _spelling_variant(reference, spelling):
    # All variants casefold to the same full 256-bit identity digest. Separate
    # spellings must not erase CANDIDATE_PAYLOAD_CONFLICT during materialization.
    bits = int.from_bytes(hashlib.sha256(_bytes(spelling)).digest(), "big")
    chars = []
    for character in reference[3:]:
        if character.isalpha():
            character = character.upper() if bits & 1 else character
            bits >>= 1
        chars.append(character)
    return reference[:3] + "".join(chars)


def qualify_response(response, *, source_unit_id, slice_start, slice_end):
    """Bijection on NFC/strip/casefold reference classes, not semantic entities.

    Explicit stable-source identity admission pins its original local reference.
    Preserve that entire class (including duplicates and endpoint/owner aliases)
    rather than rewriting a native identity or weakening the admission check.
    """
    if (
        not isinstance(source_unit_id, str) or not source_unit_id
        or type(slice_start) is not int or type(slice_end) is not int
        or slice_start < 0 or slice_end <= slice_start
    ):
        _fail("SCOPE_INVALID")
    if not isinstance(response, dict) or set(response) != {"candidates"}:
        _fail("RESPONSE_INVALID")
    candidates = response["candidates"]
    if not isinstance(candidates, list):
        _fail("RESPONSE_INVALID")
    visits, native = [], set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            _fail("RESPONSE_INVALID")
        kind = candidate.get("candidate_kind")
        fields = REFERENCE_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            _fail("RESPONSE_INVALID")
        for field in fields:
            value = candidate.get(field)
            if not isinstance(value, str) or not value.strip():
                _fail("REFERENCE_INVALID")
            key = normalize_reference(value)
            visits.append((index, field, value, key))
            if field == "local_id" and candidate.get("stable_source_identity") is not None:
                native.add(key)
    scope = {
        "source_unit_id": source_unit_id, "slice_start": slice_start, "slice_end": slice_end,
    }
    namespace = lossless_hash([source_unit_id, slice_start, slice_end])
    mapping = {
        key: key if key in native else _reference_digest(scope, key)
        for key in sorted({visit[3] for visit in visits})
    }
    if len(set(mapping.values())) != len(mapping):
        _fail("NAMESPACE_COLLISION")
    projected = deepcopy(response)
    changes = []
    spellings = {}
    for index, field, before, key in visits:
        spelling = unicodedata.normalize("NFC", before.strip())
        after = before if key in native else _spelling_variant(mapping[key], spelling)
        if key not in native:
            if after in spellings and spellings[after] != spelling:
                _fail("SPELLING_COLLISION")
            spellings[after] = spelling
        projected["candidates"][index][field] = after
        if before != after:
            changes.append({"candidate_index": index, "field": field, "before": before, "after": after})
    proof = {
        "version": VERSION, "scope": scope, "namespace": namespace,
        "mapping": [
            {"reference": key, "qualified_reference": value, "native_stable_identity": key in native}
            for key, value in mapping.items()
        ],
        "input_response_sha256": lossless_hash(response),
        "output_response_sha256": lossless_hash(projected),
        "changed_fields": changes,
        "assertions": {
            "only_reference_fields_changed": True,
            "candidate_count_unchanged": True,
            "reference_equivalence_classes_bijective": True,
            "native_stable_identity_groups_unchanged": True,
        },
    }
    return projected, proof


def verify_projection(response, artifact, **scope):
    """Recompute everything; neither retained mappings nor their hashes are authority."""
    expected, proof = qualify_response(response, **scope)
    if lossless_hash(artifact) != lossless_hash({"response": expected, "proof": proof}):
        _fail("PROJECTION_DRIFT")
    return expected


def verify_namespace_collisions(proofs):
    """Also reject cross-response digest collisions, without changing native IDs."""
    seen = {}
    native, qualified = set(), set()
    for proof in proofs:
        scope = tuple(proof["scope"][key] for key in ("source_unit_id", "slice_start", "slice_end"))
        for row in proof["mapping"]:
            reference = row["qualified_reference"]
            token = (scope[0], reference)
            if row["native_stable_identity"]:
                if token in qualified:
                    _fail("NAMESPACE_COLLISION")
                native.add(token)
                continue
            if token in native:
                _fail("NAMESPACE_COLLISION")
            qualified.add(token)
            binding = (*scope, row["reference"])
            if reference in seen and seen[reference] != binding:
                _fail("NAMESPACE_COLLISION")
            seen[reference] = binding
