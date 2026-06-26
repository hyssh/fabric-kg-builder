"""Unit tests for enrichment.densify (source-document DeviceModel hub linking)."""

from __future__ import annotations

from fabric_kg_builder.enrichment.densify import (
    densify_document,
    is_diagnostic_procedure,
    is_specific_device_model,
    is_umbrella_procedure,
    link_procedure_steps,
    link_rca_paths,
    link_symptom_cause_resolution,
    link_umbrella_steps,
)


def test_is_specific_device_model():
    assert is_specific_device_model("Surface Laptop 5")
    assert is_specific_device_model("Microsoft Surface Pro 10th Edition for Business")
    assert is_specific_device_model("Surface Studio 2")
    # Generic / placeholder names rejected.
    assert not is_specific_device_model("model")
    assert not is_specific_device_model("this device model")
    assert not is_specific_device_model("device")
    assert not is_specific_device_model(None)
    assert not is_specific_device_model("")
    # Product keyword but no digit/edition → not specific enough.
    assert not is_specific_device_model("Surface devices")


def _doc():
    return {
        "entities": [
            {"entity_id": "entity:m1", "entity_type": "DeviceModel",
             "display_name": "Surface Laptop 5"},
            {"entity_id": "entity:gen", "entity_type": "DeviceModel",
             "display_name": "model"},
            {"entity_id": "entity:c1", "entity_type": "Component",
             "display_name": "Battery"},
            {"entity_id": "entity:p1", "entity_type": "Part",
             "display_name": "Battery Pack"},
            {"entity_id": "entity:pr1", "entity_type": "Procedure",
             "display_name": "Replace Battery"},
            {"entity_id": "entity:s1", "entity_type": "Symptom",
             "display_name": "Battery expansion"},
            {"entity_id": "entity:st1", "entity_type": "Step",
             "display_name": "Unscrew"},  # Step is NOT a hub target
        ],
        "relationships": [
            # An existing model→component edge must not be duplicated.
            {"relationship_id": "rel:existing", "relationship_type": "has_component",
             "source_entity_id": "entity:m1", "target_entity_id": "entity:c1"},
        ],
    }


def test_densify_adds_hub_edges():
    doc, added = densify_document(_doc())
    # New edges: m1→p1 (Part), m1→pr1 (Procedure), m1→s1 (Symptom).
    # m1→c1 already exists (skipped); Step is not a hub target.
    assert added == 3
    new = [r for r in doc["relationships"] if r["relationship_id"] != "rel:existing"]
    pairs = {(r["source_entity_id"], r["target_entity_id"], r["relationship_type"]) for r in new}
    assert ("entity:m1", "entity:p1", "has_part") in pairs
    assert ("entity:m1", "entity:pr1", "has_procedure") in pairs
    assert ("entity:m1", "entity:s1", "has_symptom") in pairs
    # Generic model "model" is never used as a hub.
    assert all(r["source_entity_id"] != "entity:gen" for r in new)
    # Step never linked.
    assert all(r["target_entity_id"] != "entity:st1" for r in new)


def test_densify_is_idempotent():
    doc, added1 = densify_document(_doc())
    doc, added2 = densify_document(doc)
    assert added1 == 3
    assert added2 == 0  # second pass adds nothing


def test_densify_no_models_is_noop():
    doc = {
        "entities": [
            {"entity_id": "entity:c1", "entity_type": "Component", "display_name": "Battery"},
        ],
        "relationships": [],
    }
    doc, added = densify_document(doc)
    assert added == 0
    assert doc["relationships"] == []


def test_densify_synthetic_edges_well_formed():
    doc, _ = densify_document(_doc())
    syn = [r for r in doc["relationships"] if r["relationship_id"] != "rel:existing"]
    for r in syn:
        assert r["relationship_id"].startswith("rel:")
        assert r["confidence"] == 0.5
        assert r["is_placeholder"] is False
        assert "densify" in r["properties_json"]


# ---------------------------------------------------------------------------
# Symptom ↔ Cause ↔ Resolution linker
# ---------------------------------------------------------------------------


def _scr_doc():
    """Doc where 'thermal' and 'leaking' are discriminating, 'battery' is ubiquitous.

    Filler entities enlarge N so the ubiquity gate (max_df = 0.4*N) admits the
    df=3 cluster tokens while still excluding the ubiquitous 'battery' token.
    """
    fillers = [
        {"entity_id": f"entity:f{i}", "entity_type": "Symptom",
         "display_name": f"unrelated keyboard glitch number {i} alpha bravo"}
        for i in range(8)
    ]
    return {
        "entities": [
            # Causes
            {"entity_id": "entity:c_thermal", "entity_type": "Cause",
             "display_name": "battery thermal runaway"},
            {"entity_id": "entity:c_leak", "entity_type": "Cause",
             "display_name": "battery leaking electrolyte"},
            # Symptoms
            {"entity_id": "entity:s_thermal", "entity_type": "Symptom",
             "display_name": "battery thermal event"},
            {"entity_id": "entity:s_leak", "entity_type": "Symptom",
             "display_name": "battery leaking fluid"},
            # Resolutions
            {"entity_id": "entity:r_thermal", "entity_type": "Resolution",
             "display_name": "isolate battery thermal hazard and contact support"},
            {"entity_id": "entity:r_leak", "entity_type": "Resolution",
             "display_name": "contain battery leaking material safely"},
            *fillers,
        ],
        "relationships": [],
    }


def test_scr_links_by_discriminating_keyword():
    doc, added = link_symptom_cause_resolution(_scr_doc())
    pairs = {
        (r["source_entity_id"], r["target_entity_id"], r["relationship_type"])
        for r in doc["relationships"]
    }
    # thermal cluster
    assert ("entity:c_thermal", "entity:s_thermal", "causes") in pairs
    assert ("entity:s_thermal", "entity:r_thermal", "resolved_by") in pairs
    assert ("entity:c_thermal", "entity:r_thermal", "addressed_by") in pairs
    # leaking cluster
    assert ("entity:c_leak", "entity:s_leak", "causes") in pairs
    assert ("entity:s_leak", "entity:r_leak", "resolved_by") in pairs
    assert added > 0


def test_scr_does_not_cross_link_unrelated_clusters():
    doc, _ = link_symptom_cause_resolution(_scr_doc())
    pairs = {(r["source_entity_id"], r["target_entity_id"]) for r in doc["relationships"]}
    # "battery" is ubiquitous (in all 6) so it must NOT bridge thermal↔leak.
    assert ("entity:c_thermal", "entity:s_leak") not in pairs
    assert ("entity:c_leak", "entity:s_thermal") not in pairs


def test_scr_inferred_edges_tagged_and_low_confidence():
    doc, _ = link_symptom_cause_resolution(_scr_doc())
    for r in doc["relationships"]:
        assert r["confidence"] == 0.45
        assert "densify:scr" in r["properties_json"]
        assert r["content_hash"]


def test_scr_idempotent():
    doc, a1 = link_symptom_cause_resolution(_scr_doc())
    doc, a2 = link_symptom_cause_resolution(doc)
    assert a1 > 0
    assert a2 == 0


def test_scr_noop_without_symptoms():
    doc = {
        "entities": [
            {"entity_id": "entity:c1", "entity_type": "Cause", "display_name": "x cause"},
        ],
        "relationships": [],
    }
    doc, added = link_symptom_cause_resolution(doc)
    assert added == 0


# ---------------------------------------------------------------------------
# Procedure → Step linker (reading order)
# ---------------------------------------------------------------------------


def _procstep_doc():
    """Two procedures, each followed by its own steps, in document order."""
    return {
        "entities": [
            {"entity_id": "entity:p_batt", "entity_type": "Procedure",
             "display_name": "Battery Replacement"},
            {"entity_id": "entity:s_b1", "entity_type": "Step", "display_name": "Power off device"},
            {"entity_id": "entity:s_b2", "entity_type": "Step", "display_name": "Disconnect battery FPC"},
            {"entity_id": "entity:p_disp", "entity_type": "Procedure",
             "display_name": "Display Replacement"},
            {"entity_id": "entity:s_d1", "entity_type": "Step", "display_name": "Heat the display"},
        ],
        "relationships": [],
        "document_elements": [
            {"page_number": 1, "sort_order": 0, "content": "Battery Replacement procedure"},
            {"page_number": 1, "sort_order": 1, "content": "Power off device and unplug"},
            {"page_number": 1, "sort_order": 2, "content": "Disconnect battery FPC carefully"},
            {"page_number": 2, "sort_order": 0, "content": "Display Replacement procedure"},
            {"page_number": 2, "sort_order": 1, "content": "Heat the display to soften adhesive"},
        ],
    }


def test_procstep_links_steps_to_preceding_procedure():
    doc, added = link_procedure_steps(_procstep_doc())
    pairs = {(r["source_entity_id"], r["target_entity_id"]) for r in doc["relationships"]
             if r["relationship_type"] == "has_step"}
    assert ("entity:p_batt", "entity:s_b1") in pairs
    assert ("entity:p_batt", "entity:s_b2") in pairs
    assert ("entity:p_disp", "entity:s_d1") in pairs
    # Display steps must NOT attach to the battery procedure.
    assert ("entity:p_batt", "entity:s_d1") not in pairs
    assert added == 3


def test_procstep_edges_tagged_and_idempotent():
    doc, a1 = link_procedure_steps(_procstep_doc())
    for r in doc["relationships"]:
        assert r["confidence"] == 0.5
        assert "proc-step" in r["properties_json"]
        assert r["content_hash"]
    doc, a2 = link_procedure_steps(doc)
    assert a1 == 3
    assert a2 == 0


def test_procstep_noop_without_elements():
    doc = {
        "entities": [
            {"entity_id": "entity:p1", "entity_type": "Procedure", "display_name": "X"},
            {"entity_id": "entity:s1", "entity_type": "Step", "display_name": "Y"},
        ],
        "relationships": [],
        "document_elements": [],
    }
    doc, added = link_procedure_steps(doc)
    assert added == 0

def _rca_doc():
    fillers = [
        {"entity_id": f"entity:fp{i}", "entity_type": "Procedure",
         "display_name": f"unrelated cover assembly task number {i} alpha bravo"}
        for i in range(8)
    ]
    return {
        "entities": [
            {"entity_id": "entity:sym", "entity_type": "Symptom",
             "display_name": "battery thermal swelling expansion"},
            {"entity_id": "entity:diag", "entity_type": "Procedure",
             "display_name": "Run SDT thermal battery status check"},
            {"entity_id": "entity:fix", "entity_type": "Procedure",
             "display_name": "Battery thermal expansion replacement"},
            *fillers,
        ],
        "relationships": [],
    }


def test_is_diagnostic_procedure():
    assert is_diagnostic_procedure("Run the Surface Diagnostic Toolkit (SDT)")
    assert is_diagnostic_procedure("Battery Status Check")
    assert is_diagnostic_procedure("Pre-installation Device Inspection")
    assert not is_diagnostic_procedure("Battery Replacement")
    assert not is_diagnostic_procedure("Remove the Kickstand")
    assert not is_diagnostic_procedure(None)


def test_rca_links_diagnostic_and_remediation():
    doc, added = link_rca_paths(_rca_doc())
    edges = {(r["source_entity_id"], r["target_entity_id"], r["relationship_type"])
             for r in doc["relationships"]}
    assert ("entity:sym", "entity:diag", "diagnosed_by") in edges
    assert ("entity:sym", "entity:fix", "remediated_by") in edges
    assert added >= 2


def test_rca_edges_tagged_and_idempotent():
    doc, a1 = link_rca_paths(_rca_doc())
    for r in doc["relationships"]:
        assert r["confidence"] == 0.4
        assert "rca-" in r["properties_json"]
        assert r["content_hash"]
    doc, a2 = link_rca_paths(doc)
    assert a1 >= 2
    assert a2 == 0


def test_rca_noop_without_procedures():
    doc = {
        "entities": [
            {"entity_id": "entity:s", "entity_type": "Symptom", "display_name": "x"},
        ],
        "relationships": [],
    }
    doc, added = link_rca_paths(doc)
    assert added == 0

def _umbrella_doc():
    return {
        "entities": [
            {"entity_id": "entity:u", "entity_type": "Procedure",
             "display_name": "Battery Replacement Process"},
            {"entity_id": "entity:f1", "entity_type": "Procedure",
             "display_name": "Remove the Battery"},
            {"entity_id": "entity:f2", "entity_type": "Procedure",
             "display_name": "Install the Battery"},
            {"entity_id": "entity:other", "entity_type": "Procedure",
             "display_name": "Remove the Display"},
            {"entity_id": "entity:s1", "entity_type": "Step", "display_name": "Unscrew"},
            {"entity_id": "entity:s2", "entity_type": "Step", "display_name": "Lift cell"},
            {"entity_id": "entity:s3", "entity_type": "Step", "display_name": "Seat cell"},
            {"entity_id": "entity:sd", "entity_type": "Step", "display_name": "Heat panel"},
        ],
        "relationships": [
            {"relationship_id": "rel:a", "relationship_type": "has_step",
             "source_entity_id": "entity:f1", "target_entity_id": "entity:s1"},
            {"relationship_id": "rel:b", "relationship_type": "has_step",
             "source_entity_id": "entity:f1", "target_entity_id": "entity:s2"},
            {"relationship_id": "rel:c", "relationship_type": "has_step",
             "source_entity_id": "entity:f2", "target_entity_id": "entity:s3"},
            {"relationship_id": "rel:d", "relationship_type": "has_step",
             "source_entity_id": "entity:other", "target_entity_id": "entity:sd"},
        ],
    }


def test_is_umbrella_procedure():
    assert is_umbrella_procedure("Battery Replacement Process")
    assert is_umbrella_procedure("Display Module Replacement Process")
    assert is_umbrella_procedure("Kickstand Replacement")
    assert not is_umbrella_procedure("Remove the Battery")
    assert not is_umbrella_procedure(None)


def test_umbrella_rolls_up_fragment_steps():
    doc, added = link_umbrella_steps(_umbrella_doc())
    pairs = {(r["source_entity_id"], r["target_entity_id"]) for r in doc["relationships"]
             if r["relationship_type"] == "has_step"}
    # Umbrella now reaches battery fragment steps s1,s2,s3
    assert ("entity:u", "entity:s1") in pairs
    assert ("entity:u", "entity:s2") in pairs
    assert ("entity:u", "entity:s3") in pairs
    # Must NOT pull in the unrelated display step.
    assert ("entity:u", "entity:sd") not in pairs
    assert added == 3


def test_umbrella_idempotent():
    doc, a1 = link_umbrella_steps(_umbrella_doc())
    doc, a2 = link_umbrella_steps(doc)
    assert a1 == 3
    assert a2 == 0
