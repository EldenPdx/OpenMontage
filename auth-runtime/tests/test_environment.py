"""Required real identity environment checks; run explicitly, never skip."""

import httpx


def test_logto_has_real_oidc_discovery_and_signing_keys():
    with httpx.Client(trust_env=False, timeout=5) as client:
        response = client.get("http://127.0.0.1:3010/oidc/.well-known/openid-configuration")
        response.raise_for_status()
        discovery = response.json()
        assert discovery["issuer"] == "http://127.0.0.1:3010/oidc"
        assert discovery["authorization_endpoint"].startswith(discovery["issuer"] + "/")
        keys = client.get(discovery["jwks_uri"])
        keys.raise_for_status()
        assert any(key.get("kty") in {"RSA", "EC"} for key in keys.json()["keys"])


def test_fixture_memberships_come_from_real_logto_authority():
    from tests.fixtures.identity import load_fixture
    fixture = load_fixture()
    assert fixture.users["u1"].id != fixture.users["u2"].id
    assert fixture.organization_roles("u1", "A") == ["admin"]
    assert fixture.organization_roles("u2", "B") == ["admin"]
    assert fixture.organization_roles("cross", "A") == ["editor"]
    assert fixture.organization_roles("cross", "B") == ["viewer"]
    assert fixture.organization_roles("viewer", "A") == ["viewer"]


def test_logto_delivers_an_actual_invitation_to_the_local_mailbox():
    from uuid import uuid4
    from tests.fixtures.identity import load_fixture
    fixture = load_fixture()
    recipient = "auth24.mail." + uuid4().hex[:10] + "@openmontage.test"
    before = fixture.mail_ids()
    invitation = fixture.send_invitation(recipient, "A", "viewer")
    try:
        message = fixture.wait_for_mail(recipient, after=before)
        assert message["Subject"] == "OpenMontage OrganizationInvitation"
        assert any(to["Address"] == recipient for to in message["To"])
    finally:
        fixture.cancel_invitation(invitation["id"])


def _authorize(action):
    import asyncio
    from fastapi import FastAPI
    from logto import LogtoClient, LogtoConfig
    from logto.Storage import MemoryStorage
    from playwright.sync_api import sync_playwright
    from tests.browser.test_studio_submission import serving
    from tests.fixtures.identity import load_fixture
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/api/auth/callback")
    def callback():
        return {"fixture": "native OIDC callback"}

    @app.get("/en")
    def done():
        return {"fixture": "callback consumed"}

    fixture = load_fixture()
    with serving(app) as origin:
        fixture.app_for_origin(origin)
        config = fixture.private_app_configuration(origin)
        client = LogtoClient(LogtoConfig(endpoint=config["endpoint"], appId=config["app_id"], appSecret=config["app_secret"]), MemoryStorage())
        url = asyncio.run(client.signIn(config["redirect_uri"]))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith((origin + "/", "http://127.0.0.1:3010/", "http://127.0.0.1:8180/")) else route.abort())
            try:
                page.goto(url)
                action(page, fixture)
                page.wait_for_url(origin + "/api/auth/callback**")
                callback_url = page.url
                page.evaluate("history.replaceState({}, '', '/en')")
            finally:
                browser.close()
        asyncio.run(client.handleSignInCallback(callback_url))
        return client.getIdTokenClaims().sub


def test_real_logto_login_works_with_the_pinned_python_sdk():
    from tests.fixtures.identity import load_fixture
    from tests.fixtures.identity.browser import logto_sign_in
    sub = _authorize(lambda page, fixture: logto_sign_in(page, fixture.login_credentials("u1")))
    assert sub == load_fixture().users["u1"].id


def test_real_email_registration_and_password_reset_use_local_mail():
    import secrets
    from uuid import uuid4
    from tests.fixtures.identity import load_fixture
    from tests.fixtures.identity.environment import LoginCredentials
    from tests.fixtures.identity.browser import logto_register, logto_reset_password, logto_sign_in
    email = "auth24.register." + uuid4().hex[:10] + "@openmontage.test"
    first = LoginCredentials(email, email, "Aa9-" + secrets.token_urlsafe(24))
    replacement = LoginCredentials(email, email, "Bb9-" + secrets.token_urlsafe(24))
    sub = _authorize(lambda page, fixture: logto_register(page, fixture, first))
    fixture = load_fixture()
    try:
        user = fixture.api.request("GET", "/api/users/" + sub)
        assert user["primaryEmail"] == email
        def reset_then_login(page, fixture):
            logto_reset_password(page, fixture, replacement)
            logto_sign_in(page, replacement)
        assert _authorize(reset_then_login) == sub
    finally:
        fixture.api.request("DELETE", "/api/users/" + sub)


def test_real_standard_idp_metadata_and_logto_connectors_are_usable():
    from tests.fixtures.identity import load_fixture
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    import base64
    import xml.etree.ElementTree as ET
    fixture = load_fixture()
    with httpx.Client(trust_env=False) as client:
        oidc = client.get(fixture.idp["oidc"]["discovery_url"])
        oidc.raise_for_status()
        assert oidc.json()["issuer"] == fixture.idp["oidc"]["issuer"]
        saml = client.get(fixture.idp["saml"]["metadata_url"])
        saml.raise_for_status()
        xml = ET.fromstring(saml.text)
        cert = xml.find(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate")
        certificate = x509.load_der_x509_certificate(base64.b64decode(cert.text))
        assert certificate.fingerprint(hashes.SHA256()).hex() == fixture.idp["saml"]["certificate_sha256"]
    for protocol, provider in (("oidc", "OIDC"), ("saml", "SAML")):
        connector = fixture.api.request("GET", "/api/sso-connectors/" + fixture.idp[protocol]["connector_id"])
        assert connector["providerName"] == provider
        assert connector["providerConfig"]


def test_real_oidc_enterprise_sso_returns_a_verified_logto_identity():
    from tests.fixtures.identity import load_fixture
    from tests.fixtures.identity.browser import logto_sso_sign_in
    fixture = load_fixture()
    credentials = fixture.idp_login_credentials("oidc")
    sub = _authorize(lambda page, fixture: logto_sso_sign_in(page, credentials))
    user = fixture.api.request("GET", "/api/users/" + sub)
    assert user["primaryEmail"] == credentials.email
    assert sub not in {fixture.users["u1"].id, fixture.users["u2"].id}


def test_real_signed_saml_sso_returns_a_verified_logto_identity():
    from tests.fixtures.identity import load_fixture
    from tests.fixtures.identity.browser import logto_sso_sign_in
    fixture = load_fixture()
    credentials = fixture.idp_login_credentials("saml")
    sub = _authorize(lambda page, fixture: logto_sso_sign_in(page, credentials))
    user = fixture.api.request("GET", "/api/users/" + sub)
    assert user["primaryEmail"] == credentials.email
    assert sub not in {fixture.users["u1"].id, fixture.users["u2"].id}


def test_runtime_database_accounts_and_seed_credentials_are_separated():
    import json
    from pathlib import Path
    import subprocess
    import psycopg
    import pytest
    root = Path(__file__).resolve().parents[2]
    credentials = json.loads((root / "auth-runtime/.state/credentials.json").read_text())
    for role, key, foreign in (("auth_logto", "logto_db", "auth_keycloak"), ("auth_keycloak", "keycloak_db", "auth_logto")):
        dsn = f"postgresql://{role}:{credentials[key]}@127.0.0.1:55433/{role}"
        try:
            connection = psycopg.connect(dsn)
        except psycopg.Error:
            pytest.fail("Restricted identity database is unavailable", pytrace=False)
        with connection:
            flags = connection.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            assert flags == (False, False, False, False, False)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(dsn.rsplit("/", 1)[0] + "/" + foreign)
    mounted = json.loads(subprocess.check_output(["docker", "inspect", "openmontage-auth24-logto-1", "--format", "{{json .Mounts}}"], text=True))
    assert "/run/secrets/logto_bootstrap_env" not in {mount["Destination"] for mount in mounted}


def test_vendor_management_scope_is_coarse_and_ungranted_clients_are_denied():
    from tests.fixtures.identity import load_fixture
    fixture = load_fixture()
    resource = next(item for item in fixture.api.items("/api/resources") if item["indicator"] == "https://default.logto.app/api")
    scopes = fixture.api.items("/api/resources/" + resource["id"] + "/scopes")
    assert [scope["name"] for scope in scopes] == ["all"]
    config = fixture.private_app_configuration()
    with httpx.Client(trust_env=False) as client:
        denied = client.post("http://127.0.0.1:3010/oidc/token", data={
            "grant_type": "client_credentials", "client_id": config["app_id"], "client_secret": config["app_secret"],
            "resource": resource["indicator"], "scope": "all"})
        assert denied.status_code in {400, 401}
        assert "access_token" not in denied.json()
        assert client.get("http://127.0.0.1:3010/api/organizations").status_code == 401


def test_repeated_fixture_initialization_preserves_authoritative_resource_ids():
    from tests.fixtures.identity import load_fixture, seed_fixture
    fixture = load_fixture()
    paths = ("/api/users", "/api/organizations", "/api/applications", "/api/organization-roles", "/api/organization-scopes", "/api/sso-connectors")
    before = {path: sorted(item["id"] for item in fixture.api.items(path)) for path in paths}
    seed_fixture()
    seed_fixture()
    after = {path: sorted(item["id"] for item in fixture.api.items(path)) for path in paths}
    assert after == before


def test_cleanup_removes_only_recorded_logto_idp_and_mail_resources():
    from uuid import uuid4
    from tests.fixtures.identity import load_fixture, seed_fixture, cleanup_fixture
    fixture = load_fixture()
    namespace = "auth24-clean-" + uuid4().hex[:8]
    sentinel = fixture.api.request("POST", "/api/organizations", {"name": namespace + ":foreign", "customData": {"auth24": "foreign"}})
    case = seed_fixture(namespace=namespace)
    recipient = namespace + ".invite@openmontage.test"
    before = case.mail_ids()
    case.send_invitation(recipient, "A", "viewer")
    message = case.wait_for_mail(recipient, after=before)
    user_id, org_id = case.users["u1"].id, case.organizations["A"]
    try:
        cleanup_fixture(case)
        cleanup_fixture(case)
        assert fixture.api.request("GET", "/api/users/" + user_id, missing_ok=True) is None
        assert fixture.api.request("GET", "/api/organizations/" + org_id, missing_ok=True) is None
        assert fixture.api.request("GET", "/api/organizations/" + sentinel["id"])["id"] == sentinel["id"]
        assert fixture.organization_roles("u1", "A") == ["admin"]
        with httpx.Client(trust_env=False) as client:
            assert client.get("http://127.0.0.1:8180/realms/" + namespace + "/.well-known/openid-configuration").status_code == 404
            assert client.get("http://127.0.0.1:8025/api/v1/message/" + message["ID"]).status_code == 404
    finally:
        fixture.api.request("DELETE", "/api/organizations/" + sentinel["id"], missing_ok=True)


def test_foreign_keycloak_realm_is_rejected_before_mutation_and_survives_cleanup():
    import json
    import pytest
    from uuid import uuid4
    from tests.fixtures.identity import load_fixture, seed_fixture, cleanup_fixture
    fixture = load_fixture()
    state = fixture.path.parent
    namespace = "auth24-owner-" + uuid4().hex[:8]
    credentials = json.loads((state / "credentials.json").read_text())
    base = "http://127.0.0.1:8180"
    realm_url = base + "/admin/realms/" + namespace
    path = state / ("fixture-" + namespace + ".json")
    with httpx.Client(trust_env=False, timeout=20) as client:
        token = client.post(base + "/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": "auth24-admin", "password": credentials["keycloak_admin"]})
        token.raise_for_status()
        client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        assert client.get(realm_url).status_code == 404
        client.post(base + "/admin/realms", json={"realm": namespace, "enabled": True,
            "attributes": {"openmontageAuthFixture": "foreign-owner", "sentinel": "retain"}}).raise_for_status()
        try:
            client.post(realm_url + "/users", json={"username": "foreign-sentinel", "enabled": True}).raise_for_status()
            client.post(realm_url + "/clients", json={"clientId": "foreign-client", "protocol": "openid-connect", "publicClient": True}).raise_for_status()
            before_realm = client.get(realm_url).json()
            before_users = client.get(realm_url + "/users").json()
            before_clients = client.get(realm_url + "/clients").json()
            with pytest.raises(RuntimeError, match="IdP realm does not belong to this fixture"):
                seed_fixture(namespace=namespace)
            assert client.get(realm_url).json() == before_realm
            assert client.get(realm_url + "/users").json() == before_users
            assert client.get(realm_url + "/clients").json() == before_clients
            assert not (state / ("idp-" + namespace + ".json")).exists()
            cleanup_fixture(load_fixture(path))
            assert client.get(realm_url).json() == before_realm
            assert client.get(realm_url + "/users").json() == before_users
            assert client.get(realm_url + "/clients").json() == before_clients
        finally:
            if path.exists():
                cleanup_fixture(load_fixture(path))
            # Only the test creator removes this deliberately foreign-marked probe.
            assert client.delete(realm_url).status_code in {204, 404}


def test_origin_registration_is_exact_reentrant_and_rejects_foreign_origins():
    import pytest
    from tests.fixtures.identity import load_fixture
    fixture = load_fixture()
    first = fixture.app_for_origin("http://127.0.0.1:4760")
    second = fixture.app_for_origin("http://127.0.0.1:4760")
    assert second == first
    assert "app_secret" not in first
    registered = fixture.api.request("GET", "/api/applications/" + first["app_id"])
    assert registered["oidcClientMetadata"]["redirectUris"] == ["http://127.0.0.1:4760/api/auth/callback"]
    for origin in ("http://example.com", "http://127.0.0.1:4760/escape", "http://user@127.0.0.1:4760", "http://:secret@127.0.0.1:4760", "http://127.\n0.0.1:4760", "http://0.0.0.0:4760"):
        with pytest.raises(ValueError):
            fixture.app_for_origin(origin)


def test_one_bootstrap_command_reuses_the_private_environment_and_real_fixture():
    from pathlib import Path
    import subprocess
    import sys
    import pytest
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-B", str(root / "auth-runtime/bootstrap.py"), "up"], cwd=root, capture_output=True, text=True, timeout=300)
    if result.returncode:
        pytest.fail("Single-command identity bootstrap failed; inspect private command diagnostics", pytrace=False)
    with httpx.Client(trust_env=False) as client:
        assert client.get("http://127.0.0.1:8180/realms/auth24/.well-known/openid-configuration").status_code == 200


def test_restart_preserves_identity_resources_and_standard_idp_certificate():
    import subprocess
    import sys
    from pathlib import Path
    import pytest
    from tests.fixtures.identity import load_fixture
    fixture = load_fixture()
    paths = ("/api/users", "/api/organizations", "/api/applications", "/api/organization-roles", "/api/sso-connectors")
    before = {path: sorted(item["id"] for item in fixture.api.items(path)) for path in paths}
    certificate = fixture.idp["saml"]["certificate_sha256"]
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-B", str(root / "auth-runtime/manage.py"), "restart"], cwd=root, capture_output=True, text=True, timeout=300)
    if result.returncode:
        pytest.fail("Owned identity restart failed; inspect private diagnostics", pytrace=False)
    after_fixture = load_fixture()
    after = {path: sorted(item["id"] for item in after_fixture.api.items(path)) for path in paths}
    assert after == before
    assert after_fixture.idp["saml"]["certificate_sha256"] == certificate
    assert after_fixture.organization_roles("cross", "A") == ["editor"]
