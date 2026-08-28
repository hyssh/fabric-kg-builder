# SPEC-010: L7 Production Deployment Adapter Foundation

**Status:** Implemented foundation; live acceptance deferred
**Product version:** 0.2.3

## Authority and scope

L7 accepts only `L6CanonicalAgentDefinition` and exact trusted L5a/L5b hashes.
It does not accept an arbitrary legacy agent dictionary and does not require or
activate RDF. Target configuration contains names and stable IDs only, has no
environment-specific defaults, and is validated before remote access.
RemoteTool configuration requires an explicit allowed Foundry managed-identity
object ID and application role in addition to tenant and audience.

## Plan authority

Dry-run is the default. It performs identity and GET/readback operations only,
then persists canonical JSON containing exact tenant, subscription, resource
group, principal, Fabric workspace/items, Foundry project, connection
category/target/audience, model deployment authority, L5a/L5b/L6 hashes,
canonical Fabric definition file-byte hashes, signed ownership authority,
authenticated RemoteTool readiness, ETags, actions, rollback intent, expiry,
and plan hash. Signed URLs, tokens, signing keys, raw provider errors, and
credential values are forbidden.

Live deployment requires `--approve-live <exact-plan-hash>` and the matching
unexpired plan file. Before its first mutation it repeats all probes and rejects
identity, configuration, resource, ETag, audience, or definition drift.
`--resume` accepts only identical persisted state. Authenticated readiness is
rechecked immediately before the first mutation and again before success. Plan
expiry is checked from the injected UTC clock immediately before the first and
every subsequent mutation; expiry after a mutation triggers mandatory rollback.

## Adapters

- Azure Blob provides durable L6 run and receipt authority with finite leases,
  optimistic ETag/CAS, bounded waits, crash recovery, and atomic one-time
  receipt consumption. Production Graph execution requires a configured
  deadline-aware cancellable transport with bounded connect/read timeouts,
  absolute monotonic deadline, remaining timeout, and cancellation signal.
  Deadline expiry stops renewal, atomically fails the run, releases its lease,
  and ignores late transport results. An opaque injected signer provider owns
  key material.
- The RemoteTool FastAPI host publishes the same five canonical L6 schemas used
  by the L6 definition. It validates Entra auth before consuming tool bodies,
  enforces unambiguous request framing and bounded streaming under a monotonic
  ingress deadline, then applies a separate cooperative tool deadline. It
  validates response schemas, exposes health/readiness, and performs no
  synthesis. Reverse-proxy request limits and timeouts are required
  defense-in-depth, but are not the authority for ingress acceptance; the host
  remains fail-closed.
  `/health` is non-authoritative. `/ready` requires the same Entra
  authentication and returns a short-lived, hash-sealed observation of the
  authorized caller, exact OpenAPI and L6 definition hashes, and durable
  authority backend identity; missing or mismatched authorities return 503.
- Foundry project connections use ARM bearer tokens in memory, exact GET
  readback, `If-Match`/`If-None-Match`, and conditional attempt-owned rollback.
  Because Foundry GET redacts `CustomKeys` credentials, mutable connection
  metadata is never ownership authority. Adoption requires an independently
  signed Azure Blob ownership receipt binding exact connection ID/ETag,
  category, target, audience, workspace, and Data Agent. Missing, forged, stale,
  or mismatched receipts are a collision/NO-GO. Attempt-created connections
  persist their receipt from known request and exact readback. Receipt upload is
  journaled as uncertain before I/O; every `BaseException` reconciles and
  conditionally removes the exact signed receipt before connection deletion.
- The Foundry adapter creates a version from canonical instructions, model,
  limits, Fabric connection, and exact OpenAPI RemoteTool definition, then
  checks version/hash readback. Every Azure SDK boundary normalizes the complete
  `AzureError` transport/service hierarchy to sanitized L7 errors.
- Fabric is readback-only here. Every target requires canonical expected
  definition bytes plus canonical and byte hashes, followed by POST
  `getDefinition` exact readback (including bounded LRO polling). `null`,
  existence/type-only evidence, or an item API without definition readback is
  unverifiable and blocks planning/live success.
- Every mutation is journaled before and after its adapter call. Any
  `BaseException` after the first mutation triggers all conditional rollback
  attempts, including reconciliation of journal entries whose adapter call never
  returned. Rollback errors are recorded without hiding the original
  interrupt/cancellation/failure.

## Receipt and release blockers

Successful deployment emits a sealed receipt with created/updated/adopted
resources, before/after ETags, readback hashes, rollback state, and bounded
remote accounting. A failed attempt cannot emit a succeeded receipt.

Live remains NO-GO until an existing HTTPS host returns the exact authenticated
readiness authority through a distinct credential bound to the allowed Foundry
managed identity; the deployment credential is not caller proof. Release still
requires Entra application audience,
Storage Blob data/lease permissions, Foundry project and connection permissions,
Fabric workspace read permissions, confirmation of the current preview API
surface, and installed-CLI live acceptance in the 0.2.4 successor.

## Official references

- [Fabric item definitions](https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/item-definition-overview)
- [Fabric Lakehouse REST](https://learn.microsoft.com/fabric/data-engineering/lakehouse-api)
- [Fabric Ontology definition](https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/ontology-definition)
- [Azure Blob concurrency](https://learn.microsoft.com/azure/storage/blobs/concurrency-manage)
- [Blob leases with Python](https://learn.microsoft.com/azure/storage/blobs/storage-blob-lease-python)
- [Foundry agents](https://learn.microsoft.com/azure/foundry/agents/overview)
- [Foundry OpenAPI tools](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/openapi)
