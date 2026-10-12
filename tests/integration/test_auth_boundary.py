"""Public HTTP boundaries; required failures cannot become anonymous access."""

from fastapi.testclient import TestClient
import json
from uuid import uuid4
import time
import httpx
import pytest
from tests.browser.test_studio_submission import serving


def test_local_stale_csrf_is_distinct_from_a_denied_origin(tmp_path, monkeypatch):
    from backlot import server, projects_api
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", tmp_path)
    with serving(server.create_app(auth_environment={})) as origin, httpx.Client(trust_env=False, timeout=3) as client:
        body = {"brief": "Local production", "profile_id": "local"}
        stale = client.post(origin + "/api/studio/tasks", headers={"Origin": origin}, json=body)
        assert stale.status_code == 403
        assert stale.json()["error"]["code"] == "csrf_invalid"
        denied = client.post(origin + "/api/studio/tasks", headers={"Origin": "https://outside.example"}, json=body)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("locale", ["en", "zh", "ja"])
def test_invitation_landing_is_a_private_public_shell_without_identity_service(tmp_path, monkeypatch, locale):
    from backlot import server, projects_api
    root, ui = tmp_path / "projects", tmp_path / "ui"
    root.mkdir()
    ui.mkdir()
    (ui / "invitation.html").write_text("<!doctype html><title>Invitation</title>")
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
    monkeypatch.setattr(projects_api, "UI_DIR", ui)
    app = server.create_app(auth_environment={"OPENMONTAGE_AUTH_MODE": "authenticated"})
    with serving(app) as origin, httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(origin + f"/{locale}/invitations/accept")
        assert response.status_code == 200
        assert "Invitation" in response.text
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(origin + "/api/auth/me").status_code == 503
        assert client.get(origin + "/api/projects").status_code == 503


def test_authenticated_configuration_cannot_expose_local_projects(tmp_path, monkeypatch):
    from backlot import server, projects_api
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", tmp_path)
    monkeypatch.setenv("OPENMONTAGE_AUTH_MODE", "authenticated")
    with TestClient(server.create_app(), base_url="http://127.0.0.1",
                    client=("127.0.0.1", 12345)) as client:
        response = client.get("/api/projects")
        assert response.status_code == 503
        assert response.json() == {"error": {"code": "identity_unavailable", "message": "Identity service is unavailable",
                                              "retryable": False, "recovery_actions": []}}
        assert client.get("/api/health").json() == {"ok": True, "app": "backlot"}
        assert client.get("/openapi.json").status_code == 404


def test_explicit_file_only_legacy_classification_needs_no_database(tmp_path, monkeypatch):
    from backlot import server, projects_api
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
    path = tmp_path / "private-auth.json"
    path.write_text(json.dumps({"mode": "local", "root_provenance": {"canonical_root": str(root.resolve()), "classification": "file_only_legacy"}}))
    path.chmod(0o600)
    monkeypatch.setenv("OPENMONTAGE_AUTH_CONFIG_FILE", str(path))
    with TestClient(server.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/projects").status_code == 200
        assert client.get("/api/auth/me").json() == {"mode": "local"}
        assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("peer,headers", [("192.0.2.35", {}), ("127.0.0.1", {"Host": "foreign.example"}),
                                        ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
                                        ("127.0.0.1", {"Forwarded": "for=127.0.0.1;proto=http"})])
def test_anonymous_local_access_requires_the_actual_loopback_peer_and_unforwarded_host(tmp_path, monkeypatch, peer, headers):
    from backlot import server, projects_api
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", tmp_path)
    with TestClient(server.create_app(auth_environment={}), base_url="http://127.0.0.1", client=(peer, 12345)) as client:
        for path in ("/api/projects", "/api/studio/config", "/api/auth/me", "/api/health", "/ui/board.html"):
            response = client.get(path, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "forbidden"


def test_lost_inner_guards_and_complete_config_rollback_cannot_reopen_private_bytes(tmp_path, monkeypatch):
    from backlot import server, projects_api, state
    from production.ownership_guard import MARKER, mark_authenticated
    root = tmp_path / "projects"
    project = root / "private-project"
    project.mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"project_id": "private-project", "title": "Private production", "pipeline_type": "framework-smoke"}))
    (project / "private.txt").write_text("protected-output-sentinel")
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    mark_authenticated(project, binding=binding, workspace={"kind": "personal", "owner_id": "private-user"})
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
    monkeypatch.setattr(state, "PROJECTS_DIR", root)
    for directory in (root, project):
        (directory / MARKER).unlink()
    with TestClient(server.create_app(auth_environment={}), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        for path in ("/api/projects", "/api/project/private-project/state", "/media/private-project/private.txt", "/thumb/private-project/private.txt",
                     "/api/library/events", "/api/project/private-project/events", "/p/private-project", "/en/p/private-project"):
            response = client.get(path, follow_redirects=False, headers={"Range": "bytes=0-9", "If-None-Match": '"prior"'})
            assert response.status_code == 503
            assert "protected-output-sentinel" not in response.text and "private-project" not in response.text
        assert client.get("/api/health").status_code == 200
        assert client.get("/ui/board.html").status_code == 200


def test_live_local_sse_ends_when_authenticated_protection_appears(tmp_path, monkeypatch):
    from backlot import server, projects_api
    from production.ownership_guard import mark_authenticated
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
    monkeypatch.setattr(projects_api, "SSE_HEARTBEAT_SECONDS", 0.1)
    with serving(server.create_app(auth_environment={})) as origin, httpx.Client(trust_env=False, timeout=3) as client:
        with client.stream("GET", origin + "/api/library/events") as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            assert '"type": "hello"' in next(lines)
            mark_authenticated(root, binding=str(uuid4()))
            start = time.monotonic()
            assert list(lines) in ([], [""])
            assert time.monotonic() - start < 1
        assert client.get(origin + "/api/projects").status_code == 503
        assert client.get(origin + "/api/health").status_code == 200
