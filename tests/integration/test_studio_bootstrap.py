"""Run-bound instructions reach real Pi before the first paid model request."""

import json
from pathlib import Path

import pytest
import yaml

from backlot.server import create_app
from fastapi.testclient import TestClient
from production.contracts import TaskCreate, TaskState
from production.pi_config import snapshot_for
from production.policy import media_configuration_sha256
from production.worker import Worker
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_media_discovery import configured_media
from tests.integration.test_studio_repository import repository, repository_factory
from tests.integration.test_studio_worker import local_config
from tools.graphics.newapi_image import NewAPIImage
from tools.tool_registry import ToolRegistry
from tools.video.newapi_video import NewAPIVideo


def create_worker(repository, tmp_path, endpoint, configured_media):
    path, selected, _, _ = configured_media
    config = local_config(endpoint)
    request = TaskCreate(brief="A short cinematic video", profile_id="local")
    task = repository.create_task(request, snapshot_for(config.profiles["local"], request,
                                  media_models=selected, media_configuration_sha256=media_configuration_sha256(path)),
                                  "bootstrap-create-0001")
    registry = ToolRegistry()
    registry.register(NewAPIVideo(path))
    registry.register(NewAPIImage(path))
    worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects",
                    environment={"STUDIO_LOCAL_KEY": "local-only-bootstrap-key"}, registry=registry)
    return worker, task


@pytest.mark.asyncio
async def test_first_real_pi_model_request_includes_verbatim_guide_and_selected_frozen_catalog(repository, tmp_path, configured_media):
    require_pi()
    _, selected, video, image = configured_media
    def reply(body):
        if len(requests) == 1:
            return [tool_item("openmontage", {"action": "initialize", "input": {
                "title": "Bootstrap project", "pipeline_type": "cinematic"}}, "bootstrap-initialize")]
        return [text_item("The project is initialized; a text response is not a finished video.")]

    with model_server(reply) as (endpoint, requests):
        worker, task = create_worker(repository, tmp_path, endpoint, configured_media)
        result = await worker.run_once()
        assert len(requests) == 2
        marker = json.loads((tmp_path / "projects" / task.project_id / "project.json").read_text())
        app = create_app(studio_repository=repository, studio_config=worker.config, studio_environment=worker.environment)
        app.state.studio_projects_dir = worker.projects_dir
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/studio/config").json()["csrf_token"]
            response = client.post(f"/api/studio/tasks/{task.task_id}/resume",
                                   headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": token, "Idempotency-Key": "bootstrap-resume-0001"},
                                   json={"expected_version": result.version})
            assert response.status_code == 202, response.text
            resumed = await worker.run_once()
        assert len(requests) == 3
    resume_prompt = next(item for item in reversed(requests[-1]["input"]) if item.get("role") == "user")
    resume_content = "".join(part.get("text", "") for part in resume_prompt["content"])
    resumed_bootstrap = json.loads(resume_content.splitlines()[-1])["bootstrap"]
    assert resumed_bootstrap["project"] == {"path": "project.json", "content": marker}
    prompt = next(item for item in requests[0]["input"] if item.get("role") == "user")
    content = "".join(part.get("text", "") for part in prompt["content"])
    bootstrap = json.loads(content.splitlines()[-1])["bootstrap"]
    assert bootstrap["project"] is None
    assert bootstrap["instructions"] == {"path": "AGENT_GUIDE.md",
                                           "content": (Path(__file__).resolve().parents[2] / "AGENT_GUIDE.md").read_text()}
    rows = {row["name"]: row for row in bootstrap["catalog"]["tools"]}
    assert set(rows["newapi_video"]["model_catalog"]) == {selected["video"]}
    assert set(rows["newapi_image"]["model_catalog"]) == {selected["image"]}
    assert rows["newapi_video"]["model_catalog"][selected["video"]]["defaults"] == video["defaults"]
    assert rows["newapi_image"]["model_catalog"][selected["image"]]["defaults"] == image["defaults"]
    assert "media-discovery-fixture-key" not in content
    assert "gateway.private.example" not in content
    assert "local-only-bootstrap-key" not in content
    assert result.state == TaskState.BLOCKED and result.error.code == "invalid_artifact"
    assert resumed.state == TaskState.BLOCKED and resumed.error.code == "invalid_artifact"
    assert repository.unresolved_intents(task.task_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "changed", "oversized_project", "private_project_alias"])
async def test_bootstrap_refuses_missing_or_changed_frozen_catalog_before_any_model_post(repository, tmp_path, configured_media, change):
    require_pi()
    path, selected, _, _ = configured_media
    with model_server() as (endpoint, requests):
        worker, task = create_worker(repository, tmp_path, endpoint, configured_media)
        if change == "missing":
            path.unlink()
        elif change == "changed":
            value = yaml.safe_load(path.read_text())
            value["newapi"]["models"][selected["video"]]["defaults"]["metadata"]["generate_audio"] = True
            path.write_text(yaml.safe_dump(value))
        else:
            marker = tmp_path / "projects" / task.project_id / "project.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            if change == "oversized_project":
                marker.write_text(json.dumps({"title": "x" * 262_145}))
            else:
                private = tmp_path / ".private.json"
                private.write_text('{"private":"must not reach the model"}')
                marker.symlink_to(private)
        result = await worker.run_once()
    assert requests == []
    expected = {"oversized_project": "not_found", "private_project_alias": "forbidden"}.get(change, "profile_unavailable")
    assert result.state == TaskState.BLOCKED and result.error.code == expected
    assert repository.unresolved_intents(task.task_id) == []
    assert result.cost.spent_usd_micros == 0 and result.cost.reserved_usd_micros == 0
