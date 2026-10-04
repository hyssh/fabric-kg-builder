"""One CLI-driven, offline, source-to-typed-publication prototype acceptance."""

import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from tests.unit.test_l1_stage import _candidates, _intake


def test_cli_approved_domain_to_nonnull_typed_publication(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    text = "The governed record R-1 has reading 7 and describes the governed subject S-1."
    (source / "record.html").write_text(f"<p>{text}</p>", encoding="utf-8")
    candidates = _candidates("prototype")
    for candidate, name in zip(candidates["semantic_type_candidates"], ("record", "subject")):
        entity = candidate["proposed_type"]
        property_id = f"property:prototype.{name}-id"
        entity["identity_key_policy"]["key_mode"] = "business_key"
        entity["identity_key_policy"]["business_key_fields"] = [property_id]
        entity["declared_properties"] = [{
            "property_id": property_id, "display_name": f"{name.title()} ID",
            "value_type": "string", "required": True,
        }]
    candidates["semantic_type_candidates"][0]["proposed_type"]["declared_properties"].append({
        "property_id": "property:prototype.reading",
        "display_name": "Reading", "value_type": "integer", "required": False,
    })
    intake_file, candidates_file = tmp_path / "intake.json", tmp_path / "candidates.json"
    intake_file.write_text(json.dumps(_intake("prototype")), encoding="utf-8")
    candidates_file.write_text(json.dumps(candidates), encoding="utf-8")
    l1, l2, l3, l4 = [tmp_path / f"run-{n}" for n in range(1, 5)]
    domain = l1 / "domain.yaml"
    runner = CliRunner()

    def invoke(args, **kwargs):
        result = runner.invoke(cli, args, **kwargs)
        assert result.exit_code == 0, result.output
        return result

    invoke([
        "init-domain", "--input", str(source), "--intake", str(intake_file),
        "--candidates", str(candidates_file), "--non-interactive",
        "--project-id", "project:prototype", "--state-dir", str(l1), "--out", str(domain),
    ])
    proposal = json.loads((l1 / "domain-proposal.json").read_text())
    design = json.loads((l1 / "domain-design-context.json").read_text())
    invoke([
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1),
        "--approved-by", "fixture-reviewer", "--project-id", "project:prototype",
        "--run-id", design["identity"]["run_id"],
        "--proposal-hash", proposal["proposal_hash"],
    ])

    class FixtureModel:
        _config = SimpleNamespace(chat_deployment="offline-fixture")
        calls = 0

        def complete_json(self, **kwargs):
            self.calls += 1
            payload = json.loads(kwargs["user"])
            quote = payload["source_text"]
            start = payload["source_identity"]["slice_start"]
            anchor = {
                "span_start": start, "span_end": start + len(quote),
                "quote": quote, "model_authored_evidence_id": None,
            }
            rows = []
            for owner, key in (("record", "R-1"), ("subject", "S-1")):
                property_id = f"property:prototype.{owner}-id"
                rows.append({
                    "candidate_kind": "entity", "local_id": owner,
                    "observed_type": f"semantic-type:prototype.{owner}",
                    "label": key, "identity_key": {property_id: key},
                    "anchors": [anchor],
                })
                rows.append({
                    "candidate_kind": "property", "owner_local_id": owner,
                    "observed_property": property_id, "value": key,
                    "normalized_value": key, "anchor": anchor,
                })
            rows.extend([
                {
                    "candidate_kind": "property", "owner_local_id": "record",
                    "observed_property": "property:prototype.reading",
                    "value": 7, "normalized_value": 7, "anchor": anchor,
                },
                {
                    "candidate_kind": "relationship", "source_local_id": "record",
                    "target_local_id": "subject", "observed_predicate": "describes",
                    "direction": "source_to_target", "anchor": anchor,
                },
            ])
            return {"candidates": rows}

    model = FixtureModel()
    invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2), "--max-concurrent", "1",
    ], obj={"_foundry_client": model})
    assert model.calls == 1
    invoke([
        "validate-evidence", "--l1-state", str(l1), "--l2-state", str(l2),
        "--state", str(l3), "--domain", str(domain),
    ])
    projected = invoke([
        "project-serving", "--l1-state", str(l1), "--l2-state", str(l2),
        "--l3-state", str(l3), "--state", str(l4), "--domain", str(domain),
    ])
    l4_run = Path(next(
        line.split("run root: ", 1)[1]
        for line in projected.output.splitlines() if "run root: " in line
    ))
    properties = [
        row for path in l4.rglob("*.parquet")
        if "semantic_asserted_properties" in str(path)
        for row in pq.read_table(path).to_pylist()
    ]
    reading = next(row for row in properties if row["semantic_property_id"] == "property:prototype.reading")
    assert reading["normalized_value_json"] == "7"
    assert reading["entity_id"]

    materialized = tmp_path / "materialized"
    plan = tmp_path / "publication-plan.json"
    invoke([
        "app", "publish-structured", "--l4-run", str(l4_run), "--l3-root", str(l3),
        "--workspace-id", "00000000-0000-0000-0000-000000000001",
        "--plan", str(plan), "--materialize", str(materialized), "--dry-run",
    ])
    typed = [
        row for path in materialized.rglob("*.parquet")
        if "l5a_type_" in str(path)
        for row in pq.read_table(path).to_pylist()
    ]
    assert any(7 in row.values() for row in typed)
    assert plan.exists()

    def modes(value):
        if isinstance(value, dict):
            if "materialization" in value:
                yield value["materialization"]
            for child in value.values():
                yield from modes(child)
        elif isinstance(value, list):
            for child in value:
                yield from modes(child)

    materialization_modes = [
        mode for path in materialized.rglob("*.json")
        for mode in modes(json.loads(path.read_text()))
    ]
    assert materialization_modes
    assert set(materialization_modes) == {"asserted_properties"}
