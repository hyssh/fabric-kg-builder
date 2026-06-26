<#
.SYNOPSIS
  Reproduce the Surface troubleshooting knowledge graph end-to-end.

.DESCRIPTION
  Runs the full fabric-kg pipeline against sample_data\Surface_Troubleshootings:
  set-domain -> enrich -> densify -> compile-data -> compile-ontology ->
  compile-search -> package -> deploy-lakehouse -> deploy-ontology (multitype)
  -> deploy-search.

  This is the canonical, reproducible recipe. Every graph-quality step we
  learned is encoded here:
    * densify - four additive passes that connect the islands per-section
      extraction leaves behind:
        1. DeviceModel hub edges (has_component / has_part / has_procedure / has_symptom)
        2. Cause -> Symptom -> Resolution triples       (--link-scr)
        3. Procedure -> Step + umbrella-step rollup      (--link-steps)
        4. RCA diagnostic paths: diagnosed_by / remediated_by (--link-rca)
    * compile-data - additivity guard (fails if any input edge is dropped)
    * deploy-ontology --multitype - one Fabric entity type per real domain type
    * --create-data-agent-instruction - Data Agent grounding generated from the
      live graph (written to data\surface_kg\data-agent-instructions.md)

  Re-running is cheap: enrichment is the only slow/costly stage and is skipped
  when its output already exists (override with -ForceEnrich). Everything after
  enrich reuses the enriched data, so iterating on the model is fast.

.PARAMETER Env
  Target environment (dev|test|prod). Default: dev. Reads
  ontology\environments\{env}.json (copy it from the committed .example first).

.PARAMETER Deploy
  Also run the live deploy stages (requires Azure + 'az login'). Without this
  switch the script stops after 'package' (build artifacts only - no Azure calls).

.PARAMETER ForceEnrich
  Re-run enrichment even if enriched output already exists.

.EXAMPLE
  # Build artifacts only (no Azure):
  .\scripts\reproduce-surface-kg.ps1

.EXAMPLE
  # Full reproduction including live deploy to dev:
  az login
  .\scripts\reproduce-surface-kg.ps1 -Deploy
#>
[CmdletBinding()]
param(
  [ValidateSet('dev', 'test', 'prod')]
  [string]$Env = 'dev',
  [switch]$Deploy,
  [switch]$ForceEnrich
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Working directories (under data\surface_kg\ - gitignored).
$work       = 'data\surface_kg'
$source     = 'sample_data\Surface_Troubleshootings'
$questions  = 'sample_data\surface_questions.txt'
$enriched   = "$work\enriched"
$dense      = "$work\enriched_dense"
$parquet    = "$work\parquet"
$ontology   = "$work\ontology"
$search     = "$work\search"
$dist       = "$work\dist"

function Step($n, $msg) { Write-Host "`n=== [$n] $msg ===" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nPREFLIGHT FAILED: $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Preflight - fail early with a clear message instead of deep in the pipeline.
# ---------------------------------------------------------------------------
Step 0 'preflight checks'

# fabric-kg CLI installed?
if (-not (Get-Command fabric-kg -ErrorAction SilentlyContinue)) {
  Fail "fabric-kg is not on PATH. Install it first:  pip install -e .[dev]"
}
Write-Host "  fabric-kg: $((Get-Command fabric-kg).Source)" -ForegroundColor DarkGray

# Sample data present?
if (-not (Get-ChildItem "$source\*.pdf" -ErrorAction SilentlyContinue)) {
  Fail "No PDFs found in $source. Make sure the sample data is present."
}

# Env config present? (gitignored - user must copy from the .example template)
$envJson    = "ontology\environments\$Env.json"
$envExample = "ontology\environments\$Env.json.example"
if (-not (Test-Path $envJson)) {
  if ($Deploy) {
    Write-Host "" 
    Write-Host "  Create it from the template and fill in your Azure resource IDs:" -ForegroundColor Yellow
    Write-Host "    Copy-Item $envExample $envJson" -ForegroundColor Yellow
    Write-Host "    notepad $envJson" -ForegroundColor Yellow
    Fail "$envJson not found (it is gitignored)."
  } else {
    Write-Host "  NOTE: $envJson not found - fine for a build-only run (no -Deploy)." -ForegroundColor Yellow
  }
} else {
  Write-Host "  env config: $envJson" -ForegroundColor DarkGray
}

# For live deploy, confirm Azure auth.
if ($Deploy) {
  try { $acct = (az account show --query 'user.name' -o tsv 2>$null) } catch { $acct = $null }
  if (-not $acct) { Fail "Not logged in to Azure. Run 'az login' before -Deploy." }
  Write-Host "  azure account: $acct" -ForegroundColor DarkGray
}
Write-Host "  preflight OK" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 1. Domain brief - declares the field-service template + sample questions.
# ---------------------------------------------------------------------------
Step 1 'set-domain (field-service template + sample questions)'
fabric-kg set-domain `
  --industry manufacturing --business-domain field-service `
  --questions-file $questions `
  --out $enriched --force `
  --prompt "Field-service hardware troubleshooting for Microsoft Surface devices. Entity types: Device, DeviceModel, Component, Part, PartNumber, Procedure, Step, Tool, Symptom, Cause, Resolution. Key relationships: has_component, has_part, has_part_number, has_step, uses_tool, causes, resolved_by, addressed_by."

# 2. Enrich (slow/costly - skipped if already present unless -ForceEnrich).
$haveEnriched = (Test-Path $enriched) -and (Get-ChildItem "$enriched\*_canonical.json" -ErrorAction SilentlyContinue)
if ($ForceEnrich -or -not $haveEnriched) {
  Step 2 'enrich (LLM extraction - the slow stage)'
  fabric-kg enrich --input $source --out $enriched --domain-file "$enriched\domain.json" --resume
} else {
  Step 2 'enrich - SKIPPED (enriched output exists; use -ForceEnrich to redo)'
}

# 3. Densify - four additive passes (hub + S/C/R + steps/rollup + RCA).
Step 3 'densify (hub + Cause/Symptom/Resolution + Procedure/Step rollup + RCA paths)'
fabric-kg densify --input $enriched --out $dense

# 4-6. Compile canonical Parquet, ontology parts, and AI Search schemas.
Step 4 'compile-data (8 canonical Parquet tables; additivity guard runs here)'
fabric-kg compile-data --input $dense --out $parquet

Step 5 'compile-ontology'
fabric-kg compile-ontology --out $ontology

Step 6 'compile-search'
fabric-kg compile-search --input $parquet --out $search

# 7. Package - bundle build artifacts for deployment.
Step 7 'package'
$build = "$work\build_pkg"
if (Test-Path $build) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Path $build | Out-Null
Copy-Item $parquet  "$build\parquet"  -Recurse
Copy-Item $ontology "$build\ontology" -Recurse
Copy-Item $search   "$build\search"   -Recurse
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
fabric-kg package --build-dir $build --out $dist --include-search

if (-not $Deploy) {
  Write-Host "`nBuild artifacts ready under $dist." -ForegroundColor Green
  Write-Host "Re-run with -Deploy (after 'az login' and creating ontology\environments\$Env.json) to publish to Azure." -ForegroundColor Green
  return
}

# 8-10. Live deploy: Lakehouse, multi-type Ontology, AI Search.
Step 8 "deploy-lakehouse (env=$Env)"
fabric-kg deploy-lakehouse --env $Env --dist $dist

Step 9 "deploy-ontology --multitype (env=$Env) - writes data-agent-instructions.md"
fabric-kg deploy-ontology --env $Env --multitype --parquet-dir $parquet `
  --domain-file "$enriched\domain.json" `
  --agent-instruction-out "$work\data-agent-instructions.md" --no-mock

Step 10 "deploy-search (env=$Env)"
fabric-kg deploy-search --env $Env --dist $search

Write-Host "`nDone. The multi-type ontology deploy is a 202 async LRO - allow ~1-2 min to finish processing in Fabric." -ForegroundColor Green
Write-Host "Next:" -ForegroundColor Green
Write-Host "  1. Paste $work\data-agent-instructions.md into your Fabric Data Agent." -ForegroundColor Green
Write-Host "  2. Add the AI Search index '$Env-prefixed kg-chunks' as a second data source." -ForegroundColor Green
Write-Host "  3. (Optional) For a hybrid Foundry agent, use docs\foundry-hybrid-agent-prompt.md." -ForegroundColor Green
