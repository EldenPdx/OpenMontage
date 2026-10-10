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
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.integration.test_studio_media_discovery import configured_media
from tests.integration.test_studio_repository import repository, repository_factory
from tests.integration.test_studio_worker import local_config
from tools.graphics.newapi_image import NewAPIImage
from tools.tool_registry import ToolRegistry
from tools.video.newapi_video import NewAPIVideo


def create_worker(repository, tmp_path, endpoint, configured_media, **profile_changes):
    path, selected, _, _ = configured_media
    config = local_config(endpoint, **profile_changes)
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
    assert resumed_bootstrap["current_checkpoint"] is None
    prompt = next(item for item in requests[0]["input"] if item.get("role") == "user")
    content = "".join(part.get("text", "") for part in prompt["content"])
    bootstrap = json.loads(content.splitlines()[-1])["bootstrap"]
    assert bootstrap["project"] is None
    assert bootstrap["current_checkpoint"] is None
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
async def test_backend_cost_approval_supplies_no_pipeline_checkpoint_and_preserves_unknown_fee_hold(repository, tmp_path, configured_media):
    require_pi()
    with model_server() as (endpoint, requests):
        worker, task = create_worker(repository, tmp_path, endpoint, configured_media, price=None)
        waiting = await worker.run_once()
        assert waiting.state == TaskState.AWAITING_APPROVAL and waiting.approval.stage == "model_cost"
        assert requests == []
        marker = worker.projects_dir / task.project_id / "checkpoint_model_cost.json"
        marker.symlink_to(tmp_path / "outside-project.json")
        app = create_app(studio_repository=repository, studio_config=worker.config, studio_environment=worker.environment)
        app.state.studio_projects_dir = worker.projects_dir
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/studio/config").json()["csrf_token"]
            approved = client.post(f"/api/studio/tasks/{task.task_id}/approvals/{waiting.approval.binding.gate_id}/decision",
                                   headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": token,
                                            "Idempotency-Key": "bootstrap-cost-approve-0001"},
                                   json={"binding": waiting.approval.binding.model_dump(mode="json"),
                                         "expected_version": waiting.version, "decision": "approve"})
            assert approved.status_code == 202, approved.text
            continued = await worker.run_once()
    assert len(requests) == 1
    prompt = next(item for item in reversed(requests[0]["input"]) if item.get("role") == "user")
    content = "".join(part.get("text", "") for part in prompt["content"])
    assert json.loads(content.splitlines()[-1])["bootstrap"]["current_checkpoint"] is None
    assert continued.state == TaskState.BLOCKED and continued.error.code == "invalid_artifact"
    assert continued.cost.reserved_usd_micros == 500_000 and continued.cost.spent_usd_micros == 0
    calls = repository.unresolved_intents(task.task_id)
    assert len(calls) == 1 and calls[0].status == "receipted" and calls[0].actual_usd_micros is None


@pytest.mark.asyncio
async def test_browser_approved_checkpoint_reaches_resumed_real_pi_without_rewriting_or_reading_it(repository, tmp_path, configured_media):
    require_pi()
    proposal = sample_artifact("proposal_packet")
    proposal["production_plan"].update(pipeline="cinematic", render_runtime="ffmpeg")
    proposal["approval"]["status"] = "pending"

    def reply(body):
        if len(requests) == 1:
            return [
                tool_item("openmontage", {"action": "initialize", "input": {
                    "title": "Checkpoint recovery", "pipeline_type": "cinematic"}}, "checkpoint-initialize"),
                tool_item("openmontage", {"action": "checkpoint", "input": {
                    "stage": "research", "status": "completed",
                    "artifacts": {"research_brief": sample_artifact("research_brief")}}}, "checkpoint-research"),
                tool_item("openmontage", {"action": "checkpoint", "input": {
                    "stage": "proposal", "status": "awaiting_human",
                    "artifacts": {"proposal_packet": proposal}}}, "checkpoint-proposal"),
            ]
        prompt = next(item for item in reversed(body["input"]) if item.get("role") == "user")
        content = "".join(part.get("text", "") for part in prompt["content"])
        checkpoint = json.loads(content.splitlines()[-1])["bootstrap"].get("current_checkpoint")
        if checkpoint and checkpoint["content"].get("status") == "completed":
            assert checkpoint["content"]["human_approved"] is True
            return [text_item("The browser-completed proposal is already known; continue to script.")]
        if len(requests) == 2:
            return [tool_item("openmontage", {"action": "checkpoint", "input": {
                "stage": "proposal", "status": "completed",
                "artifacts": {"proposal_packet": proposal}}}, "redundant-proposal-completion")]
        if len(requests) == 3:
            return [tool_item("openmontage", {"action": "read_project", "input": {
                "path": "checkpoint_proposal.json"}}, "redundant-proposal-read")]
        return [text_item("The proposal progress was fetched after the rejected duplicate checkpoint.")]

    with model_server(reply) as (endpoint, requests):
        worker, task = create_worker(repository, tmp_path, endpoint, configured_media)
        waiting = await worker.run_once()
        assert waiting.state == TaskState.AWAITING_APPROVAL and waiting.approval.stage == "proposal"
        session = tmp_path / "runtime/sessions" / task.task_id / task.run_id / "session.jsonl"
        session_id = json.loads(session.read_text().splitlines()[0])["id"]
        app = create_app(studio_repository=repository, studio_config=worker.config, studio_environment=worker.environment)
        app.state.studio_projects_dir = worker.projects_dir
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/studio/config").json()["csrf_token"]
            approved = client.post(f"/api/studio/tasks/{task.task_id}/approvals/{waiting.approval.binding.gate_id}/decision",
                                   headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": token,
                                            "Idempotency-Key": "bootstrap-checkpoint-approve"},
                                   json={"binding": waiting.approval.binding.model_dump(mode="json"),
                                         "expected_version": waiting.version, "decision": "approve"})
            assert approved.status_code == 202, approved.text
            continued = await worker.run_once()
        canonical = json.loads((worker.projects_dir / task.project_id / "checkpoint_proposal.json").read_text())
    assert continued.state == TaskState.BLOCKED and continued.error.code == "invalid_artifact"
    assert len(requests) == 2
    prompt = next(item for item in reversed(requests[-1]["input"]) if item.get("role") == "user")
    content = "".join(part.get("text", "") for part in prompt["content"])
    assert json.loads(content.splitlines()[-1])["bootstrap"]["current_checkpoint"] == {
        "path": "checkpoint_proposal.json", "content": canonical}
    assert canonical["status"] == "completed" and canonical["human_approved"] is True
    assert "studio-policy:approval_conflict" not in session.read_text()
    assert json.loads(session.read_text().splitlines()[0])["id"] == session_id
    assert repository.unresolved_intents(task.task_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "oversized", "other_project_alias", "dangling_alias"])
async def test_current_checkpoint_read_retains_owned_path_and_size_guards_before_resumed_model_request(repository, tmp_path, configured_media, change):
    require_pi()

    def reply(body):
        if len(requests) == 1:
            return [
                tool_item("openmontage", {"action": "initialize", "input": {
                    "title": "Owned checkpoint", "pipeline_type": "cinematic"}}, "owned-initialize"),
                tool_item("openmontage", {"action": "checkpoint", "input": {
                    "stage": "research", "status": "in_progress", "artifacts": {}}}, "owned-progress"),
            ]
        return [text_item("No canonical video is complete.")]

    with model_server(reply) as (endpoint, requests):
        worker, task = create_worker(repository, tmp_path, endpoint, configured_media)
        before = await worker.run_once()
        assert before.state == TaskState.BLOCKED and before.current_stage == "research"
        assert len(requests) == 2
        checkpoint = worker.projects_dir / task.project_id / "checkpoint_research.json"
        checkpoint.unlink()
        if change == "oversized":
            checkpoint.write_text(json.dumps({"padding": "x" * 262_145}))
        elif change in {"other_project_alias", "dangling_alias"}:
            other = tmp_path / "other-project" / "checkpoint_research.json"
            if change == "other_project_alias":
                other.parent.mkdir()
                other.write_text('{"private":"must not reach the model"}')
            checkpoint.symlink_to(other)
        app = create_app(studio_repository=repository, studio_config=worker.config, studio_environment=worker.environment)
        app.state.studio_projects_dir = worker.projects_dir
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/studio/config").json()["csrf_token"]
            response = client.post(f"/api/studio/tasks/{task.task_id}/resume",
                                   headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": token,
                                            "Idempotency-Key": "bootstrap-owned-resume-0001"},
                                   json={"expected_version": before.version})
            assert response.status_code == 202, response.text
            continued = await worker.run_once()
    if change == "missing":
        assert len(requests) == 3
        prompt = next(item for item in reversed(requests[-1]["input"]) if item.get("role") == "user")
        content = "".join(part.get("text", "") for part in prompt["content"])
        assert json.loads(content.splitlines()[-1])["bootstrap"]["current_checkpoint"] is None
        assert continued.error.code == "invalid_artifact"
    else:
        assert len(requests) == 2
        assert continued.error.code == ("not_found" if change == "oversized" else "forbidden")
    assert continued.state == TaskState.BLOCKED
    assert repository.unresolved_intents(task.task_id) == []
    assert continued.cost.spent_usd_micros == 0 and continued.cost.reserved_usd_micros == 0


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
