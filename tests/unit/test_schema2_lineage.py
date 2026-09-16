"""Instance lineage is read-only and starts after verified schema approval."""

import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.enrichment.schema2_stage import run_l2
from fabric_kg_builder.lineage.schema2 import frozen_lineage_authority, trace_schema2_record
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_schema2_validation_stage import _Service, _l3, _pipeline, _reader


@pytest.fixture
def lineage_case(tmp_path):
    l1, domain, l2 = _pipeline(tmp_path, "lineage")
    l3 = _l3(tmp_path, l1, domain)
    l4 = run_l4(l3, state_root=tmp_path / "l4")
    return dict(l1_state=l1, domain=domain, l2_state=tmp_path / ".fkg" / "l2"), l3, l4


def test_unapproved_schema_blocks_before_reading_l2(tmp_path):
    with pytest.raises(ValueError, match="LINEAGE_SCHEMA_NOT_FROZEN"):
        trace_schema2_record(
            "anything", l1_state=tmp_path / "missing-l1",
            domain=tmp_path / "domain.yaml", l2_state=tmp_path / "missing-l2",
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["draft", "stale_approval"])
def test_schema_drift_blocks_data_trace(lineage_case, change):
    options, _, _ = lineage_case
    import yaml

    domain = yaml.safe_load(options["domain"].read_text())
    if change == "draft":
        domain["approval"]["status"] = "draft"
    else:
        domain["approval"]["contract_hash"] = "0" * 64
    options["domain"].write_text(yaml.safe_dump(domain))
    with pytest.raises(ValueError, match="LINEAGE_SCHEMA_NOT_FROZEN"):
        frozen_lineage_authority(l1_state=options["l1_state"], domain=options["domain"])


@pytest.mark.parametrize("kind", ["entity", "relationship"])
def test_trace_proposed_and_asserted_records_is_read_only(lineage_case, tmp_path, kind):
    options, l3, l4 = lineage_case
    candidate = next(item for item in l3.candidate_results if item.candidate_kind == kind)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    proposal = trace_schema2_record(candidate.candidate_id, **options)
    assert proposal["authority"]["schema_state"] == "frozen"
    assert proposal["validation_state"] == "L2_proposals_only"
    assert not proposal["validation"]
    assert proposal["observations"][0]["source"]["original_byte_hash"]
    assert "text" not in proposal["observations"][0]["source"]
    full = trace_schema2_record(
        candidate.semantic_id, **options, l4_run=l4.run_root, l3_root=l3.state_root,
    )
    assert full["validation"] and full["serving_records"]
    assert full["validation"][0]["lifecycle_state"] == "asserted"
    assert full["validation"][0]["evidence_span_ids"]
    assert full["semantic_accuracy"] == "not_assessed"
    by_candidate = trace_schema2_record(
        candidate.candidate_id, **options, l4_run=l4.run_root, l3_root=l3.state_root,
    )
    assert by_candidate["serving_records"] == full["serving_records"]
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_lineage_cli_requires_explicit_schema_inputs(lineage_case):
    options, l3, _ = lineage_case
    candidate = l3.candidate_results[0]
    args = ["lineage", "trace", candidate.candidate_id, "--l2-state", str(options["l2_state"])]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0 and "requires --l1-state and --domain" in result.output
    args += ["--l1-state", str(options["l1_state"]), "--domain", str(options["domain"]), "--format", "json"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["authority"]["schema_state"] == "frozen"


def test_corrupt_candidate_cannot_be_traced(lineage_case):
    options, l3, _ = lineage_case
    partition = next((options["l2_state"] / "proposed-candidates").glob("*.json"))
    partition.write_text("[]")
    with pytest.raises(ValueError, match="hash differs"):
        trace_schema2_record(l3.candidate_results[0].candidate_id, **options)


def test_unknown_record_is_not_a_success_shaped_trace(lineage_case):
    options, _, _ = lineage_case
    with pytest.raises(ValueError, match="LINEAGE_RECORD_NOT_FOUND"):
        trace_schema2_record("absent", **options)


def test_matching_schema_does_not_allow_mixed_extraction_runs(lineage_case, tmp_path):
    options, l3, l4 = lineage_case
    other_l2 = tmp_path / "other-l2"
    run_l2(
        reader=_reader(tmp_path, options["l1_state"], options["domain"], "lineage"),
        service=_Service("lineage"), state_root=other_l2,
        l1_state_root=options["l1_state"], domain_path=options["domain"],
        prompt_hash=canonical_sha256({"prompt": "different"}),
        model_version="fixture/2.0.0", model_hash=canonical_sha256({"model": "different"}),
    )
    with pytest.raises(ValueError, match="LINEAGE_RUN_DRIFT"):
        trace_schema2_record(
            l3.candidate_results[0].candidate_id,
            **{**options, "l2_state": other_l2},
            l4_run=l4.run_root, l3_root=l3.state_root,
        )


def test_audit_rows_are_not_serving_sources(lineage_case):
    _, _, l4 = lineage_case
    source = l4.sealed_source()
    assert source.audit_rows()
    with pytest.raises(ValueError, match="unknown sealed L4 serving table"):
        source.resolve("audit_candidates")


def test_audit_read_rejects_tampered_bytes(lineage_case):
    _, _, l4 = lineage_case
    source = l4.sealed_source()
    (l4.run_root / "audit_candidates.parquet").write_bytes(b"invalid parquet")
    with pytest.raises(ValueError, match="unreadable"):
        source.audit_rows()
