"""Qualification proofs are closed, byte-exact L4 dependencies, not L2 path guesses."""

from copy import deepcopy
import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.enrichment import approved_partial_handoff as partial
from fabric_kg_builder.enrichment import approved_local_identifiers as ids
from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource, L4_ACCEPTED_VERSIONS
from fabric_kg_builder.serving.business_quality import assess_business_quality
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_approved_donor_continuation import partial_donor, _snapshot  # noqa: F401
from tests.unit.test_approved_partial_handoff import _approve, _deny_network, _options
from tests.unit.test_approved_source_span_handoff import span_partial  # noqa: F401
from tests.unit.test_approved_source_span_continuation import v2_partial  # noqa: F401
from tests.unit.test_window_run_prefix_acceptance_cli import prefix_case  # noqa: F401


def _publication(donor):
    options = {**_options(donor), "qualify_local_identifiers": True}
    plan = partial.run_partial_handoff(**options)
    _approve(options, plan)
    l3 = run_l3(
        state_root=options["state_root"].with_name("l3"),
        l2_state_root=options["state_root"], l1_state_root=options["l1_state_root"],
        domain_path=options["domain_path"],
    )
    l4 = run_l4(l3, state_root=options["state_root"].with_name("l4"))
    return options, l3, l4


def _load(l3, l4):
    return SealedL4ServingSource.from_run(l4.run_root, input_manifest_search_roots=[l3.state_root])


def test_handoff_l3_l4_quality_cli_reuse_and_portable_source_witnesses(span_partial, monkeypatch):
    _deny_network(monkeypatch)
    donor_before = _snapshot(span_partial["state_root"])
    options, l3, l4 = _publication(span_partial)
    l2_before = _snapshot(options["state_root"])
    l3_before = _snapshot(l3.state_root)
    source = _load(l3, l4)
    report = assess_business_quality(source)
    assert "blocker_count" in report["summary"]
    assert l4.metrics.foundry_calls == 0
    assert l4.receipt.accepted_contract_versions[partial.WITNESS_KIND] == partial.WITNESS_VERSION
    assert dict(l4.receipt.accepted_contract_versions) != L4_ACCEPTED_VERSIONS
    bundle = json.loads((l4.run_root / partial.WITNESS_FILE).read_bytes())
    assert set(bundle["files"]) == partial._witness_dependencies(l3.inputs.partial_extraction_scope)
    for relative, descriptor in bundle["files"].items():
        origin = l3.run_root / relative.removeprefix("l3/") if relative.startswith("l3/") else options["state_root"] / relative
        payload = (l4.run_root / partial.WITNESS_DIR / relative).read_bytes()
        assert payload == origin.read_bytes()
        assert descriptor == {"sha256": hashlib.sha256(payload).hexdigest(), "byte_count": len(payload)}
    assert run_l4(l3, state_root=l4.run_root.parent.parent).receipt == l4.receipt
    assert _snapshot(span_partial["state_root"]) == donor_before
    assert _snapshot(options["state_root"]) == l2_before
    assert _snapshot(l3.state_root) == l3_before
    # Only the disposable fixture moves: a portable L4 loader cannot consult L2.
    options["state_root"].rename(options["state_root"].with_name("offline-l2"))
    moved = l4.run_root.parent.parent.with_name("portable-copy")
    shutil.copytree(l4.run_root, moved)
    portable = SealedL4ServingSource.from_run(moved, input_manifest_search_roots=[l3.state_root])
    assert assess_business_quality(portable) == report
    before = _snapshot(moved)
    output = moved.with_name("quality-report.json")
    result = CliRunner().invoke(cli, [
        "assess-business-quality", "--l4-run", str(moved), "--l3-root", str(l3.state_root),
        "--output", str(output),
    ])
    assert result.exit_code in (0, 5), result.output
    assert output.is_file()
    actual = json.loads(output.read_bytes())
    assert result.exit_code == (5 if actual["summary"]["blocker_count"] else 0)
    assert _snapshot(moved) == before


@pytest.fixture
def portable_case(v2_partial, monkeypatch):
    _deny_network(monkeypatch)
    return _publication(v2_partial)


@pytest.mark.parametrize("change", ["missing-source", "provider", "extra", "symlink", "manifest-path", "manifest-missing"])
def test_portable_witness_missing_tampered_or_escaping_dependency_rejected(portable_case, change):
    options, l3, l4 = portable_case
    root = l4.run_root
    sources = root / partial.WITNESS_DIR / "source-units"
    retained = root / partial.WITNESS_DIR / "retained-responses"
    if change == "missing-source":
        next(sources.glob("*.json")).unlink()
    elif change == "provider":
        path = next(retained.glob("*.json"))
        data = json.loads(path.read_bytes())
        data["provider_output"]["raw_output_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    elif change == "extra":
        (root / partial.WITNESS_DIR / "extra.json").write_text("{}")
    elif change == "symlink":
        path = next(sources.glob("*.json"))
        filename = path.name
        path.unlink()
        path.symlink_to(options["state_root"] / "source-units" / filename)
    elif change == "manifest-path":
        path = root / partial.WITNESS_FILE
        bundle = json.loads(path.read_bytes())
        bundle["files"]["../../outside.json"] = {"sha256": "0" * 64, "byte_count": 0}
        path.write_text(json.dumps(bundle))
    else:
        (root / partial.WITNESS_FILE).unlink()
    with pytest.raises((ValueError, OSError)):
        _load(l3, l4)


def test_resigned_witness_inventory_still_recomputes_projection(portable_case):
    _, l3, l4 = portable_case
    root = l4.run_root
    path = next((root / partial.WITNESS_DIR / "retained-responses").glob("*.json"))
    record = json.loads(path.read_bytes())
    record[ids.ARTIFACT_KEY]["response"]["candidates"][0]["local_id"] = "forged"
    record[ids.ARTIFACT_KEY]["proof"]["output_response_sha256"] = ids.lossless_hash(
        record[ids.ARTIFACT_KEY]["response"],
    )
    path.write_text(json.dumps(record))
    relative = str(path.relative_to(root / partial.WITNESS_DIR))
    integrity_path = root / partial.WITNESS_DIR / partial.INTEGRITY
    inventory = json.loads(integrity_path.read_bytes())
    inventory[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    integrity_path.write_text(json.dumps(inventory))
    manifest_path = root / partial.WITNESS_FILE
    bundle = json.loads(manifest_path.read_bytes())
    for changed in (relative, partial.INTEGRITY):
        content = (root / partial.WITNESS_DIR / changed).read_bytes()
        bundle["files"][changed] = {"sha256": hashlib.sha256(content).hexdigest(), "byte_count": len(content)}
    manifest_path.write_text(json.dumps(bundle))
    entries = [
        partial._witness_entry(
            entry.artifact_id, (root / partial.witness_artifact_path(entry.artifact_id)).read_bytes(),
            row_count=len(bundle["files"]) if entry.artifact_id == partial.WITNESS_ID else 1,
        ) if entry.artifact_id == partial.WITNESS_ID or entry.artifact_id.startswith(partial.WITNESS_PREFIX)
        else entry
        for entry in l4.output_manifest.entries
    ]
    with pytest.raises(ValueError, match="LOCAL_IDENTIFIER_PROOF_INVALID.*PROJECTION_DRIFT"):
        partial._verify_witness_bundle(
            root, l3.inputs.partial_extraction_scope, SimpleNamespace(entries=entries),
            upstream_manifest=l3.output_manifest,
        )


def test_export_rejects_foreign_unsealed_source_location_before_reading_it(portable_case):
    _, l3, l4 = portable_case
    from dataclasses import replace

    scope = deepcopy(l3.inputs.partial_extraction_scope)
    scope["plan"]["state_root"] = str(l4.run_root / "unapproved-source")
    forged = replace(l3, inputs=replace(l3.inputs, partial_extraction_scope=scope))
    destination = l4.run_root.with_name("not-written")
    with pytest.raises(ValueError, match="WITNESS_SOURCE_AUTHORITY_DRIFT"):
        partial.export_qualified_witnesses(forged, destination)
    assert not destination.exists()


def test_export_cannot_patch_witnesses_into_an_existing_sealed_run(portable_case):
    _, l3, l4 = portable_case
    before = _snapshot(l4.run_root)
    with pytest.raises(ValueError, match="WITNESS_FRESH_DESTINATION_REQUIRED"):
        partial.export_qualified_witnesses(l3, l4.run_root)
    assert _snapshot(l4.run_root) == before
