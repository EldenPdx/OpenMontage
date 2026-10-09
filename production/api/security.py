"""Local single-user browser boundary, including DNS-rebinding and CSRF checks."""

from hashlib import sha256
import hmac
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
                return error_response("forbidden", "Refresh Studio to obtain a valid browser session")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return issue
