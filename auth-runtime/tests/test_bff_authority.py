"""Public freshness checks through a controlled HTTP relay of actual Logto responses."""

import asyncio
import base64
from contextlib import contextmanager
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
import httpx

from production.auth.authority import LogtoAuthority
from production.auth.config import AuthConfig
from production.contracts import Actor, MemberDTO, OperationScope, OrganizationOperationResult, OrganizationOperationTarget, WorkspaceContext
from production.identity_store import PostgresIdentityStore
from tests.browser.test_studio_submission import serving
from tests.fixtures.auth26 import ORIGIN, auth_browser, identity_environment, signed_in, write_headers


@contextmanager
def management_relay(*, delay=0, after_scope=None):
    relay = FastAPI()
    control = {"active": False}

    @relay.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request):
        headers = {name: value for name, value in request.headers.items() if name.lower() in {"authorization", "content-type"}}
        url = "http://127.0.0.1:3010/" + path + ("?" + request.url.query if request.url.query else "")
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.request(request.method, url, headers=headers, content=await request.body())
        if control["active"]:
            if after_scope and path.startswith("api/organization-roles/") and path.endswith("/scopes"):
                after_scope()
            await asyncio.sleep(delay)
        return Response(response.content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))

    with serving(relay) as origin:
        config = AuthConfig.load().model_copy(update={"management_endpoint": origin, "issuer": origin + "/oidc"})
        yield LogtoAuthority(config), control


def test_authority_chain_has_one_deadline_instead_of_accumulating_fresh_looking_old_reads(identity_environment):
    fixture, _, _ = identity_environment
    with management_relay(delay=4.1) as (authority, control), auth_browser(auth_authority=authority) as browser:
        context = browser.new_context()
        page = signed_in(context, fixture, "cross")
        page.close()
        me = context.request.get(ORIGIN + "/api/auth/me").json()
        selected = context.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "organization", "owner_id": fixture.organizations["A"]}, headers=write_headers(me))
        assert selected.status == 200
        control["active"] = True
        start = time.monotonic()
        response = context.request.get(ORIGIN + "/api/auth/me", timeout=60000)
        elapsed, status = time.monotonic() - start, response.status
        assert status == 503
        assert response.json()["error"]["code"] == "identity_unavailable"
        assert elapsed < 16
        control["active"] = False
        assert context.request.get(ORIGIN + "/api/auth/me").status == 200
        context.close()


def test_committed_role_intent_invalidates_in_flight_old_permission_proof(identity_environment):
    fixture, _, _ = identity_environment
    config = AuthConfig.load()
    store = PostgresIdentityStore(config.database_url, schema=config.database_schema,
        encryption_keys={name: base64.urlsafe_b64decode(key) for name, key in config.encryption_keys.items()}, active_key_id=config.active_key_id)
    original = fixture.organization_roles("cross", "A")
    changed = False
    actor = None
    target_user_id = None

    def commit_role_change():
        nonlocal changed
        if changed:
            return
        scope = OperationScope(actor=actor, operation="member:role", resource_id=target_user_id, idempotency_key="auth26-inflight-role")
        target = OrganizationOperationTarget(organization_id=fixture.organizations["A"], user_id=target_user_id, role="viewer")
        intent = store.begin_organization_intent(scope, request_sha256="a" * 64, target=target).intent
        submitted = store.advance_organization_intent(scope, expected_revision=intent.revision, stage="role-submitted", status="submitted")
        fixture.set_roles("cross", "A", ["viewer"])
        changed = True
        result = OrganizationOperationResult(status_code=200, body=MemberDTO(user_id=target_user_id, roles=["viewer"]))
        store.complete_organization_intent(scope, expected_revision=submitted.revision, result=result)

    try:
        with management_relay(after_scope=commit_role_change) as (authority, control), auth_browser(auth_authority=authority) as browser:
            context, admin = browser.new_context(), browser.new_context()
            signed_in(context, fixture, "cross").close()
            signed_in(admin, fixture, "u1").close()
            for current in (context, admin):
                me = current.request.get(ORIGIN + "/api/auth/me").json()
                assert current.request.post(ORIGIN + "/api/auth/workspace", data={"kind": "organization", "owner_id": fixture.organizations["A"]}, headers=write_headers(me)).status == 200
            target_user_id = context.request.get(ORIGIN + "/api/auth/me").json()["user"]["user_id"]
            admin_cookie = next(item for item in admin.cookies() if item["name"] == "openmontage_session")
            admin_session = store.get_session(admin_cookie["value"])
            actor = Actor(identity=admin_session.metadata.identity, workspace=admin_session.metadata.workspace)
            control["active"] = True
            response = context.request.get(ORIGIN + "/api/auth/me")
            status = response.status
            assert status == 409
            assert response.json()["error"]["code"] == "auth_context_conflict"
            assert "permissions" not in response.json()
            control["active"] = False
            fresh = context.request.get(ORIGIN + "/api/auth/me")
            assert fresh.status == 200 and fresh.json()["role"] == "viewer"
            assert "production:approve" not in fresh.json()["permissions"]
            context.close()
            admin.close()
    finally:
        fixture.set_roles("cross", "A", original)
        assert fixture.organization_roles("cross", "A") == original
