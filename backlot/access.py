"""Backlot uses the verified BFF context and the shared scoped ownership store."""

import asyncio
from pathlib import Path
import re
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from production.api.security import require_permission, revalidate_context
from production.contracts import ContractViolation
_PRIVATE_NAMES = {"auth-runtime", "agents", "sessions",
                  "private", "credentials", "secrets", "auth.json", "models.json", "settings.json",
                  "credentials.json", "secrets.json", "config.json", "config.yaml", "config.yml",
                  "private-auth.json", "auth-config.json", "id_rsa", "id_ed25519"}


def private_path(path):
    return any(part.casefold() in _PRIVATE_NAMES or part.startswith(".") and part not in {".", ".."}
               for part in Path(path).parts)


def media_target(project_dir, file_path):
    if private_path(file_path):
        raise HTTPException(status_code=404, detail="media not found")
    target = (project_dir / file_path).resolve()
    try:
        relative = target.relative_to(project_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes project") from None
    if private_path(relative) or not target.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    return target


def file_headers(target):
    headers = {"X-Content-Type-Options": "nosniff"}
    if target.suffix.lower() in {".html", ".htm", ".svg", ".xhtml"}:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
    return headers


async def refresh_access(request, context_id, project_id=None):
    return await asyncio.wait_for(_refresh_access(request, context_id, project_id), timeout=10)


async def _refresh_access(request, context_id, project_id):
    if not hasattr(request.state, "backlot_auth_lock"):
        request.state.backlot_auth_lock = asyncio.Lock()
    async with request.state.backlot_auth_lock:
        context = await revalidate_context(request, context_id)
        if context is not None and project_id is not None:
            await asyncio.to_thread(request.app.state.auth_runtime.store.repository.project_ownership,
                                    project_id, context.workspace)
        require_permission(context, "resource:read")
        return context


def watch_stream(request, context_id, project_id):
    # The policy runs in the outer HTTP task, so cancellation also stops a blocked transport send.
    response_task = asyncio.current_task()

    async def watch():
        try:
            while not response_task.done():
                await asyncio.sleep(15)
                await refresh_access(request, context_id, project_id)
        except Exception:
            response_task.cancel()

    watcher = asyncio.create_task(watch())
    response_task.add_done_callback(lambda _: watcher.cancel())


class ProjectFileResponse(FileResponse):
    def __init__(self, path, *, request, project_id, **kwargs):
        super().__init__(path, **kwargs)
        self.request, self.project_id = request, project_id

    async def __call__(self, scope, receive, send):
        context = getattr(self.request.state, "auth_context", None)
        if context is None:
            return await super().__call__(scope, receive, send)
        await refresh_access(self.request, context.context_id, self.project_id)
        # Keep chunked ASGI sends observable; zero-copy pathsend cannot be revoked.
        scope = {**scope, "extensions": {name: value for name, value in scope.get("extensions", {}).items()
                                        if name != "http.response.pathsend"}}
        return await super().__call__(scope, receive, send)


async def visible_project(request, context, project_id):
    if context is None:
        return True
    try:
        await asyncio.to_thread(request.app.state.auth_runtime.store.repository.project_ownership,
                                project_id, context.workspace)
    except ContractViolation as error:
        if error.code == "not_found":
            return False
        raise
    return True


def page_login_redirect(request):
    if request.method != "GET":
        return None
    match = re.fullmatch(r"/(?:((?:zh|ja|en))/)?p/(.+)", request.url.path.rstrip("/"))
    if match is None:
        return None
    from production.auth.oidc import safe_return_path
    locale = match[1] or "zh"
    try:
        target = safe_return_path(f"/{locale}/p/{match[2]}")
    except ContractViolation:
        target = f"/{locale}"
    return RedirectResponse("/api/auth/login?" + urlencode({"return_to": target}), status_code=302)


def install_access(app):
    async def policy(request, context):
        path = request.url.path.rstrip("/")
        repository = app.state.auth_runtime.store.repository
        match = (re.fullmatch(r"/api/project/([^/]+)/(?:state|events)", path)
                 or re.fullmatch(r"/(?:media|thumb)/([^/]+)/.+", path)
                 or re.fullmatch(r"/(?:zh/|ja/|en/)?p/(.+)", path))
        if match is not None:
            await asyncio.to_thread(repository.project_ownership, match[1], context.workspace)
        elif path not in {"/api/projects", "/api/library/events"}:
            raise ContractViolation("Not found", "not_found")
        require_permission(context, "resource:read")
        if path == "/api/projects":
            request.state.project_ids = await asyncio.to_thread(repository.project_ids_for_workspace, context.workspace)
        if path.endswith("/events") or path.startswith(("/media/", "/thumb/")):
            watch_stream(request, context.context_id, match[1] if match is not None else None)

    app.state.auth_policies["projects"] = policy
    app.state.auth_page_redirect = page_login_redirect
