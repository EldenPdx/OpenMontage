"""Issue #29's real Logto/browser/PG boundary; only isolated projects are arranged."""

from contextlib import contextmanager
import asyncio
import json
import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright

from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.studio.repository import test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4763"


@pytest.fixture
def backlot_environment(tmp_path, monkeypatch):
    if not os.environ.get("STUDIO_TEST_DATABASE_URL") or not os.environ.get("STUDIO_TEST_SESSION_KEY"):
        pytest.fail("Backlot acceptance requires the real private PostgreSQL database and session key")
    monkeypatch.setenv("STUDIO_REQUIRE_INTEGRATION", "1")
    fixture = load_fixture()
    with isolated_repository() as repository:
        config = fixture.private_app_configuration(ORIGIN)
        ledger = json.loads(fixture.path.read_text())
        config.update(mode="authenticated", origin=ORIGIN, http_development=True,
                      management_endpoint="http://127.0.0.1:3010", management_resource="https://default.logto.app/api",
                      management_app_id=ledger["management"]["app_id"], management_app_secret=ledger["management"]["app_secret"],
                      database_url=os.environ["STUDIO_TEST_DATABASE_URL"], database_schema=repository.schema,
                      encryption_keys={"test": os.environ["STUDIO_TEST_SESSION_KEY"]}, active_key_id="test")
        config_path = tmp_path / "private-auth.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        monkeypatch.setenv("OPENMONTAGE_AUTH_CONFIG_FILE", str(config_path))
        monkeypatch.setenv("OPENMONTAGE_AUTH_MODE", "authenticated")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
        monkeypatch.setattr(projects_api, "THUMB_CACHE_DIR", tmp_path / "thumbs")
        monkeypatch.setattr(projects_api, "_summary_cache", {})
        monkeypatch.setattr(projects_api, "_PROJECTS_ROOT_STR", os.path.normcase(str(repository.projects_dir.resolve())))
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        yield fixture, repository


class DelayedEventTransport:
    """Delay one real ASGI event send; identity and public handlers remain untouched."""
    def __init__(self, app, *, control):
        self.app, self.control = app, control

    async def __call__(self, scope, receive, send):
        async def delayed_send(message):
            if scope.get("path", "").endswith("/events") and message["type"] == "http.response.body" and self.control["armed"]:
                self.control["armed"] = False
                self.control["blocked"] = True
                await asyncio.sleep(45)
            await send(message)
        await self.app(scope, receive, delayed_send)


@contextmanager
def backlot_browser(repository, *, delayed_event_transport=None):
    from backlot.server import create_app
    app = create_app(studio_repository=repository)
    if delayed_event_transport is not None:
        app.add_middleware(DelayedEventTransport, control=delayed_event_transport)
    with serving(app, port=4763) as origin, sync_playwright() as playwright:
        assert origin == ORIGIN
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser, app
        finally:
            browser.close()


def sign_in(context, fixture, alias="u1"):
    page = context.new_page()
    page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"} else route.abort())
    page.goto(ORIGIN + "/api/auth/login?return_to=/en")
    logto_sign_in(page, fixture.login_credentials(alias))
    page.wait_for_url(ORIGIN + "/en")
    me = context.request.get(ORIGIN + "/api/auth/me")
    assert me.status == 200
    page.close()
    return me.json()


def write_headers(me):
    return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]}


def select_organization(context, fixture, organization, me):
    response = context.request.post(ORIGIN + "/api/auth/workspace", headers=write_headers(me),
                                    data={"kind": "organization", "owner_id": fixture.organizations[organization]})
    assert response.status == 200
    return response.json()


def project_for(context, app, repository, *, title):
    """Arrange ownership through the public Repository seam using the real stored login."""
    from production.contracts import Actor, ConfigSnapshot, TaskCreate
    cookie = next(item["value"] for item in context.cookies() if item["name"] == "openmontage_session")
    session = app.state.auth_runtime.store.get_session(cookie)
    actor = Actor(identity=session.metadata.identity, workspace=session.metadata.workspace)
    snapshot = ConfigSnapshot(profile_id="local", provider="local", model="test-model", api="openai-responses",
                              configuration_sha256="a" * 64, price_status="quoted")
    task = repository.create_task(TaskCreate(brief=title), snapshot, "backlot-" + title,
                                  workspace=actor.workspace, actor=actor)
    project = repository.projects_dir / task.project_id
    project.mkdir(exist_ok=True)
    (project / "project.json").write_text(json.dumps({"project_id": task.project_id, "title": title, "pipeline_type": "cinematic"}))
    return project
