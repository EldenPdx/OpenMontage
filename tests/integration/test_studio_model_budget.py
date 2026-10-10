"""Unpriced receipts can exhaust the budget before the next native model POST."""

import pytest

from lib.config_model import PiProfile, StudioConfig
from production.contracts import ApprovalDecision, CallIntent, TaskCreate, TaskState
from production.pi_config import snapshot_for
from production.worker import Worker
from tests.fixtures.studio.model_server import model_server, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory
from tools.tool_registry import ToolRegistry


@pytest.mark.asyncio
async def test_real_pi_budget_exhaustion_retains_receipt_hold_and_reports_reconciliation(repository, tmp_path):
    require_pi()

    def reply(body):
        return [tool_item("openmontage", {"action": "catalog", "input": {}}, "budget_catalog")]

    with model_server(reply) as (url, requests):
        profile = PiProfile(provider="local", model="test-model", base_url=url,
                            credential_env="STUDIO_LOCAL_KEY", reasoning=False, thinking_level="off",
                            max_output_tokens=256, max_turns=3, request_timeout_seconds=5,
                            idle_timeout_seconds=10, task_timeout_seconds=30, price=None)
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        request = TaskCreate(brief="Discover the available local production tools", profile_id="local",
                             budget_usd_micros=500_000)
        task = repository.create_task(request, snapshot_for(profile, request), "model-budget-create")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects",
                        environment={"STUDIO_LOCAL_KEY": "local-only-budget-key"}, registry=ToolRegistry())
        waiting = await worker.run_once()
        assert waiting.state == TaskState.AWAITING_APPROVAL
        assert waiting.approval.stage == "model_cost"
        assert requests == []
        assert waiting.approval.scope.authorized_limit_usd_micros == 500_000
        repository.decide_gate(ApprovalDecision(expected_version=waiting.version,
                                               binding=waiting.approval.binding, decision="approve"),
                               idempotency_key="model-budget-approve")
        outcome = await worker.run_once()

        assert len(requests) == 1, "The budget guard must stop before a second model POST"
        receipts = [intent for intent in repository.unresolved_intents(task.task_id) if isinstance(intent, CallIntent)]
        assert len(receipts) == 1
        assert receipts[0].status == "receipted"
        assert receipts[0].usage["input"] == 10 and receipts[0].usage["output"] == 5
        assert receipts[0].actual_usd_micros is None, "Token usage does not establish the provider's unquoted fee"
        assert outcome.cost.reserved_usd_micros == 500_000
        assert outcome.cost.spent_usd_micros == 0
        assert outcome.cost.price_status == "unquoted"
        assert outcome.state == TaskState.BLOCKED, outcome.model_dump(mode="json")
        assert outcome.error.code == "budget_exceeded"
        assert "reconcile" in outcome.error.recovery_actions
        assert await worker.run_once() is None, "Budget exhaustion must await verified billing evidence"
        assert len(requests) == 1
        assert repository.get_call(receipts[0].call_id).actual_usd_micros is None
        assert repository.get_task(task.task_id).cost.reserved_usd_micros == 500_000
