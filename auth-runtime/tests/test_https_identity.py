"""Native login on real HTTPS proves Secure-cookie delivery with a plaintext positive control."""

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.auth.config import AuthConfig
from production.auth.routes import SESSION_COOKIE
from tests.fixtures.auth32_roles import isolated_runtime_roles
from tests.fixtures.auth32_tls import HOST, ORIGIN, HTTP_WITNESS, https_and_cookie_witness
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in


def request_from_page(page, path, *, method="GET", body=None, headers=None):
    return page.evaluate("""async ({path, method, body, headers}) => {
        const response = await fetch(path, {method, credentials:'include',
            headers:{'Content-Type':'application/json', ...(headers || {})},
            ...(body === null ? {} : {body:JSON.stringify(body)})});
        return {status:response.status, body:await response.json()};
    }""", {"path": path, "method": method, "body": body, "headers": headers})


def test_native_login_secure_cookie_is_withheld_from_same_host_plaintext_while_probe_cookie_is_sent(tmp_path, monkeypatch):
    fixture = load_fixture()
    with isolated_runtime_roles(tmp_path) as (configuration, migration, bff, worker):
        values = fixture.private_app_configuration(ORIGIN)
        ledger = json.loads(fixture.path.read_text())
        values.update(mode="authenticated", origin=ORIGIN, http_development=True, management_endpoint="http://127.0.0.1:3010",
                      management_resource=ledger["management"]["resource"], management_app_id=ledger["management"]["app_id"],
                      management_app_secret=ledger["management"]["app_secret"], database_url=configuration["bff_database_url"], database_schema=bff.schema,
                      encryption_keys={"auth32": configuration["session_encryption_key"]}, active_key_id="auth32")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", bff.projects_dir)
        monkeypatch.setattr(state, "PROJECTS_DIR", bff.projects_dir)
        profile = PiProfile(provider="controlled", base_url="http://127.0.0.1:9999/v1", model="local-model", credential_env="AUTH32_MODEL_KEY",
                            reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        app = create_app(studio_repository=bff, studio_config=StudioConfig(enabled=True, default_profile="local", profiles={"local": profile}),
                         studio_environment={"AUTH32_MODEL_KEY": "local-https-control-only"}, auth_config=AuthConfig.model_validate(values))
        with https_and_cookie_witness(app, tmp_path) as received, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True,
                args=["--host-resolver-rules=MAP " + HOST + " 127.0.0.1", "--no-proxy-server"])
            context = browser.new_context(ignore_https_errors=True, locale="en-US")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith((ORIGIN + "/", HTTP_WITNESS + "/",
                "http://127.0.0.1:3010/", "http://127.0.0.1:8180/")) else route.abort())
            try:
                page = context.new_page()
                assert page.goto(HTTP_WITNESS).status == 200
                page.goto(ORIGIN + "/api/auth/login?return_to=/en")
                try:
                    logto_sign_in(page, fixture.login_credentials("u1"))
                except Exception:
                    evidence = os.environ.get("AUTH32_PRIVATE_EVIDENCE_DIR")
                    if evidence:
                        path = Path(evidence) / "https-native-login-error.html"
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(descriptor, "w") as stream:
                            stream.write(page.content())
                    raise
                page.wait_for_url(ORIGIN + "/en")
                me = request_from_page(page, "/api/auth/me")
                assert me["status"] == 200 and me["body"]["role"] == "personal"
                session = next(cookie for cookie in context.cookies(ORIGIN) if cookie["name"] == SESSION_COOKIE)
                assert session["secure"] is True and session["httpOnly"] is True
                assert session["domain"] == HOST and session["path"] == "/" and session["sameSite"] == "Lax"
                created = request_from_page(page, "/api/studio/tasks", method="POST", body={"brief": "Real TLS ownership check", "profile_id": "local"},
                    headers={"X-CSRF-Token": me["body"]["csrf_token"], "X-Auth-Context": me["body"]["context_id"], "Idempotency-Key": "tls-ownership-control-001"})
                assert created["status"] == 202
                assert request_from_page(page, "/api/studio/tasks/" + created["body"]["task_id"])["status"] == 200
                assert page.goto(HTTP_WITNESS).status == 200
                assert "auth32_plaintext_probe=observed" in received[-1]
                assert SESSION_COOKIE + "=" not in received[-1]
            finally:
                browser.close()
