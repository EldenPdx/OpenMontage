"""Required public BFF acceptance against real Logto, PostgreSQL and Chromium."""

from contextlib import contextmanager
import json
import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.studio.repository import test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4760"


@pytest.fixture
def identity_environment(tmp_path, monkeypatch):
    if not os.environ.get("STUDIO_TEST_DATABASE_URL") or not os.environ.get("STUDIO_TEST_SESSION_KEY"):
        pytest.fail("BFF acceptance requires the real isolated PostgreSQL database and private encryption key")
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
def auth_browser():
    from backlot.server import create_app
    with serving(create_app(), port=4760) as origin, sync_playwright() as playwright:
        assert origin == ORIGIN
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def signed_in(context, fixture, alias="u1", *, return_to="/en"):
    page = context.new_page()
    page.goto(ORIGIN + "/api/auth/login?return_to=" + return_to)
    logto_sign_in(page, fixture.login_credentials(alias))
    page.wait_for_url(ORIGIN + return_to)
    return page


def test_real_login_returns_only_verified_personal_projection(identity_environment):
    fixture, repository, _ = identity_environment
    with auth_browser() as browser:
        context = browser.new_context()
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        signed_in(context, fixture)
        response = context.request.get(ORIGIN + "/api/auth/me")
        assert response.status == 200
        me = response.json()
        assert set(me) == {"mode", "user", "workspace", "role", "permissions", "context_id", "csrf_token", "expires_at"}
        assert set(me["user"]) == {"user_id", "name", "email", "email_verified"}
        assert me["workspace"] == {"kind": "personal", "owner_id": me["user"]["user_id"],
                                    "key": "personal:" + me["user"]["user_id"], "name": "Personal"}
        assert me["role"] == "personal"
        assert "production:create" in me["permissions"]
        assert response.headers["x-auth-context"] == me["context_id"]
        cookie = next(item for item in context.cookies() if item["name"] == "openmontage_session")
        assert cookie["httpOnly"] and cookie["path"] == "/" and cookie["sameSite"] == "Lax"
        assert not cookie["secure"] and len(cookie["value"]) >= 43
        assert response.headers["cache-control"] == "private, no-store"
        assert repository.projects_dir.joinpath(".openmontage-authenticated").is_file()
        context.close()


def write_headers(me):
    return {"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]}


def test_workspace_switch_verifies_membership_and_invalidates_pending_writes(identity_environment):
    fixture, _, _ = identity_environment
    with auth_browser() as browser:
        context = browser.new_context()
        signed_in(context, fixture, "cross")
        old = context.request.get(ORIGIN + "/api/auth/me").json()
        old_cookie = next(item for item in context.cookies() if item["name"] == "openmontage_session")
        response = context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "organization", "owner_id": fixture.organizations["A"]}, headers=write_headers(old))
        assert response.status == 200
        current = response.json()
        assert current["workspace"]["key"] == "organization:" + fixture.organizations["A"]
        assert current["role"] == "editor" and "production:approve" in current["permissions"]
        assert current["context_id"] != old["context_id"] and current["csrf_token"] != old["csrf_token"]
        assert next(item for item in context.cookies() if item["name"] == "openmontage_session")["value"] != old_cookie["value"]
        stale = context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "personal", "owner_id": current["user"]["user_id"]}, headers=write_headers(old))
        assert stale.status == 409 and stale.json()["error"]["code"] == "auth_context_conflict"
        assert context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "personal", "owner_id": "someone-else"}, headers=write_headers(current)).status == 404
        cloned = browser.new_context()
        cloned.add_cookies([old_cookie])
        assert cloned.request.get(ORIGIN + "/api/auth/me").status == 401
        cloned.close()
        context.close()


def test_logout_revokes_local_cookie_and_completes_official_logto_signout(identity_environment):
    fixture, _, _ = identity_environment
    with auth_browser() as browser:
        context = browser.new_context()
        page = signed_in(context, fixture)
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        old_cookie = next(item for item in context.cookies() if item["name"] == "openmontage_session")
        response = context.request.post(ORIGIN + "/api/auth/logout", data={}, headers=write_headers(me))
        assert response.status == 200
        logout_url = response.json()["logout_url"]
        parsed = urlsplit(logout_url)
        assert parsed.scheme == "http" and parsed.netloc == "127.0.0.1:3010" and parsed.path == "/oidc/session/end"
        assert parse_qs(parsed.query) == {"client_id": [fixture.private_app_configuration(ORIGIN)["app_id"]], "post_logout_redirect_uri": [ORIGIN + "/en"]}
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        cloned = browser.new_context()
        cloned.add_cookies([old_cookie])
        assert cloned.request.get(ORIGIN + "/api/auth/me").status == 401
        cloned.close()
        page.goto(logout_url)
        try:
            page.wait_for_url(ORIGIN + "/en", timeout=5000)
        except PlaywrightTimeoutError:
            page.get_by_role("button", name=re.compile(r"^(Yes|Sign out|Continue)$", re.I)).click()
        page.wait_for_url(ORIGIN + "/en")
        page.goto(ORIGIN + "/api/auth/login")
        page.locator('input[name="identifier"]').wait_for()
        assert page.url.startswith("http://127.0.0.1:3010/")
        context.close()


def test_two_real_sessions_refresh_independently_and_survive_bff_restart(identity_environment):
    fixture, _, _ = identity_environment
    from backlot.server import create_app
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        first, second = browser.new_context(), browser.new_context()
        try:
            app = create_app()
            with serving(app, port=4760):
                signed_in(first, fixture, "u1")
                signed_in(second, fixture, "u2")
                one = first.request.get(ORIGIN + "/api/auth/me").json()
                two = second.request.get(ORIGIN + "/api/auth/me").json()
                assert one["user"]["user_id"] != two["user"]["user_id"]
                assert one["context_id"] != two["context_id"] and one["csrf_token"] != two["csrf_token"]
                cookie = next(item for item in first.cookies() if item["name"] == "openmontage_session")
                store = app.state.auth_runtime.store
                session = store.get_session(cookie["value"])
                values = dict(session.sdk_storage)
                tokens = json.loads(values["accessTokenMap"])
                tokens["x"][""]["expiresAt"] = 0
                values["accessTokenMap"] = json.dumps(tokens)
                store.update_session(cookie["value"], expected_revision=session.metadata.revision, sdk_storage=values)
                refreshed = first.request.get(ORIGIN + "/api/auth/me")
                assert refreshed.status == 200 and refreshed.json()["user"] == one["user"]
                assert second.request.get(ORIGIN + "/api/auth/me").json()["user"] == two["user"]
            with serving(create_app(), port=4760):
                restored_one = first.request.get(ORIGIN + "/api/auth/me")
                restored_two = second.request.get(ORIGIN + "/api/auth/me")
                assert restored_one.status == 200 and restored_one.json()["user"] == one["user"]
                assert restored_two.status == 200 and restored_two.json()["user"] == two["user"]
                assert restored_one.json()["context_id"] == one["context_id"]
                assert restored_two.json()["context_id"] == two["context_id"]
        finally:
            browser.close()
