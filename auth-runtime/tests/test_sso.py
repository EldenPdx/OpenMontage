"""Standard Keycloak enterprise identities must reach the real persistent BFF."""

import json
import os
import base64
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import re
import subprocess
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

import httpx
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.fixtures.auth30 import ORIGIN, sso_browser


@pytest.mark.parametrize("protocol,organization", [("oidc", "A"), ("saml", "B")])
def test_enterprise_callback_replay_cannot_create_another_bff_session(monkeypatch, protocol, organization):
    with sso_browser(monkeypatch) as sso:
        member, _ = sso.sign_in(protocol, organization=organization)
        callback, binding_cookie = sso.callback_url, sso.binding_cookie
        assert sso.callback_status == 303
        task = sso.create(member)
        resource = ORIGIN + "/api/studio/tasks/" + task["task_id"]
        assert member.request.get(resource).status == 200
        repeated = sso.browser.new_context()
        repeated.add_cookies([binding_cookie])
        response = repeated.request.get(callback, max_redirects=0)
        assert response.status == 401
        assert repeated.request.get(ORIGIN + "/api/auth/me").status == 401
        assert repeated.request.get(resource).status == 401
        assert not any(value["name"] == "openmontage_session" for value in repeated.cookies(ORIGIN))
        assert member.request.get(resource).status == 200
        repeated.close()


@pytest.mark.parametrize("protocol,organization", [("oidc", "A"), ("saml", "B")])
def test_enterprise_secrets_stay_private_and_logout_revokes_the_resource_session(monkeypatch, caplog, protocol, organization):
    started = datetime.now(timezone.utc).isoformat()
    with sso_browser(monkeypatch) as sso:
        context, page = sso.sign_in(protocol, organization=organization)
        me_response = context.request.get(ORIGIN + "/api/auth/me")
        assert me_response.status == 200 and me_response.headers["cache-control"] == "private, no-store"
        me = me_response.json()
        task = sso.create(context)
        resource_url = ORIGIN + "/api/studio/tasks/" + task["task_id"]
        resource = context.request.get(resource_url)
        assert resource.status == 200
        cookie = next(value for value in context.cookies(ORIGIN) if value["name"] == "openmontage_session")
        runtime = sso.app.state.auth_runtime
        private = runtime.store.get_session(cookie["value"]).sdk_storage
        idp_private = json.loads(Path(sso.fixture._data["idp_private_file"]).read_text())
        protected = [runtime.config.app_secret, runtime.config.management_app_secret,
            private["idToken"], private["refreshToken"], idp_private["oidc_secret"],
            sso.fixture.idp_login_credentials(protocol).password, "private-auth30-model-credential"]
        if protocol == "saml":
            encoded = parse_qs(sso.saml_post)["SAMLResponse"][0]
            raw = base64.b64decode(encoded, validate=True).decode()
            certificate = ET.fromstring(raw).find(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate")
            protected.extend([encoded, raw, certificate.text.strip()])
        public = me_response.text() + json.dumps(task) + resource.text() + page.url + page.content() + page.evaluate(
            "JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
        assert all(value and value not in public for value in protected), "Enterprise credentials or assertions must not enter business DTOs, application URLs or browser storage"
        logout = context.request.post(ORIGIN + "/api/auth/logout", data={}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]})
        assert logout.status == 200
        logout_url = logout.json()["logout_url"]
        parsed = urlsplit(logout_url)
        assert parsed.scheme == "http" and parsed.netloc == "127.0.0.1:3010" and parsed.path == "/oidc/session/end"
        assert parse_qs(parsed.query) == {"client_id": [runtime.config.app_id], "post_logout_redirect_uri": [ORIGIN + "/en"]}
        assert all(value not in logout.text() for value in protected)
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        assert context.request.get(resource_url).status == 401
        copied = sso.browser.new_context()
        copied.add_cookies([cookie])
        assert copied.request.get(resource_url).status == 401
        copied.close()
        page.goto(logout_url)
        try:
            page.wait_for_url(ORIGIN + "/en", timeout=5000)
        except PlaywrightTimeoutError:
            page.get_by_role("button", name=re.compile(r"^(Yes|Sign out|Continue)$", re.I)).click()
        page.wait_for_url(ORIGIN + "/en")
        page.goto(ORIGIN + "/api/auth/login")
        page.locator('input[name="identifier"]').wait_for()
        assert page.url.startswith("http://127.0.0.1:3010/")
        application_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith(("production", "backlot")))
        native_logs = subprocess.run(["docker", "logs", "--since", started, "openmontage-auth24-logto-1"],
            capture_output=True, text=True, timeout=15)
        assert native_logs.returncode == 0, "Native log privacy requires actual operator-readable logs"
        logs = application_logs + native_logs.stdout + native_logs.stderr
        assert all(value not in logs for value in protected), "Enterprise secrets must not enter ordinary application or native logs"


def test_standard_oidc_sso_creates_a_persistent_personal_identity_and_owned_resource(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        context, page = sso.sign_in("oidc")
        me = sso.me(context)
        assert me["workspace"] == {"kind": "personal", "owner_id": me["user"]["user_id"],
            "key": "personal:" + me["user"]["user_id"], "name": "Personal"}
        assert me["role"] == "personal"
        principal = sso.record_principal(context, "oidc")
        assert principal["sso_identity_id"] == principal["fixture_idp_subject"]
        assert principal["sso_issuer"] == sso.fixture.idp["oidc"]["issuer"]
        assert principal["email"] == principal["fixture_idp_email"]
        assert principal["native_subject"] not in {user.id for user in sso.fixture.users.values()}
        task = sso.create(context)
        owned = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
        assert owned.status == 200
        assert owned.json()["ownership"]["workspace"] == {"kind": "personal", "owner_id": me["user"]["user_id"]}
        signed_out = sso.browser.new_context()
        assert signed_out.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 401
        cookie = next(value for value in context.cookies(ORIGIN) if value["name"] == "openmontage_session")
        assert cookie["httpOnly"] and cookie["sameSite"] == "Lax"
        assert page.url == ORIGIN + "/en"


def test_standard_oidc_sso_selects_only_its_explicit_editor_membership(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        context, _ = sso.sign_in("oidc", organization="A")
        me = sso.me(context)
        assert me["workspace"]["key"] == "organization:" + sso.fixture.organizations["A"]
        assert me["role"] == "editor" and "production:create" in me["permissions"]
        expected = sso.fixture._data["sso_subjects"]["oidc"]
        principal = sso.record_principal(context, "oidc")
        assert principal["native_subject"] == expected["native_subject"]
        task = sso.create(context)
        resource = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
        assert resource.status == 200
        assert resource.json()["ownership"]["workspace"] == {"kind": "organization", "owner_id": sso.fixture.organizations["A"]}
        personal, _ = sso.sign_in("oidc")
        assert sso.me(personal)["role"] == "personal"
        assert personal.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 404


def test_standard_signed_saml_sso_creates_its_own_persistent_personal_identity(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        context, _ = sso.sign_in("saml")
        me = sso.me(context)
        assert me["role"] == "personal"
        assert me["workspace"]["owner_id"] == me["user"]["user_id"]
        principal = sso.record_principal(context, "saml")
        assert principal["signed_saml_name_id_format"] == "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
        assert principal["sso_identity_id"] == principal["signed_saml_name_id"]
        assert principal["xml_signature_count"] == 2
        assert principal["email"] == principal["fixture_idp_email"]
        assert principal["native_subject"] != sso.fixture._data["sso_subjects"]["oidc"]["native_subject"]
        assert principal["native_subject"] not in {user.id for user in sso.fixture.users.values()}
        task = sso.create(context)
        assert context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 200
        logout = context.request.post(ORIGIN + "/api/auth/logout", data={}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"], "X-Auth-Context": me["context_id"]})
        assert logout.status == 200
        assert context.request.get(ORIGIN + "/api/auth/me").status == 401
        context.close()
        fresh, _ = sso.sign_in("saml")
        second = sso.record_principal(fresh, "saml")
        assert second["signed_saml_name_id_format"] == principal["signed_saml_name_id_format"]
        assert second["signed_response_request_id"] != principal["signed_response_request_id"]
        assert second["signed_saml_name_id"] == principal["signed_saml_name_id"]
        assert second["native_subject"] == principal["native_subject"]
        assert second["public_user_id"] == principal["public_user_id"]
        assert second["sso_binding_count"] == principal["sso_binding_count"] == 1
        directory = os.environ.get("AUTH30_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / "saml-stability.json"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                json.dump({"first": principal, "fresh_after_local_logout": second, "fresh_browser_contexts": 2}, output, indent=2)


def test_standard_signed_saml_sso_keeps_explicit_a_viewer_and_b_editor_separate(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        creator, _ = sso.sign_in("oidc", organization="A")
        a_task = sso.create(creator)
        viewer, _ = sso.sign_in("saml", organization="A")
        a_me = sso.me(viewer)
        assert a_me["workspace"]["key"] == "organization:" + sso.fixture.organizations["A"]
        assert a_me["role"] == "viewer" and "production:create" not in a_me["permissions"]
        assert viewer.request.get(ORIGIN + "/api/studio/tasks/" + a_task["task_id"]).status == 200
        denied = viewer.request.post(ORIGIN + "/api/studio/tasks", data={}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": a_me["csrf_token"], "X-Auth-Context": a_me["context_id"],
            "Idempotency-Key": "auth30-viewer-create"})
        assert denied.status == 403
        editor, _ = sso.sign_in("saml", organization="B")
        b_me = sso.me(editor)
        assert b_me["user"]["user_id"] == a_me["user"]["user_id"]
        assert b_me["workspace"]["key"] == "organization:" + sso.fixture.organizations["B"]
        assert b_me["role"] == "editor"
        assert sso.record_principal(editor, "saml")["native_subject"] == sso.fixture._data["sso_subjects"]["saml"]["native_subject"]
        b_task = sso.create(editor)
        assert editor.request.get(ORIGIN + "/api/studio/tasks/" + b_task["task_id"]).status == 200
        assert editor.request.get(ORIGIN + "/api/studio/tasks/" + a_task["task_id"]).status == 404
        assert creator.request.get(ORIGIN + "/api/studio/tasks/" + b_task["task_id"]).status == 404


def test_valid_oidc_identity_cannot_select_a_foreign_organization_hint(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        member, _ = sso.sign_in("oidc", organization="A")
        task = sso.create(member)
        assert member.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 200
        member.close()
        nonmember, page = sso.sign_in("oidc", organization="B")
        assert sso.callback_status == 404
        assert nonmember.request.get(ORIGIN + "/api/auth/me").status == 401
        assert not any(value["name"] == "openmontage_session" for value in nonmember.cookies(ORIGIN))
        assert page.url == ORIGIN + "/en"


def test_same_email_distinct_oidc_identity_cannot_link_an_existing_account(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        valid, _ = sso.sign_in("oidc", organization="A", direct=True)
        task = sso.create(valid)
        assert valid.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 200
        valid.close()
        original = sso.fixture.users["u1"]
        before = sso.fixture.api.request("GET", "/api/users/" + original.id + "/all-identities")
        assert before["ssoIdentities"] == []
        attacker, _ = sso.sign_in("same-email", organization="A", direct=True, expect_email_rejection=True)
        me = attacker.request.get(ORIGIN + "/api/auth/me")
        after = sso.fixture.api.request("GET", "/api/users/" + original.id + "/all-identities")
        resource = attacker.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
        evidence = {"control_resource_status": 200, "existing_native_subject": original.id,
            "fixture_idp_subject": sso.fixture.idp["users"]["same-email"]["id"],
            "me_status": me.status, "resource_status": resource.status,
            "native_rejection": sso.email_rejection,
            "new_sso_bindings": [{key: item["ssoIdentity"][key] for key in ("ssoConnectorId", "issuer", "identityId", "userId")}
                for item in after["ssoIdentities"]]}
        if me.status == 200:
            current = me.json()
            evidence["authorized_role"] = current["role"]
            response = attacker.request.post(ORIGIN + "/api/auth/logout", data={}, headers={
                "Origin": ORIGIN, "X-CSRF-Token": current["csrf_token"], "X-Auth-Context": current["context_id"]})
            assert response.status == 200
        directory = os.environ.get("AUTH30_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / "same-email-observations.json"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                json.dump(evidence, output, indent=2)
        attacker.close()
        assert after["ssoIdentities"] == [], "Native SSO must not attach an unlinked immutable identity by email"
        assert sso.email_rejection == {"status": 422, "code": "user.email_already_in_use"}
        assert me.status == 401
        assert resource.status == 401


def test_signed_saml_assertion_has_one_native_concurrent_winner_and_rejects_replay(monkeypatch):
    with sso_browser(monkeypatch) as sso:
        valid, _ = sso.sign_in("saml", organization="B")
        task = sso.create(valid)
        assert valid.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 200
        valid.close()
        capture = {}
        context, page = sso.sign_in("saml", organization="B", hold_saml_post=capture)
        acs = sso.fixture.idp["saml"]["acs_url"]
        body = capture["body"]
        cookies = {item["name"]: item["value"] for item in context.cookies("http://127.0.0.1:3010")}

        def submit():
            with httpx.Client(trust_env=False, cookies=cookies, timeout=15, follow_redirects=False) as client:
                return client.post(acs, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(submit) for _ in range(2)]
            responses = [future.result(timeout=20) for future in futures]
        repeated = submit()
        accepting = next(response for response in responses if response.status_code == 302)
        page.goto(accepting.headers["location"])
        page.wait_for_url(ORIGIN + "/en")
        me = sso.me(context)
        resumed = context.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"])
        assert resumed.status == 200 and me["role"] == "editor"
        evidence = {"control_resource_status": 200, "signed_body_sha256": sha256(body.encode()).hexdigest(),
            "native_first_statuses": [response.status_code for response in responses],
            "native_repeat_status": repeated.status_code, "resumed_resource_status": resumed.status}
        directory = os.environ.get("AUTH30_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / "native-replay-observations.json"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                json.dump(evidence, output, indent=2)
        assert sum(response.status_code == 302 for response in responses) == 1
        assert all(response.status_code in {400, 403, 404, 409} for response in responses if response is not accepting)
        assert repeated.status_code in {400, 403, 404, 409}
