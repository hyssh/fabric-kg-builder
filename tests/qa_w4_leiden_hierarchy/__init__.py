"""W4 independent QA package: relation-witness repair + Leiden hierarchy + Schema-2 integration.

Owned exclusively by the W4 QA workstream. Direct-import acceptance tests
against the actual production code, not a reference reimplementation:

- synthetic fixtures with explicit, independently-reasoned expected outcomes,
  derived before running any fixture through a provider (``fixtures.py``),
- acceptance tests that import and exercise the actual W2 hierarchy-build
  entrypoint with an injected scripted partition provider, and the actual
  evidence-sidecar validation path (``test_v2_acceptance.py``),
- acceptance tests that run the same fixtures through the real pinned
  ``graspologic_native`` provider, not a scripted stand-in
  (``test_real_native_corpus.py``).

No file in this package may be modified by W1/W2/W3 implementation work;
conversely this package must not modify `tests/unit/*` implementer tests.
"""
