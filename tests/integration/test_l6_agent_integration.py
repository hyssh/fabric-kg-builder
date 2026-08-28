from __future__ import annotations

import json

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
    _GraphReceiptStore,
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

    monkeypatch.setattr(
        l6,
        "_require_l6_evidence_publication",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        l6,
        "require_l5a_publication_receipt",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        l6,
        "require_l5b_publication_receipt",
        lambda *args, **kwargs: None,
    )
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
        graph_receipt_authority=_GraphReceiptStore(),
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
    assert output.operation_accounting.downstream_synthesis_calls == 0


@pytest.mark.integration
@pytest.mark.offline
def test_l6_canonical_definition_persists_and_reads_back_exactly(tmp_path):
    definition = l6.build_l6_agent_definition(
        agent_name="KG evidence agent",
        fabric_data_agent_connection_id="connection:fabric",
    )
    path = tmp_path / "l6-agent-definition.json"

    definition_hash = l6.persist_l6_agent_definition(path, definition)

    assert definition_hash == definition.definition_hash
    assert path.read_bytes() == definition.canonical_bytes
    assert json.loads(path.read_bytes()) == json.loads(definition.canonical_bytes)
