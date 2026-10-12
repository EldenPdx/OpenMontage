"""Issue #26's private real BFF setup; no credentials are returned to browser DTOs."""

from contextlib import contextmanager
import json
import os
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import pytest
from playwright.sync_api import sync_playwright

from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.studio.repository import test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4760"


@pytest.fixture
def identity_environment(tmp_path, monkeypatch):
    if not os.environ.get("STUDIO_TEST_DATABASE_URL") or not os.environ.get("STUDIO_TEST_SESSION_KEY"):
        pytest.fail("BFF acceptance requires the real isolated PostgreSQL database and private encryption key")
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
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        yield fixture, repository, config_path


@contextmanager
def auth_browser(*, callback_capture=None, **dependencies):
    from backlot.server import create_app
    app = create_app(**dependencies)
    if callback_capture is not None:
        from fastapi.responses import PlainTextResponse

        @app.middleware("http")
        async def hold_protocol_callback(request, call_next):
            if request.url.path == "/api/auth/callback" and callback_capture["hold"]:
                callback_capture["url"] = str(request.url)
                return PlainTextResponse("Callback held for protocol verification", headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
            return await call_next(request)
    with serving(app, port=4760) as origin, sync_playwright() as playwright:
        assert origin == ORIGIN
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def signed_in(context, fixture, alias="u1", *, return_to="/en"):
    page = context.new_page()
    page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"} else route.abort())
    page.goto(ORIGIN + "/api/auth/login?return_to=" + return_to)
    logto_sign_in(page, fixture.login_credentials(alias))
    page.wait_for_url(ORIGIN + return_to)
    return page


def write_headers(me):
    return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]}


def intercepted_callback(context, fixture, capture, *, wrong_challenge=False):
    page = context.new_page()
    page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"} else route.abort())
    initiation = context.request.get(ORIGIN + "/api/auth/login", max_redirects=0)
    location = initiation.headers["location"]
    if wrong_challenge:
        parsed = urlsplit(location)
        query = [(key, "A" * 43 if key == "code_challenge" else value) for key, value in parse_qsl(parsed.query)]
        location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    page.goto(location)
    logto_sign_in(page, fixture.login_credentials("u1"))
    page.wait_for_url(lambda url: str(url).startswith(ORIGIN + "/api/auth/callback"))
    capture["hold"] = False
    return capture["url"]
