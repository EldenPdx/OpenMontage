"""A revised exact media quote can be corrected without inheriting its consent."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from lib.config_model import PiPrice, PiProfile
from production.contracts import ContractViolation, TaskCreate, TaskState, ToolCall
from production.pi_config import prepare_pi, snapshot_for
from production.pi_rpc import PiRPC
from production.policy import media_configuration_sha256
from production.tool_bridge import BridgeServer, ProductionToolBridge
from tests.contracts.test_newapi_video import gateway, mp4
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_api import api
from tests.integration.test_studio_policy import intent
from tools.tool_registry import ToolRegistry
from tools.video.newapi_video import NewAPIVideo


def approve_plan(api, tmp_path, gateway, profile):
    client, repo, headers = api
    client.app.state.studio_config.profiles["local"] = profile
    projects = tmp_path / "projects"
    client.app.state.studio_projects_dir = projects
    gateway["configuration"]["newapi"]["models"]["gateway-video"] = {
        "capabilities": ["video_generation"], "operations": ["text_to_video"], "supports_sync": False, "supports_async": True,
        "supported_parameters": ["prompt", "seconds", "metadata"], "parameter_map": {"duration": "seconds"},
        "defaults": {"seconds": 4, "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": False}},
        "limits": {"seconds": {"type": "integer", "minimum": 4, "maximum": 30},
                   "metadata": {"type": "object", "properties": {"resolution": {"enum": ["480p", "720p"]},
                                                                "ratio": {"enum": ["16:9", "9:16"]},
                                                                "generate_audio": {"type": "boolean"}},
                                "required": ["resolution", "ratio"], "additionalProperties": False}},
    }
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]))
    request = TaskCreate(brief="A local cinematic request", profile_id="local", duration_seconds=12)
    task = repo.create_task(request, snapshot_for(profile, request, media_models={"video": "gateway-video"},
                                                media_configuration_sha256=media_configuration_sha256(gateway["config_path"])),
                            "media-revision-create")
    claim = repo.claim_command("media-revision-worker", lease_seconds=120)
    repo.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
    registry = ToolRegistry()
    registry.register(NewAPIVideo(config_path=gateway["config_path"]))
    bridge = ProductionToolBridge(repo, projects, model_profile=profile, registry=registry, runtime_root=tmp_path / "runtime")
    bridge.bind(claim.context)
    bridge.initialize(claim.context, title="Cost revision", pipeline_type="cinematic")
    bridge.checkpoint(claim.context, stage="research", status="completed",
                      artifacts={"research_brief": sample_artifact("research_brief")})
    for stage, name in (("proposal", "proposal_packet"), ("script", "script"), ("scene_plan", "scene_plan")):
        value = sample_artifact(name)
        if name == "proposal_packet":
            value["production_plan"]["render_runtime"] = "ffmpeg"
        bridge.checkpoint(claim.context, stage=stage, status="awaiting_human", artifacts={name: value})
        pending = repo.get_task(task.task_id)
        response = client.post(f"/api/studio/tasks/{task.task_id}/approvals/{pending.approval.binding.gate_id}/decision",
                               headers={**headers, "Idempotency-Key": "approve-" + stage},
                               json={"expected_version": pending.version, "binding": pending.approval.binding.model_dump(mode="json"),
                                     "decision": "approve"})
        assert response.status_code == 202
        repo.transition(task.task_id, TaskState.RUNNING, expected_version=response.json()["version"], fence=claim.context.fence)
        assert bridge.apply_approval(claim.context)
    return client, repo, headers, bridge, claim.context


def decide(api, context, decision, *, binding=None):
    client, repo, headers = api
    task = repo.get_task(context.task_id)
    binding = binding or task.approval.binding
    response = client.post(f"/api/studio/tasks/{task.task_id}/approvals/{binding.gate_id}/decision",
                           headers={**headers, "Idempotency-Key": decision + "-" + binding.gate_id},
                           json={"expected_version": task.version, "binding": binding.model_dump(mode="json"),
                                 "decision": decision, "comment": "Use the declared nested metadata fields"})
    return response


async def wait_settled(runner):
    async for event in runner.events():
        if event["type"] == "agent_settled":
            return
    raise AssertionError("Real Pi exited before settling")


@pytest.mark.asyncio
async def test_media_cost_revision_opens_a_new_exact_gate_in_the_same_real_pi_session(api, tmp_path, gateway):
    require_pi()
    bad = {"model": "gateway-video", "duration_seconds": 12, "aspect_ratio": "16:9", "generate_audio": True,
           "prompt": "A blue future city", "output_path": "assets/video/city.mp4"}
    good = {"model": "gateway-video", "duration_seconds": 12,
            "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": True},
            "prompt": "A blue future city", "output_path": "assets/video/city.mp4"}
    phase = {"revision": False}

    def reply(body):
        if body.get("input", [])[-1].get("type") == "function_call_output":
            return [text_item("The exact request is ready for the browser.")]
        return [tool_item("openmontage", {"action": "execute", "input": {
            "tool_name": "newapi_video", "inputs": good if phase["revision"] else bad,
        }}, "good-media" if phase["revision"] else "bad-media")]

    with model_server(reply) as (endpoint, model_requests):
        profile = PiProfile(provider="controlled", model="local-model", base_url=endpoint,
                            credential_env="TEST_PI_KEY", reasoning=False, thinking_level="off",
                            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        client, repo, headers, bridge, context = approve_plan(api, tmp_path, gateway, profile)

        async def prompt(context, command_id):
            loop_bridge = ProductionToolBridge(repo, tmp_path / "projects", model_profile=profile,
                                                registry=bridge.registry, runtime_root=tmp_path / "runtime")
            loop_bridge.bind(context)
            with BridgeServer(loop_bridge, context, profile) as control:
                managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"TEST_PI_KEY": "local-only-key"},
                                     trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
                runner = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **control.environment},
                               session_root=managed.session_root)
                session = await runner.start(context)
                try:
                    await runner.prompt("Propose the exact media request for browser review", command_id=command_id)
                    await asyncio.wait_for(wait_settled(runner), 15)
                    return session
                finally:
                    await runner.close()

        first_session = await prompt(context, "media-before-revision")
        waiting = repo.get_task(context.task_id)
        assert waiting.state == TaskState.AWAITING_APPROVAL and waiting.approval.stage == "media_cost"
        old_gate = waiting.approval
        evidence_path = tmp_path / "projects" / context.project_id / old_gate.artifact.path
        old_evidence = json.loads(evidence_path.read_text())
        assert gateway["calls"] == []
        response = decide(api, context, "revise")
        assert response.status_code == 202
        repo.transition(context.task_id, TaskState.RUNNING, expected_version=response.json()["version"], fence=context.fence)
        context = repo.bind_session(context, first_session)
        phase["revision"] = True
        second_session = await prompt(context, "media-after-revision")
        assert second_session.session_id == first_session.session_id
        new_task = repo.get_task(context.task_id)
        assert new_task.state == TaskState.AWAITING_APPROVAL
        new_gate = new_task.approval
        assert new_gate.stage == "media_cost" and new_gate.status == "pending"
        assert new_gate.binding.gate_id != old_gate.binding.gate_id
        assert new_gate.binding.artifact_sha256 != old_gate.binding.artifact_sha256
        new_evidence = json.loads(evidence_path.read_text())
        assert new_evidence["request_sha256"] != old_evidence["request_sha256"]
        assert new_evidence["inputs"] == good
        assert repo.approved_gate(context.task_id, old_gate.binding.gate_id) is None
        assert gateway["calls"] == []
        assert decide(api, context, "approve", binding=old_gate.binding).status_code == 409
        for payload in (bad, good):
            receipt = await bridge.execute(ToolCall(call_id="not-yet-approved", context=context, tool_name="newapi_video", inputs=payload))
            assert not receipt.success and receipt.error.code == "approval_conflict"
        response = decide(api, context, "approve")
        assert response.status_code == 202
        repo.transition(context.task_id, TaskState.RUNNING, expected_version=response.json()["version"], fence=context.fence)
        assert bridge.apply_approval(context)
        result = await bridge.execute(ToolCall(call_id="approved-corrected-request", context=context, tool_name="newapi_video", inputs=good))
        assert result.success, result.error
        posts = [call for call in gateway["calls"] if call[0] == "POST"]
        assert len(posts) == 1 and posts[0][3]["seconds"] == "12"
        assert posts[0][3]["metadata"] == {"resolution": "720p", "ratio": "16:9", "generate_audio": True}
        assert len(model_requests) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_stage,decision,opens_new_gate", [
    ("media_cost", "revise", True), ("media_cost", "reject", False),
    ("model_cost", "approve", False), ("model_cost", "revise", False),
])
async def test_only_revised_media_cost_can_repropose_an_exact_request(api, tmp_path, gateway, gate_stage, decision, opens_new_gate):
    profile = PiProfile(provider="controlled", model="local-model", credential_env="TEST_PI_KEY",
                        reasoning=False, thinking_level="off", price=None)
    client, repo, headers, bridge, context = approve_plan(api, tmp_path, gateway, profile)
    inputs = {"model": "gateway-video", "duration_seconds": 12, "prompt": "A blue city",
              "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": True},
              "output_path": "assets/video/city.mp4"}
    if gate_stage == "model_cost":
        with pytest.raises(ContractViolation):
            await bridge.authorize_model(intent(context, "model-consent-probe"))
    else:
        blocked = await bridge.execute(ToolCall(call_id="first-exact-cost", context=context, tool_name="newapi_video", inputs=inputs))
        assert not blocked.success and blocked.error.code == "approval_conflict"
    old = repo.get_task(context.task_id).approval
    assert old.stage == gate_stage
    response = decide(api, context, decision)
    assert response.status_code == 202
    if decision == "reject":
        response = client.post(f"/api/studio/tasks/{context.task_id}/resume", headers={**headers, "Idempotency-Key": "resume-rejected-cost"},
                               json={"expected_version": response.json()["version"]})
        assert response.status_code == 202
    repo.transition(context.task_id, TaskState.RUNNING, expected_version=response.json()["version"], fence=context.fence)
    if decision == "approve":
        assert bridge.apply_approval(context)
    result = await bridge.execute(ToolCall(call_id="corrected-exact-cost", context=context, tool_name="newapi_video", inputs=inputs))
    assert not result.success and result.error.code == "approval_conflict"
    current = repo.get_task(context.task_id)
    assert gateway["calls"] == []
    if opens_new_gate:
        assert current.state == TaskState.AWAITING_APPROVAL
        assert current.approval.stage == "media_cost" and current.approval.status == "pending"
        assert current.approval.binding.gate_id != old.binding.gate_id
    else:
        assert current.state == TaskState.RUNNING
        assert current.approval.binding == old.binding
        assert current.approval.status == ("approved" if decision == "approve" else "revised" if decision == "revise" else "rejected")


@pytest.mark.asyncio
async def test_revised_cost_gate_still_requires_completed_canonical_predecessors(api, tmp_path, gateway):
    profile = PiProfile(provider="controlled", model="local-model", credential_env="TEST_PI_KEY",
                        reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
    _, repo, _, bridge, context = approve_plan(api, tmp_path, gateway, profile)
    inputs = {"model": "gateway-video", "duration_seconds": 12, "prompt": "A blue city",
              "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": True},
              "output_path": "assets/video/city.mp4"}
    blocked = await bridge.execute(ToolCall(call_id="first-cost", context=context, tool_name="newapi_video", inputs=inputs))
    assert not blocked.success
    response = decide(api, context, "revise")
    assert response.status_code == 202
    repo.transition(context.task_id, TaskState.RUNNING, expected_version=response.json()["version"], fence=context.fence)
    old = repo.get_task(context.task_id).approval
    bridge.checkpoint(context, stage="script", status="failed", artifacts={})
    result = await bridge.execute(ToolCall(call_id="no-valid-script", context=context, tool_name="newapi_video", inputs=inputs))
    assert not result.success
    assert repo.get_task(context.task_id).approval.binding == old.binding
    assert gateway["calls"] == []
