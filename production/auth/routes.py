"""Public login/me boundary and the context seam used by business routers."""

import asyncio
import base64
from datetime import datetime, timezone
import json
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from production.auth.authority import LogtoAuthority
from production.auth.oidc import PrivateLogtoClient, PrivateStorage, safe_return_path
from production.contracts import AuthContext, ContractModel, ContractViolation, WorkspaceContext
from production.identity_store import PostgresIdentityStore
from production.ownership_guard import mark_authenticated

SESSION_COOKIE = "openmontage_session"
BINDING_COOKIE = "openmontage_login"


class LogoutRequest(ContractModel):
    pass


def protocol_query(request, allowed):
    values = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in values or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ContractViolation("Request does not match the identity contract")
        values[key] = value
    return values


class AuthRuntime:
    def __init__(self, config, *, store=None, authority=None):
        self.config = config
        self.store = store or PostgresIdentityStore(config.database_url, schema=config.database_schema,
            encryption_keys={name: base64.urlsafe_b64decode(value) for name, value in config.encryption_keys.items()},
            active_key_id=config.active_key_id)
        self.authority = authority or LogtoAuthority(config)

    async def startup(self, projects_dir):
        binding = await asyncio.to_thread(self.store.repository.database_binding)
        await asyncio.to_thread(mark_authenticated, projects_dir, binding=binding)

    def set_session_cookie(self, response, handle):
        remaining = max(1, int((handle.session.metadata.absolute_expires_at - datetime.now(timezone.utc)).total_seconds()))
        response.set_cookie(SESSION_COOKIE, handle.cookie, httponly=True, secure=self.config.origin.startswith("https:"),
                            samesite="lax", path="/", max_age=remaining, expires=handle.session.metadata.absolute_expires_at)

    async def require_context(self, request, *, write=False):
        cookie = request.cookies.get(SESSION_COOKIE)
        if write:
            session = await asyncio.to_thread(self.store.validate_context, cookie,
                request.headers.get("x-csrf-token"), request.headers.get("x-auth-context"))
        else:
            session = await asyncio.to_thread(self.store.get_session, cookie)
            if session is None:
                raise ContractViolation("Authentication is required", "unauthenticated")
        storage = PrivateStorage(session.sdk_storage)
        client = PrivateLogtoClient(self.config, storage)
        info = await client.fetchUserInfo()
        core = await client.getOidcCore()
        core.verifyIdToken(client.getIdToken(), self.config.app_id)
        if core.claims["sub"] != session.metadata.identity.sub or info.sub != core.claims["sub"]:
            raise ContractViolation("Authentication could not be verified", "unauthenticated")
        authority = await self.authority.verify(session.metadata.identity, session.metadata.workspace)
        if storage.values != session.sdk_storage:
            session = await asyncio.to_thread(self.store.update_session, cookie,
                expected_revision=session.metadata.revision, sdk_storage=storage.values)
        fresh = await asyncio.to_thread(self.store.touch_session, cookie)
        if fresh.metadata.context_id != session.metadata.context_id:
            raise ContractViolation("Identity context changed", "auth_context_conflict")
        context = AuthContext(identity=fresh.metadata.identity, workspace=fresh.metadata.workspace,
            role=authority["role"], permissions=authority["permissions"], context_id=fresh.metadata.context_id,
            session_ref=fresh.metadata.session_ref, authority_verified_at=datetime.now(timezone.utc))
        request.state.auth_context, request.state.auth_session = context, fresh
        request.state.auth_user = {"user_id": context.identity.user_id, "name": info.name,
                                   "email": info.email, "email_verified": info.email_verified is True}
        request.state.auth_workspace_name = authority["name"]
        return context

    async def revalidate(self, request, context_id):
        context = await self.require_context(request)
        if context.context_id != context_id:
            raise ContractViolation("Identity context changed", "auth_context_conflict")
        return context

    def me(self, request):
        context, session = request.state.auth_context, request.state.auth_session
        return {"mode": "authenticated", "user": request.state.auth_user,
                "workspace": {**context.workspace.model_dump(), "key": context.workspace.key,
                              "name": request.state.auth_workspace_name},
                "role": context.role, "permissions": context.permissions, "context_id": context.context_id,
                "csrf_token": session.csrf_token, "expires_at": session.metadata.absolute_expires_at.isoformat()}


def mount_auth(app, runtime):
    router = APIRouter(prefix="/api/auth")

    @router.get("/login")
    async def login(request: Request):
        query = protocol_query(request, {"return_to", "screen_hint", "organization_id"})
        return_to = safe_return_path(query.get("return_to", "/en"))
        hint = query.get("screen_hint", "sign_in")
        if hint not in {"sign_in", "sign_up"}:
            raise ContractViolation("Invalid screen hint")
        organization = query.get("organization_id")
        if organization is not None and (len(organization) > 256 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in organization)):
            raise ContractViolation("Invalid organization hint")
        storage = PrivateStorage()
        client = PrivateLogtoClient(runtime.config, storage)
        url = await client.signIn(runtime.config.redirect_uri, interactionMode="signUp" if hint == "sign_up" else "signIn")
        sdk = json.loads(storage.get("signInSession"))
        binding, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await asyncio.to_thread(runtime.store.create_login_interaction, binding, state=sdk["state"], nonce=nonce,
            code_verifier=sdk["codeVerifier"], return_to=return_to, organization_hint=organization,
            sdk_storage=storage.values, previous_session_cookie=request.cookies.get(SESSION_COOKIE))
        parsed = urlsplit(url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parse_qsl(parsed.query) + [("nonce", nonce)]), ""))
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(BINDING_COOKIE, binding, httponly=True, secure=runtime.config.origin.startswith("https:"),
                            samesite="lax", path="/", max_age=600)
        return response

    @router.get("/callback")
    async def callback(request: Request):
        query = protocol_query(request, {"code", "state", "error", "error_description", "iss"})
        if not query.get("state") or bool(query.get("code")) == bool(query.get("error")) or query.get("iss", runtime.config.issuer) != runtime.config.issuer:
            raise ContractViolation("Authentication could not be verified", "unauthenticated")
        interaction = await asyncio.to_thread(runtime.store.consume_login_interaction, request.cookies.get(BINDING_COOKIE), query["state"])
        if "error" in query:
            raise ContractViolation("Authentication could not be verified", "unauthenticated")
        storage = PrivateStorage(interaction.sdk_storage)
        client = PrivateLogtoClient(runtime.config, storage, nonce=interaction.nonce)
        await client.handleSignInCallback(runtime.config.redirect_uri + "?" + urlencode(query))
        core = await client.getOidcCore()
        core.verifyIdToken(client.getIdToken(), runtime.config.app_id)
        info = await client.fetchUserInfo()
        if info.sub != core.claims["sub"]:
            raise ContractViolation("Authentication could not be verified", "unauthenticated")
        identity = await asyncio.to_thread(runtime.store.resolve_user, runtime.config.issuer, core.claims["sub"])
        workspace = WorkspaceContext(kind="organization", owner_id=interaction.organization_hint) if interaction.organization_hint else WorkspaceContext(kind="personal", owner_id=identity.user_id)
        authority = await runtime.authority.verify(identity, workspace)
        if workspace.kind == "organization":
            await asyncio.to_thread(runtime.store.remember_verified_organization, workspace.owner_id, authority["name"])
        handle = await asyncio.to_thread(runtime.store.complete_login, interaction, identity, workspace,
            sdk_storage=storage.values, absolute_seconds=runtime.config.absolute_seconds, idle_seconds=runtime.config.idle_seconds)
        response = RedirectResponse(interaction.return_to, status_code=303)
        runtime.set_session_cookie(response, handle)
        response.delete_cookie(BINDING_COOKIE, path="/")
        return response

    @router.get("/me")
    async def me(request: Request):
        await runtime.require_context(request)
        return runtime.me(request)

    @router.post("/workspace")
    async def workspace(body: WorkspaceContext, request: Request):
        context = await runtime.require_context(request, write=True)
        authority = await runtime.authority.verify(context.identity, body)
        if body.kind == "organization":
            await asyncio.to_thread(runtime.store.remember_verified_organization, body.owner_id, authority["name"])
        handle = await asyncio.to_thread(runtime.store.rotate_session, request.cookies.get(SESSION_COOKIE),
            expected_revision=request.state.auth_session.metadata.revision, workspace=body)
        request.state.auth_context = AuthContext(identity=context.identity, workspace=body,
            role=authority["role"], permissions=authority["permissions"], context_id=handle.session.metadata.context_id,
            session_ref=handle.session.metadata.session_ref, authority_verified_at=datetime.now(timezone.utc))
        request.state.auth_session, request.state.auth_workspace_name = handle.session, authority["name"]
        response = JSONResponse(runtime.me(request))
        runtime.set_session_cookie(response, handle)
        return response

    @router.post("/logout")
    async def logout(body: LogoutRequest, request: Request):
        session = await asyncio.to_thread(runtime.store.validate_context, request.cookies.get(SESSION_COOKIE),
            request.headers.get("x-csrf-token"), request.headers.get("x-auth-context"))
        await asyncio.to_thread(runtime.store.revoke_session, request.cookies.get(SESSION_COOKIE))
        logout_url = None
        try:
            client = PrivateLogtoClient(runtime.config, PrivateStorage(session.sdk_storage))
            candidate = await client.signOut(runtime.config.post_logout_redirect_uri)
            parsed = urlsplit(candidate)
            metadata = (await client.getOidcCore()).metadata
            if urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) != metadata.end_session_endpoint or parsed.fragment or dict(parse_qsl(parsed.query)) != {
                    "client_id": runtime.config.app_id, "post_logout_redirect_uri": runtime.config.post_logout_redirect_uri} or len(parse_qsl(parsed.query)) != 2:
                raise ValueError("Untrusted SDK sign-out URL")
            logout_url = candidate
        except Exception:
            pass  # Local revocation already committed; remote sign-out is unavailable.
        response = JSONResponse({"logout_url": logout_url})
        for name in (SESSION_COOKIE, BINDING_COOKIE):
            response.delete_cookie(name, path="/", httponly=True, secure=runtime.config.origin.startswith("https:"), samesite="lax")
        return response

    app.include_router(router)
