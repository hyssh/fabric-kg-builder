"""fabric_kg_builder.agent — M8 Foundry prompt-agent package.

Exports the public API used by deploy-agent, app, and tests.
"""

from fabric_kg_builder.agent.citation import Citation, CitationSource, normalize_citations
from fabric_kg_builder.agent.instructions import build_routing_instructions, INSTRUCTIONS_VERSION
from fabric_kg_builder.agent.metadata import AgentMetadata, load_agent_metadata
from fabric_kg_builder.agent.l6_integration import (
    L6AgentOrchestrator,
    L6Authorities,
    build_l6_agent_definition,
    build_l6_agent_instructions,
    build_l6_tool_definitions,
    persist_l6_agent_definition,
)

__all__ = [
    "AgentMetadata",
    "Citation",
    "CitationSource",
    "INSTRUCTIONS_VERSION",
    "build_routing_instructions",
    "load_agent_metadata",
    "normalize_citations",
    "L6AgentOrchestrator",
    "L6Authorities",
    "build_l6_agent_definition",
    "build_l6_agent_instructions",
    "build_l6_tool_definitions",
    "persist_l6_agent_definition",
]
