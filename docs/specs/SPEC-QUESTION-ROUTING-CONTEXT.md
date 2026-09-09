# Question execution context

Status: implementation specification, 2026-09-09.
This refines roadmap track A; the multi-ontology work in track B stays deferred.

## Decision

When collecting the domain, questions, expected answers and background, distinguish
ontology/graph retrieval from numerical, count and trend analysis best handled by
Lakehouse SQL. Preserve the relevant decision and data requirements as execution
context through the pipeline.

This is not a blanket ban on numbers, a new SQL execution engine, or a request to
create quantity/metric/trend entities merely to satisfy ontology question coverage.
Ordinary source facts, source quotations, identifiers, safety conditions and
structural ordering metadata remain intact.

## Information to retain

For each explicitly routed question, retain its original ID, text and criticality,
the selected backend, analytical operation when applicable, routing rationale,
population/scope, counting or aggregation grain, filters, time context and required
source information. Unknown requirements remain unknown.

Classification depends on intent and expected answer shape, not keywords alone.
A question containing a model number, SKU or safety threshold is not necessarily
an analytical question. Mixed requests must preserve both retrieval scope and
analytical requirements, or explicitly require review.

Keep this information outside ontology entities and relationships. It is query
planning context, not an asserted fact, SQL statement, actual table binding,
computed metric, or authorization to run a query.

## Authority and compatibility

- Explicit intake routing must not be silently overwritten by model proposals.
- Model-proposed routing remains part of an unapproved design until the existing
  review/compile/approval boundary accepts that exact context.
- Keep SQL-directed questions visible and business-critical when the user made
  them critical. Do not remove them or mark them answered to satisfy graph gates.
- Questions delegated to SQL do not need invented graph paths or ontology
  quantity/unit properties. Graph coverage and SQL execution readiness are
  separate states.
- Preserve historical artifact serialization and hash behavior when no routing
  metadata exists. New context is typed/versioned and bound to the relevant
  artifact/approved domain hash.
- Reject unknown question IDs, conflicting authoritative routing and tampered
  context. Never reseal an older approval against altered requirements.

## Producer/consumer flow

| Stage | Context responsibility |
|---|---|
| Intake / design | Capture explicit context or propose a backend with rationale; preserve source and business background |
| Design evaluation | Report ontology gaps separately from SQL data/binding requirements |
| Compilation / approval | Seal the reviewed context with the domain; preserve original questions and criticality |
| Extraction | Carry approved context in requests and request identity; do not reinterpret SQL requirements as new ontology vocabulary |
| Evidence / serving | Retain the same approved context through existing sealed domain authority; do not fabricate analytic fact rows |
| CLI export / agent | Expose backend and data requirements, explain missing bindings and choose the appropriate execution surface |

Reuse existing sealed-domain propagation rather than copying independent,
unbound context files at every stage. If a sidecar/export is needed, include
the source artifact/domain identity and context hash.

## Runtime boundary

Choosing `lakehouse_sql` means the question should be handled there; it does not
prove that the required SQL tables, columns, joins, permissions or connection are
available. Until actual bindings are verified, readiness remains unresolved.

No SQL execution is introduced by this change. A graph-only executor must not
silently answer a SQL-routed question through a substitute graph traversal.
Export or forward the context to the configured SQL-capable consumer, or report
the unavailable capability explicitly.

Counting connected records requires a defined population and grain. Distinct SKU
counts and physical occurrences are not interchangeable. Deduplicate by the
appropriate governed identity; do not count truncated conversational output.
Trend requests need a time basis and scope. Missing source multiplicity or
timestamps must not be replaced by invented values or implicit zeroes.

## Acceptance

1. Explicit SQL routing survives intake, design, compilation and approval without
   altering the original question or its criticality.
2. Model-proposed routing is visible for review and cannot override explicit
   intake routing silently.
3. A mixed graph/SQL domain compiles without storing quantities solely for the
   SQL question or inventing a graph answer path for it.
4. SQL population/grain/filter/time/source requirements reach the extraction
   request and the serving/agent context with matching authority bindings.
5. CLI context discovery/export is read-only and does not call a model or SQL.
6. SQL readiness is not inferred from ontology approval or context presence.
7. Existing unrouted contracts retain their serialization/hash behavior.
8. Unknown IDs, conflicting routing and tampering are rejected.
9. Numeric identifiers, source facts and ordering metadata are not blanket-banned.

Developer regressions use the existing pytest runner. Public CLI checks verify
the exported context and approval boundaries. Offline provider tests are not
evidence of executed Lakehouse SQL or deployed Fabric answer correctness.
