# fabric-kg 0.2.4 Release Proof

## Candidate

- Version: `0.2.4`
- Base: `bfb9f2b24ff820174267932bf1dd3171788077a0`
- Acceptance runtime: installed `fabric-kg` wheel in an external Python 3.12 virtual environment
- Top-level command inventory: 36

## Scope

The release candidate adds `fabric-kg app deploy-l7`. It consumes only files and
configuration, defaults to GET-only dry-run planning, emits an immutable
sanitized plan, and requires `--live --approve-live <exact-plan-hash>`.
For an explicitly authorized one-shot live test, `--live` performs the complete
read-only preflight, persists its exact plan/hash, and immediately executes that
same plan without a prompt. Supplying `--approve-live` instead consumes only the
matching persisted plan.

The live plan covers exact Fabric definition readback, release-owned Azure AI
Search index/knowledge source/knowledge base names, and release-owned Foundry
Search/Fabric Data Agent connections. Existing `surface-tech-*` resources are
never adopted or modified by name.

## Deferred

Public RemoteTool hosting, Blob-lease multi-process L6 authority, signer
rotation/revocation, and RDF serialization are deferred. The canonical L6
five-tool definition remains generated/local. Foundry may use only supported
built-in Search and Fabric Data Agent connections; the proof must not claim the
deferred RemoteTool path is deployed.

## Acceptance Results

Record the final candidate SHA, archive hash, wheel hash, sdist hash, test
counts, external package origin, plan hash, and sanitized receipt hash here.
Do not paste access tokens, secrets, connection strings, or user configuration.

## Environment and Administrative Blockers

Record exact capability NO-GO results, including missing Search managed-identity
roles, unsupported Fabric `getDefinition` operations, or unavailable exact
Foundry rollback. A direct Search fallback does not satisfy preview agentic
success.

For a reproducible product defect, preserve the sanitized JSONL/receipt, analyze
the failing causal stage and rollback status, and open a GitHub issue with the
installed CLI version, candidate SHA, stable resource types, hashes, HTTP status
classes, and reproduction command. Never attach credentials or source content.
