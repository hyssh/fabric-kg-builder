# Release 0.2.3 Solution Proof

Date: 2026-08-23

## Release gate

```text
uv run pytest tests/unit tests/contract -m "not slow and not integration" -q
2694 passed, 4 deselected
```

The release packages were built offline from the tested tree:

| Artifact | SHA-256 |
|---|---|
| `fabric_kg_builder-0.2.3-py3-none-any.whl` | `6d61bed8afac1baeaba4c963463c5f29fea530c034dff4e6bba417da7b625c9f` |
| `fabric_kg_builder-0.2.3.tar.gz` | `ebe0c81af96f26f6dcdb7a08ff61be58b842f75b90c49bb17106589a7e484db2` |

The Lakehouse deployment mock completed successfully with all 19 SQL-facing
tables using explicit scalar projections and no live Fabric mutation.

## Issue #25: Data Agent LRO diagnostics and cleanup

### Implemented proof

- `DataAgentLroFailedError` preserves the operation URL, status, complete
  response body, response headers, request ID, and elapsed time.
- Failed create LROs resolve and delete any newly created Data Agent shell.
- Publication/read-back failures clean newly created targets.
- Cleanup failures preserve the original operation diagnostics and report both
  failures.

### Regression proof

- A failed `UnknownError` LRO retains its operation and request identifiers.
- A failed create LRO with a visible shell issues an exact-item `DELETE`.
- Existing publication receipt and persisted read-back tests remain green.

### Closure decision

Keep open until one live Fabric create/update failure confirms the diagnostic
payload and verifies that no empty Data Agent remains in workspace inventory.

## Issue #26: SQL endpoint-incompatible ARRAY columns

### Implemented proof

- Every default Lakehouse table now has an explicit scalar-only projection.
- Native list columns such as `aliases`, `search_aliases`, and `evidence_ids`
  are excluded while scalar JSON alternatives remain available.
- A pre-write Arrow schema gate rejects list, large-list, fixed-list, map,
  struct, and union fields before Delta mutation.
- Schema failures remain per-table results so partial deployment evidence is
  not discarded.

### Regression proof

- Projection tests verify native list fields are absent.
- A nested field is rejected before `write_deltalake` is called.
- Scalar projected tables still write successfully.
- The deployment mock lists scalar column counts for every default table.

### Closure decision

Code-level root cause and regression coverage are complete. Close when the
0.2.3 fix is merged; a live SQL endpoint synchronization smoke remains useful
release evidence but is not required to prove the unsupported type cannot be
written by this path.

## Issue #27: Graph diagnostics, path validation, and recovery deployment

### Implemented proof

- `deploy-serving` now surfaces captured Graph deployment errors and partial
  failures before persisted readiness checks.
- Failure output includes configured and returned Graph Model IDs.
- `dataSources/1.1.0` validation rejects unknown item references, absolute
  `abfss://` paths combined with `referenceName`, and malformed relative
  Lakehouse paths.
- `deploy-graph` accepts compiled `graph-definition.json` and its label catalog,
  allowing recovery without rebuilding from semantic Parquet tables.

### Regression proof

- Relative `Tables/<schema>/<table>` paths pass validation.
- Absolute paths combined with a Lakehouse reference fail before deployment.
- `deploy-graph --graph-definition-file ... --dry-run` succeeds.
- Existing Graph compilation and semantic artifact tests remain green.

### Closure decision

Keep open until a live schema-enabled Lakehouse Graph update confirms the
tenant's accepted relative path and demonstrates that a Fabric validation LRO
is printed without masking.
