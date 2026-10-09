"""Full production pipeline with real PostgreSQL, official Pi and local FFmpeg."""

import asyncio
import json
import subprocess

from fastapi.testclient import TestClient
import pytest

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.worker import Worker
from tests.fixtures.studio.model_server import model_server
from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
from tests.fixtures.studio.repository import require_pi, test_repository as isolated_repository


def test_browser_api_to_real_pi_full_pipeline_revision_and_verified_local_video(tmp_path, request):
    require_pi()
    projects, runtime = tmp_path / "projects", tmp_path / "runtime"
    with isolated_repository() as repo:
        flow = ProductionFlow(repo, projects)
        with model_server(flow) as (url, model_requests):
            profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="E2E_MODEL_KEY",
                                reasoning=False, thinking_level="off", context_window=200000, max_output_tokens=8192,
                                price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
            config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
            environment = {"E2E_MODEL_KEY": "e2e-private-credential"}
            registry, submissions = production_registry()
            request.addfinalizer(registry.fixture_manager.shutdown)
            worker = Worker(repo, config, runtime, projects, environment=environment, registry=registry)
            app = create_app(studio_repository=repo, studio_config=config, studio_environment=environment)
            app.state.studio_projects_dir = projects
            with TestClient(app, base_url="http://127.0.0.1") as client:
                token = client.get("/api/studio/config").json()["csrf_token"]
                headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": token, "Idempotency-Key": "e2e-create-0001"}
                response = client.post("/api/studio/tasks", headers=headers,
                                       json={"brief": "A two second animation of a blue lighthouse", "profile_id": "local",
                                             "duration_seconds": 2, "narration": False})
                assert response.status_code == 202
                task_id = response.json()["task_id"]
                stages = []
                script_revised = False
                for turn in range(8):
                    asyncio.run(worker.run_once())
                    task = client.get(f"/api/studio/tasks/{task_id}").json()
                    if task["state"] == "succeeded":
                        break
                    assert task["state"] == "awaiting_approval", (task.get("error"), flow.index, flow.errors)
                    gate = task["approval"]
                    stages.append(gate["stage"])
                    assert (runtime / "runs" / task_id / task["run_id"] / "process.json").exists() is False
                    decision = "revise" if gate["stage"] == "script" and not script_revised else "approve"
                    if decision == "revise":
                        script_revised = True
                    if gate["stage"] in {"proposal", "script", "scene_plan"}:
                        assert list(submissions) == []
                    accepted = client.post(f"/api/studio/tasks/{task_id}/approvals/{gate['binding']['gate_id']}/decision",
                        headers={**headers, "Idempotency-Key": f"e2e-decision-{turn:04d}"}, json={
                            "binding": gate["binding"], "expected_version": task["version"], "decision": decision,
                            "comment": "Make the script simpler" if decision == "revise" else ""})
                    assert accepted.status_code == 202, accepted.text
                else:
                    pytest.fail("The actual Pi did not complete the approved production pipeline")
                assert stages == ["proposal", "script", "script", "scene_plan", "assets"]
                assert task["result"]["verified"] is True
                assert task["result"]["duration_seconds"] == pytest.approx(2, abs=0.1)
                assert list(submissions) == [("newapi_image", "Images2.5-Flare"), ("newapi_video", "dreamina-seedance-2-5-260628")]
                download = client.get(task["result"]["download_url"])
                assert download.status_code == 200
                assert len(download.content) > 1000
                assert "e2e-private-credential" not in client.get(f"/api/studio/tasks/{task_id}").text
                assert "e2e-private-credential" not in client.get(f"/api/studio/tasks/{task_id}/events").text
                output = projects / task["project_id"] / task["result"]["video"]["path"]
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
                                       capture_output=True, text=True, check=True, timeout=10)
                assert any(stream["codec_type"] == "video" and stream["width"] == 1280
                           for stream in json.loads(probe.stdout)["streams"])
                assert len(model_requests) >= 20
                assert asyncio.run(worker.run_once()) is None
