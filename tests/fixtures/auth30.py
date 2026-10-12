"""Real standard enterprise SSO through the public persistent BFF on its own origin."""

from contextlib import contextmanager
import base64
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import pytest
from playwright.sync_api import Error, sync_playwright

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.auth.config import AuthConfig
from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sso_sign_in
from tests.fixtures.studio.repository import test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4765"


class SsoBrowser:
    def __init__(self, browser, fixture, repository, app):
        self.browser, self.fixture, self.repository, self.app = browser, fixture, repository, app

    def sign_in(self, protocol, *, organization=None, direct=False, hold_saml_post=None, expect_email_rejection=False):
        context = self.browser.new_context()
        page = context.new_page()
        self.callback_status = None
        self.callback_url = None
        self.email_rejection = None

        def capture_callback(response):
            if response.url.split("?", 1)[0] == ORIGIN + "/api/auth/callback":
                self.callback_status = response.status
                self.callback_url = response.url
            elif response.url.startswith("http://127.0.0.1:3010/api/") and response.status == 422:
                try:
                    code = response.json().get("code")
                except (Error, ValueError):
                    return
                if code == "user.email_already_in_use":
                    self.email_rejection = {"status": response.status, "code": code}

        page.on("response", capture_callback)
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(
            (ORIGIN + "/", "http://127.0.0.1:3010/", "http://127.0.0.1:8180/")) else route.abort())
        if protocol == "saml":
            self.saml_post = None

            def capture_assertion(request):
                if request.url == self.fixture.idp["saml"]["acs_url"] and request.method == "POST":
                    self.saml_post = request.post_data

            page.on("request", capture_assertion)
            if hold_saml_post is not None:
                def hold_assertion(route):
                    hold_saml_post["body"] = route.request.post_data
                    route.fulfill(status=200, body="Signed assertion held for its native boundary check",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

                page.route(self.fixture.idp["saml"]["acs_url"], hold_assertion)
        parameters = {"return_to": "/en"}
        if organization is not None:
            parameters["organization_id"] = self.fixture.organizations[organization]
        initiation = context.request.get(ORIGIN + "/api/auth/login?" + urlencode(parameters), max_redirects=0)
        assert initiation.status == 302
        self.binding_cookie = next(value for value in context.cookies(ORIGIN) if value["name"] == "openmontage_login")
        location = initiation.headers["location"]
        if direct:
            parsed = urlsplit(location)
            connector = self.fixture.idp["oidc"]["connector_id"]
            location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                urlencode(parse_qsl(parsed.query) + [("direct_sign_in", "sso:" + connector)]), ""))
        page.goto(location)
        try:
            credentials = self.fixture.idp_login_credentials(protocol)
            if direct:
                page.locator("#username").fill(credentials.username)
                page.locator("#password").fill(credentials.password)
                page.locator("#kc-login").click()
            else:
                logto_sso_sign_in(page, credentials)
        except (RuntimeError, Error):
            directory = os.environ.get("AUTH30_EVIDENCE_DIR")
            if directory:
                path = Path(directory) / "browser-failure.html"
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                    output.write(page.content())
                path.chmod(0o600)
            raise RuntimeError("Real enterprise sign-in could not complete") from None
        if expect_email_rejection:
            deadline = time.monotonic() + 10
            while self.email_rejection is None and self.callback_status is None and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert self.email_rejection is not None or self.callback_status is not None, "Native sign-in must finish with an observable outcome"
        else:
            page.wait_for_url(self.fixture.idp["saml"]["acs_url"] if hold_saml_post is not None else ORIGIN + "/en")
        return context, page

    def me(self, context):
        response = context.request.get(ORIGIN + "/api/auth/me")
        assert response.status == 200
        return response.json()

    def create(self, context):
        me = self.me(context)
        response = context.request.post(ORIGIN + "/api/studio/tasks", headers={
            "Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"],
            "X-Auth-Context": me["context_id"], "Idempotency-Key": "auth30-owned-resource"},
            data={"brief": "A two second lighthouse animation", "profile_id": "local", "duration_seconds": 2})
        assert response.status == 202, response.json().get("error", {}).get("code")
        return response.json()

    def record_principal(self, context, protocol):
        cookie = next(value for value in context.cookies(ORIGIN) if value["name"] == "openmontage_session")
        session = self.app.state.auth_runtime.store.get_session(cookie["value"])
        native_sub = session.metadata.identity.sub
        connector = self.fixture.idp["oidc" if protocol == "same-email" else protocol]["connector_id"]
        user = self.fixture.api.request("GET", "/api/users/" + native_sub)
        expected = self.fixture.idp["users"][protocol]
        saml = {}
        if protocol == "saml":
            assertion = ET.fromstring(base64.b64decode(parse_qs(self.saml_post)["SAMLResponse"][0], validate=True))
            name_id = assertion.find(".//{urn:oasis:names:tc:SAML:2.0:assertion}NameID")
            saml = {"signed_saml_name_id": name_id.text, "signed_saml_name_id_format": name_id.attrib.get("Format"),
                "signed_response_request_id": assertion.attrib.get("InResponseTo"),
                "xml_signature_count": len(assertion.findall(".//{http://www.w3.org/2000/09/xmldsig#}Signature"))}
        all_identities = self.fixture.api.request("GET", "/api/users/" + native_sub + "/all-identities")
        bindings = [item["ssoIdentity"] for item in all_identities["ssoIdentities"] if item["ssoConnectorId"] == connector]
        identity_id = saml.get("signed_saml_name_id", expected["id"])
        matching = [value for value in bindings if value["identityId"] == identity_id]
        assert len(matching) == 1
        identity = matching[0]
        evidence = {"namespace": self.fixture._data["namespace"], "protocol": protocol,
            "connector_id": connector, "native_subject": native_sub,
            "email": user.get("primaryEmail"), "sso_issuer": identity["issuer"],
            "sso_identity_id": identity["identityId"], "fixture_idp_subject": expected["id"],
            "fixture_idp_email": expected["email"], "public_user_id": self.me(context)["user"]["user_id"],
            "sso_binding_count": len(bindings), **saml}
        directory = os.environ.get("AUTH30_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / ("principal-" + protocol + ".json")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
                json.dump(evidence, output, indent=2)
                output.write("\n")
            path.chmod(0o600)
        return evidence


@contextmanager
def sso_browser(monkeypatch):
    if not os.environ.get("STUDIO_TEST_DATABASE_URL") or not os.environ.get("STUDIO_TEST_SESSION_KEY"):
        pytest.fail("SSO acceptance requires a real isolated PostgreSQL database and private session key")
    monkeypatch.setenv("STUDIO_REQUIRE_INTEGRATION", "1")
    fixture = load_fixture()
    assert fixture._data["namespace"] == "auth30", "SSO cases require their own lifecycle-owner fixture"
    ledger = json.loads(fixture.path.read_text())
    with isolated_repository() as repository:
        values = fixture.private_app_configuration()
        assert values["redirect_uri"] == ORIGIN + "/api/auth/callback"
        values.update(mode="authenticated", origin=ORIGIN, http_development=True,
            management_endpoint=values["endpoint"], management_resource=ledger["management"]["resource"],
            management_app_id=ledger["management"]["app_id"], management_app_secret=ledger["management"]["app_secret"],
            database_url=os.environ["STUDIO_TEST_DATABASE_URL"], database_schema=repository.schema,
            encryption_keys={"test": os.environ["STUDIO_TEST_SESSION_KEY"]}, active_key_id="test")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        profile = PiProfile(provider="controlled", base_url="http://127.0.0.1:9/v1", model="local-model",
            credential_env="AUTH30_MODEL_KEY", reasoning=False, thinking_level="off",
            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        app = create_app(studio_repository=repository,
            studio_config=StudioConfig(enabled=True, default_profile="local", profiles={"local": profile}),
            studio_environment={"AUTH30_MODEL_KEY": "private-auth30-model-credential"},
            auth_config=AuthConfig.model_validate(values))
        with serving(app, port=4765), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                yield SsoBrowser(browser, fixture, repository, app)
            finally:
                browser.close()
