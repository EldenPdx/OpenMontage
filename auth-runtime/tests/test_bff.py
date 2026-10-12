"""Required public BFF acceptance against real Logto, PostgreSQL and Chromium."""

from concurrent.futures import ThreadPoolExecutor
import json
import re
import secrets
import subprocess
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from tests.browser.test_studio_submission import serving
from tests.fixtures.identity.environment import LoginCredentials
from tests.fixtures.identity.browser import logto_register, logto_reset_password, logto_sign_in

from tests.fixtures.auth26 import ORIGIN, identity_environment, auth_browser, signed_in, write_headers, intercepted_callback








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




def test_real_concurrent_callback_has_one_winner_and_spoofed_host_cannot_consume_it(identity_environment):
    fixture, _, _ = identity_environment
    capture = {"hold": True, "url": None}
    with auth_browser(callback_capture=capture) as browser:
        context = browser.new_context()
        callback = intercepted_callback(context, fixture, capture)
        forged = context.request.get(callback, headers={"Host": "foreign.example"}, max_redirects=0)
        assert forged.status == 403
        cookies = {item["name"]: item["value"] for item in context.cookies(ORIGIN)}
        names = set(cookies)
        assert "openmontage_login" in names

        with httpx.Client(cookies=cookies, follow_redirects=False) as first, httpx.Client(cookies=cookies, follow_redirects=False) as second, ThreadPoolExecutor(max_workers=2) as workers:
            requests = [workers.submit(client.get, callback) for client in (first, second)]
            responses = [request.result() for request in requests]
            statuses = sorted(response.status_code for response in responses)
            assert statuses == [303, 401], [response.json().get("error", {}).get("message") for response in responses if response.status_code != 303]
            winner = first if responses[0].status_code == 303 else second
            assert winner.get(ORIGIN + "/api/auth/me").status_code == 200
        context.close()


def test_real_pkce_failure_and_browser_error_clean_protocol_secrets_from_history(identity_environment):
    fixture, _, _ = identity_environment
    capture = {"hold": True, "url": None}
    with auth_browser(callback_capture=capture) as browser:
        context = browser.new_context()
        callback = intercepted_callback(context, fixture, capture, wrong_challenge=True)
        page = context.new_page()
        response = page.goto(callback)
        status = response.status
        assert status == 401
        page.wait_for_url(ORIGIN + "/en")
        assert page.evaluate("location.search") == ""
        assert "nonce-" in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "no-referrer"
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        context.close()


def test_live_role_union_empty_roles_removal_and_suspension_never_use_stale_authority(identity_environment):
    fixture, _, _ = identity_environment
    original = fixture.organization_roles("cross", "A")
    removed = False
    try:
        with auth_browser() as browser:
            context = browser.new_context()
            signed_in(context, fixture, "cross")
            me = context.request.get(ORIGIN + "/api/auth/me").json()
            switched = context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "organization", "owner_id": fixture.organizations["A"]}, headers=write_headers(me))
            assert switched.status == 200
            fixture.set_roles("cross", "A", ["viewer", "admin"])
            updated = context.request.get(ORIGIN + "/api/auth/me")
            assert updated.status == 200 and updated.json()["role"] == "admin"
            assert "organization:manage" in updated.json()["permissions"]
            fixture.set_roles("cross", "A", ["viewer"])
            viewer = context.request.get(ORIGIN + "/api/auth/me").json()
            assert viewer["role"] == "viewer" and "production:approve" not in viewer["permissions"]
            fixture.set_roles("cross", "A", [])
            empty = context.request.get(ORIGIN + "/api/auth/me").json()
            assert empty["role"] is None and empty["permissions"] == []
            fixture.remove_member("cross", "A")
            removed = True
            assert context.request.get(ORIGIN + "/api/auth/me").status == 404
            fixture.add_member("cross", "A", original)
            removed = False
            fixture.suspend("cross", True)
            assert context.request.get(ORIGIN + "/api/auth/me").status == 401
            fixture.suspend("cross", False)
            assert context.request.get(ORIGIN + "/api/auth/me").status == 401
            context.clear_cookies()
            signed_in(context, fixture, "cross")
            assert context.request.get(ORIGIN + "/api/auth/me").status == 200
            context.close()
    finally:
        fixture.suspend("cross", False)
        if removed:
            fixture.add_member("cross", "A", original)
        else:
            fixture.set_roles("cross", "A", original)
        assert fixture.organization_roles("cross", "A") == original


def test_real_logto_outage_fails_closed_but_logout_still_revokes_locally(identity_environment):
    fixture, _, _ = identity_environment
    owner = fixture.path.parents[2]
    containers = subprocess.run(["docker", "ps", "--filter", "label=com.docker.compose.project=openmontage-auth24",
        "--filter", "label=com.docker.compose.service=logto", "--format", "{{.Names}}"], cwd=owner, capture_output=True, text=True, check=True).stdout.splitlines()
    assert len(containers) == 1, "Required isolated real Logto container is unavailable"
    with auth_browser() as browser:
        context = browser.new_context()
        signed_in(context, fixture)
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        subprocess.run(["docker", "pause", containers[0]], cwd=owner, capture_output=True, check=True)
        try:
            response = context.request.get(ORIGIN + "/api/auth/me")
            assert response.status == 503 and response.json()["error"]["code"] == "identity_unavailable"
            assert context.request.get(ORIGIN + "/api/projects").status == 503
            logout = context.request.post(ORIGIN + "/api/auth/logout", data={}, headers=write_headers(me))
            assert logout.status == 200 and logout.json() == {"logout_url": None}
            assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        finally:
            subprocess.run(["docker", "unpause", containers[0]], cwd=owner, capture_output=True, check=True)
            with httpx.Client(trust_env=False, timeout=5) as client:
                assert client.get("http://127.0.0.1:3010/oidc/.well-known/openid-configuration").status_code == 200
        context.close()


def test_fixed_cookie_csrf_origin_host_headers_and_intermediate_business_routes_cannot_grant_access(identity_environment):
    fixture, _, _ = identity_environment
    protected = ["/api/projects", "/api/projects/", "/api/project/foreign/state", "/api/project/foreign/events",
                 "/api/library/events", "/media/foreign/file.mp4", "/thumb/foreign/file.svg", "/p/foreign",
                 "/en/p/foreign", "/zh/p/foreign", "/ja/p/foreign", "/api/studio/config", "/api/studio/tasks",
                 "/api/studio/tasks/foreign", "/api/studio/tasks/foreign/events", "/api/studio/tasks/foreign/result?download=1",
                 "/api/organizations", "/api/organizations/foreign/members"]
    with auth_browser() as browser:
        context = browser.new_context()
        context.add_cookies([{"name": "openmontage_session", "value": "X" * 43, "url": ORIGIN, "httpOnly": True, "sameSite": "Lax"}])
        for path in protected:
            result = context.request.get(ORIGIN + path, max_redirects=0, headers={"Range": "bytes=0-9", "If-None-Match": '"foreign-etag"'})
            status = result.status
            assert status == 401
            assert not {"etag", "content-range", "accept-ranges"} & result.headers.keys()
        assert context.request.get(ORIGIN + "/ui/board.html").status == 200
        assert context.request.get(ORIGIN + "/openapi.json").status == 404
        signed_in(context, fixture)
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        assert next(item for item in context.cookies() if item["name"] == "openmontage_session")["value"] != "X" * 43
        for path in protected:
            result = context.request.get(ORIGIN + path, max_redirects=0, headers={"Range": "bytes=0-9", "If-None-Match": '"foreign-etag"'})
            status = result.status
            assert status == 503
            assert result.json()["error"]["code"] == "dependency_unavailable"
        for headers in ({"Origin": "https://foreign.example", "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]},
                        {"Origin": ORIGIN, "X-Auth-Context": me["context_id"]},
                        {**write_headers(me), "X-CSRF-Token": "invalid"},
                        {**write_headers(me), "Host": "foreign.example"},
                        {**write_headers(me), "X-Forwarded-Host": "127.0.0.1:4760"}):
            assert context.request.post(ORIGIN + "/api/auth/logout", data={}, headers=headers).status == 403
        assert context.request.get(ORIGIN + "/api/auth/me").json()["context_id"] == me["context_id"]
        invalid = context.request.post(ORIGIN + "/api/auth/logout", data={"client_secret": "rejected-input-sentinel"}, headers=write_headers(me))
        assert invalid.status == 422 and "rejected-input-sentinel" not in invalid.text()
        for return_to in ("https://foreign.example", "//foreign.example", "%2f%2fforeign.example", "/en\\escape", "/en\n", "/admin", "/studio"):
            result = context.request.get(ORIGIN + "/api/auth/login", params={"return_to": return_to}, max_redirects=0)
            status = result.status
            assert status == 422 and "location" not in result.headers
        for query in ("code=fault&state=x&state=y", "code=fault&code=another&state=x"):
            assert context.request.get(ORIGIN + "/api/auth/callback?" + query).status == 422
        for query in ("code=fault", "code=fault&state=unknown", "error=access_denied&code=fault&state=x", "code=fault&state=x&iss=https://foreign.example"):
            assert context.request.get(ORIGIN + "/api/auth/callback?" + query).status == 401
        context.close()


@pytest.mark.parametrize("lifetime", ["absolute", "idle"])
def test_real_session_expiry_never_falls_back_to_anonymous(identity_environment, lifetime):
    fixture, _, config_path = identity_environment
    config = json.loads(config_path.read_text())
    config["absolute_seconds" if lifetime == "absolute" else "idle_seconds"] = 2
    config_path.write_text(json.dumps(config))
    with auth_browser() as browser:
        context = browser.new_context()
        signed_in(context, fixture)
        assert context.request.get(ORIGIN + "/api/auth/me").status == 200
        time.sleep(2.3)
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        assert context.request.get(ORIGIN + "/api/projects").status == 401
        context.close()


def test_native_registration_and_password_reset_use_real_mail_and_keep_the_same_identity(identity_environment):
    fixture, _, _ = identity_environment
    namespace = json.loads(fixture.path.read_text())["namespace"]
    email = namespace + ".bff." + uuid4().hex[:12] + "@openmontage.test"
    credentials = LoginCredentials("", email, secrets.token_urlsafe(24) + "Aa1!")
    try:
        with auth_browser() as browser:
            context = browser.new_context()
            page = context.new_page()
            page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"} else route.abort())
            hinted = context.request.get(ORIGIN + "/api/auth/login", params={"screen_hint": "sign_up"}, max_redirects=0)
            assert hinted.status == 302 and parse_qs(urlsplit(hinted.headers["location"]).query)["interaction_mode"] == ["signUp"]
            page.goto(ORIGIN + "/api/auth/login")
            logto_register(page, fixture, credentials)
            page.wait_for_url(ORIGIN + "/en")
            me = context.request.get(ORIGIN + "/api/auth/me").json()
            assert me["user"]["email"] == email and me["user"]["email_verified"] is True
            context.close()
            changed = LoginCredentials("", email, secrets.token_urlsafe(24) + "Aa1!")
            reset = browser.new_context()
            reset_page = reset.new_page()
            reset_page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"} else route.abort())
            reset_page.goto(ORIGIN + "/api/auth/login")
            logto_reset_password(reset_page, fixture, changed)
            logto_sign_in(reset_page, changed)
            reset_page.wait_for_url(ORIGIN + "/en")
            returned = reset.request.get(ORIGIN + "/api/auth/me")
            assert returned.status == 200 and returned.json()["user"]["user_id"] == me["user"]["user_id"]
            reset.close()
    finally:
        for user in fixture.api.items("/api/users"):
            if user.get("primaryEmail") == email:
                fixture.api.request("DELETE", "/api/users/" + user["id"])


def test_logout_racing_workspace_rotation_accepts_only_one_session_generation(identity_environment):
    fixture, _, _ = identity_environment
    with auth_browser() as browser:
        context = browser.new_context()
        signed_in(context, fixture).close()
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        cookie = next(item for item in context.cookies() if item["name"] == "openmontage_session")["value"]
        with httpx.Client(cookies={"openmontage_session": cookie}) as one, httpx.Client(cookies={"openmontage_session": cookie}) as two, ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(one.post, ORIGIN + "/api/auth/workspace", json={"kind": "organization", "owner_id": fixture.organizations["A"]}, headers=write_headers(me))
            second = workers.submit(two.post, ORIGIN + "/api/auth/logout", json={}, headers=write_headers(me))
            results = [first.result(), second.result()]
            statuses = [result.status_code for result in results]
            assert statuses.count(200) == 1
            assert all(status in {200, 401, 409} for status in statuses)
            assert httpx.get(ORIGIN + "/api/auth/me", headers={"Cookie": "openmontage_session=" + cookie}).status_code == 401
        context.close()
