"""agent/tools/__init__.py — tool adapters for the Fabric KG grounded agent."""

from fabric_kg_builder.agent.tools.kb_tool import KnowledgeBaseTool, KBResult
from fabric_kg_builder.agent.tools.fabric_data import FabricDataAgentAdapter, FabricDataResult
from fabric_kg_builder.agent.tools.lineage import SafeLineageTool, LineageSourceMetadata

__all__ = [
    "FabricDataAgentAdapter",
    "FabricDataResult",
    "KBResult",
    "KnowledgeBaseTool",
    "LineageSourceMetadata",
    "SafeLineageTool",
]
