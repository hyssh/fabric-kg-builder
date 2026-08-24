from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

import fabric_kg_builder.domain.proposal as proposal_module
import fabric_kg_builder.cli.init_domain_cmd as init_domain_module
from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli.enrich_cmd import _resolve_domain_brief
from fabric_kg_builder.domain import (
    compute_contract_hash,
    evaluate_domain_guard_status,
    load_domain_contract,
    load_domain_proposal,
)
from fabric_kg_builder.sources.inspector import (
    InferredSuggestions,
    ObservedFacts,
    SourceProfile,
    SourceProposalSample,
    compute_source_profile_hash,
    load_source_profile,
    save_source_profile,
)
from tests.conftest import combined_output, make_cli_runner

pytestmark = pytest.mark.unit

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "domain_proposals"
_USER_MESSAGE_START = "<untrusted_domain_proposal_input>\n"
_USER_MESSAGE_END = "\n</untrusted_domain_proposal_input>"


@dataclass(frozen=True)
class DraftArtifacts:
    contract_path: Path
    profile_path: Path
    proposal_path: Path


def _load_fixture_json(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _five_question_intake() -> dict[str, Any]:
    payload = _load_fixture_json("facility_maintenance_intake.json")
    payload["competency_questions"] = payload["competency_questions"][:5]
    payload["desired_outcomes"] = [
        "Connect facilities, equipment, and work orders with evidence-backed relationships."
    ]
    return payload


def _unsupported_intake() -> dict[str, Any]:
    return _load_fixture_json("facility_maintenance_intake.json")


def _supported_candidates(*, description: str | None = None) -> dict[str, Any]:
    payload = _load_fixture_json("facility_maintenance_candidates.json")
    payload["question_routes"] = payload["question_routes"][:5]
    payload["warnings"] = []
    if description is not None:
        payload["domain_description"] = description
    return payload


def _unsupported_candidates() -> dict[str, Any]:
    return _load_fixture_json("facility_maintenance_candidates.json")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _proposal_sample(
    *,
    sample_id: str,
    sample_kind: str,
    element_type: str,
    source_file_id: str,
    citation_path: str,
    excerpt: str,
    page_number: int | None = None,
    section_path: str | None = None,
    row_index: int | None = None,
    sort_order: int | None = None,
) -> SourceProposalSample:
    return SourceProposalSample(
        sample_id=sample_id,
        sample_kind=sample_kind,
        element_type=element_type,
        source_file_id=source_file_id,
        citation_path=citation_path,
        page_number=page_number,
        section_path=section_path,
        row_index=row_index,
        sort_order=sort_order,
        excerpt=excerpt,
        content_hash=_content_hash(excerpt),
    )


def _source_profile() -> SourceProfile:
    return SourceProfile(
        observed=ObservedFacts(
            total_file_count=5,
            format_counts={"csv": 1, "markdown": 2, "pdf": 2},
            total_bytes=20_480,
            date_range=["2024", "2026"],
            csv_column_names=["asset_id", "facility", "status"],
        ),
        inferred=InferredSuggestions(
            document_categories=["equipment schedules", "maintenance records"],
            entity_candidates=["Equipment", "Facility", "Work Order"],
            extraction_risks=[],
        ),
        proposal_samples=[
            _proposal_sample(
                sample_id="sample:facility-layout",
                sample_kind="heading",
                element_type="section",
                source_file_id="source-file:facility-layout",
                citation_path="docs/facility-layout.pdf#page=1",
                page_number=1,
                section_path="Facility summary",
                excerpt=(
                    "Building A contains air handler AHU-4 and pump P-220 in the central plant."
                ),
            ),
            _proposal_sample(
                sample_id="sample:equipment-register",
                sample_kind="table",
                element_type="table",
                source_file_id="source-file:equipment-register",
                citation_path="data/equipment-register.csv",
                row_index=14,
                excerpt="AHU-4, Building A, HVAC, Active",
            ),
            _proposal_sample(
                sample_id="sample:work-order-log",
                sample_kind="text",
                element_type="paragraph",
                source_file_id="source-file:work-order-log",
                citation_path="logs/work-orders.md#wo-17",
                sort_order=17,
                excerpt="WO-17 applies to AHU-4 after a vibration alarm in Building A.",
            ),
            _proposal_sample(
                sample_id="sample:maintenance-log",
                sample_kind="text",
                element_type="paragraph",
                source_file_id="source-file:maintenance-log",
                citation_path="logs/maintenance-history.md#ahu-4",
                sort_order=44,
                excerpt="Maintenance history lists WO-12 and WO-17 against AHU-4.",
            ),
            _proposal_sample(
                sample_id="sample:service-manual",
                sample_kind="visual",
                element_type="vision_description",
                source_file_id="source-file:service-manual",
                citation_path="manuals/ahu-4-service.pdf#page=3",
                page_number=3,
                excerpt="The service manual labels AHU-4 as installed in Building A.",
            ),
        ],
        sampling_warnings=[],
        domain_description="Facility maintenance source profile for proposal tests.",
        source_hash="abc123",
        inspected_at_utc="2026-08-01T00:00:00Z",
        approved=False,
    )


class FakeFoundryClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = [copy.deepcopy(item) for item in responses]
        self.calls: list[dict[str, Any]] = []

    def execution_identity(self) -> dict[str, str]:
        return {
            "provider": "fake-foundry",
            "deployment": "gpt-test-domain",
            "api_version": "2026-08-01-preview",
        }

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(kwargs))
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return copy.deepcopy(self._responses[index])

    @property
    def correction_instructions(self) -> list[str | None]:
        instructions: list[str | None] = []
        for call in self.calls:
            user_message = call["user"]
            assert user_message.startswith(_USER_MESSAGE_START)
            assert user_message.endswith(_USER_MESSAGE_END)
            payload = json.loads(
                user_message[
                    len(_USER_MESSAGE_START) : -len(_USER_MESSAGE_END)
                ]
            )
            instructions.append(payload.get("user_correction_instruction"))
        return instructions


@pytest.fixture()
def runner():
    return make_cli_runner()


@pytest.fixture(autouse=True)
def _stub_source_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    def _build_source_profile(
        _source_path: Path,
        domain_description: str | None = None,
        *,
        include_proposal_samples: bool = False,
    ) -> SourceProfile:
        profile = _source_profile().model_copy(deep=True)
        if not include_proposal_samples:
            profile.proposal_samples = []
            profile.sampling_warnings = []
        if domain_description is not None:
            profile.domain_description = domain_description
        return profile

    monkeypatch.setattr(init_domain_module, "build_source_profile", _build_source_profile)


def _write_intake(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _draft_artifacts(tmp_path: Path) -> DraftArtifacts:
    return DraftArtifacts(
        contract_path=tmp_path / "domain.yaml",
        profile_path=tmp_path / ".fkg" / "source-profile.json",
        proposal_path=tmp_path / ".fkg" / "domain-proposal.json",
    )


def _invoke_init_domain(
    runner,
    tmp_path: Path,
    fake_client: FakeFoundryClient,
    *,
    intake_payload: dict[str, Any],
    candidate_responses: list[dict[str, Any]] | None = None,
    intake_suffix: str = ".json",
    input_text: str = "",
    non_interactive: bool = False,
) -> tuple[Any, DraftArtifacts, FakeFoundryClient]:
    del candidate_responses  # responses are provided through fake_client
    artifacts = _draft_artifacts(tmp_path)
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    intake_path = tmp_path / f"domain-intake{intake_suffix}"
    _write_intake(intake_path, intake_payload)
    args = [
        "init-domain",
        "--input",
        str(source_dir),
        "--intake",
        str(intake_path),
        "--out",
        str(artifacts.contract_path),
        "--profile-out",
        str(artifacts.profile_path),
        "--proposal-out",
        str(artifacts.proposal_path),
    ]
    if non_interactive:
        args.append("--non-interactive")
    result = runner.invoke(
        cli,
        args,
        input=input_text,
        obj={
            "_foundry_client": fake_client,
            "_foundry_model_version": "test-model",
        },
    )
    return result, artifacts, fake_client


def _invoke_domain_approve(
    runner,
    artifacts: DraftArtifacts,
    *,
    approved_by: str | None,
):
    args = [
        "domain",
        "approve",
        "--file",
        str(artifacts.contract_path),
        "--proposal",
        str(artifacts.proposal_path),
        "--source-profile",
        str(artifacts.profile_path),
    ]
    if approved_by is not None:
        args.extend(["--approved-by", approved_by])
    return runner.invoke(cli, args)


def _assert_draft_artifacts(artifacts: DraftArtifacts) -> None:
    contract = load_domain_contract(artifacts.contract_path)
    proposal = load_domain_proposal(artifacts.proposal_path)
    profile = load_source_profile(artifacts.profile_path)
    assert contract.approval.status == "draft"
    assert proposal.contract.approval.status == "draft"
    assert profile.approved is False


def _create_schema_2_draft(
    runner,
    tmp_path: Path,
) -> tuple[DraftArtifacts, FakeFoundryClient]:
    result, artifacts, fake_client = _invoke_init_domain(
        runner,
        tmp_path,
        FakeFoundryClient([_supported_candidates()]),
        intake_payload=_five_question_intake(),
        non_interactive=True,
    )
    assert result.exit_code == 0, combined_output(result)
    return artifacts, fake_client


def test_interactive_one_summary_approve_marks_contract_and_profile_approved(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FABRIC_KG_APPROVER", "interactive-owner@example.com")

    result, artifacts, fake_client = _invoke_init_domain(
        runner,
        tmp_path,
        FakeFoundryClient([_supported_candidates()]),
        intake_payload=_five_question_intake(),
        input_text="approve\n",
    )

    output = combined_output(result)
    assert result.exit_code == 0, output
    assert output.count("Domain proposal summary") == 1
    assert "approval is still required" not in output
    assert len(fake_client.calls) == 1
    assert (
        "path 1: relationship-type:contains (contains); "
        "entity-type:equipment -> entity-type:facility; traversal=reverse"
        in output
    )
    assert (
        "path 2: relationship-type:work-order-for (work_order_for); "
        "entity-type:equipment -> entity-type:work-order; traversal=reverse"
        in output
    )

    contract = load_domain_contract(artifacts.contract_path)
    profile = load_source_profile(artifacts.profile_path)
    proposal = load_domain_proposal(artifacts.proposal_path)
    assert output.count("      path ") == sum(
        len(plan.required_path) for plan in proposal.contract.question_plans
    )

    assert contract.approval.status == "approved"
    assert contract.approval.approved_by == "interactive-owner@example.com"
    assert contract.approval.contract_hash == compute_contract_hash(contract)
    assert contract.approval.proposal_hash == proposal.proposal_hash
    assert profile.approved is True
    assert profile.approved_by == "interactive-owner@example.com"


def test_free_form_correct_triggers_second_generation_and_rerenders_summary(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FABRIC_KG_APPROVER", "interactive-owner@example.com")
    correction = "Use more precise wording for the approved domain description."
    first = _supported_candidates(
        description="Initial facility maintenance proposal description."
    )
    second = _supported_candidates(
        description="Corrected facility maintenance proposal description."
    )

    result, artifacts, fake_client = _invoke_init_domain(
        runner,
        tmp_path,
        FakeFoundryClient([first, second]),
        intake_payload=_five_question_intake(),
        input_text=f"correct\n{correction}\napprove\n",
    )

    output = combined_output(result)
    assert result.exit_code == 0, output
    assert output.count("Domain proposal summary") == 2
    assert "Initial facility maintenance proposal description." in output
    assert "Corrected facility maintenance proposal description." in output
    assert fake_client.correction_instructions == [None, correction]

    proposal = load_domain_proposal(artifacts.proposal_path)
    contract = load_domain_contract(artifacts.contract_path)
    assert proposal.correction_instruction == correction
    assert contract.domain.description == second["domain_description"]


def test_abort_leaves_draft_artifacts_unapproved_and_exits_4(
    runner,
    tmp_path: Path,
) -> None:
    result, artifacts, fake_client = _invoke_init_domain(
        runner,
        tmp_path,
        FakeFoundryClient([_supported_candidates()]),
        intake_payload=_five_question_intake(),
        input_text="abort\n",
    )

    output = combined_output(result)
    assert result.exit_code == 4, output
    assert "aborted; draft artifacts remain unapproved" in output
    assert len(fake_client.calls) == 1
    _assert_draft_artifacts(artifacts)


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_noninteractive_intake_writes_draft_artifacts_and_explicit_approval_guidance(
    runner,
    tmp_path: Path,
    suffix: str,
) -> None:
    result, artifacts, fake_client = _invoke_init_domain(
        runner,
        tmp_path,
        FakeFoundryClient([_supported_candidates()]),
        intake_payload=_five_question_intake(),
        intake_suffix=suffix,
        non_interactive=True,
    )

    output = combined_output(result)
    assert result.exit_code == 0, output
    assert len(fake_client.calls) == 1
    assert "draft proposal written" in output
    assert "draft schema-2.0 contract written" in output
    assert "approval is still required: fabric-kg domain approve" in output
    assert '--approved-by "$OPERATOR"' in output
    _assert_draft_artifacts(artifacts)


def test_domain_approve_seals_hashes_when_given_explicit_inputs(
    runner,
    tmp_path: Path,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)

    result = _invoke_domain_approve(
        runner,
        artifacts,
        approved_by="approver@example.com",
    )

    output = combined_output(result)
    assert result.exit_code == 0, output
    assert "approved schema-2.0 contract" in output

    contract = load_domain_contract(artifacts.contract_path)
    proposal = load_domain_proposal(artifacts.proposal_path)
    profile = load_source_profile(artifacts.profile_path)

    assert contract.approval.status == "approved"
    assert contract.approval.approved_by == "approver@example.com"
    assert contract.approval.contract_hash == compute_contract_hash(contract)
    assert contract.approval.proposal_hash == proposal.proposal_hash
    assert contract.approval.source_profile_hash == proposal.source_profile_hash
    assert contract.approval.prompt_hash == proposal.prompt_hash
    assert contract.approval.prompt_version == proposal.prompt_version
    assert contract.approval.model_version == "test-model"
    assert contract.approval.model_hash == proposal.model_hash
    assert profile.approved is True
    assert profile.approved_by == "approver@example.com"
    assert compute_source_profile_hash(profile) == proposal.source_profile_hash
    status = evaluate_domain_guard_status(str(artifacts.contract_path))
    assert status.ready_for_enrichment is True
    (
        brief,
        manifest_path,
        domain_hash,
        schema_version,
        schema2_context,
    ) = _resolve_domain_brief(
        domain_prompt=None,
        domain_file=str(artifacts.contract_path),
        output_dir=tmp_path / "enriched",
    )
    assert brief.key_relationship_types
    assert manifest_path.is_file()
    assert domain_hash == contract.approval.contract_hash
    assert schema_version == "2.0"
    assert schema2_context.contract_hash == domain_hash


def test_domain_approve_requires_explicit_approved_by_for_schema_2(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)
    monkeypatch.setenv("FABRIC_KG_APPROVER", "env-owner@example.com")

    result = _invoke_domain_approve(runner, artifacts, approved_by=None)

    assert result.exit_code != 0
    assert (
        "Schema-2.0 approval requires an explicit --approved-by identity."
        in combined_output(result)
    )


def test_domain_approve_rejects_changed_contract_contents(
    runner,
    tmp_path: Path,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)
    contract = load_domain_contract(artifacts.contract_path)
    contract.domain.description = "Mutated after proposal generation."
    from fabric_kg_builder.domain import save_domain_contract

    save_domain_contract(contract, artifacts.contract_path)

    result = _invoke_domain_approve(
        runner,
        artifacts,
        approved_by="approver@example.com",
    )

    assert result.exit_code != 0
    assert (
        "Domain contract does not match the proposal contract hash."
        in combined_output(result)
    )


def test_domain_approve_rejects_proposal_hash_mismatch_after_model_hash_change(
    runner,
    tmp_path: Path,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)
    payload = json.loads(artifacts.proposal_path.read_text(encoding="utf-8"))
    payload["model_hash"] = "f" * 64
    artifacts.proposal_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = _invoke_domain_approve(
        runner,
        artifacts,
        approved_by="approver@example.com",
    )

    assert result.exit_code != 0
    assert (
        "Domain proposal hash is stale or mismatched. Regenerate the proposal."
        in combined_output(result)
    )


def test_domain_approve_rejects_embedded_proposal_contract_mismatch(
    runner,
    tmp_path: Path,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)
    mutated_proposal = json.loads(
        artifacts.proposal_path.read_text(encoding="utf-8")
    )
    mutated_proposal["contract"]["domain"]["description"] = (
        "Proposal content drifted."
    )
    artifacts.proposal_path.write_text(
        json.dumps(mutated_proposal, indent=2),
        encoding="utf-8",
    )

    result = _invoke_domain_approve(
        runner,
        artifacts,
        approved_by="approver@example.com",
    )

    assert result.exit_code != 0
    assert (
        "contract_hash must match the embedded proposal contract."
        in combined_output(result)
    )


def test_domain_approve_rejects_changed_source_profile(
    runner,
    tmp_path: Path,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)
    profile = load_source_profile(artifacts.profile_path)
    profile.domain_description = "Different profile context."
    save_source_profile(profile, artifacts.profile_path)

    result = _invoke_domain_approve(
        runner,
        artifacts,
        approved_by="approver@example.com",
    )

    assert result.exit_code != 0
    assert "Source profile is stale or does not match the proposal." in combined_output(result)


def test_domain_approve_rejects_prompt_hash_mismatch(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _fake_client = _create_schema_2_draft(runner, tmp_path)
    monkeypatch.setattr(proposal_module, "compute_prompt_hash", lambda: "e" * 64)

    result = _invoke_domain_approve(
        runner,
        artifacts,
        approved_by="approver@example.com",
    )

    assert result.exit_code != 0
    assert (
        "Proposal prompt hash does not match the installed prompt version."
        in combined_output(result)
    )


def test_unsupported_business_critical_question_stays_visible_and_blocks_approval(
    runner,
    tmp_path: Path,
) -> None:
    result, artifacts, fake_client = _invoke_init_domain(
        runner,
        tmp_path,
        FakeFoundryClient([_unsupported_candidates(), _unsupported_candidates()]),
        intake_payload=_unsupported_intake(),
        input_text="approve\nAdd cited vendor SLA sources.\nabort\n",
    )

    output = combined_output(result)
    assert result.exit_code == 4, output
    assert "cq:vendor-response-commitment" in output
    assert "UNSUPPORTED: Representative samples do not cite vendor response-time commitments" in output
    assert "approval blocked by 1 deterministic error(s)" in output
    assert output.count("Domain proposal summary") == 2
    assert len(fake_client.calls) == 2
    _assert_draft_artifacts(artifacts)


def test_legacy_schema_1_init_compatibility_path_still_writes_schema_1_contract(
    runner,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    contract_path = tmp_path / "legacy-domain.yaml"
    profile_path = tmp_path / ".fkg" / "legacy-source-profile.json"

    result = runner.invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source_dir),
            "--out",
            str(contract_path),
            "--profile-out",
            str(profile_path),
            "--approve",
        ],
    )

    output = combined_output(result)
    assert result.exit_code == 0, output
    assert "--approve selects legacy schema-1.0 compatibility" in output
    contract = load_domain_contract(contract_path)
    assert contract.schema_version == "1.0"
    assert load_source_profile(profile_path).proposal_samples == []


@pytest.mark.parametrize(
    "args",
    [
        ["domain", "init", "--help"],
        ["domain", "review", "--help"],
        ["domain", "approve", "--help"],
    ],
)
def test_legacy_domain_command_paths_remain_available(runner, args: list[str]) -> None:
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, combined_output(result)
