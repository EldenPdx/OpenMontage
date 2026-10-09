"""Browser decisions bind the displayed current files and durable PostgreSQL gate."""

from hashlib import sha256

from production.contracts import CallIntent, TaskState
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.integration.test_studio_api import api


def pending_gate(api, tmp_path):
    from production.tool_bridge import ProductionToolBridge
    from tools.tool_registry import ToolRegistry

    client, repo, headers = api
    task = client.post("/api/studio/tasks", headers=headers,
                       json={"brief": "制作测试短片", "profile_id": "local"}).json()
    claim = repo.claim_command("approval-worker", lease_seconds=120)
    repo.transition(task["task_id"], TaskState.RUNNING, expected_version=task["version"], fence=claim.context.fence)
    projects = tmp_path / "projects"
    client.app.state.studio_projects_dir = projects
    bridge = ProductionToolBridge(repo, projects,
                                  model_profile=client.app.state.studio_config.profiles["local"], registry=ToolRegistry())
    bridge.bind(claim.context)
    bridge.store.initialize(claim.context, title="Approval test", pipeline_type="framework-smoke")
    value = sample_artifact("research_brief")
    bridge.checkpoint(claim.context, stage="research", status="awaiting_human", artifacts={"research_brief": value},
                      summary="请确认当前研究版本")
    repo.finish_command(claim)
    task = repo.get_task(task["task_id"])
    body = {"binding": task.approval.binding.model_dump(mode="json"), "expected_version": task.version,
            "decision": "approve"}
    return client, repo, headers, task, body, projects


def test_approval_is_version_bound_idempotent_and_queues_one_continuation(api, tmp_path):
    client, repo, headers, task, body, projects = pending_gate(api, tmp_path)
    path = f"/api/studio/tasks/{task.task_id}/approvals/{task.approval.binding.gate_id}/decision"
    headers = {**headers, "Idempotency-Key": "browser-approve-0001"}
    old = client.post(path, headers=headers, json={**body, "expected_version": task.version - 1})
    assert old.status_code == 409
    first = client.post(path, headers=headers, json=body)
    assert first.status_code == 202
    duplicate = client.post(path, headers=headers, json=body)
    assert duplicate.json() == first.json()
    assert first.json()["state"] == "queued"
    claim = repo.claim_command("approval-worker")
    assert claim.command.kind == "continue"
    assert repo.claim_command("another-worker") is None
    # Browser approval itself does not silently rewrite the production checkpoint.
    checkpoint = projects / task.project_id / task.approval.checkpoint.path
    assert sha256(checkpoint.read_bytes()).hexdigest() == task.approval.checkpoint.sha256
    repo.finish_command(claim)


def test_changed_artifact_is_rejected_and_revision_queues_only_the_user_feedback(api, tmp_path):
    client, repo, headers, task, body, projects = pending_gate(api, tmp_path)
    path = f"/api/studio/tasks/{task.task_id}/approvals/{task.approval.binding.gate_id}/decision"
    artifact = projects / task.project_id / task.approval.artifact.path
    original = artifact.read_bytes()
    artifact.write_bytes(original + b" ")
    conflict = client.post(path, headers={**headers, "Idempotency-Key": "browser-approve-0001"}, json=body)
    assert conflict.status_code == 409
    assert repo.get_task(task.task_id).state == TaskState.AWAITING_APPROVAL
    artifact.write_bytes(original)
    revised = client.post(path, headers={**headers, "Idempotency-Key": "browser-revise-0001"},
                          json={**body, "decision": "revise", "comment": "请解释得更简单"})
    assert revised.status_code == 202
    assert revised.json()["approval"]["status"] == "revised"
    claim = repo.claim_command("approval-worker")
    assert claim.command.kind == "revise"
    assert claim.command.payload["decision"]["comment"] == "请解释得更简单"
    assert repo.get_task(task.task_id).cost.spent_usd_micros == task.cost.spent_usd_micros
    repo.finish_command(claim)


def test_resume_rejects_unknown_submissions_but_requeues_a_recoverable_block_once(api):
    client, repo, headers = api
    created = client.post("/api/studio/tasks", headers=headers,
                          json={"brief": "制作测试短片", "profile_id": "local"}).json()
    claim = repo.claim_command("approval-worker")
    running = repo.transition(created["task_id"], TaskState.RUNNING, expected_version=created["version"], fence=claim.context.fence)
    blocked = repo.transition(running.task_id, TaskState.BLOCKED, expected_version=running.version, fence=claim.context.fence)
    repo.finish_command(claim)
    path = f"/api/studio/tasks/{blocked.task_id}/resume"
    headers = {**headers, "Idempotency-Key": "browser-resume-0001"}
    resumed = client.post(path, headers=headers, json={"expected_version": blocked.version})
    assert resumed.status_code == 202
    duplicate = client.post(path, headers=headers, json={"expected_version": blocked.version})
    assert duplicate.json()["version"] == resumed.json()["version"]
    next_claim = repo.claim_command("approval-worker")
    assert next_claim.command.kind == "resume"
    task = repo.get_task(blocked.task_id)
    running = repo.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=next_claim.context.fence)
    intent = CallIntent(call_id="lost-model-call", task_id=task.task_id, run_id=task.run_id,
                        fence=next_claim.context.fence, kind="model", operation="inference", provider="controlled",
                        model="local-model", request_sha256="b" * 64, price_status="quoted", reserved_usd_micros=1000)
    submitted = repo.reserve_call(intent)
    repo.update_call(submitted.model_copy(update={"status": "submitted"}))
    repo.update_call(submitted.model_copy(update={"status": "outcome_unknown"}))
    recovery = repo.transition(task.task_id, TaskState.RECOVERY_REQUIRED, expected_version=running.version,
                               fence=next_claim.context.fence)
    repo.finish_command(next_claim)
    refused = client.post(path, headers={**headers, "Idempotency-Key": "browser-resume-0002"},
                          json={"expected_version": recovery.version})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "outcome_unknown"
    assert repo.get_task(task.task_id).cost.reserved_usd_micros == 1000
