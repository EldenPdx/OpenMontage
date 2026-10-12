"""Issue #31's own Chromium/Logto/PG UI boundary; never seed shared identities."""

from contextlib import contextmanager
import asyncio
import json
import os
import re
import secrets
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright

from lib.config_model import PiProfile, StudioConfig
from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.identity.environment import LoginCredentials
from tests.fixtures.studio.repository import test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4764"


@pytest.fixture
def ui_environment(tmp_path, monkeypatch):
    if not os.environ.get("STUDIO_TEST_DATABASE_URL") or not os.environ.get("STUDIO_TEST_SESSION_KEY"):
        pytest.fail("UI acceptance requires real PostgreSQL and a private session encryption key")
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
        path = tmp_path / "private-ui-auth.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        monkeypatch.setenv("OPENMONTAGE_AUTH_CONFIG_FILE", str(path))
        monkeypatch.setenv("OPENMONTAGE_AUTH_MODE", "authenticated")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
        monkeypatch.setattr(projects_api, "THUMB_CACHE_DIR", tmp_path / "thumbs")
        monkeypatch.setattr(projects_api, "_summary_cache", {})
        monkeypatch.setattr(projects_api, "_PROJECTS_ROOT_STR", os.path.normcase(str(repository.projects_dir.resolve())))
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        yield fixture, repository


class DelayedReadTransport:
    """Hold one real HTTP response body; authentication and handlers remain real."""
    def __init__(self, app, control):
        self.app, self.control = app, control

    async def __call__(self, scope, receive, send):
        async def delayed_send(message):
            if scope.get("path") == "/api/studio/tasks" and scope.get("method") == "GET" \
                    and message["type"] == "http.response.body" and self.control["armed"]:
                self.control["armed"] = False
                self.control["entered"].set()
                await asyncio.to_thread(self.control["release"].wait, 30)
            await send(message)
        await self.app(scope, receive, delayed_send)


@contextmanager
def ui_browser(repository, *, delayed_read=None, **dependencies):
    from backlot.server import create_app
    if "studio_config" not in dependencies:
        dependencies["studio_config"] = StudioConfig(enabled=True, default_profile="local", profiles={"local": PiProfile(
            provider="local", base_url="http://127.0.0.1:9999/v1", model="local-model", credential_env="UI_MODEL_KEY",
            reasoning=False, thinking_level="off")})
        dependencies["studio_environment"] = {"UI_MODEL_KEY": "private-controlled-model"}
    app = create_app(studio_repository=repository, **dependencies)
    if delayed_read is not None:
        app.add_middleware(DelayedReadTransport, control=delayed_read)
    with serving(app, port=4764) as origin, sync_playwright() as playwright:
        assert origin == ORIGIN
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser, app
        finally:
            if delayed_read is not None:
                delayed_read["release"].set()
            browser.close()


def sign_in(context, fixture, alias="u1", *, return_to="/en"):
    page = context.new_page()
    page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {
        "127.0.0.1", "localhost", "::1"} else route.abort())
    page.goto(ORIGIN + "/api/auth/login?return_to=" + return_to)
    logto_sign_in(page, fixture.login_credentials(alias))
    page.wait_for_url(ORIGIN + return_to)
    return page


def ui_context(browser, **options):
    context = browser.new_context(**options)
    context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {
        "127.0.0.1", "localhost", "::1"} else route.abort())
    return context


def write_headers(me):
    return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]}


def invitation_ticket(fixture, recipient, *, after):
    mail = fixture.wait_for_mail(recipient, after=after)
    matches = re.findall(r"#ticket=([A-Za-z0-9_-]{43,128})", mail.get("Text", "") + mail.get("HTML", ""))
    assert matches and len(set(matches)) == 1, "The real invitation must contain one restricted ticket"
    return matches[0]


@contextmanager
def mailbox_user(fixture):
    """Own one fresh native recipient without modifying the read-only fixture ledger."""
    username = "auth31_ui_" + secrets.token_hex(10)
    email, password = username + "@openmontage.test", "Aa9-" + secrets.token_urlsafe(24)
    user = fixture.api.request("POST", "/api/users", {"username": username, "primaryEmail": email,
        "password": password, "name": username, "customData": {"auth31ui": username}})
    try:
        yield user["id"], LoginCredentials(username, email, password)
    finally:
        record = fixture.api.request("GET", "/api/users/" + user["id"])
        assert record["username"] == username and record["customData"].get("auth31ui") == username
        fixture.api.request("DELETE", "/api/users/" + user["id"])


@contextmanager
def identity_outage(app):
    """A failed real HTTP authority read; no successful identity data is mocked."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    failure = FastAPI()

    @failure.api_route("/{path:path}", methods=["GET", "POST"])
    async def unavailable(path: str):
        return JSONResponse({"error": "controlled authority outage"}, status_code=503)

    authority = app.state.auth_runtime.authority
    original = authority.config
    with serving(failure) as address:
        authority.config = original.model_copy(update={"management_endpoint": address})
        try:
            yield
        finally:
            authority.config = original
