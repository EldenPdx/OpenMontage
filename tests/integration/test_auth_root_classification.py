"""Trusted restored-root checks observed through anonymous HTTP, using real PostgreSQL."""

import os
from uuid import uuid4

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
