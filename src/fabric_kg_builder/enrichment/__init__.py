"""LLM enrichment orchestration via Microsoft Foundry SDK (azure-ai-projects).

Runs batched LLM calls to extract entities, relationships, evidence,
chunks, and visual assets from source records. Supports checkpointing
for long-running enrichment jobs (--resume / --force).
"""
