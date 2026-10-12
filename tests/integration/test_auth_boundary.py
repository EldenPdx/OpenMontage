"""Public HTTP boundaries; required failures cannot become anonymous access."""

from fastapi.testclient import TestClient


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
