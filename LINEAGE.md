# Historical lineage lookup

The lineage archive and publication-binding commands provide historical,
read-only lookup. They do not replace `lineage trace`, change extraction or
evidence support, promote candidates, or approve publication. Local snapshot
identity, stored-record verification, field-contributor completeness and
authorized access to original bytes are separate guarantees.

## Create an archive

Retain an intact, historically approved L1/domain/L2/L3/L4 bundle and the
`tables/` and `definitions/` produced by the prototype dry-run materializer.
Use the same workspace and prefix that produced that directory.

```bash
fabric-kg lineage archive \
  --l1-state .fkg/l1 \
  --domain approved-domain.yaml \
  --l2-state .fkg/l2 \
  --l3-root .fkg/l3 \
  --l4-run .fkg/l4/runs/SEALED_RUN_ID \
  --publication build/prototype/materialized \
  --workspace-id 00000000-0000-0000-0000-000000000001 \
  --name-prefix demo \
  --publication-version local-release-001 \
  --source-root /authorized/source-snapshot \
  --layout-cache /authorized/cache/CACHE_KEY.json \
  --out /authorized/archive/local-release-001
```

The UUID and paths are placeholders, not approved live targets. This command
recompiles the default publication offline and compares every typed table's
multiset/schema and each definition. Alternate or additional table/definition
scopes, including unconfigured derived/quality/fallback variants, are refused.
It does not deploy, call DI or invoke a model.

`--source-root` and repeatable `--layout-cache` files are optional and explicitly
authorize reading those inputs. Originals must use corpus-listed safe relative
paths and match the sealed SHA256 and byte count. Cache input/config/response
hashes are verified. Paths and URIs embedded in raw cache fields are never
followed; nothing is automatically downloaded. Without a source root, metadata can be retained
without copying originals; lookup reports `NOT_ARCHIVED` until an explicit
authorized source root is supplied.

The archive retains publication tables/definitions, approved domain and corpus,
SourceUnits, verified evidence definitions, candidate/batch records, L4 audit,
ordered L1-L4 receipts and consumed manifests. Missing or altered evidence is
rejected at creation. Original content and raw cache data are private runtime
artifacts: **do not commit an archive or its originals to Git**.

The create-only output must not overlap input roots, stage authority, release
state, registry or caches, including symlink aliases. Existing destinations,
including empty directories or dangling links, are refused. The no-replace
writer uses private directory/file permissions of 0700/0600 and flushes file
contents before publication. It does not configure encryption, backups, tenant
ACLs, retention, remote replication or crash-recovery guarantees.

Retain the returned `archive_hash` in a separately trusted operator index or
release receipt. A self-hash detects modification, not a malicious rewrite of
both manifest and contents. Pass it as `--archive-hash` for local lookup: that
option is not required by the local CLI, so omission does not establish an
independently pinned identity. Binding creation requires it. An archive alone
is not a live Fabric binding.

## Resolve without producer directories

Copy the archive without changing its bytes. No producer stage directories or
current working domain contract are consulted:

```bash
fabric-kg lineage resolve \
  --archive /consumer/archives/local-release-001 \
  --archive-hash TRUSTED_ARCHIVE_HASH \
  --publication-version local-release-001 \
  --table l5a_type_SEMANTIC_TYPE \
  --record CANONICAL_ENTITY_ID \
  --field PHYSICAL_PROPERTY_COLUMN
```

Use the physical table/column mapping in archived `definitions/parquet.json`
and `tables/*.parquet`. Omitting `--field` resolves a record; relationship IDs
are also supported. The carried `l4_semantic_asserted_*` tables support lookup
by their canonical entity/relationship/property assertion ID. Unknown keys,
unsupported versions, version mismatch and missing, modified or symlinked
metadata are explicit errors, not empty success.

CLI stdout contains only JSON; dependency diagnostics go to stderr. JSON inputs
and indexes are bounded to 128 MiB each, other local members/source bytes to
1 GiB each. The local backend reads one bounded archive in memory.

Optional `--source-root /authorized/source-snapshot` verifies originals in that
explicit root instead of archived copies. Missing, denied or changed originals
under the supplied root never fall back to archived bytes. Source results are `VERIFIED`,
`NOT_ARCHIVED`, `MISSING`, `DENIED` or `VERSION_MISMATCH`. `DENIED` requires an
actual permission failure when opening bytes. Unsafe paths and size violations
are explicit errors.

| Output | Meaning |
| --- | --- |
| `metadata_status` | Assertion/evidence/SourceUnit/version linkage, not source access. |
| `field_binding_status` | Exact field-to-assertion selection or an explicit incomplete reason. |
| `resolution_status` | Complete record/field linkage only when every required link is present. |
| `source_bytes_status`, `original_source_retrieval_verified` | Actual local bytes opened and hashed against the recorded version, not quote re-extraction. |
| `raw_location` | Recorded locator and bounded cache-to-raw mapping, not visual understanding. |
| `local_source_location` | Geometry derived from verified PDF bytes, separate from DI and assertion truth. |
| `process_receipts`, `candidates`, `audit` | Recorded stage identities and available candidate/merge contributors, not a reconstructed model recipe. |
| `publication_authority`, `live_fabric_verified` | Always false for local `lineage resolve`. |

Typed property lookup uses archived physical mappings and exact L4 property
assertions, never value-equality search. Multiple possible property contributors
remain `INCOMPLETE_PROPERTY_CONTRIBUTORS`; the resolver does not guess the winner.
Differing raw/normalized values without an archived normalization recipe remain
`INCOMPLETE_EXTRACTION_NORMALIZATION`. Unsupported derived/administrative
columns remain incomplete even when their values match.

To inspect a nonasserted or rejected candidate, use the same archive/hash/version
arguments with `--table audit_candidates --record CANDIDATE_ID`. The result is
`AUDIT_ONLY`, with original source-unit provenance and lifecycle audit but no
serving promotion or verified assertion evidence.

## Bind a completed publication

After a separately approved successful create-only prototype publication, reuse
its exact approved plan and completed journal:

```bash
fabric-kg lineage bind-publication \
  --archive /authorized/archive/local-release-001 \
  --archive-hash TRUSTED_ARCHIVE_HASH \
  --plan build/prototype/plan.json \
  --journal build/prototype/journal.json \
  --out /authorized/provenance-index/publication-001
```

This command makes no cloud calls. It checks plan/hash, historical archive
compilation/definitions/tables, completed structural publication and Ontology
readiness, returned workspace/Lakehouse identity, default `dbo` schema, exact
Delta paths/scope, integer Delta version `0` and journal table/readback proofs.
Partial, operator-reconciled, altered, different-scope or unverified publications
fail closed. Arbitrary legacy/adopted resources and historical formats are not
supported; missing provenance is not reconstructed from current row values.

The create-only private output contains `binding.json` and exact `plan.json`
and `journal.json` bytes. Retain the returned `binding_hash` in a separately
trusted operator/publication index. Its lookup identity is workspace, Lakehouse,
`dbo` table and Delta version; record and optional field are supplied at lookup.
Copy both archive and binding directories without changing bytes to relocate.
No registry service, retention backend, tenant ACL or encryption is provisioned.

A caller-supplied plan/journal is not a Fabric signature or independent remote
attestation. Matching self-hashes do not authenticate a maliciously rewritten
index. Binding does not perform readback, grant source access, approve
publication or promote evidence. An external trusted `binding_hash` is mandatory.

For automatic binding after an already separately approved
`app publish-structured --prototype-create-only --live` operation, add all three
options to the existing invocation:

```text
--lineage-archive /authorized/archive/local-release-001
--lineage-archive-hash TRUSTED_ARCHIVE_HASH
--lineage-binding-out /authorized/provenance-index/publication-001
```

The existing approved `--plan`, `--prototype-journal` and `--materialize` inputs,
exact `--approve-live PLAN_HASH` and limitation approvals remain required.
Archive/plan and disjoint-output preflight occurs before publisher side effects;
binding is created only after the completed journal validates. If post-publication
binding fails, resources are retained: correct the local inputs and retry
standalone `lineage bind-publication`, not publication/redeployment. Omitting
these options preserves normal publication behavior.

## Resolve stored identity

```bash
fabric-kg lineage resolve-stored \
  --binding /consumer/provenance-index/publication-001 \
  --binding-hash TRUSTED_BINDING_HASH \
  --archive /consumer/archives/local-release-001 \
  --workspace-id 00000000-0000-0000-0000-000000000001 \
  --lakehouse-id 00000000-0000-0000-0000-000000000002 \
  --table l5a_type_SEMANTIC_TYPE \
  --delta-version 0 \
  --record CANONICAL_ENTITY_ID \
  --field PHYSICAL_PROPERTY_COLUMN
```

By default this validates the binding and resolves archived metadata without
target discovery, credential creation or Fabric reads:
`stored_readback_status=NOT_RUN`, `stored_record_verified=false` and
`live_fabric_verified=false`. Local `publication-version` and integer remote
`delta-version` are separate identities.

Only after identifying the exact authorized target and approving its reads,
repeat the same command with:

```text
--read-remote --approve-read TRUSTED_BINDING_HASH --max-rows 10000
```

The built-in read-only backend uses the current Azure CLI credential's storage
token for the bound OneLake path and pins Delta version `0`. It checks exact
commit ownership/version and publisher run/plan/table action metadata, compares
the entire typed table's schema/multiset to its approved proof, then compares the
unique canonical record and field value. It never substitutes latest rows.
History is read from the log head and bounded to 100 commits. Missing history,
ownership/version/value/schema drift, denied reads and backend failures stop
explicitly; archived values never stand in for readback.

The full-table cap defaults to 10,000 rows and can be raised to 100,000. Larger
tables fail rather than reporting partial-table success. Exact comparison may
set `stored_readback_status=VERIFIED`, `stored_record_verified=true` and
`stored_field_verified=true` for a supplied field. Only the built-in OneLake
backend marks `live_fabric_verified=true`; injected readers remain false. This
does not verify Ontology queries, semantic truth, every contributor or a
normalization recipe.

A verified stored value may still have incomplete contributor/normalization
lineage or unavailable original bytes. Explicit source-root failures do not fall
back to archived originals.

## Location precision and limits

The cached-reader bridge requires the exact `CACHE_KEY/pages/PAGE` locator,
`docintel-pages/1.0.0`, matching input byte hash and explicitly archived
`CachedLayout`. A single contiguous `unicodeCodePoint` page span can map
source-relative assertion offsets to raw offsets only when whitespace-stripped
text matches without NFC changes. Recorded table-cell row/column/merge spans,
figure IDs, polygons and page dimensions/units are retained.

`exact_regions` means exact offset overlap, not that an assertion describes that
cell or figure. Other regions are `page_context_regions`; caption overlap alone
is not image evidence. `claim_to_region_verified` and
`assertion_trust_promoted` remain false. Unsupported index conventions,
noncontiguous spans, changed normalization without offset mapping, absent caches
and missing geometry/locators remain unsupported, incomplete or unavailable.

For opened, hash-verified PDF originals, the local PyMuPDF backend locates an
exact case-sensitive whitespace-token phrase on the recorded 1-based page.
It reports unique/ambiguous/not-found outcomes, native text boxes, dimensions,
coordinate convention and backend version. Detected table cells and embedded
images are page context, not inferred field contributors. Merged-cell extent
and vector-only figure interpretation are not inferred. Parsing does not occur
without authorized/archived bytes or after source access/hash failure; parser
failures are explicit errors.

Table `bbox` preserves the backend-detected region, which can vary by parser
version. A separate `native_rule_enclosure_bbox` requires original stroked,
axis-aligned line/rectangle paths continuously enclosing all four nearest sides
within 0.01 PDF-point tolerance, with source drawing/item/edge indices retained.
Gapped, fill-only or non-axis-aligned boundaries remain `NOT_PROVEN`. Text
extents are never promoted to cell borders. This proves native rule geometry,
not semantic cell identity, merge extent, rendering visibility or absence of
decorative frames; `semantic_cell_boundary_verified` remains false.

Operators must separately choose archive/index storage, a trusted
deployed-version-to-binding-hash index, retention/access/encryption policy,
authorized original-retrieval adapters and supported historical schemas.
Do not silently migrate an old archive into current serving trust.
