import pytest

from lib.config_model import PiProfile, StudioConfig
from production.contracts import ApprovalDecision, CallIntent, TaskCreate, TaskState
from production.pi_config import snapshot_for
from production.task_service import TaskService
from production.worker import Worker
from tests.fixtures.studio.model_server import model_server, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory
from tests.integration.test_studio_worker import completed_project
from tools.tool_registry import ToolRegistry


@pytest.mark.asyncio
async def test_verified_delivery_succeeds_when_budget_blocks_only_the_final_model_reply(repository, tmp_path, monkeypatch):
    require_pi()
    claims = []
    claim_command = repository.claim_command

    def capture_claim(*args, **kwargs):
        claim = claim_command(*args, **kwargs)
        if claim is not None:
            claims.append(claim)
        return claim

    monkeypatch.setattr(repository, "claim_command", capture_claim)

    def reply(body):
        context, _ = completed_project(repository, tmp_path, context=claims[-1].context)
        assert TaskService(tmp_path / "projects").completion(context).verified
        return [tool_item("openmontage", {"action": "catalog", "input": {}}, "final_catalog")]

    with model_server(reply) as (url, requests):
        profile = PiProfile(provider="local", model="test-model", base_url=url,
                            credential_env="STUDIO_LOCAL_KEY", reasoning=False, thinking_level="off",
                            max_output_tokens=256, max_turns=3, request_timeout_seconds=5,
                            task_timeout_seconds=30, price=None)
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        request = TaskCreate(brief="Deliver a verified one-second local video", profile_id="local",
                             duration_seconds=1, budget_usd_micros=500_000)
        task = repository.create_task(request, snapshot_for(profile, request), "completed-budget-create")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects",
                        environment={"STUDIO_LOCAL_KEY": "local-only-completed-budget-key"}, registry=ToolRegistry())
        waiting = await worker.run_once()
        assert waiting.state == TaskState.AWAITING_APPROVAL
        repository.decide_gate(ApprovalDecision(expected_version=waiting.version, binding=waiting.approval.binding,
                                               decision="approve"), idempotency_key="completed-budget-approve")
        outcome = await worker.run_once()

    assert len(requests) == 1, "The real provider guard must reject the extra summary request before POST"
    receipts = [intent for intent in repository.unresolved_intents(task.task_id) if isinstance(intent, CallIntent)]
    assert len(receipts) == 1 and receipts[0].status == "receipted"
    assert receipts[0].actual_usd_micros is None
    assert outcome.cost.reserved_usd_micros == 500_000
    assert outcome.state == TaskState.SUCCEEDED, outcome.error
    assert outcome.result.verified and outcome.result.video.path == "renders/final.mp4"
    assert (outcome.result.width, outcome.result.height, outcome.result.duration_seconds) == (320, 180, 1)
