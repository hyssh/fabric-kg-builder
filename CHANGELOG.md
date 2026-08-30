# Changelog

## 0.2.4

- Fixed L3 discarding almost all verifiable evidence. Model-authored anchor
  offsets were trusted verbatim, so a candidate was rejected whenever the model
  miscounted code points even though its quoted text appeared verbatim in the
  SourceUnit. On a 14,947-unit corpus this rejected 40,175 of 46,304 candidates
  with `EVIDENCE_QUOTE_MISMATCH` and left the accepted set empty. The anchor is
  now untrusted for arithmetic as well as identity: when the proposed bounds do
  not already delimit the quote, the bounds are re-derived from the exact NFC
  source text and accepted only when the quote occurs exactly once, recording
  the informational `EVIDENCE_ANCHOR_RELOCATED` reason code. Ambiguous quotes
  and quotes absent from the text stay rejected, and every minted span still
  satisfies `text[start:end] == quote`, so this strengthens rather than relaxes
  the evidence contract. The extraction verifier and L3 validator versions move
  to `1.1.0` accordingly, which also invalidates stale leaf checkpoints.
- Activated the schema-2 L3 and L4 stages in the CLI as `validate-evidence`
  and `project-serving`. Both were already implemented and tested but had no
  entry point, so a completed schema-2 L2 handoff could not be carried any
  further. `validate-evidence` verifies every L2-proposed candidate against its
  recorded source text and mints evidence spans, reusing leaf checkpoints on
  re-run; `project-serving` projects a validated result into the canonical
  audit and asserted-only serving Parquet tables. Both stages are local and
  make no LLM, Foundry, Document Intelligence, embedding, Search, or Fabric
  call. Schema-2 keeps its own serving shape rather than being down-converted
  into the schema-1 `build/enriched`/`compile-data` tables, which cannot
  represent assertion state, publication authority, or required-member
  manifests.
- Added strict `app deploy-l7` planning with dry-run as the default, immutable
  plan hashing, exact live approval, expiry/drift gates, and sanitized rollback
  receipts.
- Added exact Foundry project-connection bearer authentication and readback.
- Added Python 3.12 external installed-CLI acceptance tooling and release proof
  templates.
- Kept the 0.2.4 acceptance path local and CLI-only.
- Hardened long-running schema-2 enrichment: transient Foundry transport
  failures (connectivity, timeout, throttling, server faults) are retried with
  bounded backoff instead of aborting a resumable run, while request,
  authentication, authorization, and validation failures still fail fast.
  A single empty or unparseable model completion is likewise retried and, if
  it persists, reported as an exact empty-completion failure rather than an
  opaque JSON parse position.
- Survived sustained provider outages during schema-2 enrichment. Beyond the
  request-local retry budget, a shared circuit breaker applies bounded delayed
  backoff across every concurrent worker, so a multi-minute outage no longer
  terminates a checkpoint-resumable stage and no worker burns an independent
  budget. The breaker remains bounded — `FABRIC_KG_FOUNDRY_OUTAGE_BUDGET_SECONDS`
  (default 900) caps the total wait, after which queued work fails fast with
  the underlying transport error as its cause. Deterministic request, identity,
  and authority failures still bypass the breaker entirely, and sanitized
  outage counters are written to `enrichment-metrics.json`.
- Recorded the real L1 failure cause in the early-failure audit and error, with
  bounded, secret-redacted detail instead of a bare code.
- Made the L1 state commit crash-safe: a commit journal reconciles an
  interrupted `domain.yaml`/state-root rename on the next run instead of
  silently discarding the retained backup.
- Reported `app deploy-l7` failures honestly: the failure event distinguishes
  `preflight` from `execution`, states `mutation_possible`, and advertises
  receipt paths only when receipts were persisted. Azure AI Search `401`/`403`
  readback failures now name the endpoint and the exact roles required.
- Surfaced the actual reason for every `init-domain` schema-2 precondition
  failure instead of a bare `L1_STAGE_FAILED` code.
- Treated the L1 commit journal as untrusted input: reconciliation derives the
  backup root itself, requires the journal to describe the current commit, and
  never deletes or relocates a path the journal names.
- Extended failure-detail redaction to quoted values, storage/service-bus
  connection keys, URL userinfo, and bare JWTs.
- Allowed `app deploy-l7` to prove the direct Azure AI Search index path when
  the Search managed identity lacks its Foundry role, via an explicit
  `search.agentic_components: "deferred"` opt-in. The preview knowledge source
  and knowledge base are then reported as deferred components and are never
  created or claimed as successful; the default remains a capability NO-GO, a
  release-owned name collision is still a NO-GO, and the index itself can never
  be deferred.
- Recorded why a live `app deploy-l7` mutation failed: the rollback receipt now
  carries a bounded `failure_cause` and the raised error names it, instead of
  reporting only that rollback completed.
- Compared Azure AI Search readbacks against the shape the release actually
  declared. The service populates every unset property on create, so requiring
  a verbatim echo of the submitted index could never succeed; declared values,
  missing declared keys, and list-length drift are all still rejected.
