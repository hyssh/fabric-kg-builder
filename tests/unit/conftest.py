"""Shared fixtures for offline review and publication tests."""

import hashlib

import pytest


@pytest.fixture
def publication_graph_lro(request):
    return getattr(request, "param", False)


@pytest.fixture
def legacy_quote_producer():
    from fabric_kg_builder.contracts.base import canonical_sha256
    from fabric_kg_builder.enrichment import approved_quote_review as review
    from fabric_kg_builder.enrichment import approved_reextraction as core

    identity = {
        name: hashlib.sha256(f"synthetic-legacy-producer:{name}".encode()).hexdigest()
        for name in core._code_identity()
    }
    # Model a single approved historical producer, not whichever source bytes
    # happen to be checked out. All real equality/hash guards still execute.
    # A separate context survives tests that undo their own transport patches.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core, "_code_identity", lambda: dict(identity))
        patch.setattr(review, "KNOWN_PRODUCER", canonical_sha256(identity))
        yield dict(identity)
