# 0.2.6 design-first CLI

Status: implementation specification. This release is local development, not a
claim of completed Fabric deployment or six-question live answer acceptance.

## Goal

Combine business intent, an optional existing YAML design, and bounded document
samples into a reviewable ontology draft. Evaluate the draft against the supplied
questions without making question coverage a prerequisite for saving the draft.
Only an explicitly compiled, reviewed and approved Schema-2 domain may drive
extraction. Preserve the existing source, identity, evidence and approval rules.

The release does not restart from 0.2.3, introduce a fixed domain taxonomy, or
replace the downstream Schema-2 pipeline.

## State and authority boundaries

| Artifact | Producer | Consumer | Authority |
|---|---|---|---|
| Design context | `domain design` | Model and draft compiler | Intake, seed YAML and verified bounded samples; not extracted facts |
| Design draft | `domain design` | `domain evaluate-design`, `domain compile-design` | Unapproved design choices, including common and currently unused concepts |
| Design evaluation | `domain evaluate-design` | Reviewer and compilation preflight | Structural support and gaps; not proof of correct live answers |
| Compiled L1 proposal | `domain compile-design` | Existing `domain approve` | Existing strict Schema-2 proposal, still unapproved |
| Approved domain | Existing `domain approve` | Existing `enrich` | Exact approved contract and source bindings |

Artifact versions are independent of the package version. A draft is not
DomainContractV2, cannot impersonate an approved domain, and does not inherit
approval from a seed. Existing sealed artifacts are not rewritten or rehashed.
Historical L7 receipt release `0.2.4` remains unchanged under CLI `0.2.6`.

## Input contract

- Preserve the full parsed seed YAML, not only its description. Accept a reference
  sketch as well as an existing domain contract. Reference examples are design
  suggestions, never verified facts or executable instructions.
- Bind intake, seed, corpus, sampling policy, model identity and design format to
  the draft. Record the actual bounded samples; corpus inventory is not full
  document comprehension.
- Preserve the user's questions and criticality. Do not remove questions, mark
  them non-critical, or fabricate relevance to make compilation pass.
- Treat supplied text as data. Do not execute YAML tags or fetch external ontology
  content. A source conflict must be visible rather than silently reconciled.
- Re-read current sources before compilation. Reject drift; never reseal a stale
  draft against new inputs.
- No live model call or write in planning mode. Explicit live generation is
  bounded. No cloud provisioning, extraction or publication is implied.

## Draft validity versus design evaluation

Hard draft errors remain malformed data, duplicate identifiers, unknown
references, parent cycles, incompatible declared identity ownership and invented
evidence references. An inability to support a question is not such an error.

Types and relationships may have zero, one or multiple question references.
Common/domain distinctions and independent types must remain representable.
Relevance follows the design; questions do not define the entire vocabulary.

Evaluation reports each question, the proposed answer fields and connections,
gaps, and execution limitations. A path alone cannot prove that action text,
quantities, conditions, applicability or complete source evidence exists.
Unresolved semantic adequacy remains a review requirement.

The current compiler's four-hop, minimum-path, relationship-count and intake
limits are compatibility constraints, not universal ontology validity rules.
Drafts outside that capability remain reviewable. Compiler limits are reported
explicitly rather than bypassed or disguised as coverage.

Do not create a completeness declaration simply to turn a failing score green.
Design bookkeeping is not permission, factual evidence or a completeness proof.

## Compilation and approval

Compilation is local and model-free. It uses the exact draft and freshly checked
source context and passes through the existing Schema-2 compiler. It must not
silently drop isolated types, alter identity keys, reclassify critical questions,
or replace a declared route with unsupported facts.

If the compiler cannot represent the draft, return a diagnostic and leave the
draft/evaluation intact. No success-shaped fallback, legacy conversion or fake
approval receipt is allowed. A successful compilation still requires the existing
explicit `domain approve` operation before extraction.

The new workflow is additive. Existing `init-domain` remains the strict
compatibility path. Schema-2 options it cannot honor must fail with actionable
guidance, rather than silently ignore seed YAML or domain descriptions.

## Implementation order and ownership

1. Freeze the boundaries above and preserve existing local work.
2. In parallel, implement core draft/evaluation/compilation and the CLI command
   surface against the agreed core interface. Core and CLI have disjoint owners.
3. Integrate command help, plugin helper, documentation and package metadata.
4. Run targeted existing regression tests and new negative/positive boundary cases.
5. Exercise the public CLI with real source documents, authorized inference and
   the user's seed sketch. Inspect the persistent draft and report, not merely
   an exit status. Record local commits; no PR, new worktree or push.

## Acceptance cases

| Case | Required result |
|---|---|
| Common type with empty question list | Saved with its meaning and rationale |
| Type supports multiple questions | All references retained |
| All questions unsupported | Draft/evaluation retained; strict compile not ready |
| Missing action/quantity representation | Explicit question gap, not green path-only coverage |
| Independent context type | Retained in draft; compile must not silently discard it |
| Unknown endpoint, property or evidence | Explicit structural error |
| Cyclic hierarchy or invalid identity ownership | Explicit structural error |
| Seed content changes | Different binding; no stale reuse |
| Source changes after design | Compilation rejected |
| Tampered draft or evaluation | Hash/provenance mismatch rejected |
| Unapproved draft passed to extraction | Rejected |
| Existing approved Schema-2 domain | Existing approval/evidence behavior unchanged |
| Planning mode | No output writes, model calls or cloud mutations |
| Real inference | Seed reaches the request; generated design persists without fixture substitution |

Developer tests may use the existing pytest runner. Live prototype acceptance
uses only public CLI commands, not scripts importing project functions.

## Explicit non-goals

No automatic production schema migration, third-party ontology import, full-corpus
semantic recall claim, inferred compatibility, source-free safety reconciliation,
or claim that the example Surface design is universal. Actual Fabric publication
and six-question answer correctness remain separate acceptance milestones.
