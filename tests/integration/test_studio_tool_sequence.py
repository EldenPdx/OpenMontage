"""Official Pi batches preserve production dependencies and browser gates."""

import asyncio
import json
from pathlib import Path
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.contracts import TaskState
from production.pi_config import prepare_pi
from production.pi_rpc import PiRPC
from production.tool_bridge import BridgeServer
from production.worker import Worker
from tests.contracts.test_newapi_video import gateway, mp4
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
from tests.fixtures.studio.repository import require_pi, test_repository as isolated_repository
from tests.integration.test_studio_api import api
from tests.integration.test_studio_media_cost_revision import approve_plan, wait_settled


class BatchedComposeFlow(ProductionFlow):
    def _plan(self):
        super()._plan()
        guidance = [action for action in self.actions if action[0] == "read" and action[1]["path"] in {
            "skills/pipelines/explainer/compose-director.md", ".agents/skills/ffmpeg/SKILL.md"}]
        self.actions = [action for action in self.actions if action not in guidance]
        self.batch_index = next(index for index, action in enumerate(self.actions)
                                if action[0] == "checkpoint" and action[1]["stage"] == "edit")
        self.actions[self.batch_index:self.batch_index] = guidance
        self.batch_index += len(guidance)

    def __call__(self, body):
        if self.actions is None:
            self._plan()
        if self.index == self.batch_index:
            batch = self.actions[self.index:self.index + 2]
            self.index += 2
            return [tool_item("openmontage", {"action": action, "input": inputs}, f"batch-compose-{index}")
                    for index, (action, inputs) in enumerate(batch)]
        return super().__call__(body)


def delay_checkpoint(monkeypatch, stage):
    dispatch = BridgeServer.dispatch
    order = []

    def delayed(server, route, body):
        checkpoint = route == "/checkpoint" and body.get("stage") == stage
        if checkpoint:
            order.append("checkpoint-start")
            time.sleep(0.5)
        elif route == "/execute" and order:
            order.append("execute")
        result = dispatch(server, route, body)
        if checkpoint:
            order.append("checkpoint-complete")
        return result

    monkeypatch.setattr(BridgeServer, "dispatch", delayed)
    return order


def test_real_pi_batches_completed_edit_before_its_dependent_ffmpeg_render(tmp_path, request, monkeypatch):
    require_pi()
    order = delay_checkpoint(monkeypatch, "edit")
    projects, runtime = tmp_path / "projects", tmp_path / "runtime"
    with isolated_repository() as repo:
        flow = BatchedComposeFlow(repo, projects, revise_script=False)
        with model_server(flow) as (url, model_requests):
            profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="BATCH_MODEL_KEY",
                                reasoning=False, thinking_level="off", context_window=200000, max_output_tokens=8192,
                                price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
            config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
            environment = {"BATCH_MODEL_KEY": "local-fixture-key"}
            registry, _ = production_registry()
            request.addfinalizer(registry.fixture_manager.shutdown)
            worker = Worker(repo, config, runtime, projects, environment=environment, registry=registry)
            app = create_app(studio_repository=repo, studio_config=config, studio_environment=environment)
            app.state.studio_projects_dir = projects
            with TestClient(app, base_url="http://127.0.0.1") as client:
                token = client.get("/api/studio/config").json()["csrf_token"]
                headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": token, "Idempotency-Key": "batch-create-0001"}
                response = client.post("/api/studio/tasks", headers=headers,
                                       json={"brief": "A two second moving lighthouse", "profile_id": "local",
                                             "duration_seconds": 2, "narration": False})
                assert response.status_code == 202
                task_id = response.json()["task_id"]
                for turn in range(5):
                    asyncio.run(worker.run_once())
                    task = client.get(f"/api/studio/tasks/{task_id}").json()
                    if task["state"] == "succeeded":
                        break
                    assert task["state"] == "awaiting_approval", (task.get("error"), flow.errors, order)
                    gate = task["approval"]
                    approved = client.post(f"/api/studio/tasks/{task_id}/approvals/{gate['binding']['gate_id']}/decision",
                                           headers={**headers, "Idempotency-Key": f"batch-approve-{turn:04d}"},
                                           json={"binding": gate["binding"], "expected_version": task["version"], "decision": "approve"})
                    assert approved.status_code == 202, approved.text
                assert task["state"] == "succeeded", (task.get("error"), flow.errors, order)
                assert task["result"]["verified"] is True
                assert order == ["checkpoint-start", "checkpoint-complete", "execute"]
                assert any({"batch-compose-0", "batch-compose-1"}.issubset({item.get("call_id")
                           for item in body.get("input", []) if item.get("type") == "function_call_output"})
                           for body in model_requests)
                output = projects / task["project_id"] / task["result"]["video"]["path"]
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
                                       capture_output=True, text=True, check=True, timeout=10)
                assert any(stream["codec_type"] == "video" and stream["width"] == 1280
                           for stream in json.loads(probe.stdout)["streams"])
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
                               capture_output=True, check=True, timeout=20)


@pytest.mark.asyncio
async def test_real_pi_pending_gate_stops_the_remaining_paid_batch_and_next_model_post(api, tmp_path, gateway, mp4, monkeypatch):
    require_pi()
    order = delay_checkpoint(monkeypatch, "assets")
    actions = []

    def reply(body):
        return actions if body.get("input", [])[-1].get("type") != "function_call_output" else [text_item("Stop at the browser gate.")]

    with model_server(reply) as (url, model_requests):
        profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="BATCH_MODEL_KEY",
                            reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        _, repo, _, bridge, context = approve_plan(api, tmp_path, gateway, profile)
        bridge.tool_quotes = {("newapi_video", "gateway-video"): 0}
        existing = tmp_path / "projects" / context.project_id / "assets/video/reviewed.mp4"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(mp4)
        artifact = {"version": "1.0", "assets": [{"id": "existing-clip", "type": "video", "path": str(existing),
                                                   "source_tool": "newapi_video", "scene_id": "scene-1"}]}
        actions.extend([
            tool_item("openmontage", {"action": "checkpoint", "input": {
                "stage": "assets", "status": "awaiting_human", "artifacts": {"asset_manifest": artifact}}}, "batch-human-gate"),
            tool_item("openmontage", {"action": "execute", "input": {"tool_name": "newapi_video", "inputs": {
                "model": "gateway-video", "duration_seconds": 12, "prompt": "Do not generate past this gate",
                "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": True},
                "output_path": "assets/video/forbidden-extra.mp4"}}}, "batch-after-human-gate"),
        ])
        with BridgeServer(bridge, context, profile) as control:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"BATCH_MODEL_KEY": "local-fixture-key"},
                                 trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
            runner = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **control.environment}, session_root=managed.session_root)
            try:
                await runner.start(context)
                await runner.prompt("Review the assets; do not execute past a pending gate", command_id="batch-human-gate-command")
                await asyncio.wait_for(wait_settled(runner), 20)
                waiting = repo.get_task(context.task_id)
                assert waiting.state == TaskState.AWAITING_APPROVAL and waiting.approval.stage == "assets"
                assert not [call for call in gateway["calls"] if call[0] == "POST"]
                assert len(model_requests) == 1
                assert order == ["checkpoint-start", "checkpoint-complete", "execute"]
            finally:
                await runner.close()
