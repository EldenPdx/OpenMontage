"""Ordinary browser approval preserves the cost evidence already displayed."""

import json

from production.contracts import CallIntent, TaskState
from production.tool_bridge import ProductionToolBridge
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.integration.test_studio_api import api
from tools.tool_registry import ToolRegistry


def test_browser_approval_preserves_known_spend_and_unquoted_hold_in_checkpoint(api, tmp_path):
    client, repo, headers = api
    created = client.post("/api/studio/tasks", headers=headers, json={
        "brief": "A local cinematic approval test", "profile_id": "local", "budget_usd_micros": 10_000_000,
    })
    assert created.status_code == 202
    task = created.json()
    claim = repo.claim_command("snapshot-worker", lease_seconds=120)
    repo.transition(task["task_id"], TaskState.RUNNING, expected_version=task["version"], fence=claim.context.fence)

    paid = repo.reserve_call(CallIntent(
        call_id="known-billing-receipt", task_id=task["task_id"], run_id=task["run_id"], fence=claim.context.fence,
        kind="tool", operation="generate", provider="controlled", model="local-model", request_sha256="a" * 64,
        price_status="quoted", reserved_usd_micros=2_000_000,
    ))
    repo.update_call(paid.model_copy(update={"status": "settled", "actual_usd_micros": 1_663_522}))
    held = repo.reserve_call(CallIntent(
        call_id="received-unquoted-call", task_id=task["task_id"], run_id=task["run_id"], fence=claim.context.fence,
        kind="model", operation="inference", provider="controlled", model="local-model", request_sha256="b" * 64,
        price_status="unquoted", reserved_usd_micros=500_000,
    ))
    repo.update_call(held.model_copy(update={"status": "receipted", "usage": {"input": 100, "output": 20}}))

    projects = tmp_path / "projects"
    client.app.state.studio_projects_dir = projects
    profile = client.app.state.studio_config.profiles["local"]
    bridge = ProductionToolBridge(repo, projects, model_profile=profile, registry=ToolRegistry())
    bridge.bind(claim.context)
    bridge.initialize(claim.context, title="Cost evidence", pipeline_type="cinematic")
    bridge.checkpoint(claim.context, stage="research", status="completed",
                      artifacts={"research_brief": sample_artifact("research_brief")})
    proposal = sample_artifact("proposal_packet")
    proposal["production_plan"]["render_runtime"] = "ffmpeg"
    bridge.checkpoint(claim.context, stage="proposal", status="awaiting_human",
                      artifacts={"proposal_packet": proposal})
    repo.finish_command(claim)
    task = client.get(f"/api/studio/tasks/{task['task_id']}").json()
    before_cost = task["cost"]
    gate = task["approval"]
    checkpoint_path = projects / task["project_id"] / gate["checkpoint"]["path"]
    expected_snapshot = {
        "total_spent_usd": 1.663522, "total_reserved_usd": 0.5, "budget_remaining_usd": 7.836478,
        "price_status": "unquoted", "unknown_call_count": 1,
    }
    assert json.loads(checkpoint_path.read_text())["cost_snapshot"] == expected_snapshot

    accepted = client.post(
        f"/api/studio/tasks/{task['task_id']}/approvals/{gate['binding']['gate_id']}/decision",
        headers={**headers, "Idempotency-Key": "approve-known-cost-evidence"},
        json={"expected_version": task["version"], "binding": gate["binding"], "decision": "approve"},
    )
    assert accepted.status_code == 202
    continuation = repo.claim_command("snapshot-worker", lease_seconds=120)
    assert continuation.command.kind == "continue"
    repo.transition(task["task_id"], TaskState.RUNNING, expected_version=accepted.json()["version"],
                    fence=continuation.context.fence)
    continued = ProductionToolBridge(repo, projects, model_profile=profile, registry=ToolRegistry())
    continued.bind(continuation.context)
    assert continued.apply_approval(continuation.context) is True
    completed = json.loads(checkpoint_path.read_text())
    assert completed["status"] == "completed" and completed["human_approved"] is True
    assert client.get(f"/api/studio/tasks/{task['task_id']}").json()["cost"] == before_cost
    assert completed.get("cost_snapshot") == expected_snapshot
