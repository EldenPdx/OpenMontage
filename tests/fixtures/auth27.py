"""Issue #27's real browser/API seam; shared identity fixtures stay read-only."""

from contextlib import contextmanager
import json
import re
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import sync_playwright

from tests.browser.test_studio_submission import serving
from tests.fixtures.identity import load_fixture
from tests.fixtures.identity.browser import logto_sign_in
from tests.fixtures.identity.environment import private_json
from tests.fixtures.studio.repository import test_repository as isolated_repository

ORIGIN = "http://127.0.0.1:4761"


@pytest.fixture
def organization_environment(monkeypatch, tmp_path):
    from production.auth.config import AuthConfig
    import os

    fixture = load_fixture()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with isolated_repository() as repository:
        values = fixture.private_app_configuration(ORIGIN)
        ledger = json.loads(fixture.path.read_text())
        values.update(mode="authenticated", origin=ORIGIN, http_development=True,
                      management_endpoint="http://127.0.0.1:3010", management_resource="https://default.logto.app/api",
                      management_app_id=ledger["management"]["app_id"], management_app_secret=ledger["management"]["app_secret"],
                      database_url=os.environ["STUDIO_TEST_DATABASE_URL"], database_schema=repository.schema,
                      encryption_keys={"test": os.environ["STUDIO_TEST_SESSION_KEY"]}, active_key_id="test")
        from backlot import projects_api, state
        monkeypatch.setattr(projects_api, "PROJECTS_DIR", repository.projects_dir)
        monkeypatch.setattr(state, "PROJECTS_DIR", repository.projects_dir)
        yield fixture, repository, AuthConfig.model_validate(values)


@contextmanager
def organization_browser(environment):
    from backlot.server import create_app
    fixture, repository, config = environment
    app = create_app(auth_config=config, studio_repository=repository)
    with serving(app, port=4761), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser, app
        finally:
            browser.close()


def signed_in(browser, fixture, alias="u1"):
    context = browser.new_context()
    page = context.new_page()
    page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"} else route.abort())
    page.goto(ORIGIN + "/api/auth/login?return_to=/en")
    logto_sign_in(page, fixture.login_credentials(alias))
    page.wait_for_url(ORIGIN + "/en")
    page.close()
    return context


def me(context):
    response = context.request.get(ORIGIN + "/api/auth/me")
    assert response.status == 200
    return response.json()


def headers(context, key=None):
    identity = me(context)
    result = {"Origin": ORIGIN, "X-CSRF-Token": identity["csrf_token"], "X-Auth-Context": identity["context_id"]}
    if key:
        result["Idempotency-Key"] = key
    return result


def owned_organization(fixture, response):
    """Record only API-created resources after proving their operation marker."""
    organization = response.json()
    native = fixture.api.request("GET", "/api/organizations/" + organization["organization_id"])
    assert native["name"] == organization["name"]
    assert native["customData"]["openmontageOperationId"].startswith("operation-")
    record_resource("organizations", {"id": native["id"], "name": native["name"], "operation_id": native["customData"]["openmontageOperationId"]})
    return organization


def record_resource(kind, resource):
    path = Path(__file__).resolve().parents[2] / ".runtime/auth27-resources.json"
    ledger = json.loads(path.read_text()) if path.is_file() else {}
    if resource not in ledger.setdefault(kind, []):
        ledger[kind].append(resource)
    private_json(path, ledger)


def unique_name():
    return "auth27-" + uuid4().hex


def create_organization(context, fixture, *, key="organization-create-001"):
    response = context.request.post(ORIGIN + "/api/organizations", data={"name": unique_name()}, headers=headers(context, key))
    assert response.status == 201
    return owned_organization(fixture, response)


def organization_items(context):
    items, query = [], {"limit": 100}
    for _ in range(10):
        response = context.request.get(ORIGIN + "/api/organizations", params=query)
        assert response.status == 200
        page = response.json()
        items.extend(page["items"])
        if page["next_cursor"] is None:
            return items
        query["after"] = page["next_cursor"]
    raise AssertionError("The dedicated identity dataset exceeds the bounded organization inventory")


def select_organization(context, organization_id):
    response = context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "organization", "owner_id": organization_id}, headers=headers(context))
    assert response.status == 200
    return response.json()


def invitation_ticket(fixture, recipient, *, after):
    """Read Mailpit without updating the shared fixture's ownership ledger."""
    import httpx
    deadline = time.monotonic() + 15
    with httpx.Client(trust_env=False, timeout=5) as client:
        while time.monotonic() < deadline:
            response = client.get("http://127.0.0.1:8025/api/v1/messages", params={"limit": 1000})
            response.raise_for_status()
            for message in response.json()["messages"]:
                if message["ID"] not in after and any(to["Address"] == recipient for to in message["To"]):
                    mail = client.get("http://127.0.0.1:8025/api/v1/message/" + message["ID"])
                    mail.raise_for_status()
                    matches = re.findall(r"#ticket=([A-Za-z0-9_-]{43,128})", mail.json().get("Text", "") + mail.json().get("HTML", ""))
                    assert matches and len(set(matches)) == 1, "Expected restricted invitation ticket in real email"
                    record_resource("mail", {"id": message["ID"], "recipient": recipient})
                    return matches[0]
            time.sleep(0.1)
    raise AssertionError("Real invitation email was not received")


def invite(context, fixture, organization_id, alias="cross", role="editor", *, key="invitation-create-001"):
    baseline = fixture.mail_ids()
    response = context.request.post(ORIGIN + "/api/organizations/" + organization_id + "/invitations",
        data={"email": fixture.users[alias].email, "role": role}, headers=headers(context, key))
    assert response.status == 201
    record_resource("invitations", {"id": response.json()["invitation_id"], "organization_id": organization_id})
    return response.json(), invitation_ticket(fixture, fixture.users[alias].email, after=baseline)


def accept(context, ticket, *, key="invitation-accept-001"):
    return context.request.post(ORIGIN + "/api/invitations/accept", data={"ticket": ticket}, headers=headers(context, key))


def short_invitation(context, app, fixture, organization_id, alias, *, seconds, key):
    """The approved real Store seam prepares expiry using a real verified browser session."""
    from datetime import datetime, timedelta, timezone
    import secrets
    from production.contracts import Actor, InvitationCreationMaterial, OperationScope, OrganizationOperationTarget, canonical_sha256

    verified = me(context)
    cookie = next(cookie["value"] for cookie in context.cookies() if cookie["name"] == "openmontage_session")
    service = app.state.organization_service
    metadata = service.store.get_session(cookie).metadata
    assert metadata.context_id == verified["context_id"] and metadata.identity.user_id == verified["user"]["user_id"]
    actor = Actor(identity=metadata.identity, workspace=metadata.workspace)
    target = OrganizationOperationTarget(organization_id=organization_id, email=fixture.users[alias].email, role="editor")
    scope = OperationScope(actor=actor, operation="invitation:create", resource_id=organization_id, idempotency_key=key)
    intent = service.store.begin_organization_intent(scope, request_sha256=canonical_sha256(target), target=target).intent
    role_id = fixture._data["roles"]["editor"]
    expires = datetime.fromtimestamp(int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp() * 1000) / 1000, timezone.utc)
    material = InvitationCreationMaterial(ticket=secrets.token_urlsafe(32), expires_at=expires,
        organization_role_id=role_id, prior_native_invitation_ids=())
    service.store.prepare_invitation_creation(scope, expected_revision=intent.revision, material=material)
    baseline = fixture.mail_ids()
    response = context.request.post(ORIGIN + "/api/organizations/" + organization_id + "/invitations",
        data={"email": target.email, "role": target.role}, headers=headers(context, key))
    assert response.status == 201
    assert invitation_ticket(fixture, target.email, after=baseline) == material.ticket
    return response.json(), material.ticket, expires
