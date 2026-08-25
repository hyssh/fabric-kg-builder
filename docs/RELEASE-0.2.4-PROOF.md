# fabric-kg 0.2.4 Release Notes and Local Proof

Date: 2026-08-24

Status: Local release gates passed; post-merge live validation is required

## Release scope

0.2.4 adds the schema-2 Copilot domain workflow and closes the code-level
recovery defects recorded in issue #29:

- cited domain proposals with explicit approval of bounded relationship
  vocabulary `N` and traversal depth `K`;
- exact locally verified evidence for asserted relationships;
- explicit asserted, unresolved, rejected, and discovery lifecycle handling;
- raw audit surfaces separated from the sealed semantic serving projection;
- Ontology, Graph, Search, package, deployment, and runtime authority bound by
  hashes and receipts;
- bounded persisted Graph query plans and runtime execution;
- full-pipeline resume fingerprints rather than semantic-only invalidation;
- authoritative ARM endpoint preservation for adopted Azure services;
- canonical `DocumentChunk.entity_id` to physical `chunk_id` remediation;
- sanitized per-work-unit exception category, message, identity, and retry
  history;
- package, lock, CLI, API, plugin, and marketplace version alignment at 0.2.4.

Schema-1 contracts remain readable through their existing compatibility path.
0.2.4 does not add automatic migration or external ontology discovery.

## Local release gates

The final proof run must record:

```text
uv run pytest tests/unit tests/contract -m "not slow and not integration" -q
uv build
UV_TOOL_DIR=<temporary> UV_TOOL_BIN_DIR=<temporary> uv tool install --force .
fabric-kg --version
fabric-kg --help
fabric-kg <every top-level command> --help
scripts/smoke-0.2.4-local.sh
```

Full fast suite:

```text
2985 passed, 2 skipped, 4 deselected
```

Focused hardening result:

```text
247 passed
```

The wheel was installed non-editably into a fresh isolated virtual environment.
Because the implementation environment was offline and one locked transitive
wheel was absent from the uv artifact cache, the equivalent validation copied
the already locked dependency environment and installed the built wheel with
`--no-deps`. The product package itself was not editable. The isolated command
reported:

```text
fabric-kg, version 0.2.4
36 commands discovered
```

A clean temporary schema-2 fixture run then:

- deterministically validated an approved contract;
- compiled 2 raw relationships into 1 asserted semantic relationship while
  retaining the audit row;
- wrote 14 canonical/semantic Parquet tables;
- passed data-integrity gates;
- completed `build-deploy --dry-run` with status `planned`, a nonempty plan
  fingerprint, and the complete expected stage list;
- performed no Azure or Fabric mutation.

## Artifact proof

| Artifact | SHA-256 |
|---|---|
| `fabric_kg_builder-0.2.4-py3-none-any.whl` | `861918f7a15d32134fef14269a2cfd7ff42de67ddf855326ff0aa141b4bb96a8` |
| `fabric_kg_builder-0.2.4.tar.gz` | `8293f19a8b6400d07a1dac5bcf4454450cc9e6abdc87377701206e48498125ca` |

## Acceptance status

| PRD section 18 criterion | Local status |
|---|---|
| 1-11: domain, evidence, lifecycle, projection, and deployment authority | Covered by unit/contract/local fixture gates |
| 12: fresh live nonempty Lakehouse, Ontology, Graph, and Search | Pending post-merge live smoke |
| 13: live read-back with exact count equality and no unexplained drops | Pending post-merge live smoke |
| 14: generated Graph plans never exceed approved K | Covered locally; live plan execution pending |
| 15: isolated installed CLI and release tests | Passed locally; environment-dependent live checks remain pending |
| 16: all version surfaces report 0.2.4 | Passed, including isolated installed CLI |

Local compilation and mock/read-back contracts do not prove that a tenant's
live Fabric preview APIs accepted the deployment. Complete
`docs/SMOKE-TEST-0.2.4.md` after merge before reporting 0.2.4 live success.
