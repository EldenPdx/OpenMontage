"""Local single-user browser boundary, including DNS-rebinding and CSRF checks."""

from hashlib import sha256
import hmac
from pathlib import Path
import re
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from production.contracts import ErrorDTO


def error_response(code, message, status=403):
    return JSONResponse({"error": ErrorDTO(code=code, message=message).model_dump(mode="json")}, status_code=status)


def install_security(app):
    signing_key = secrets.token_bytes(32)

    def issue(request: Request, response):
        if getattr(app.state, "auth_mode", "local") != "local":
            return request.state.auth_session.csrf_token
        value = request.cookies.get("studio_session", "")
        if not valid(value):
            nonce = secrets.token_hex(24)
            value = nonce + "." + hmac.new(signing_key, nonce.encode(), sha256).hexdigest()
        response.set_cookie("studio_session", value, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/api/studio", max_age=86400)
        return value

    def valid(value):
        if not re.fullmatch(r"[a-f0-9]{48}\.[a-f0-9]{64}", value):
            return False
        nonce, separator, signature = value.partition(".")
        return bool(separator and len(nonce) == 48 and hmac.compare_digest(
            signature, hmac.new(signing_key, nonce.encode(), sha256).hexdigest()))

    @app.middleware("http")
    async def studio_boundary(request, call_next):
        if getattr(app.state, "auth_mode", "local") != "local":
            return await call_next(request)
        if not request.url.path.startswith("/api/studio"):
            return await call_next(request)
        host = request.headers.get("host", "")
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host, re.IGNORECASE):
            return error_response("forbidden", "Studio accepts trusted loopback hosts only")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin", "").lower() != f"{request.url.scheme}://{host}".lower():
                return error_response("forbidden", "Studio writes require the same browser origin")
            cookie = request.cookies.get("studio_session", "")
            token = request.headers.get("x-csrf-token", "")
            if not valid(cookie) or not token.isascii() or not hmac.compare_digest(cookie, token):
                return error_response("csrf_invalid", "Refresh Studio to obtain a valid browser session")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return issue


async def require_context(request, *, write=False):
    """Validate identity, never resource capability before scoped ownership."""
    if request.app.state.auth_mode == "local":
        return None
    runtime = request.app.state.auth_runtime
    if runtime is None or request.app.state.auth_unavailable:
        from production.contracts import ContractViolation
        raise ContractViolation("Identity service is unavailable", "identity_unavailable")
    return await runtime.require_context(request, write=write)


def require_permission(context, permission):
    from production.contracts import ContractViolation
    if context is not None and permission not in context.permissions:
        raise ContractViolation("Permission is required", "forbidden")


async def revalidate_context(request, context_id):
    """Streams call before each sensitive frame and every idle 15-second heartbeat."""
    if request.app.state.auth_mode == "local":
        return None
    return await request.app.state.auth_runtime.revalidate(request, context_id)


def local_root_protected(root):
    from production.ownership_guard import has_guard
    try:
        return has_guard(root) or root.is_dir() and any(has_guard(entry) for entry in root.iterdir() if entry.is_dir())
    except (OSError, RuntimeError):
        return True


def install_boundary(app, *, config, projects_dir):
    import asyncio
    import ipaddress
    import logging
    from urllib.parse import urlsplit
    from production.contracts import ContractViolation
    from fastapi.exceptions import RequestValidationError
    local_contract_error = app.exception_handlers.get(ContractViolation)
    local_validation_error = app.exception_handlers.get(RequestValidationError)

    class RedactAccessQuery(logging.Filter):
        def filter(self, record):
            if isinstance(record.args, tuple) and len(record.args) == 5 and isinstance(record.args[2], str):
                record.args = (*record.args[:2], record.args[2].split("?", 1)[0], *record.args[3:])
            return True

    logging.getLogger("uvicorn.access").addFilter(RedactAccessQuery())

    statuses = {"unauthenticated": 401, "forbidden": 403, "csrf_invalid": 403, "not_found": 404,
                "auth_context_conflict": 409, "state_conflict": 409, "invitation_invalid": 409, "last_admin": 409,
                "identity_unavailable": 503,
                "dependency_unavailable": 503}

    async def failure(request, error):
        if error.code not in statuses and local_contract_error is not None:
            return await local_contract_error(request, error)
        return error_response(error.code, str(error), statuses.get(error.code, 422))

    async def local_access():
        if local_root_protected(projects_dir) or config is None or any((config.issuer, config.app_id, config.app_secret, config.encryption_keys, config.management_app_secret)):
            raise ContractViolation("Protected data cannot be served anonymously", "dependency_unavailable")
        provenance = config.root_provenance
        if provenance is None:
            if config.database_url:
                raise ContractViolation("Root classification is unavailable", "dependency_unavailable")
            return
        if str(Path(provenance.canonical_root).resolve()) != str(projects_dir.resolve()):
            raise ContractViolation("Root classification is unavailable", "dependency_unavailable")
        if provenance.classification == "file_only_legacy" and not config.database_url:
            return
        if provenance.classification != "adopted_or_restored" or not config.database_url or not provenance.expected_binding:
            raise ContractViolation("Root classification is unavailable", "dependency_unavailable")
        try:
            from production.repository import PostgresRepository
            repository = PostgresRepository(config.database_url, schema=config.database_schema)
            await asyncio.to_thread(repository.assert_legacy_root, expected_binding=provenance.expected_binding)
        except Exception:
            raise ContractViolation("Root classification is unavailable", "dependency_unavailable") from None

    @app.exception_handler(ContractViolation)
    async def auth_error(request, error):
        if app.state.auth_mode == "local" and local_contract_error is not None:
            return await local_contract_error(request, error)
        return await failure(request, error)

    @app.exception_handler(RequestValidationError)
    async def auth_validation_error(request, error):
        if app.state.auth_mode == "local" and local_validation_error is not None:
            return await local_validation_error(request, error)
        return error_response("invalid_input", "Request does not match the identity contract", 422)

    @app.middleware("http")
    async def boundary(request, call_next):
        path = request.url.path
        invitation = path.rstrip("/") in {"/ui/invitation.html", "/zh/invitations/accept", "/ja/invitations/accept", "/en/invitations/accept"}
        public = invitation or path == "/api/health" or path == "/ui" or path.startswith("/ui/") or path.rstrip("/") in {
            "", "/studio", "/zh", "/ja", "/en", "/zh/studio", "/ja/studio", "/en/studio"}
        try:
            host = request.headers.get("host", "")
            peer = request.client.host if request.client else ""
            forwarded = any(name.lower() == "forwarded" or name.lower().startswith("x-forwarded-") for name in request.headers)
            if app.state.auth_mode == "local":
                try:
                    trusted = ipaddress.ip_address(peer).is_loopback and bool(re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host, re.IGNORECASE))
                except ValueError:
                    trusted = False
                if not trusted or forwarded:
                    raise ContractViolation("Trusted loopback access is required", "forbidden")
                if not public:
                    await local_access()
            else:
                if path.rstrip("/") == "/openapi.json":
                    return error_response("not_found", "Not found", 404)
                if config is not None:
                    expected = urlsplit(config.origin)
                    if host.lower() != expected.netloc.lower() or (not forwarded and request.url.scheme != expected.scheme) or forwarded and (peer not in config.trusted_proxy_ips or
                        request.headers.get("x-forwarded-proto") != expected.scheme or request.headers.get("x-forwarded-host", host).lower() != expected.netloc.lower() or "forwarded" in request.headers):
                        raise ContractViolation("Trusted browser origin is required", "csrf_invalid")
                    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") != config.origin:
                        raise ContractViolation("Same browser origin is required", "csrf_invalid")
                if not public:
                    if app.state.auth_unavailable or app.state.auth_runtime is None:
                        raise ContractViolation("Identity service is unavailable", "identity_unavailable")
                    if not path.startswith("/api/auth/"):
                        context = await require_context(request, write=request.method not in {"GET", "HEAD", "OPTIONS"})
                        module = "studio" if path.startswith("/api/studio") else "organizations" if path.startswith(("/api/organizations", "/api/invitations")) else "projects"
                        policy = app.state.auth_policies.get(module)
                        if policy is None:
                            raise ContractViolation("Resource authorization is unavailable", "dependency_unavailable")
                        await policy(request, context)
            response = await call_next(request)
        except ContractViolation as error:
            response = await failure(request, error)
            if error.code == "unauthenticated":
                redirect = getattr(app.state, "auth_page_redirect", None)
                if redirect is not None:
                    response = redirect(request) or response
        except Exception:
            if app.state.auth_mode == "local":
                raise
            response = error_response("identity_unavailable", "Identity service is unavailable", 503)
        if path.rstrip("/") == "/api/auth/callback" and response.status_code >= 400 and "text/html" in request.headers.get("accept", "").lower():
            from fastapi.responses import HTMLResponse
            if hasattr(response, "body_iterator"):
                async for _ in response.body_iterator:
                    pass
            nonce = secrets.token_urlsafe(24)
            response = HTMLResponse('<!doctype html><meta charset="utf-8"><title>Sign in could not complete</title>'
                '<p>Sign in could not complete.</p><a href="/api/auth/login">Sign in again</a>'
                f'<script nonce="{nonce}">history.replaceState(null,"","/en");</script>', status_code=response.status_code,
                headers={"Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'"})
        if app.state.auth_mode != "local" and not public:
            response.headers.update({"Cache-Control": "private, no-store", "Vary": "Cookie", "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "no-referrer"})
            context = getattr(request.state, "auth_context", None)
            if context is not None:
                response.headers["X-Auth-Context"] = context.context_id
        elif app.state.auth_mode == "local" and not public and hasattr(response, "body_iterator"):
            original = response.body_iterator

            async def guarded_stream():
                async for chunk in original:
                    try:
                        await local_access()
                    except ContractViolation:
                        return
                    yield chunk
            response.body_iterator = guarded_stream()
        if public and path != "/api/health":
            response.headers["Cache-Control"] = "private, no-store" if invitation else "no-cache"
            if invitation:
                response.headers.update({"Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Vary": "Cookie"})
        return response
