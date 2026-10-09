"""Explicit xvan compatibility checks. No live requests run in default CI."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from production.contracts import ApprovalDecision, ErrorDTO, TaskCreate, TaskState
from production.pi_config import load_studio_config, prepare_pi, snapshot_for

pytestmark = pytest.mark.live_api
ROOT = Path(__file__).resolve().parents[2]


def live_profile():
    if os.environ.get("STUDIO_XVAN_LIVE_SMOKE") != "1":
        pytest.skip("Enable the separate Studio xvan smoke explicitly")
    profile = load_studio_config().profiles["xvan"]
    if not os.environ.get(profile.credential_env):
        pytest.skip("The backend xvan credential reference is not configured")
    assert (profile.provider, profile.api, profile.model) == ("xvan", "openai-responses", "gpt-5.6-sol")
    return profile


def test_studio_xvan_readonly_catalog():
    profile = live_profile()
    response = requests.get(profile.base_url + "/models",
                            headers={"Authorization": "Bearer " + os.environ[profile.credential_env]}, timeout=30)
    assert response.status_code == 200, "The configured xvan catalog was unavailable"
    models = {item["id"] for item in response.json()["data"]}
    assert "gpt-5.6-sol" in models


@pytest.mark.asyncio
async def test_studio_xvan_real_pi_stream_tool_result_and_usage():
    profile = live_profile()
    if os.environ.get("STUDIO_XVAN_PAID_APPROVED") != "1":
        pytest.skip("Real inference needs separate operator authorization, including unknown gateway fees")
    if not os.environ.get("STUDIO_DATABASE_URL"):
        pytest.skip("Paid smoke requires persistent PostgreSQL journals")
    from production.pi_rpc import PiRPC
    from production.repository import PostgresRepository
    from production.tool_bridge import BridgeServer, ProductionToolBridge
    from tools.tool_registry import ToolRegistry

    # The dedicated persistent schema keeps unknown-price holds for reconciliation.
    repository = PostgresRepository(os.environ["STUDIO_DATABASE_URL"], schema="studio_live_smoke")
    repository.migrate()
    after = None
    while previous := repository.list_tasks(limit=200, after=after):
        if any(item.state in {TaskState.QUEUED, TaskState.RUNNING, TaskState.AWAITING_APPROVAL, TaskState.CANCEL_REQUESTED, TaskState.RECOVERY_REQUIRED} or item.cost.unknown_call_count for item in previous):
            pytest.fail("Reconcile earlier live smoke commands and fees before another paid sample")
        after = previous[-1].task_id
    profile = profile.model_copy(update={"max_output_tokens": 512, "max_turns": 2, "max_retries": 0})
    request = TaskCreate(brief="Pi streaming and one read-tool compatibility sample", budget_usd_micros=1_000_000)
    task = repository.create_task(request, snapshot_for(profile, request), "live-smoke-" + uuid4().hex)
    claim = repository.claim_command("live-smoke", lease_seconds=3600)
    assert claim is not None and claim.context.task_id == task.task_id
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
    bridge = ProductionToolBridge(repository, ROOT / "projects", model_profile=profile, registry=ToolRegistry())
    bridge.bind(claim.context)
    bridge.store.initialize(claim.context, title="Authorized Pi compatibility sample", pipeline_type="framework-smoke")
    report_path = ROOT / "projects" / task.project_id / "pi-live-report.json"
    events = []
    runner = control = None
    try:
        control = BridgeServer(bridge, claim.context, profile)
        managed = prepare_pi(profile, claim.context, ROOT / ".runtime/studio/live-smoke",
                             trusted_extension=ROOT / "pi-runtime/extensions/openmontage.ts")
        runner = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **control.environment},
                       session_root=managed.session_root, redact_values=(*managed.redact_values, control.token))
        session = await runner.start(claim.context)
        repository.bind_session(claim.context, session)
        await runner.prompt("Use openmontage read for AGENT_GUIDE.md exactly once, then reply OK. Do not produce media.",
                            command_id="live-first")
        async for event in runner.events():
            if event["type"] == "agent_settled":
                break
        pending = repository.get_task(task.task_id)
        assert pending.state == TaskState.AWAITING_APPROVAL and pending.approval.stage == "model_cost"
        repository.decide_gate(ApprovalDecision(expected_version=pending.version, binding=pending.approval.binding,
                                               decision="approve", comment="Separately authorized live inference smoke"),
                               idempotency_key="live-fee-approval-" + uuid4().hex)
        queued = repository.get_task(task.task_id)
        repository.transition(task.task_id, TaskState.RUNNING, expected_version=queued.version, fence=claim.context.fence)
        bridge.apply_approval(claim.context)
        await runner.prompt("The operator approved this sample. Read AGENT_GUIDE.md using openmontage once, then reply OK.",
                            command_id="live-authorized")
        async def consume():
            async for event in runner.events():
                events.append(event["type"])
                if event["type"] == "agent_settled":
                    return
        await asyncio.wait_for(consume(), timeout=profile.request_timeout_seconds * 3)
        assert "tool_execution_start" in events and "tool_execution_end" in events
        assert "message_update" in events
        assert repository.get_task(task.task_id).cost.unknown_call_count == 2
    finally:
        if runner is not None:
            await runner.close()
        if control is not None:
            control.close()
        current = repository.get_task(task.task_id)
        if current.state == TaskState.RUNNING:
            repository.transition(task.task_id, TaskState.BLOCKED, expected_version=current.version,
                                  fence=claim.context.fence, updates={"error": ErrorDTO(code="quote_required",
                                      message="Live smoke finished; actual gateway fees await trusted reconciliation")})
        repository.finish_command(claim)
        current = repository.get_task(task.task_id)
        report_path.write_text(json.dumps({"kind": "authorized_live_smoke", "task_id": task.task_id,
            "provider": profile.provider, "api": profile.api, "model": profile.model, "pi_version": "1.1.0",
            "observed_events": sorted(set(events)), "cost": current.cost.model_dump(mode="json"),
            "notice": "Reservations are estimates, not a gateway billing hard cap. Journals remain in studio_live_smoke."}, indent=2))
