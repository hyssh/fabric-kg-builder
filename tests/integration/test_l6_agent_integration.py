from __future__ import annotations

import pytest

from fabric_kg_builder.agent import l6_integration as l6
from tests.contract.test_c0_runtime_contracts import (
    ontology_scope,
    resolved_ontology_scope,
    resolved_retrieval_scope,
)
from tests.unit.test_l6_agent_integration import (
    _EvidenceHost,
    _GraphHost,
    _Resolver,
    _access,
    _authorities,
    _evidence,
    _graph_query,
    _graph_result,
)


@pytest.mark.integration
@pytest.mark.offline
def test_l6_fake_hosts_end_to_end(monkeypatch):
    """Exercise the complete L6 sequence with fake Graph and sealed-L5b hosts."""

    monkeypatch.setattr(l6, "_validate_authorities", lambda *args, **kwargs: None)
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    evidence_result, context, budget, _, _ = _evidence()
    query = _graph_query(ontology)
    graph = _GraphHost(_graph_result(ontology, query))
    evidence = _EvidenceHost(evidence_result)
    orchestrator = l6.L6AgentOrchestrator(
        resolver=_Resolver(ontology, retrieval),
        graph_host=graph,
        evidence_host=evidence,
        authorities=_authorities(),
    )

    output = orchestrator.run(
        l6.L6RunRequest(
            question="Return only the sealed evidence package.",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )

    assert output.status == "complete"
    assert output.zero_synthesis is True
    assert graph.calls == evidence.calls == 1
    assert output.operation_accounting["downstream_synthesis_calls"] == 0
