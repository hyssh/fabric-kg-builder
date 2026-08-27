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
ownership, ETags, actions, rollback intent, expiry, and plan hash. Signed URLs,
tokens, signing keys, raw provider errors, and credential values are forbidden.

Live deployment requires `--approve-live <exact-plan-hash>` and the matching
unexpired plan file. Before its first mutation it repeats all probes and rejects
identity, configuration, resource, ETag, audience, or definition drift.
`--resume` accepts only identical persisted state.

## Adapters

- Azure Blob provides durable L6 run and receipt authority with finite leases,
  optimistic ETag/CAS, bounded waits, crash recovery, and atomic one-time
  receipt consumption. An opaque injected signer provider owns key material.
- The RemoteTool FastAPI host publishes the same five canonical L6 schemas used
  by the L6 definition. It validates Entra auth, body size, deadlines, response
  schemas, health/readiness, and performs no synthesis.
- Foundry project connections use ARM bearer tokens in memory, exact GET
  readback, `If-Match`/`If-None-Match`, and conditional attempt-owned rollback.
  Because Foundry GET redacts `CustomKeys` credentials, L7 records a non-secret
  binding commitment for exact adoption and refuses to update a mismatched
  preexisting Fabric connection that cannot be restored safely.
- The Foundry adapter creates a version from canonical instructions, model,
  limits, Fabric connection, and exact OpenAPI RemoteTool definition, then
  checks version/hash readback.
- Fabric is readback-only here. Exact item IDs/types and configured definitions
  are verified. Unsupported Data Agent mutation fails before any other mutation.

## Receipt and release blockers

Successful deployment emits a sealed receipt with created/updated/adopted
resources, before/after ETags, readback hashes, rollback state, and bounded
remote accounting. A failed attempt cannot emit a succeeded receipt.

Release still requires an existing HTTPS host, Entra application audience,
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
