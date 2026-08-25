# fabric-kg 0.2.4 Post-Merge Live Smoke Checklist

Date prepared: 2026-08-24

Status: Pending post-merge live validation

This checklist must run from a fresh environment after the layer-7 PR and its
stack are merged. It intentionally contains no credentials, tenant IDs, source
content, or real resource IDs.

## 1. Isolated installation

```bash
git checkout <merged-commit>
TMP_ROOT="$(mktemp -d)"
export UV_TOOL_DIR="$TMP_ROOT/tools"
export UV_TOOL_BIN_DIR="$TMP_ROOT/bin"
uv tool install --force .
export PATH="$UV_TOOL_BIN_DIR:$PATH"

fabric-kg --version
fabric-kg --help
```

Require `fabric-kg, version 0.2.4`. Run `fabric-kg <command> --help` for every
top-level command shown by `fabric-kg --help`.

## 2. Configure and approve schema-2 authority

Populate the normal ignored environment/config files with resource references.
Do not copy secrets into the run directory or this report.

```bash
az login
fabric-kg infra preflight --env "$ENVIRONMENT" --infra-dir ./infra --json

fabric-kg init-domain \
  --input "$SOURCE_ROOT" \
  --intake "$INTAKE_FILE" \
  --non-interactive \
  --out "$DOMAIN_FILE"

fabric-kg domain validate --file "$DOMAIN_FILE"
fabric-kg domain approve \
  --file "$DOMAIN_FILE" \
  --proposal ./.fkg/domain-proposal.json \
  --source-profile ./.fkg/source-profile.json \
  --approved-by "$OPERATOR"
fabric-kg domain status --file "$DOMAIN_FILE"
```

Review the proposal citations, N, K, endpoint policies, publication policy,
source profile hash, prompt/model identity, mappings, vocabulary, and IDs before
approval.

## 3. Mandatory dry-run and explicit live approval

```bash
RUN_ID="$(python -c 'import uuid; print(uuid.uuid4())')"

fabric-kg build-deploy \
  --input "$SOURCE_ROOT" \
  --domain-contract "$DOMAIN_FILE" \
  --semantic-contract "$SEMANTIC_CONTRACT" \
  --semantic-mappings "$SEMANTIC_MAPPINGS" \
  --semantic-vocabulary "$SEMANTIC_VOCABULARY" \
  --semantic-ids-lock "$SEMANTIC_IDS_LOCK" \
  --env "$ENVIRONMENT" \
  --run-id "$RUN_ID" \
  --manifest "$DEPLOYMENT_MANIFEST" \
  --dry-run

jq '{status, plan_fingerprint, planned_stages}' \
  "build/runs/$RUN_ID/state.json"
```

An identified operator must review resource actions, names, adopted ARM
endpoints, enabled stages, semantic authority, and the plan fingerprint. Do not
continue on an unexplained create, replace, delete, endpoint, or name.

Only after that review:

```bash
fabric-kg build-deploy \
  --input "$SOURCE_ROOT" \
  --domain-contract "$DOMAIN_FILE" \
  --semantic-contract "$SEMANTIC_CONTRACT" \
  --semantic-mappings "$SEMANTIC_MAPPINGS" \
  --semantic-vocabulary "$SEMANTIC_VOCABULARY" \
  --semantic-ids-lock "$SEMANTIC_IDS_LOCK" \
  --env "$ENVIRONMENT" \
  --run-id "$RUN_ID" \
  --manifest "$DEPLOYMENT_MANIFEST" \
  --resume \
  --approve-live \
  --graph-preview-acknowledged
```

If the CLI reports a plan-fingerprint mismatch, stop. Review a new dry-run;
never bypass the mismatch.

## 4. Lakehouse typed-table read-back

The live materialization receipt is produced from typed-table persistence and
read-back, not only local Parquet:

```bash
MATERIALIZATION="build/runs/$RUN_ID/release/materialization-deployment.json"

jq -e '
  .status == "succeeded" and
  .mock == false and
  (.tables | length) > 0 and
  ([.tables[] |
    (.persisted_row_count == .planned_row_count) and
    (.persisted_row_hash == .planned_row_hash) and
    (.persisted_schema_hash == .planned_schema_hash)
  ] | all)
' "$MATERIALIZATION"

jq '{
  expected_managed_tables,
  actual_managed_tables,
  tables: [.tables[] | {
    table_name,
    semantic_id,
    persisted_row_count,
    persisted_row_hash,
    persisted_schema_hash
  }]
}' "$MATERIALIZATION"
```

Require nonempty typed entity and relationship tables, identical expected and
actual managed-table sets, exact row-count/hash equality, and no raw
unresolved/rejected relationship table bound for serving.

## 5. Ontology definition read-back

```bash
ONTOLOGY="build/runs/$RUN_ID/release/ontology-deployment.json"

jq -e '
  .mock == false and
  .status == "succeeded" and
  (.ontology_item_id | length) > 0 and
  (.ontology_persisted_projection_hash ==
   .ontology_submitted_projection_hash) and
  (.materialized_tables | length) > 0
' "$ONTOLOGY"

jq '{
  ontology_item_id,
  semantic_model_manifest_hash,
  semantic_crosswalk_hash,
  ontology_submitted_projection_hash,
  ontology_persisted_projection_hash,
  materialized_tables
}' "$ONTOLOGY"
```

Require live definition read-back, exact entity/relationship bindings to the
typed tables, the active semantic hash, and no audit-only lifecycle rows.

## 6. Graph mappings and serving read-back

```bash
SERVING="build/runs/$RUN_ID/release/serving-deployment.json"

jq -e '
  .mock == false and
  .status == "succeeded" and
  (.graph_definition_counts.node_types > 0) and
  (.graph_definition_counts.relationship_types > 0) and
  (.graph_fresh_projection_hash == .ontology_fresh_projection_hash)
' "$SERVING"

jq '{
  graph_item_id,
  graph_definition_counts,
  source_tables,
  materialized_tables,
  ontology_fresh_projection_hash,
  graph_fresh_projection_hash
}' "$SERVING"
```

Require every Graph label and edge mapping to reference the sealed typed
Lakehouse tables and require fresh Graph/Ontology projection hash equality.

## 7. Search counts and provenance

For every index listed by the serving receipt, use Entra authentication. Select
only provenance fields; do not print `source_quote`, chunk text, or vectors.

```bash
for INDEX in $(jq -r '.search_indexes[].name' "$SERVING"); do
  az rest \
    --method post \
    --resource https://search.azure.com \
    --url "$AZURE_SEARCH_ENDPOINT/indexes/$INDEX/docs/search?api-version=2024-07-01" \
    --headers Content-Type=application/json \
    --body '{"search":"*","count":true,"top":1,"select":"chunk_id,source_file_id,source_quote_is_verbatim,semantic_contract_hash"}' \
    --output json |
    jq '{count: ."@odata.count", provenance: [.value[] | {
      chunk_id,
      source_file_id,
      source_quote_is_verbatim,
      semantic_contract_hash
    }]}'
done
```

Require nonzero counts, stable IDs, exact active semantic hash where present,
and provenance/verification fields on sampled documents.

## 8. Persisted projection and bounded live plan execution

```bash
fabric-kg validate-projection \
  --semantic-dir "build/runs/$RUN_ID/build/semantic" \
  --materialization-receipt "$MATERIALIZATION" \
  --ontology-receipt "$ONTOLOGY" \
  --serving-receipt "$SERVING" \
  --out "build/runs/$RUN_ID/release/persisted-projection-receipt.json"

fabric-kg collect-evidence \
  --competency-contract "build/runs/$RUN_ID/build/agents/competency-contract.json" \
  --runtime-config "$RUNTIME_CONFIG" \
  --out "build/runs/$RUN_ID/release/runtime-evidence.json"

jq -e '
  [.cases[] |
    (.graph.actual_hop_count <= .graph.approved_max_hops) and
    (.graph.query_authority_hash != null) and
    (.graph.semantic_plan_hash != null)
  ] | all
' "build/runs/$RUN_ID/release/runtime-evidence.json"

fabric-kg validate-deployment \
  --evidence "build/runs/$RUN_ID/release/runtime-evidence.json" \
  --build-dir "build/runs/$RUN_ID/build" \
  --out "build/runs/$RUN_ID/release/deployment-validation.json"
```

Require representative competency questions to execute from persisted plans,
actual hop count to be at or below approved K, exact relationship evidence to be
returned, and no raw physical query text in diagnostics.

## 9. Acceptance record

Record only non-secret item IDs, counts, hashes, statuses, and limitations.
Mark the live smoke failed or deferred if any of these remain unproven:

- nonempty Lakehouse typed tables;
- Ontology persisted definition;
- Graph mappings and fresh projection;
- Search document counts and provenance;
- exact compile/deploy count and hash equality;
- bounded live plan execution under K.

Azure quota, tenant preview availability, or a service interruption must be
reported as pending evidence, not converted into a success-shaped result.
