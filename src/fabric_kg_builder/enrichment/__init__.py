"""LLM enrichment orchestration via the Azure OpenAI SDK.

Runs batched LLM calls to extract entities, relationships, evidence,
chunks, and visual assets from source records. Supports checkpointing
for long-running enrichment jobs (--resume / --force).
"""
