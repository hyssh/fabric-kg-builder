"""tests/unit/test_knowledge_capability_discovery.py

``discover_capabilities`` is the gate in front of every agentic-retrieval
operation: if it cannot confirm an API version, ``SearchKbClient`` refuses to
construct and no knowledge source or knowledge base can be created.

It shipped probing ``GET {endpoint}/servicestatistics``.  The Azure AI Search
service statistics path is ``/servicestats``; ``/servicestatistics`` returns
404 on a real service.  Because a 404 is a 4xx, the probe loop treated it as
"this version is not supported" and disqualified *every* candidate version, so
agentic retrieval was reported unavailable on services that fully support it.

The function had no tests at all, which is why a wrong path shipped.  These
tests pin the request the probe actually makes, not just its return value.
"""

from __future__ import annotations

import pytest

from fabric_kg_builder.knowledge.models import (
    AgentFeature,
    PreviewNotAcknowledged,
    discover_capabilities,
)
from fabric_kg_builder.knowledge.transport import FakeTransport, HttpResponse

_ENDPOINT = "https://svc.search.windows.net"
_GA = "2026-04-01"
_PREVIEW = "2026-05-01-preview"


def _ok_transport() -> FakeTransport:
    transport = FakeTransport()
    transport.register("GET", "/servicestats", HttpResponse(200, body={}))
    return transport


def _token() -> str:
    return "fake-token"


def test_probe_uses_the_servicestats_path() -> None:
    """The exact path matters: /servicestatistics 404s on a real service."""
    transport = _ok_transport()
    discover_capabilities(_ENDPOINT, transport, _token)

    assert len(transport.calls) == 1
    url = transport.calls[0].url
    assert "/servicestats?" in url
    assert "servicestatistics" not in url


def test_probe_targets_the_configured_endpoint_and_version() -> None:
    transport = _ok_transport()
    discover_capabilities(_ENDPOINT, transport, _token)

    assert transport.calls[0].url == f"{_ENDPOINT}/servicestats?api-version={_GA}"
    assert transport.calls[0].method.upper() == "GET"


def test_trailing_slash_does_not_produce_a_double_slash() -> None:
    transport = _ok_transport()
    discover_capabilities(_ENDPOINT + "/", transport, _token)

    assert "//servicestats" not in transport.calls[0].url


def test_ga_probe_reports_ga_features() -> None:
    result = discover_capabilities(_ENDPOINT, _ok_transport(), _token)

    assert result.api_version == _GA
    assert AgentFeature.KNOWLEDGE_SOURCES in result.available_features
    assert AgentFeature.KNOWLEDGE_BASES in result.available_features
    assert AgentFeature.FABRIC_ONTOLOGY_SOURCE not in result.available_features


def test_a_404_disqualifies_the_version() -> None:
    """Documents the blast radius: one wrong path silently disables everything."""
    transport = FakeTransport()
    transport.register("GET", "/servicestats", HttpResponse(404, body={}))

    result = discover_capabilities(_ENDPOINT, transport, _token)

    assert result.api_version is None
    assert not result.available_features


def test_preview_probe_tries_preview_before_ga() -> None:
    transport = FakeTransport()
    transport.register("GET", "/servicestats", HttpResponse(200, body={}))

    result = discover_capabilities(
        _ENDPOINT, transport, _token,
        prefer_preview=True, preview_acknowledged=True,
    )

    assert result.api_version == _PREVIEW
    assert f"api-version={_PREVIEW}" in transport.calls[0].url
    assert AgentFeature.FABRIC_ONTOLOGY_SOURCE in result.available_features


def test_preview_falls_back_to_ga_when_preview_is_rejected() -> None:
    transport = FakeTransport()
    transport.register("GET", "/servicestats", HttpResponse(200, body={}))
    transport.register("GET", f"api-version={_PREVIEW}", HttpResponse(400, body={}))

    result = discover_capabilities(
        _ENDPOINT, transport, _token,
        prefer_preview=True, preview_acknowledged=True,
    )

    assert result.api_version == _GA
    assert len(transport.calls) == 2


def test_preview_requires_explicit_acknowledgement() -> None:
    with pytest.raises(PreviewNotAcknowledged):
        discover_capabilities(_ENDPOINT, _ok_transport(), _token, prefer_preview=True)
