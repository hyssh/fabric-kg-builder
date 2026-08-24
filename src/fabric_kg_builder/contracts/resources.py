"""Future-measurement resource metrics without selected numeric thresholds."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, RequiredText, Sha256, canonical_sha256, sorted_unique
from .identity import CanonicalIdentityEnvelope
from .receipts import StageId, StageReceipt

Counter = Annotated[int, Field(ge=0)]


class StageResourceMetrics(ContractModel):
    identity: CanonicalIdentityEnvelope
    resource_metrics_id: RequiredText
    stage_id: StageId
    stage_name: RequiredText
    wall_ms: Counter
    cpu_ms: Counter
    peak_rss_bytes: Counter
    storage_read_bytes: Counter
    storage_write_bytes: Counter
    network_request_bytes: Counter
    network_response_bytes: Counter
    source_units_read: Counter
    source_units_written: Counter
    source_units_skipped: Counter
    document_intelligence_calls: Counter
    document_intelligence_pages: Counter
    foundry_calls: Counter
    foundry_input_tokens: Counter
    foundry_output_tokens: Counter
    embedding_calls: Counter
    embedding_items: Counter
    fabric_calls: Counter
    fabric_rows_read: Counter
    fabric_rows_written: Counter
    search_calls: Counter
    search_documents_read: Counter
    search_documents_written: Counter
    retry_count: Counter
    retry_wait_ms: Counter
    cache_hits: Counter
    cache_misses: Counter
    max_observed_concurrency: Counter
    budget_snapshot_hash: Sha256
    exceeded_dimensions: tuple[str, ...] = ()
    metrics_hash: Sha256

    @field_validator("exceeded_dimensions", mode="before")
    @classmethod
    def _dimensions(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="exceeded_dimensions")
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "StageResourceMetrics":
        if self.identity.contract_kind != "c0.stage_resource_metrics":
            raise ValueError("invalid resource metrics identity contract_kind")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"metrics_hash"})
        )
        if self.metrics_hash != expected:
            raise ValueError("metrics_hash does not match resource metrics")
        return self


def validate_receipt_resources(
    receipt: StageReceipt,
    metrics: StageResourceMetrics,
) -> None:
    """Bind a receipt to its metrics and enforce confirmed hard dimensions only."""
    if receipt.resource_metrics_id != metrics.resource_metrics_id:
        raise ValueError("receipt resource_metrics_id mismatch")
    if receipt.resource_metrics_hash != metrics.metrics_hash:
        raise ValueError("receipt resource_metrics_hash mismatch")
    if receipt.stage_id != metrics.stage_id or receipt.stage_name != metrics.stage_name:
        raise ValueError("receipt and metrics stage identity mismatch")
    if receipt.status == "succeeded" and metrics.exceeded_dimensions:
        raise ValueError("succeeded receipt cannot exceed a declared hard dimension")
