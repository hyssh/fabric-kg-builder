"""Contract status helpers and the enrichment approval guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .models import AnyDomainContract, DomainContract, DomainContractV2, DomainReview
from .review import run_deterministic_validation
from .service import (
    DomainContractError,
    DomainContractValidationError,
    compute_contract_hash,
    load_domain_contract,
    load_domain_review_file,
    review_path_for_contract,
    save_json_document,
    utc_now_text,
)


@dataclass(slots=True)
class DomainGuardStatus:
    """Resolved contract and review state for CLI reporting and gating."""

    contract_path: Path | None = None
    review_path: Path | None = None
    legacy_path: Path | None = None
    contract: AnyDomainContract | None = None
    review: DomainReview | None = None
    contract_hash: str | None = None
    deterministic_error_count: int = 0
    deterministic_warning_count: int = 0
    ready_for_enrichment: bool = False
    messages: list[str] = field(default_factory=list)


class EnrichmentContractError(DomainContractError):
    """Raised when enrichment is attempted without a ready contract."""


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def locate_domain_contract(
    explicit_path: str | None = None,
    *,
    output_dir: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Locate YAML and legacy JSON domain artifacts."""
    if explicit_path:
        candidate = Path(explicit_path)
        if candidate.suffix.lower() in {".yaml", ".yml"}:
            return candidate, None
        if candidate.suffix.lower() == ".json":
            return None, candidate
        return candidate, None

    yaml_candidates = [Path("domain.yaml"), Path("domain.yml")]
    legacy_candidates = [Path("build") / "enriched" / "domain.json"]
    if output_dir is not None:
        yaml_candidates.extend([output_dir / "domain.yaml", output_dir / "domain.yml"])
        legacy_candidates.append(output_dir / "domain.json")
    return _first_existing(yaml_candidates), _first_existing(legacy_candidates)


def evaluate_domain_guard_status(
    explicit_path: str | None = None,
    *,
    output_dir: Path | None = None,
) -> DomainGuardStatus:
    """Evaluate missing, stale, legacy, review, and approval state."""
    contract_path, legacy_path = locate_domain_contract(explicit_path, output_dir=output_dir)
    status = DomainGuardStatus(contract_path=contract_path, legacy_path=legacy_path)

    if contract_path is None:
        if legacy_path is not None:
            status.messages.append(
                "Legacy domain.json detected. Run 'fabric-kg domain convert-legacy --legacy-file "
                f"{legacy_path}' and then 'fabric-kg domain review' + 'fabric-kg domain approve'."
            )
        else:
            status.messages.append(
                "No approved domain.yaml found. Run 'fabric-kg domain init', 'domain review', and 'domain approve'."
            )
        return status

    try:
        contract = load_domain_contract(contract_path)
    except DomainContractError as exc:
        status.messages.append(str(exc))
        return status

    status.contract = contract
    status.contract_hash = compute_contract_hash(contract)
    review_path = review_path_for_contract(contract_path)
    status.review_path = review_path

    findings, _coverage = run_deterministic_validation(contract)
    status.deterministic_error_count = sum(
        1 for finding in findings if finding.severity == "error"
    )
    status.deterministic_warning_count = sum(
        1 for finding in findings if finding.severity == "warning"
    )
    if status.deterministic_error_count:
        status.messages.append(
            f"Deterministic validation found {status.deterministic_error_count} error(s). Run 'fabric-kg domain validate'."
        )

    if isinstance(contract, DomainContractV2):
        if contract.approval.status != "approved":
            status.messages.append(
                "Schema-2.0 contract is not approved. Use the cited proposal in "
                "'fabric-kg domain approve --proposal ... --approved-by ...'."
            )
            return status
        if contract.approval.contract_hash != status.contract_hash:
            status.messages.append(
                "Schema-2.0 approval is stale because its contract hash does not "
                "match the current domain.yaml."
            )
            return status
        required_v2_metadata = (
            contract.approval.approved_by,
            contract.approval.approved_at_utc,
            contract.approval.proposal_hash,
            contract.approval.source_profile_hash,
            contract.approval.prompt_hash,
            contract.approval.prompt_version,
            contract.approval.model_version,
            contract.approval.model_hash,
        )
        if any(value in (None, "") for value in required_v2_metadata):
            status.messages.append(
                "Schema-2.0 approval metadata is incomplete. Re-run explicit "
                "proposal approval."
            )
            return status
        status.ready_for_enrichment = status.deterministic_error_count == 0
        return status

    if review_path.exists():
        try:
            status.review = DomainReview.model_validate(load_domain_review_file(review_path))
        except (DomainContractValidationError, ValidationError) as exc:
            status.messages.append(f"Domain review file is invalid: {exc}")
            return status
    else:
        status.messages.append(
            "No domain.review.json found. Run 'fabric-kg domain review' before approval."
        )
        return status

    if status.review.contract_hash != status.contract_hash:
        status.messages.append(
            "Domain review is stale. Re-run 'fabric-kg domain review' because the contract changed after review."
        )
        return status

    if contract.approval.status != "approved":
        status.messages.append(
            "Domain contract is not approved. Run 'fabric-kg domain approve' after review."
        )
        return status

    if contract.approval.contract_hash != status.contract_hash:
        status.messages.append(
            "Approval is stale because the approved contract hash does not match the current domain.yaml."
        )
        return status

    required_metadata = (
        contract.approval.approved_by,
        contract.approval.approved_at_utc,
        contract.approval.schema_version,
        contract.approval.prompt_version,
        contract.approval.model_version,
    )
    if any(value in (None, "") for value in required_metadata):
        status.messages.append(
            "Approval metadata is incomplete. Re-run 'fabric-kg domain approve'."
        )
        return status

    status.ready_for_enrichment = status.deterministic_error_count == 0
    return status


def require_ready_domain_contract(
    explicit_path: str | None = None,
    *,
    output_dir: Path | None = None,
) -> tuple[AnyDomainContract, DomainReview | None, DomainGuardStatus]:
    """Return the approved contract or raise a clear compatibility/migration error."""
    status = evaluate_domain_guard_status(explicit_path, output_dir=output_dir)
    missing_review = (
        status.contract is not None
        and not isinstance(status.contract, DomainContractV2)
        and status.review is None
    )
    if not status.ready_for_enrichment or status.contract is None or missing_review:
        raise EnrichmentContractError(" ".join(status.messages))
    return status.contract, status.review, status


def write_domain_run_manifest(
    output_dir: Path | str,
    *,
    contract_path: Path,
    contract: AnyDomainContract,
    review: DomainReview | None,
) -> Path:
    """Write approval metadata used by the enrichment run."""
    manifest_path = Path(output_dir) / "domain.run-manifest.json"
    approval = contract.approval
    save_json_document(
        {
            "schema_version": contract.schema_version,
            "generated_at_utc": utc_now_text(),
            "domain_contract": {
                "path": str(contract_path),
                "contract_hash": compute_contract_hash(contract),
                "approval_status": approval.status,
                "approved_by": approval.approved_by,
                "approved_at_utc": approval.approved_at_utc,
                "schema_version": getattr(
                    approval,
                    "schema_version",
                    contract.schema_version,
                ),
                "prompt_version": approval.prompt_version,
                "model_version": approval.model_version,
                "review_quality_score": (
                    review.quality_score if review is not None else None
                ),
                "reviewed_at_utc": (
                    review.reviewed_at_utc if review is not None else None
                ),
                "review_file": (
                    str(review_path_for_contract(contract_path))
                    if review is not None
                    else None
                ),
                "proposal_hash": getattr(approval, "proposal_hash", None),
                "source_profile_hash": getattr(
                    approval,
                    "source_profile_hash",
                    None,
                ),
            },
        },
        manifest_path,
    )
    return manifest_path
