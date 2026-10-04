"""Deterministic coalescing of surface variants of one asserted scalar.

L4 keeps every evidence-bound property assertion. When several merged
candidates assert the same single-valued property on one entity, the asserted
strings can differ only in surface form (``"REMOVAL"`` vs ``"Removal"``) or by
a trailing parenthetical qualifier (``"3IP"`` vs ``"3IP (Torx-Plus)"``). Such
variants are coalesced to one representative; any other disagreement remains a
fail-closed conflict.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable

COALESCE_RULE_VERSION = "surface-variant-coalesce/1.0.0"

_QUALIFIER = re.compile(r"^\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$")


def _surface(value_json: str) -> str | None:
    try:
        value = json.loads(value_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, str):
        return None
    return " ".join(value.split()).casefold()


def _qualifies(short: str, long: str) -> bool:
    return long.startswith(short) and bool(_QUALIFIER.match(long[len(short):]))


def coalesce_value_jsons(value_jsons: Iterable[str]) -> str | None:
    """Return the representative normalized JSON, or ``None`` on true conflict.

    Representative: among values with the most specific surface form, the most
    frequently asserted one; ties break on the lexicographically smallest JSON.
    """

    counts = Counter(value_jsons)
    if len(counts) == 1:
        return next(iter(counts))
    surfaces = {value: _surface(value) for value in counts}
    if any(surface is None for surface in surfaces.values()):
        return None
    distinct = set(surfaces.values())
    if len(distinct) == 1:
        top = distinct.pop()
    else:
        top = max(distinct, key=len)
        if any(s != top and not _qualifies(s, top) for s in distinct):
            return None
    eligible = [value for value, surface in surfaces.items() if surface == top]
    return min(eligible, key=lambda value: (-counts[value], value))


LABEL_FALLBACK_RULE_VERSION = "required-string-label-fallback/1.0.0"


def grounded_label(entity: object) -> str | None:
    """Return the entity's evidence-grounded presentation label, if any.

    A label is grounded only when it is nonblank and its
    ``label_evidence_span_id`` is one of the entity's own evidence spans.
    """

    if not hasattr(entity, "get"):
        return None
    label = entity.get("label")  # type: ignore[union-attr]
    if not isinstance(label, str) or not label.strip():
        return None
    spans = entity.get("evidence_span_ids") or ()  # type: ignore[union-attr]
    if entity.get("label_evidence_span_id") not in list(spans):  # type: ignore[union-attr]
        return None
    return " ".join(label.split())
