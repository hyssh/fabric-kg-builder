#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

cd "$ROOT"

PYTHONPATH="$ROOT/src" uv run --no-sync pytest tests/unit tests/contract \
  -m "not slow and not integration" -q
uv build --out-dir "$TMP_ROOT/dist"

export UV_TOOL_DIR="$TMP_ROOT/tools"
export UV_TOOL_BIN_DIR="$TMP_ROOT/bin"
uv export --locked --no-dev --format requirements-txt --no-hashes \
  --output-file "$TMP_ROOT/constraints.raw.txt"
sed '/^-e \.$/d' "$TMP_ROOT/constraints.raw.txt" \
  > "$TMP_ROOT/constraints.txt"
uv tool install --force \
  --constraints "$TMP_ROOT/constraints.txt" \
  "$TMP_ROOT/dist/fabric_kg_builder-0.2.4-py3-none-any.whl"
FABRIC_KG="$UV_TOOL_BIN_DIR/fabric-kg"

test "$("$FABRIC_KG" --version)" = "fabric-kg, version 0.2.4"
"$FABRIC_KG" --help >/dev/null

commands=(
  app assets build-deploy collect-evidence compile-agent compile-data
  compile-graph compile-ontology compile-search compile-semantic densify
  deploy-data-agent deploy-graph deploy-lakehouse deploy-ontology
  deploy-search deploy-serving domain enrich evaluate infra init init-domain
  inspect-diagnostics inspect-ontology inspect-source knowledge lineage package
  report set-domain trace validate validate-artifacts validate-deployment
  validate-projection
)
for command in "${commands[@]}"; do
  "$FABRIC_KG" "$command" --help >/dev/null
done

WORK="$TMP_ROOT/work"
mkdir -p "$WORK"

PYTHONPATH="$ROOT/src:$ROOT" uv run --no-sync python - "$WORK" <<'PY'
from pathlib import Path
import sys

from fabric_kg_builder.domain.models import ApprovalMetadataV2
from fabric_kg_builder.domain.service import (
    compute_contract_hash,
    load_domain_contract,
    save_domain_contract,
)
from tests.unit.test_compile_data_cmd import _write_schema2_compile_input

work = Path(sys.argv[1])
contract = load_domain_contract(
    "tests/fixtures/domains/facility-maintenance-v2.yaml"
)
contract_hash = compute_contract_hash(contract)
approved = contract.model_copy(
    update={
        "approval": ApprovalMetadataV2(
            status="approved",
            approved_by="local-release-smoke",
            approved_at_utc="2026-08-24T00:00:00Z",
            contract_hash=contract_hash,
            proposal_hash="1" * 64,
            source_profile_hash="2" * 64,
            prompt_hash="3" * 64,
            prompt_version="release-smoke.v1",
            model_version="fixture-model",
            model_hash="4" * 64,
        )
    }
)
save_domain_contract(approved, work / "domain.yaml")
sample = work / "sample"
sample.mkdir()
_write_schema2_compile_input(sample)
PY

"$FABRIC_KG" domain validate --file "$WORK/domain.yaml"
"$FABRIC_KG" domain status --file "$WORK/domain.yaml"
"$FABRIC_KG" compile-data \
  --input "$WORK/sample/build/enriched" \
  --out "$WORK/sample/build/parquet" \
  --validate

RUN_ID="02400000-0000-4000-8000-000000000024"
"$FABRIC_KG" build-deploy \
  --input "$ROOT/tests/fixtures/csv/sample.csv" \
  --domain-contract "$WORK/domain.yaml" \
  --env local \
  --run-id "$RUN_ID" \
  --state-dir "$WORK/runs" \
  --infra-dir "$ROOT/tests/fixtures/release_024/infra" \
  --no-provision \
  --dry-run

STATE="$WORK/runs/$RUN_ID/state.json"
test "$(jq -r '.status' "$STATE")" = "planned"
test "$(jq -r '.plan_fingerprint' "$STATE")" != "null"
jq -e '.planned_stages | index("compile_semantic") != null' "$STATE" >/dev/null

PYTHONPATH="$ROOT/src:$ROOT" uv run --no-sync pytest \
  tests/unit/test_schema2_materialization_deployment.py \
  tests/unit/test_bounded_graph_query_authority.py \
  -q --no-cov --basetemp "$TMP_ROOT/sample-pytest"

echo "fabric-kg 0.2.4 local release smoke passed"
