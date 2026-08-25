#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

cd "$ROOT"

PYTHONPATH="$ROOT/src" uv run --no-sync pytest tests/unit tests/contract \
  -m "not slow and not integration" -q
if [[ -n "${FABRIC_KG_PREBUILT_DIST:-}" ]]; then
  mkdir -p "$TMP_ROOT/dist"
  cp "$FABRIC_KG_PREBUILT_DIST"/fabric_kg_builder-0.2.4* \
    "$TMP_ROOT/dist/"
else
  uv build --out-dir "$TMP_ROOT/dist"
fi

uv export --locked --extra dev --no-emit-project --no-hashes \
  --output-file "$TMP_ROOT/locked-requirements.txt"
SOURCE_PYTHON="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
mkdir -p "$TMP_ROOT/wheelhouse"
env -u PYTHONPATH "$SOURCE_PYTHON" - \
  "$ROOT/pyproject.toml" "$TMP_ROOT/wheelhouse" <<'PY'
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata
import io
from pathlib import Path
import re
import sys
import zipfile

from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

project = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
wheelhouse = Path(sys.argv[2])
requirements = [
    *project["project"]["dependencies"],
    *project["project"]["optional-dependencies"]["dev"],
]
pending = [Requirement(value) for value in requirements]
packed: set[str] = set()
while pending:
    requirement = pending.pop()
    if requirement.marker and not requirement.marker.evaluate():
        continue
    normalized = requirement.name.lower().replace("_", "-")
    if normalized in packed or normalized == "fabric-kg-builder":
        continue
    distribution = importlib.metadata.distribution(requirement.name)
    packed.add(normalized)
    for dependency in distribution.requires or ():
        candidate = Requirement(dependency)
        if candidate.marker and not candidate.marker.evaluate():
            continue
        pending.append(candidate)
    files: dict[str, bytes] = {}
    for relative in distribution.files or ():
        source = Path(distribution.locate_file(relative)).resolve()
        destination_relative = Path(relative)
        if ".." in destination_relative.parts:
            continue
        if source.is_file():
            files[destination_relative.as_posix()] = source.read_bytes()
    dist_info = next(
        path
        for path in files
        if path.endswith(".dist-info/WHEEL")
    ).rsplit("/", 1)[0]
    wheel_metadata = files[f"{dist_info}/WHEEL"].decode("utf-8")
    tag = next(
        line.split(":", 1)[1].strip()
        for line in wheel_metadata.splitlines()
        if line.startswith("Tag:")
    )
    name = re.sub(r"[^\w\d.]+", "_", distribution.metadata["Name"])
    version = re.sub(r"[^\w\d.]+", "_", distribution.version)
    wheel_path = wheelhouse / f"{name}-{version}-{tag}.whl"
    record_path = f"{dist_info}/RECORD"
    files.pop(record_path, None)
    rows = []
    for path, content in sorted(files.items()):
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()
        ).rstrip(b"=").decode("ascii")
        rows.append((path, f"sha256={digest}", str(len(content))))
    rows.append((record_path, "", ""))
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(rows)
    files[record_path] = record_buffer.getvalue().encode("utf-8")
    with zipfile.ZipFile(
        wheel_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
print(f"packed_dependency_wheels={len(packed)}")
PY

uv venv --python "$SOURCE_PYTHON" "$TMP_ROOT/installed-env"
uv pip install --offline --find-links "$TMP_ROOT/wheelhouse" \
  --python "$TMP_ROOT/installed-env/bin/python" \
  -r "$TMP_ROOT/locked-requirements.txt" \
  "$TMP_ROOT/dist/fabric_kg_builder-0.2.4-py3-none-any.whl[dev]"

INSTALLED_PYTHON="$TMP_ROOT/installed-env/bin/python"
FABRIC_KG="$TMP_ROOT/installed-env/bin/fabric-kg"
WORK="$TMP_ROOT/work"
mkdir -p "$WORK"
cd "$WORK"

test "$(command -v "$FABRIC_KG")" = "$FABRIC_KG"
env -u PYTHONPATH "$INSTALLED_PYTHON" - "$ROOT" "$FABRIC_KG" <<'PY'
from pathlib import Path
import sys

import fabric_kg_builder
import fabric_kg_builder.cli.build_deploy_cmd as build_deploy_cmd
import fabric_kg_builder.infra.apply as infra_apply

repo = Path(sys.argv[1]).resolve()
binary = Path(sys.argv[2]).resolve()
prefix = Path(sys.prefix).resolve()
assert binary.is_relative_to(prefix), (binary, prefix)
for module in (fabric_kg_builder, build_deploy_cmd, infra_apply):
    origin = Path(module.__file__).resolve()
    assert origin.is_relative_to(prefix), (module.__name__, origin, prefix)
    assert not origin.is_relative_to(repo), (module.__name__, origin, repo)
PY

test "$(env -u PYTHONPATH "$FABRIC_KG" --version)" = "fabric-kg, version 0.2.4"
env -u PYTHONPATH "$FABRIC_KG" --help >/dev/null

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
  env -u PYTHONPATH "$FABRIC_KG" "$command" --help >/dev/null
done

env -u PYTHONPATH "$INSTALLED_PYTHON" - "$WORK" "$ROOT" <<'PY'
from pathlib import Path
import sys

from fabric_kg_builder.domain.models import ApprovalMetadataV2
from fabric_kg_builder.domain.service import (
    compute_contract_hash,
    load_domain_contract,
    save_domain_contract,
)
work = Path(sys.argv[1])
root = Path(sys.argv[2])
contract = load_domain_contract(
    root / "tests/fixtures/domains/facility-maintenance-v2.yaml"
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
PY

mkdir -p "$WORK/golden/enriched"
cp "$ROOT/tests/fixtures/golden/surface_mini_canonical.json" \
  "$WORK/golden/enriched/"

env -u PYTHONPATH "$FABRIC_KG" domain validate --file "$WORK/domain.yaml"
env -u PYTHONPATH "$FABRIC_KG" domain status --file "$WORK/domain.yaml"
env -u PYTHONPATH "$FABRIC_KG" compile-data \
  --input "$WORK/golden/enriched" \
  --out "$WORK/golden/parquet" \
  --validate

printf '%s\n' \
  '{"fabricWorkspaceId":"fixture-workspace","fabricLakehouseId":"fixture-lakehouse"}' \
  > "$WORK/fixture-outputs.json"
RUN_ID="02400000-0000-4000-8000-000000000024"
env -u PYTHONPATH \
  AZURE_TENANT_ID="00000000-0000-4000-8000-000000000024" \
  "$FABRIC_KG" \
  --config "$ROOT/fabric-kg.yaml" \
  build-deploy \
  --input "$ROOT/tests/fixtures/csv/sample.csv" \
  --domain-contract "$WORK/domain.yaml" \
  --env local \
  --run-id "$RUN_ID" \
  --state-dir "$WORK/runs" \
  --infra-dir "$ROOT/tests/fixtures/release_024/infra" \
  --semantic-mappings "$ROOT/ontology/mappings.yaml" \
  --semantic-vocabulary "$ROOT/ontology/vocabulary.yaml" \
  --semantic-ids-lock "$ROOT/ontology/ids.lock.json" \
  --no-provision \
  --no-deploy-serving \
  --infra-outputs "$WORK/fixture-outputs.json" \
  --dry-run

STATE="$WORK/runs/$RUN_ID/state.json"
test "$(jq -r '.status' "$STATE")" = "planned"
test "$(jq -r '.plan_fingerprint' "$STATE")" != "null"
test "$(jq -r '.resolved_mutation_authority_hash' "$STATE")" != "null"
jq -e '.planned_stages | index("compile_semantic") != null' "$STATE" >/dev/null

mkdir -p "$WORK/installed-tests/unit" "$WORK/installed-tests/integration"
cp "$ROOT/tests/unit/test_golden_canonical.py" "$WORK/installed-tests/unit/"
cp "$ROOT/tests/integration/test_e2e_trace.py" \
  "$WORK/installed-tests/integration/"
cp -R "$ROOT/tests/fixtures" "$WORK/installed-tests/"
env -u PYTHONPATH "$INSTALLED_PYTHON" -m pytest \
  "$WORK/installed-tests/unit/test_golden_canonical.py" \
  "$WORK/installed-tests/integration/test_e2e_trace.py" \
  -q -o addopts='' --basetemp "$TMP_ROOT/installed-pytest"

echo "fabric-kg 0.2.4 local release smoke passed"
