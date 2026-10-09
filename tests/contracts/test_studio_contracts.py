"""Public Web Studio contracts; these fixtures are downstream module inputs."""

import pytest
from pydantic import ValidationError

from production.contracts import TaskCreate


def test_browser_submission_accepts_profile_but_cannot_inject_execution_settings():
    request = TaskCreate(brief="Explain how a lighthouse works", profile_id="xvan")
    assert request.narration is False
    assert request.budget_usd_micros == 10_000_000
    for injected in (
        {"base_url": "https://attacker.invalid"},
        {"output_path": "../../.env"},
        {"command": "curl https://attacker.invalid"},
    ):
        with pytest.raises(ValidationError):
            TaskCreate(brief="A lighthouse", profile_id="xvan", **injected)


def test_task_state_cannot_be_completed_by_a_pi_prompt_acknowledgement():
    from production.contracts import ContractViolation, check_transition

    with pytest.raises(ContractViolation, match="render"):
        check_transition("running", "succeeded")
    with pytest.raises(ContractViolation, match="state"):
        check_transition("running", "agent_settled")
    with pytest.raises(ContractViolation, match="transition"):
        check_transition("queued", "succeeded")
    check_transition("running", "awaiting_approval")
    check_transition("awaiting_approval", "queued")


def test_approval_is_bound_to_the_current_artifact_checkpoint_and_scope():
    from production.contracts import ApprovalBinding, ApprovalDecision, ContractViolation, check_approval

    binding = ApprovalBinding(
        task_id="task-a", run_id="run-a", gate_id="gate-script", checkpoint_revision=2,
        artifact_revision=2, checkpoint_sha256="a" * 64, artifact_sha256="b" * 64,
        scope_sha256="c" * 64,
    )
    decision = ApprovalDecision(expected_version=4, binding=binding, decision="approve")
    check_approval(decision, binding, current_version=4)
    for changed in (
        {"artifact_revision": 3}, {"checkpoint_sha256": "d" * 64},
        {"scope_sha256": "e" * 64}, {"run_id": "run-b"},
    ):
        with pytest.raises(ContractViolation) as error:
            check_approval(decision, binding.model_copy(update=changed), current_version=4)
        assert error.value.code == "approval_conflict"
    with pytest.raises(ContractViolation) as error:
        check_approval(decision, binding, current_version=5)
    assert error.value.code == "version_conflict"
    with pytest.raises(ValidationError):
        ApprovalDecision(expected_version=4, binding=binding, decision="revise")


def test_file_references_reject_external_absolute_and_parent_paths():
    from production.contracts import FileReference

    FileReference(path="artifacts/script.json", revision=1, sha256="a" * 64)
    for path in ("/etc/passwd", "https://attacker.invalid/media", "../.env", "assets/../.env", "C:\\secret", "assets//secret"):
        with pytest.raises(ValidationError):
            FileReference(path=path, revision=1, sha256="a" * 64)


def test_public_task_success_requires_a_verified_managed_render():
    from production.contracts import ConfigSnapshot, RenderResult, TaskRecord

    snapshot = ConfigSnapshot(
        profile_id="xvan", provider="xvan", model="gpt-5.6-sol", api="openai-responses",
        configuration_sha256="a" * 64,
    )
    fields = dict(task_id="task-a", project_id="project-a", run_id="run-a", version=2,
                  state="succeeded", request=TaskCreate(brief="A lighthouse"), config_snapshot=snapshot)
    with pytest.raises(ValidationError):
        TaskRecord(**fields)
    result = RenderResult(
        render_report={"path": "artifacts/render_report.json", "revision": 1, "sha256": "b" * 64},
        video={"path": "renders/final.mp4", "revision": 1, "sha256": "c" * 64},
        bytes=4096, duration_seconds=3, width=1920, height=1080, verified=True,
    )
    assert TaskRecord(**fields, result=result).state.value == "succeeded"
    with pytest.raises(ValidationError):
        RenderResult(**{**result.model_dump(), "verified": False})


def test_model_and_media_intents_keep_unknown_cost_distinct_from_free():
    from production.contracts import CallIntent

    intent = CallIntent(
        call_id="call-a", task_id="task-a", run_id="run-a", fence=3, kind="model",
        operation="prompt", provider="xvan", model="gpt-5.6-sol", request_sha256="a" * 64,
        price_status="unquoted",
    )
    assert intent.reserved_usd_micros is None
    assert intent.actual_usd_micros is None
    with pytest.raises(ValidationError):
        CallIntent(**{**intent.model_dump(), "status": "outcome_unknown", "actual_usd_micros": 0})


def test_gate_cannot_display_a_different_artifact_from_its_binding():
    from production.contracts import ApprovalRequest

    gate = {
        "binding": {"task_id": "task-a", "run_id": "run-a", "gate_id": "gate-script",
                    "checkpoint_revision": 2, "checkpoint_sha256": "a" * 64,
                    "artifact_revision": 2, "artifact_sha256": "b" * 64, "scope_sha256": "c" * 64},
        "stage": "script", "summary": "Review the script",
        "artifact": {"path": "artifacts/script.json", "revision": 3, "sha256": "b" * 64},
        "checkpoint": {"path": "checkpoint_script.json", "revision": 2, "sha256": "a" * 64},
        "scope": {"provider": "xvan", "model": "gpt-5.6-sol", "budget_usd_micros": 10000000,
                  "configuration_sha256": "d" * 64},
    }
    with pytest.raises(ValidationError):
        ApprovalRequest.model_validate(gate)


def test_fixed_downstream_fixtures_validate_through_public_schema_and_dtos():
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
    from production import contracts

    root = Path(__file__).resolve().parents[2] / "schemas" / "studio"
    schema = json.loads((root / "contracts.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    for case in json.loads((root / "fixtures" / "browser-flow.json").read_text(encoding="utf-8")):
        public_schema = {**schema, "$ref": "#/$defs/" + case["schema"]}
        Draft202012Validator(public_schema).validate(case["value"])
        getattr(contracts, case["schema"]).model_validate(case["value"])
    invalid = {"brief": "A lighthouse", "profile_id": "xvan", "output_path": "../../.env"}
    with pytest.raises(SchemaValidationError):
        Draft202012Validator({**schema, "$ref": "#/$defs/TaskCreate"}).validate(invalid)
    with pytest.raises(SchemaValidationError):
        Draft202012Validator({**schema, "$ref": "#/$defs/FileReference"}).validate({
            "path": "assets/../.env", "revision": 1, "sha256": "a" * 64,
        })


def test_same_state_bookkeeping_still_requires_a_real_mutation():
    from production.contracts import ContractViolation, check_transition

    check_transition("running", "running", updates={"current_stage": "script"})
    with pytest.raises(ContractViolation):
        check_transition("running", "running")


def test_unquoted_price_consent_requires_a_positive_bounded_reservation_limit():
    from production.contracts import ApprovalScope

    fields = dict(provider="local",model="test-model",budget_usd_micros=1000000,configuration_sha256="a"*64,unknown_price=True)
    for invalid in (None, 0, 1000001):
        with pytest.raises(ValidationError):
            ApprovalScope(**fields,authorized_limit_usd_micros=invalid)
    assert ApprovalScope(**fields,authorized_limit_usd_micros=250000).unknown_price is True


def test_frozen_media_configuration_has_a_valid_sha256_and_is_backend_only():
    from production.contracts import ConfigSnapshot

    fields=dict(profile_id="local",provider="local",model="test-model",api="openai-responses",configuration_sha256="a"*64)
    assert ConfigSnapshot(**fields,media_configuration_sha256="b"*64).media_configuration_sha256=="b"*64
    with pytest.raises(ValidationError):
        ConfigSnapshot(**fields,media_configuration_sha256="https://attacker.invalid")
    with pytest.raises(ValidationError):
        TaskCreate(brief="A lighthouse",media_configuration_sha256="b"*64)


@pytest.mark.parametrize("brief", ["   ", "broken\ud800text"])
def test_empty_or_invalid_unicode_brief_fails_at_the_submission_boundary(brief):
    with pytest.raises(ValidationError):
        TaskCreate(brief=brief)
