# Schema-2 prototype integration contracts

Status: frozen prototype boundary, revision 7. Changes require a new revision and
producer/consumer fixture changes before dependent implementation continues.

This is the implementation contract for the local prototype, not a claim that
all supported cloud deployments already work. DomainContractV2 remains the
business authority. Existing C0 contracts retain their independently versioned
formats. Existing safety and capability gates must not be weakened.

For CLI 0.2.6, [the design-first specification](SPEC-0.2.6-DESIGN-FIRST.md)
adds a separate unapproved draft and evaluation boundary before this contract.
Question gaps may exist in that draft without invalidating its structure.
They do not relax the approved DomainContractV2 or evidence rules below.

## Scope and authority

The supported lifecycle is provisional design, document assessment, explicit
review, a new unapproved proposal, approved extraction, evidence validation and
typed projection. Assessments and change plans never assert facts, approve a
domain, mutate deployed resources or become extraction evidence.

Local development uses the current checkout, task-labelled commits and no push.
Only the coordinator mutates Git state. Existing user changes are preserved.

## B1: source and assessment input

Reuse the complete `SourceCorpusManifest`, immutable snapshots, normalized
`SourceUnit` and exact codepoint evidence primitives. Assessment accepts a valid
draft or approved DomainContractV2; extraction still requires approved L1.

Every corpus entry has an explicit disposition. Every assessed text window has
a source-file ID, source-unit ID, start/end codepoints, text hash and model-input
fingerprint. Budget-excluded windows are deferred, not assessed successfully.
Unsupported/image-only inputs must not be described as successfully OCR-read.

Optional DI cache successor: `contract_version="1.1.0"`, `input_sha256` over the
original bytes, nonsecret `extractor_identity` (endpoint identity, API, model,
options), losslessly encoded `analyze_result_json`, its byte `response_hash`, and
`cache_key=canonical_sha256({input_sha256, extractor_identity})`.
Encode raw JSON with sorted keys, ASCII escapes, compact separators and finite
numbers; do not NFC-normalize raw keys/content. Expose decoded `analyze_result`
to consumers. Canonical SourceUnit normalization happens after original span
selection. Legacy 1.0 caches require explicit version-aware handling; do not
silently reseal historical hashes or start a paid reanalysis.
Loading requires an exact request identity and validates the response hash.
Writing is create-only; an identical record may be reused, a different record
at the same key is an error. Never search by filename or use a response from
another version/options tuple. No automatic cloud reanalysis on cache miss.
Page text derives from validated raw content spans through the existing DI
normalizer. Missing page spans may yield full content with page null, never a
fabricated page number. Preserve complete raw geometry/tables for later replay.

`domain analyze-layout` is a separate explicit producer: default read-only plan,
`--live` permits one new analysis POST using AzureCliCredential, with whole-file
page and byte caps. It uses no API-key fallback and never silently selects only
some pages. Exact cache reuse makes no new analysis POST. Ambiguous interrupted
analysis leaves a reservation that blocks blind retries; an explicit 401/403
is surfaced as access denied rather than retried.

Small inputs may fit one context. Larger inputs are bounded into windows with
explicit coverage. This prototype does not claim semantic completeness or
cross-document conflict detection merely from processing all windows.

## B2: persisted property carrier, additive successor

The existing raw property candidate already contains `owner_local_id`, `value`,
`normalized_value` and `temporal_key`. Preserve them using:

| Layer | Fields |
|---|---|
| L2 ProposedCandidateRecord / L3 ProposedCandidateView | `proposed_owner_entity_id`, `value_json`, `normalized_value_json`, `temporal_key` |
| L3 PropertyObservationRecord | `entity_id`, `value_json`, `normalized_value_json`, `temporal_key` |
| L4 semantic_asserted_properties | `entity_id`, `normalized_value_json` in addition to existing assertion/type/evidence/hash fields |

All added carrier fields are `str | None = None` for historical reading.
An asserted property requires a nonempty owner ID and both scalar JSON strings.
An asserted L4 row requires owner ID and normalized scalar JSON.

Use existing `canonical_json`, preserving JSON scalar type. New scalar values
may be strings, integers, finite numbers or booleans. Reject JSON null,
containers and nonfinite numbers. Missing historical proof is Python None, not
an asserted JSON null. Zero, false and empty string are not absence.
The approved property declaration supplies value_type; the model does not.
ISO date/datetime values are represented as strings and validated against the
approved declared type. Temporal key is not an inferred observed_at timestamp.

Do not add a redundant owner-type field. Derive it from validated classification.
Keep current normalized business-key persistence and semantic-ID algorithms.
Recompute property identity from owner, approved property ID, normalized value
and temporal key. All carrier fields participate in payload/version integrity.

## B3: property assertion proof

Persistence alone is not source proof. Assertion requires:

1. Owner entity exists and is asserted under the same approved authority.
2. Property is declared/effective for the owner's validated classification.
3. The exact evidence quote is verified against the trusted SourceUnit.
4. Owner is grounded in that quote under existing endpoint-grounding rules.
5. The observed scalar is supported by the quote: literal string occurrence,
   or a whole scalar token for numbers and boolean literals. No fuzzy matches,
   substring matches inside larger numeric tokens, or yes/no inference.
6. Raw and normalized canonical JSON are equal in this prototype. Different
   values remain unsupported until a separately specified transformation proves
   the conversion; do not silently trust model normalization.
7. Declared value-type validation and property-identity recomputation succeed.

Empty strings without explicit source proof remain non-asserting. A quote that
mentions only an owner cannot prove a supplied number. Existing negative
owner-only fixtures must remain negative.

The rule is deliberately conservative. It may abstain on valid normalization;
report unsupported outcomes rather than inventing conversions.

## B4: typed projection

L4 requires complete owner/value proof, asserted owner presence and agreement
among records grouped into the same assertion. L5a joins properties by
`(entity_id, semantic_property_id)`.

Identical normalized values may coalesce while retaining assertion/evidence
provenance. Distinct values are an explicit conflict; never choose first/last.
Populate type columns and relationship endpoint-key columns from validated
properties, not from labels or an identity hash.

Optional unobserved properties may be null. Missing required properties or
required endpoint keys fail publication explicitly. Do not reconstruct them
from business keys without property evidence.

Reuse existing Arrow normalizers: strict int64 excluding booleans, finite
float64 excluding booleans, boolean, string, ISO date and timezone-aware UTC
datetime. Reject incompatible JSON encodings and noncanonical representations.

## B5: version and legacy rules

- L2 extractor successor: 1.3.0; bind actual carrier shape into schema identity.
- L2 proposed-candidate partition successor: 1.1.0.
- L3 validator successor: 1.2.0.
- L3 property-observation successor: 1.1.0.
- L4 projection-code successor: 1.1.0.
- L5a publication-code identity: `l5a-publication/1.1.0`, independent of the
  package release label; it changes when materialization semantics change.

Update producer declarations and matching readers together. Preserve raw field
presence when validating historical hashes; default-null additions must not
rewrite old payloads. Unsupported historical formats fail with explicit
re-extraction guidance, not fabricated or re-sealed receipts.

Legacy rows without owner/value proof remain non-asserting. New projection
schemas invalidate old projection caches. Owner identity/classification is part
of property validation dependencies. Domain and C0 assertion schema versions do
not change solely to carry fields already represented by their contracts.

## B6: document-assessment and review artifacts

New domain-local records have `contract_version: "1.0.0"` and canonical hashes.
They are not registered C0 assertions.

Assessment identity binds domain contract hash, corpus manifest hash, model
identity, prompt version, window parameters and budget limits.

An accepted finding carries category, summary, suggested action, exact quote,
source file/unit identity, codepoint range, affected known question IDs and
known semantic IDs. IDs and evidence positions are locally minted/verified.
Unknown existing-reference IDs, fabricated quotes and malformed model responses
are errors, not successful empty findings.

Categories: `ontology_gap`, `source_conflict`, `extraction_risk`, `out_of_scope`.
Suggested actions are recommendations, never mutation permission.
Window states: `assessed`, `deferred`, `failed`, `unsupported`.
Aggregate coverage: `complete` only when all supported text windows are assessed
and no file/range failed, was deferred or unsupported; otherwise `partial`.
This is processing coverage, not semantic recall.

Review decisions map finding IDs to `accepted`, `rejected` or `deferred` plus
nonempty rationale. They bind the exact assessment hash and actor. Unknown or
duplicate finding IDs are rejected. Deferred findings remain visible.
A revision request binds parent domain hash, assessment/review hashes and
accepted findings. It never changes the parent contract or approves the child.

Before revision, rebuild source windows and compare every report window's ID,
source reference, page, range, text and hash. Revalidate finding references
against the parent contract. A rehashed locally forged quote is not evidence.
The revision model receives the parent contract definitions, not only its hash.
Prototype revisions may not remove existing semantic IDs or silently alter
identity/hierarchy/property types without a separate breaking-change workflow.
Emit the actual parent-to-child change summary for review.

Accepted finding locations outside the original bounded sample must be
reverified against the current corpus and minted as fresh `domain_design`
evidence. Include those source units/spans in the new L1 manifests/profile/design
context, with explicit bounded sampling accounting. Assessment finding IDs
themselves are never promoted to extraction/design authority. At most sixteen
supplemental findings are permitted; exceeding the cap is explicit, not truncation.
Revision model responses use the same immutable request-fingerprinted response
cache pattern before downstream validation so failed attempts remain replayable.

## B7: CLI and cost boundary

Add domain-local assessment/review operations without changing existing
top-level command counts. The exact help and tests are the callable contract.
Assessment dry-run performs inventory/coverage planning only: no model calls
and no output-file writes. Execution requires an explicit live-model or offline
response-fixture mode, a call limit and a per-call input/output bound.

All output artifacts have explicit paths and hashes. Live model calls use the
configured existing Foundry client and ordinary authentication; no key listing.
Persist completed bounded responses for audit/replay with request fingerprints.
Do not call a different resource silently when authentication or configuration
fails. Never include endpoint secrets in tracked fixtures.

Review and revision-plan operations are local-only. Remote apply, release
promotion and an all-estate rollback remain capability-gated and are not implied
by a successful assessment command.

## B8: executable acceptance

Use the existing pytest runner and fake provider boundaries:

- Property owner/value/temporal persistence and old-field-presence handling.
- Positive exact scalar evidence and negative owner-only/substring evidence.
- Inherited property typing, missing/unasserted owner, tampered identity/value.
- L4/L5a non-null values and endpoint keys, conflicts and required-key absence.
- Assessment of multiple files, explicit deferred windows, exact quote checks,
  unknown references, bounded calls, stable hashes and dry-run without writes.
- Review hash/actor binding, invalid decisions and immutable parent authority.

Live acceptance uses small authorized sandbox inputs and existing resources.
An unavailable provider or unsafe publication capability is reported blocked;
it does not justify weakening the contract or claiming deployment success.

## B9: explicitly authorized create-only Fabric prototype

The transactional publication policy remains unchanged. A separate
`--prototype-create-only` mode may create isolated test items after the user
authorizes a workspace and name prefix, and the exact dry-run plan is approved.
It deliberately does not promise transactional rollback.

Inputs are sealed L4/L3 artifacts and their approved Schema-2 authority.
Do not substitute legacy model files, independently authored tables or fake
receipts. Reuse native definition compilers, preserving their diagnostics.

The approved plan binds source/schema/data hashes, actual destination workspace,
prefix, per-run unique names, target types, dependency order, semantic conversion
limitations, and the retain-on-failure policy. Refuse empty semantic data and
unapproved semantic widening/type exclusions. Item existence alone is not
business acceptance.

Before every create, persist intent. Record returned IDs and long-running
operation references immediately. Names must start with the approved prefix.
Refuse pre-existing-name collisions; never adopt an item solely by name.
Only exact items/operations already proved by this journal may be resumed.
Ambiguous creation outcomes stop for reconciliation rather than another POST.

Create a schema-enabled Lakehouse and write new Delta tables in create-only
mode. Never call an overwrite path on a pre-existing Lakehouse. Bind native
Ontology/Semantic Model/Graph definitions to actual returned IDs and verified
table locations. Record service-created companions; do not delete them.

Read back definitions, schemas, row counts and required values. If required
refresh/framing is unsupported, remain partial and do not claim a usable
ontology. Record all partial resources and errors; no automatic deletion,
replacement, compensation or modification of unrelated workspace items.

Emit a distinct prototype publication journal/report, not a transactional
L5a success receipt. Final acceptance requires actual question results from
the deployed ontology/graph and its bound evidence, with model/variant/scope
and source citations. Mere creation, successful JSON generation or local
tests do not satisfy that gate.

## B10: compact model-facing design, full Schema-2 authority

An explicit compact proposal mode may request a smaller semantic sketch from
the model instead of repeating complete internal contract envelopes. This is
not Schema-1, a candidate fixture, or permission to bypass Schema-2 validation.

Type question references may be empty when a design rationale or evidence
supports the type, matching the formal Schema-2 type contract. A common type
does not need an invented question assignment. The new design-first draft is
not required to pass the strict proposal gates in this section before being
saved and evaluated.

The sketch must retain domain intent, type/property meanings and value types,
identity-property choices, directed relationship endpoints, question bindings,
and ordered/unordered membership requirements. Simple local keys reference
definitions within the same sketch; undefined references are errors.

The CLI deterministically expands the sketch into DomainProposalCandidatesV2:
mint stable typed identifiers, qualify property IDs by their declaring type,
derive endpoint IDs from a single relationship definition, calculate scores
from verified references and question bindings, and fill mechanical ordering
policy fields. A root identity key can use only that root's properties.

Metadata expansion must not invent instance facts, counts, compatibility,
source quotations, evidence IDs or approvals. Governance rationale may describe
the customer's supplied question/requirement as schema-design authority; it
does not prove an extracted business fact.

The expanded proposal passes the same evidence, hierarchy, identity, endpoint,
selection and critical-question gates. A connected path alone is not acceptance
of the actual technician answer. Preserve the raw sketch and expanded output
in opt-in CLI traces for comparison. The verbose proposal mode remains available
for compatibility; proposal-format/version is part of execution identity.

CQ tags describe schema usage, not access control or instance evidence. For an
explicitly supported sketch route, the expander may resolve a bounded path over
declared governance-supported relationships and attach that question ID to the
relationships actually used. Record this deterministic usage propagation.
Never clear an explicit unsupported reason, invent an edge, or treat propagated
tags as proof that source data answers the question.
The same usage binding may reuse a declared completeness rule on that path.
When none exists, the compiler may add a required-role check on an actual path
edge, using its exact declared forward endpoints and the customer's question
as design governance. This adds no relationship, source fact, cardinality or
ordering assumption. It is not a claim that all real instances were extracted.
This compatibility behavior is not a substitute for evaluating design gaps in
the new design-first workflow; new compilation must expose unsupported
completeness requirements rather than silently manufacture a passing assessment.

## B11: explicit cached-layout text units

`enrich --ocr-cache ... --ocr-identity ...` may use complete verified DI page
text instead of detached native PDF headings/paragraphs. Cache identity and
original byte hash must match; PDF page coverage must match the original file.
No cache miss may trigger a hidden analysis request or native-text fallback.

These are normalized text-stream SourceUnits, represented by the existing
text/paragraph evidence kind. Their actual page granularity is explicit in
`native_layer_id=docintel-pages/1.0.0`, page coordinates and cache-key-backed
native object IDs. This does not claim they are single original paragraphs.
SourceUnit hashes bind the normalization, content and provenance. Existing
literal evidence and assertion checks remain unchanged.

Dry-run validates/materializes these units in memory, reports their count/hash,
and performs no model calls or writes. It must not mistake unit accounting for
semantic completeness, or document order for unconditional execution order.

## B12: canonical source-identity publication binding

A versioned publication-crosswalk successor may represent an approved
`stable_source_identity` policy without inventing a business-ID property.
The binding must explicitly identify the identity-root type, approved policy
mode/hash, sealed L4 authority and the unchanged canonical `entity_id`.

For this binding only, an empty business-key-property list is valid:
`L4.entity_id -> __canonical_id`, and relationship endpoints bind the actual
`__source_entity_id` / `__target_entity_id`. This is the canonical identity
already re-proven by L3, not a physical surrogate substituted for meaning.

Business-key policies retain their declared-property/value-proof requirements.
No empty-key relaxation applies to old 1.1/1.2 contracts; their hashes and
validation remain unchanged. Propertyless source-identity types are allowed
only when the approved domain permits them. All source/target type constraints,
evidence and readback checks remain required.

The new representation needs explicit producer/consumer support and schema
version negotiation. It does not promise identity continuity after source-unit
or local-reference changes across document revisions.
