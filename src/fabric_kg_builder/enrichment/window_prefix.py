"""Plan only exact approved prefix ranges without changing the source inventory."""

from fabric_kg_builder.domain.window_run_acceptance import WindowRunPrefixAcceptance

from .schema2_work_units import L2_SPLIT_POLICY_VERSION, L2WorkUnit, _work_unit_id, plan_work_units


class WindowPrefixScopeError(ValueError):
    """Approved prefix ranges differ from the immutable materialized source."""


def prefix_span_allowed(contract, *, source_unit_id, span_start, span_end, source_text_hash):
    """Check actual resolved evidence against the union of exact accepted ranges."""
    acceptance = getattr(contract, "window_run_acceptance", None)
    if not isinstance(acceptance, WindowRunPrefixAcceptance):
        return True
    if span_start < 0 or span_end <= span_start:
        return False
    cursor = span_start
    for chunk in sorted(
        (chunk for chunk in acceptance.selected_chunks if chunk.source_unit_id == source_unit_id),
        key=lambda chunk: (chunk.slice_start, chunk.slice_end),
    ):
        if chunk.source_text_hash != source_text_hash:
            return False
        if chunk.slice_end <= cursor:
            continue
        if chunk.slice_start > cursor:
            return False
        cursor = chunk.slice_end
        if cursor >= span_end:
            return True
    return False


def validate_prefix_candidate_anchors(leaves, *, contract, source_units):
    """Reject fresh or cached L2 proposals whose quote resolves outside scope."""
    if not isinstance(contract.window_run_acceptance, WindowRunPrefixAcceptance):
        return
    from fabric_kg_builder.contracts.base import normalize_nfc
    from .schema2_evidence import locate_unique_quote

    units = {unit.source_unit_id: unit for unit in source_units}
    selected = {chunk.source_unit_id: chunk.source_text_hash for chunk in contract.window_run_acceptance.selected_chunks}
    for leaf in leaves:
        for candidate in leaf.proposed_candidates:
            unit = units.get(candidate.source_unit_id)
            if unit is None:
                raise WindowPrefixScopeError("WINDOW_PREFIX_EVIDENCE_SOURCE_MISSING")
            if selected.get(unit.source_unit_id) != unit.text_content_hash:
                raise WindowPrefixScopeError("WINDOW_PREFIX_EVIDENCE_OUTSIDE_SCOPE")
            anchor = candidate.proposed_anchor
            if anchor is None:
                continue
            quote = normalize_nfc(anchor.quote)
            bounds = (
                (anchor.span_start, anchor.span_end)
                if 0 <= anchor.span_start < anchor.span_end <= unit.codepoint_count
                and unit.text[anchor.span_start:anchor.span_end] == quote
                else locate_unique_quote(unit.text, quote)
            )
            if bounds is not None and not prefix_span_allowed(
                contract, source_unit_id=unit.source_unit_id, span_start=bounds[0], span_end=bounds[1],
                source_text_hash=unit.text_content_hash,
            ):
                raise WindowPrefixScopeError(
                    f"WINDOW_PREFIX_EVIDENCE_OUTSIDE_SCOPE: {candidate.candidate_id}"
                )


def plan_approved_work_units(source_units, *, contract, pass_name, authority_fingerprint):
    acceptance = contract.window_run_acceptance
    if not isinstance(acceptance, WindowRunPrefixAcceptance):
        return plan_work_units(source_units, pass_name=pass_name, authority_fingerprint=authority_fingerprint)
    if contract.approval.status != "approved" or contract.window_run_binding.scope_acceptance_hash != acceptance.acceptance_hash:
        raise WindowPrefixScopeError("WINDOW_PREFIX_REQUIRES_APPROVED_SCOPE")
    units = {unit.source_unit_id: unit for unit in source_units}
    roots = []
    for chunk in acceptance.selected_chunks:
        unit = units.get(chunk.source_unit_id)
        if (
            unit is None or unit.source_file_id != chunk.source_file_id
            or unit.text_content_hash != chunk.source_text_hash
            or not 0 <= chunk.slice_start < chunk.slice_end <= unit.codepoint_count
        ):
            raise WindowPrefixScopeError("WINDOW_PREFIX_SOURCE_RANGE_DRIFT")
        key = _work_unit_id(
            source_unit=unit, slice_start=chunk.slice_start, slice_end=chunk.slice_end,
            pass_name=pass_name, authority_fingerprint=authority_fingerprint,
            split_policy_version=L2_SPLIT_POLICY_VERSION,
        )
        roots.append(L2WorkUnit(
            work_unit_id=key, root_work_unit_id=key, source_unit_id=unit.source_unit_id,
            source_text_hash=unit.text_content_hash, source_text=unit.text,
            slice_start=chunk.slice_start, slice_end=chunk.slice_end,
            pass_name=pass_name, authority_fingerprint=authority_fingerprint,
        ))
    if len({root.work_unit_id for root in roots}) != len(roots):
        raise WindowPrefixScopeError("WINDOW_PREFIX_DUPLICATE_RANGE")
    return tuple(roots)
