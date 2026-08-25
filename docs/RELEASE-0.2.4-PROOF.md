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
3016 passed, 2 skipped, 4 deselected
```

Focused hardening result:

```text
139 passed
```

The wheel and the exact locked core/dev dependency closure were installed into
a fresh virtual environment by the package resolver from a local wheelhouse.
The wheelhouse was reconstructed from the synchronized locked environment for
offline operation; the resolver still enforced the lock and dependency
specifiers. No editable install, repository `PYTHONPATH`, or repository working
directory was used. Package/module origins and the invoked binary were asserted
under the isolated environment and outside the repository. The isolated gate
reported:

```text
fabric-kg, version 0.2.4
36 commands discovered
64 packages resolved
18 committed-fixture tests passed
```

A clean temporary installed-tool run then:

- deterministically validated an approved contract;
- compiled the committed golden canonical fixture into 14 Parquet tables;
- passed data-integrity gates;
- completed `build-deploy --dry-run` with status `planned`, a nonempty plan
  fingerprint, a resolved mutation-authority snapshot/hash, and the complete
  expected stage list;
- passed the installed golden canonical and offline end-to-end trace selectors;
- performed no Azure or Fabric mutation.

The release gate also parsed all source and test modules with Python 3.10
grammar and retains the Python 3.10 `tomli` dependency fallback. The local host
did not have a Python 3.10 interpreter cached, so an actual 3.10 runtime run
remains delegated to the existing CI 3.10 matrix.

## Artifact proof

| Artifact | SHA-256 |
|---|---|
| `fabric_kg_builder-0.2.4-py3-none-any.whl` | `e00ffb7517ea15f382fba88d15714ab4716514704f485f4273675c9915d5f54c` |
| `fabric_kg_builder-0.2.4.tar.gz` | `42f4dcc1222eb5f78c5ac4dd981daaa75205f8eb92dd647afb5be212856d39be` |

## Acceptance status

| PRD section 18 criterion | Local status |
|---|---|
| 1-11: domain, evidence, lifecycle, projection, and deployment authority | Covered by unit/contract/local fixture gates |
| 12: fresh live nonempty Lakehouse, Ontology, Graph, and Search | Pending post-merge live smoke |
| 13: live read-back with exact count equality and no unexplained drops | Pending post-merge live smoke |
| 14: generated Graph plans never exceed approved K | Covered locally; live plan execution pending |
| 15: isolated installed CLI and release tests | Passed for committed offline fixtures; real-PDF and live selectors remain pending |
| 16: all version surfaces report 0.2.4 | Passed, including isolated installed CLI |

Local compilation and mock/read-back contracts do not prove that a tenant's
live Fabric preview APIs accepted the deployment. Complete
`docs/SMOKE-TEST-0.2.4.md` after merge before reporting 0.2.4 live success.
