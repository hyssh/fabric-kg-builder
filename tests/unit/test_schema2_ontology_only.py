"""Schema-2 publications are ontology-only: legacy standalone-Graph flows fail closed."""

import json
import uuid

import pytest

from fabric_kg_builder.deploy import schema2_companion_verification as v
from fabric_kg_builder.deploy import schema2_graph_presentation as gp
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.deploy import schema2_prototype_reconcile as r

PLAN = {"names": {"lakehouse": "x_lakehouse", "ontology": "x_ontology"}, "description": "x"}


@pytest.fixture
def files(tmp_path):
    plan, journal = tmp_path / "plan.json", tmp_path / "journal.json"
    plan.write_text(json.dumps(PLAN))
    journal.write_text(json.dumps({"actions": {}}))
    for name in ("materialize", "l4", "l3"):
        (tmp_path / name).mkdir()
    return tmp_path, plan, journal


def test_structural_publication_names_the_ontology_companion():
    assert "ontology-companion" in p.STRUCTURAL_PUBLICATION
    assert "independent-graph" not in p.STRUCTURAL_PUBLICATION


def test_reconcile_rejects_graph_create_without_standalone_graph(files):
    root, plan, journal = files
    with pytest.raises(r.Error, match="no standalone graph"):
        r.reconcile_prototype_create(
            plan_path=plan, journal_path=journal, materialize=root / "materialize",
            l4_run=root / "l4", l3_root=root / "l3", kind="graph", item_id=str(uuid.uuid4()),
            review_path=root / "review.json", returned_id_runtime_repair=True,
        )


def test_companion_verification_rejects_ontology_only_publication(files):
    root, plan, journal = files
    with pytest.raises(p.PrototypePublicationError, match="legacy publications"):
        v._prepare(
            plan, journal, root / "materialize", root / "l4", root / "l3",
            str(uuid.uuid4()), root / "companion.json",
        )


def test_graph_label_repair_rejects_ontology_only_publication(files):
    root, plan, journal = files
    with pytest.raises(gp.Error, match="ontology-only"):
        gp._local(
            graph_id=str(uuid.uuid4()), plan_path=plan, journal_path=journal,
            materialize=root / "materialize", l4_run=root / "l4", l3_root=root / "l3",
            workspace_id=str(uuid.uuid4()),
        )
