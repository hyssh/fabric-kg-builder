import json

import pytest

from fabric_kg_builder.serving.property_value_coalesce import coalesce_value_jsons, grounded_label


def _j(value):
    return json.dumps(value, ensure_ascii=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["REMOVAL"], "REMOVAL"),
        (["REMOVAL", "Removal"], "REMOVAL"),
        (["Removal", "REMOVAL", "Removal"], "Removal"),
        (["Anti-static wrist strap", "anti-static  wrist strap"], "Anti-static wrist strap"),
        (["3IP", "3IP (Torx-Plus)"], "3IP (Torx-Plus)"),
        (["3IP", "3ip [Torx-Plus]", "3IP"], "3ip [Torx-Plus]"),
    ],
)
def test_coalesces_surface_variants(values, expected):
    jsons = [_j(v) for v in values]
    assert coalesce_value_jsons(jsons) == _j(expected)
    assert coalesce_value_jsons(reversed(jsons)) == _j(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    "values",
    [
        ["3IP", "T5"],
        ["3IP", "3IP Torx"],
        ["3IP", "3IP (Torx)", "3IP (Phillips)"],
        ["Screw", "Screwdriver"],
        [1, 2],
        ["1", 1],
        [True, "true"],
    ],
)
def test_true_conflicts_fail_closed(values):
    assert coalesce_value_jsons([_j(v) for v in values]) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        ({"label": "  Kick\tstand ", "label_evidence_span_id": "s1", "evidence_span_ids": ["s1"]}, "Kick stand"),
        ({"label": "Kickstand", "label_evidence_span_id": "s2", "evidence_span_ids": ["s1"]}, None),
        ({"label": "Kickstand", "label_evidence_span_id": None, "evidence_span_ids": ["s1"]}, None),
        ({"label": "   ", "label_evidence_span_id": "s1", "evidence_span_ids": ["s1"]}, None),
        ({"label": None, "label_evidence_span_id": "s1", "evidence_span_ids": ["s1"]}, None),
        ({"label": "Kickstand", "label_evidence_span_id": "s1", "evidence_span_ids": None}, None),
    ],
)
def test_grounded_label_requires_evidence_bound_nonblank_label(entity, expected):
    assert grounded_label(entity) == expected
