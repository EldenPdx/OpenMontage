"""Trusted restored-root checks observed through anonymous HTTP, using real PostgreSQL."""

import os
from uuid import uuid4
import json

import pytest

from fastapi.testclient import TestClient

from production.auth.config import AuthConfig, RootProvenance
from tests.fixtures.studio.repository import test_repository as isolated_repository


def test_adopted_root_requires_the_preserved_binding_and_available_registry(monkeypatch):
    from backlot import projects_api, server, state
    with isolated_repository() as repository:
        root = repository.projects_dir
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
        monkeypatch.setattr(state, "PROJECTS_DIR", root)
        expected = repository.database_binding()
        config = AuthConfig(mode="local", database_url=os.environ["STUDIO_TEST_DATABASE_URL"], database_schema=repository.schema,
            root_provenance=RootProvenance(canonical_root=str(root.resolve()), classification="adopted_or_restored", expected_binding=expected))
        with TestClient(server.create_app(auth_config=config), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
            assert client.get("/api/projects").status_code == 200
        for denied in (config.model_copy(update={"root_provenance": config.root_provenance.model_copy(update={"expected_binding": str(uuid4())})}),
                       config.model_copy(update={"database_schema": "missing_registry"}),
                       config.model_copy(update={"database_url": "postgresql://invalid@127.0.0.1:1/unavailable"}),
                       config.model_copy(update={"root_provenance": config.root_provenance.model_copy(update={"classification": "authenticated_or_unknown"})}),
                       config.model_copy(update={"root_provenance": None})):
            with TestClient(server.create_app(auth_config=denied), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
                assert client.get("/api/projects").status_code == 503
                assert client.get("/api/health").status_code == 200
        with isolated_repository() as replacement:
            swapped = config.model_copy(update={"database_schema": replacement.schema})
            with TestClient(server.create_app(auth_config=swapped), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
                assert client.get("/api/projects").status_code == 503


@pytest.mark.parametrize("spelling,alias", [("Projects", "projects"), ("résumé", "re\u0301sume\u0301"),
                                          ("Parent/Projects", "parent/projects")])
def test_native_directory_aliases_cannot_reopen_a_root_after_inner_marker_loss(tmp_path, monkeypatch, spelling, alias):
    from backlot import projects_api, server, state
    from production.ownership_guard import MARKER, WITNESS_DIRECTORY, mark_authenticated
    root, alternate = tmp_path / spelling, tmp_path / alias
    project = root / "private-project"
    project.mkdir(parents=True)
    metadata, output = project / "project.json", project / "private.txt"
    metadata.write_text(json.dumps({"project_id": "private-project", "title": "Private production", "pipeline_type": "framework-smoke"}))
    output.write_text("protected-alias-output")
    original = (metadata.read_bytes(), output.read_bytes())
    mark_authenticated(root, binding=str(uuid4()))
    (root / MARKER).unlink()
    assert (root.parent / WITNESS_DIRECTORY / root.name).is_file()
    paths = [root]
    if alternate.exists():
        assert os.path.samefile(root, alternate)
        paths.append(alternate)
    for protected in paths:
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", protected)
        monkeypatch.setattr(state, "PROJECTS_DIR", protected)
        config = AuthConfig(mode="local", root_provenance=RootProvenance(
            canonical_root=str(protected.resolve()), classification="file_only_legacy"))
        with TestClient(server.create_app(auth_config=config, auth_environment={}), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 12345)) as client:
            for path in ("/api/projects", "/api/project/private-project/state", "/media/private-project/private.txt",
                         "/thumb/private-project/private.txt", "/api/library/events", "/api/project/private-project/events", "/en/p/private-project"):
                response = client.get(path, follow_redirects=False, headers={"Range": "bytes=0-9", "If-None-Match": '"prior"'})
                assert response.status_code == 503
                assert "protected-alias-output" not in response.text and "private-project" not in response.text
                assert not {"etag", "content-range", "accept-ranges"} & response.headers.keys()
            assert client.get("/api/health").status_code == 200
            assert client.get("/ui/board.html").status_code == 200
    assert (metadata.read_bytes(), output.read_bytes()) == original


@pytest.mark.parametrize("fault", ["file", "symlink", "public", "malformed_record"])
def test_unsafe_native_protection_namespace_refuses_anonymous_business(tmp_path, monkeypatch, fault):
    from backlot import projects_api, server
    from production.ownership_guard import WITNESS_DIRECTORY
    root = tmp_path / "projects"
    root.mkdir()
    namespace = root.parent / WITNESS_DIRECTORY
    if fault == "file":
        namespace.write_text("invalid protection namespace")
    elif fault == "symlink":
        namespace.symlink_to(tmp_path / "absent")
    else:
        namespace.mkdir(mode=0o700)
        if fault == "public":
            namespace.chmod(0o755)
        else:
            (namespace / root.name).write_text("invalid protection record")
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
    with TestClient(server.create_app(auth_environment={}), base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/projects").status_code == 503
        assert client.get("/api/auth/me").status_code == 503
        assert client.get("/api/health").status_code == 200
