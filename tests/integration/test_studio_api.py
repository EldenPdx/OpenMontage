"""Studio browser API acceptance with real PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

from fastapi.testclient import TestClient
import pytest

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.contracts import FileReference, RenderResult, TaskState
from tests.fixtures.studio.repository import test_repository as isolated_repository


@pytest.fixture
def api():
    profile = PiProfile(provider="controlled", base_url="http://127.0.0.1:9999/v1", model="local-model",
                        credential_env="TEST_PI_KEY", reasoning=False, thinking_level="off",
                        price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
    config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
    with isolated_repository() as repo:
        app = create_app(studio_repository=repo, studio_config=config,
                         studio_environment={"TEST_PI_KEY": "private-test-credential"})
        with TestClient(app, base_url="http://127.0.0.1") as client:
            config_response = client.get("/api/studio/config")
            assert config_response.status_code == 200
            headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": config_response.json()["csrf_token"],
                       "Idempotency-Key": "browser-create-0001"}
            yield client, repo, headers


def test_task_creation_is_durable_and_repeated_submission_is_idempotent(api):
    client, repo, headers = api
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/studio/tasks", headers=headers,
                                                       json={"brief": "制作一段本地动画", "profile_id": "local"}), range(2)))
    assert all(response.status_code == 202 for response in responses)
    task = responses[0].json()
    assert task["task_id"] == responses[1].json()["task_id"]
    assert task["state"] == "queued"
    assert repo.get_task(task["task_id"]).project_id == task["project_id"]
    assert len(client.get("/api/studio/tasks").json()) == 1
    assert client.get(f"/api/studio/tasks/{task['task_id']}").json()["board_url"] == f"/p/{task['project_id']}"
    assert "private-test-credential" not in responses[0].text
    conflict = client.post("/api/studio/tasks", headers=headers, json={"brief": "不同内容", "profile_id": "local"})
    assert conflict.status_code == 409


def test_cancel_is_a_durable_command_and_duplicate_clicks_do_not_repeat_it(api):
    client, repo, headers = api
    task = client.post("/api/studio/tasks", headers=headers,
                       json={"brief": "请生成短片", "profile_id": "local"}).json()
    headers = {**headers, "Idempotency-Key": "browser-cancel-0001"}
    response = client.post(f"/api/studio/tasks/{task['task_id']}/cancel", headers=headers,
                           json={"expected_version": task["version"]})
    assert response.status_code == 202
    assert response.json()["state"] == "cancel_requested"
    duplicate = client.post(f"/api/studio/tasks/{task['task_id']}/cancel", headers=headers,
                            json={"expected_version": task["version"]})
    assert duplicate.json()["version"] == response.json()["version"]
    claim = repo.claim_command("test-worker")
    assert claim.command.kind == "cancel"
    repo.transition(task["task_id"], TaskState.CANCELLED, expected_version=response.json()["version"],
                    fence=claim.context.fence)
    repo.finish_command(claim)
    assert repo.claim_command("test-worker") is None


def test_cross_site_rebinding_csrf_and_secret_input_are_rejected_without_echo(api):
    client, repo, headers = api
    body = {"brief": "短片需求", "profile_id": "local"}
    assert client.post("/api/studio/tasks", json=body, headers={**headers, "Origin": "https://outside.example"}).status_code == 403
    assert client.post("/api/studio/tasks", json=body, headers={**headers, "Host": "outside.example"}).status_code == 403
    assert client.post("/api/studio/tasks", json=body, headers={**headers, "X-CSRF-Token": "forged"}).status_code == 403
    invalid = client.post("/api/studio/tasks", json={**body, "api_key": "client-secret", "base_url": "https://outside.example"}, headers=headers)
    assert invalid.status_code == 422
    assert "client-secret" not in invalid.text
    assert repo.list_tasks() == []


def test_malformed_browser_session_is_rejected_instead_of_crashing_the_api(api):
    client, _, _ = api
    response = client.post("/api/studio/tasks", json={"brief": "短片需求", "profile_id": "local"}, headers=[
        (b"Origin", b"http://127.0.0.1"), (b"Cookie", b"studio_session=" + b"a" * 48 + b".\xff"),
        (b"X-CSRF-Token", b"invalid"), (b"Idempotency-Key", b"browser-malformed-1")])
    assert response.status_code == 403


def test_missing_real_pi_is_visible_and_cannot_accept_a_task_as_ready(api, tmp_path, monkeypatch):
    import production.api.tasks
    client, repo, headers = api
    monkeypatch.setattr(production.api.tasks, "PI_CLI", tmp_path / "missing-pi.js", raising=False)
    configuration = client.get("/api/studio/config").json()
    assert configuration["ready"] is False
    response = client.post("/api/studio/tasks", headers=headers,
                           json={"brief": "短片需求", "profile_id": "local"})
    assert response.status_code == 503
    assert repo.list_tasks() == []


def test_sse_replays_persisted_events_after_cursor_and_never_crosses_tasks(api):
    client, repo, headers = api
    first = client.post("/api/studio/tasks", json={"brief": "第一个短片", "profile_id": "local"}, headers=headers).json()
    cancel_headers = {**headers, "Idempotency-Key": "browser-cancel-0001"}
    cancelled = client.post(f"/api/studio/tasks/{first['task_id']}/cancel", headers=cancel_headers,
                            json={"expected_version": first["version"]}).json()
    claim = repo.claim_command("test-worker")
    repo.transition(first["task_id"], TaskState.CANCELLED, expected_version=cancelled["version"], fence=claim.context.fence)
    repo.finish_command(claim)
    second = client.post("/api/studio/tasks", json={"brief": "第二个短片", "profile_id": "local"},
                         headers={**headers, "Idempotency-Key": "browser-create-0002"}).json()
    initial = repo.events(first["task_id"])[0]
    response = client.get(f"/api/studio/tasks/{first['task_id']}/events", headers={"Last-Event-ID": str(initial.event_id)})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert first["task_id"] in response.text
    assert second["task_id"] not in response.text
    assert f"id: {initial.event_id}\n" not in response.text
    assert '"state":"cancelled"' in response.text
    future = client.get(f"/api/studio/tasks/{first['task_id']}/events", headers={"Last-Event-ID": "99999999"})
    assert future.status_code == 409
    assert future.json()["error"]["code"] == "cursor_expired"


def test_result_delivery_requires_verified_state_and_unchanged_recorded_bytes(api, tmp_path):
    client, repo, headers = api
    task = client.post("/api/studio/tasks", json={"brief": "测试交付", "profile_id": "local"}, headers=headers).json()
    endpoint = f"/api/studio/tasks/{task['task_id']}/result"
    assert client.get(endpoint).status_code == 409


    projects = tmp_path / "projects"
    client.app.state.studio_projects_dir = projects
    project = projects / task["project_id"]
    (project / "renders").mkdir(parents=True)
    (project / "artifacts").mkdir()
    video, report = project / "renders/final.mp4", project / "artifacts/render_report.json"
    video.write_bytes(b"worker-verified-byte-fixture")
    report.write_bytes(b"{}")
    claim = repo.claim_command("delivery-worker")
    running = repo.transition(task["task_id"], TaskState.RUNNING, expected_version=task["version"], fence=claim.context.fence)
    result = RenderResult(video=FileReference(path="renders/final.mp4", revision=1, sha256=sha256(video.read_bytes()).hexdigest()),
                          render_report=FileReference(path="artifacts/render_report.json", revision=1, sha256=sha256(report.read_bytes()).hexdigest()),
                          bytes=video.stat().st_size, duration_seconds=1, width=64, height=64, verified=True)
    repo.transition(task["task_id"], TaskState.SUCCEEDED, expected_version=running.version, fence=claim.context.fence,
                    updates={"result": result.model_dump(mode="json")})
    repo.finish_command(claim)
    delivery = client.get(endpoint + "?download=1")
    assert delivery.status_code == 200
    assert delivery.content == b"worker-verified-byte-fixture"
    assert "attachment" in delivery.headers["content-disposition"]
    video.write_bytes(b"modified")
    assert client.get(endpoint).status_code == 409


def test_project_active_documents_cannot_execute_in_the_studio_origin(api, tmp_path, monkeypatch):
    import backlot.server
    client, _, _ = api
    projects = tmp_path / "projects"
    project = projects / "legacy-project"
    project.mkdir(parents=True)
    (project / "preview.html").write_text("<script>fetch('/api/studio/config')</script>")
    monkeypatch.setattr(backlot.server, "PROJECTS_DIR", projects)
    response = client.get("/media/legacy-project/preview.html")
    assert response.status_code == 200
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
