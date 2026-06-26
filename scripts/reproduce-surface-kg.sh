#!/usr/bin/env bash
# Reproduce the Surface troubleshooting knowledge graph end-to-end (POSIX).
#
# Runs the full fabric-kg pipeline against sample_data/Surface_Troubleshootings.
# This is the canonical, reproducible recipe. Every graph-quality step we learned
# is encoded here:
#   * densify - four additive passes:
#       1. DeviceModel hub edges (has_component/has_part/has_procedure/has_symptom)
#       2. Cause -> Symptom -> Resolution triples       (--link-scr)
#       3. Procedure -> Step + umbrella-step rollup      (--link-steps)
#       4. RCA diagnostic paths: diagnosed_by/remediated_by (--link-rca)
#   * compile-data - additivity guard (fails if any input edge is dropped)
#   * deploy-ontology --multitype - one Fabric entity type per real domain type
#   * --create-data-agent-instruction - grounding generated from the live graph
#
# Usage:
#   ./scripts/reproduce-surface-kg.sh                 # build artifacts only
#   az login && ./scripts/reproduce-surface-kg.sh --deploy [--env dev] [--force-enrich]
set -euo pipefail

ENV="dev"
DEPLOY=0
FORCE_ENRICH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy) DEPLOY=1; shift ;;
    --force-enrich) FORCE_ENRICH=1; shift ;;
    --env) ENV="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

WORK="data/surface_kg"
SOURCE="sample_data/Surface_Troubleshootings"
QUESTIONS="sample_data/surface_questions.txt"
ENRICHED="$WORK/enriched"
DENSE="$WORK/enriched_dense"
PARQUET="$WORK/parquet"
ONTOLOGY="$WORK/ontology"
SEARCH="$WORK/search"
DIST="$WORK/dist"

step() { echo; echo "=== [$1] $2 ==="; }
fail() { echo; echo "PREFLIGHT FAILED: $1" >&2; exit 1; }

# --- Preflight -------------------------------------------------------------
step 0 "preflight checks"
command -v fabric-kg >/dev/null 2>&1 || fail "fabric-kg is not on PATH. Install:  pip install -e .[dev]"
ls "$SOURCE"/*.pdf >/dev/null 2>&1 || fail "No PDFs found in $SOURCE."

ENV_JSON="ontology/environments/$ENV.json"
ENV_EXAMPLE="ontology/environments/$ENV.json.example"
if [[ ! -f "$ENV_JSON" ]]; then
  if [[ "$DEPLOY" -eq 1 ]]; then
    echo "  Create it from the template and fill in your Azure resource IDs:" >&2
    echo "    cp $ENV_EXAMPLE $ENV_JSON" >&2
    fail "$ENV_JSON not found (it is gitignored)."
  else
    echo "  NOTE: $ENV_JSON not found - fine for a build-only run (no --deploy)."
  fi
fi
if [[ "$DEPLOY" -eq 1 ]]; then
  ACCT="$(az account show --query 'user.name' -o tsv 2>/dev/null || true)"
  [[ -n "$ACCT" ]] || fail "Not logged in to Azure. Run 'az login' before --deploy."
  echo "  azure account: $ACCT"
fi
echo "  preflight OK"

# --- Pipeline --------------------------------------------------------------
step 1 "set-domain (field-service template + sample questions)"
fabric-kg set-domain \
  --industry manufacturing --business-domain field-service \
  --questions-file "$QUESTIONS" \
  --out "$ENRICHED" --force \
  --prompt "Field-service hardware troubleshooting for Microsoft Surface devices. Entity types: Device, DeviceModel, Component, Part, PartNumber, Procedure, Step, Tool, Symptom, Cause, Resolution. Key relationships: has_component, has_part, has_part_number, has_step, uses_tool, causes, resolved_by, addressed_by."

if [[ "$FORCE_ENRICH" -eq 1 ]] || ! ls "$ENRICHED"/*_canonical.json >/dev/null 2>&1; then
  step 2 "enrich (LLM extraction - the slow stage)"
  fabric-kg enrich --input "$SOURCE" --out "$ENRICHED" --domain-file "$ENRICHED/domain.json" --resume
else
  step 2 "enrich - SKIPPED (enriched output exists; pass --force-enrich to redo)"
fi

step 3 "densify (hub + Cause/Symptom/Resolution + Procedure/Step rollup + RCA paths)"
fabric-kg densify --input "$ENRICHED" --out "$DENSE"

step 4 "compile-data (additivity guard runs here)"
fabric-kg compile-data --input "$DENSE" --out "$PARQUET"

step 5 "compile-ontology"
fabric-kg compile-ontology --out "$ONTOLOGY"

step 6 "compile-search"
fabric-kg compile-search --input "$PARQUET" --out "$SEARCH"

step 7 "package"
BUILD="$WORK/build_pkg"
rm -rf "$BUILD"; mkdir -p "$BUILD"
cp -r "$PARQUET" "$BUILD/parquet"
cp -r "$ONTOLOGY" "$BUILD/ontology"
cp -r "$SEARCH" "$BUILD/search"
rm -rf "$DIST"
fabric-kg package --build-dir "$BUILD" --out "$DIST" --include-search

if [[ "$DEPLOY" -eq 0 ]]; then
  echo; echo "Build artifacts ready under $DIST."
  echo "Re-run with --deploy (after 'az login' and creating $ENV_JSON) to publish to Azure."
  exit 0
fi

step 8 "deploy-lakehouse (env=$ENV)"
fabric-kg deploy-lakehouse --env "$ENV" --dist "$DIST"

step 9 "deploy-ontology --multitype (env=$ENV) - writes data-agent-instructions.md"
fabric-kg deploy-ontology --env "$ENV" --multitype --parquet-dir "$PARQUET" \
  --domain-file "$ENRICHED/domain.json" \
  --agent-instruction-out "$WORK/data-agent-instructions.md" --no-mock

step 10 "deploy-search (env=$ENV)"
fabric-kg deploy-search --env "$ENV" --dist "$SEARCH"

echo; echo "Done. The multi-type ontology deploy is a 202 async LRO - allow ~1-2 min to finish."
echo "Next:"
echo "  1. Paste $WORK/data-agent-instructions.md into your Fabric Data Agent."
echo "  2. Add the AI Search index ('$ENV'-prefixed kg-chunks) as a second data source."
echo "  3. (Optional) For a hybrid Foundry agent, use docs/foundry-hybrid-agent-prompt.md."
