# L4 parity with frozen PR #33

PR #33 and tag `archive/pr-33-ac54321c` are read-only historical references.
The 0.2.4 L4 implementation does not cherry-pick, rebase, modify, or close that
work.

| Historical behavior | L4 decision |
|---|---|
| Complete lifecycle and deduplicated-input audit accounting | Keep, using current C0 dispositions and sealed L3 lifecycle results |
| Asserted-only serving with evidence and endpoint gates | Keep, using current L3 evidence, hierarchy, identity, and governance authorities |
| Deterministic hashes, atomic output, resume, and corruption recovery | Keep, using current receipts, manifests, metrics, Arrow schemas, and fingerprinted run roots |
| Monolithic schema-2 compile/deploy path | Change to an isolated local L4 stage with a sealed-source adapter |
| Legacy entity/relationship physical shapes as schema-2 serving input | Change to dedicated asserted entity, type-assertion, relationship, property, and required-member tables |
| Collection completeness derived during projection | Drop; `RequiredMemberManifest@1.1.0` is the only membership authority |
| Raw canonical fallback | Drop and reject at the schema-2 source gate |
| Remote publication, Fabric, Graph, Search, runtime, or CLI activation | Drop from L4; these remain later-layer work |

The resulting parity target is behavioral rather than source-level: all PR #33
keep cases remain covered, change cases use the current C0/L3 contracts, and
drop cases are blocked explicitly.
