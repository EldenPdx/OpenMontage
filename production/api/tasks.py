"""Durable task submission; HTTP requests never start or wait for an agent."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse

from production.api.security import error_response, install_security
from production.contracts import CallIntent, CancelRequest, ContractViolation, TaskCommand, TaskCreate, TaskState
from production.pi_config import load_studio_config, public_profiles, snapshot_for

PI_CLI = Path(__file__).resolve().parents[2] / ".runtime/pi/source/packages/coding-agent/dist/bundle/cli.js"


def task_view(task):
    result = task.model_dump(mode="json")
    result["board_url"] = f"/p/{task.project_id}"
    if task.result:
        result["result"]["preview_url"] = f"/api/studio/tasks/{task.task_id}/result"
        result["result"]["download_url"] = f"/api/studio/tasks/{task.task_id}/result?download=1"
    return result


def idempotency_key(value):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        raise ContractViolation("A valid Idempotency-Key is required")
    return value


def task_identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
        raise ContractViolation("Invalid task identifier")
    return value


def mount_studio(app, *, repository=None, config=None, environment=None, projects_dir=None):
    if environment is None:
        from lib.env_loader import load_env
        load_env()
        environment = dict(os.environ)
    else:
        environment = dict(environment)
    config = config or load_studio_config(environment=environment)
    unavailable = "Studio execution is disabled; enable it in trusted backend configuration"
    if repository is None and config.enabled and environment.get("STUDIO_DATABASE_URL"):
        try:
            from production.repository import PostgresRepository
            repository = PostgresRepository(environment["STUDIO_DATABASE_URL"])
            repository.migrate()
        except (ImportError, ContractViolation):
            unavailable = "PostgreSQL is unavailable; install Studio dependencies and migrate the database"
    elif repository is None and config.enabled:
        unavailable = "PostgreSQL connection is not configured"
    app.state.studio_repository = repository
    app.state.studio_config = config
    app.state.studio_environment = environment
    app.state.studio_projects_dir = projects_dir
    issue_session = install_security(app)

    def backend():
        if not config.enabled or repository is None:
            raise ContractViolation(unavailable, "dependency_unavailable")
        return repository

    app.state.studio_backend = backend

    @app.exception_handler(ContractViolation)
    async def contract_error(request, exc):
        status = 422
        if exc.code == "not_found":
            status = 404
        elif exc.code == "forbidden":
            status = 403
        elif exc.code in {"profile_unavailable", "dependency_unavailable"}:
            status = 503
        elif exc.code in {"version_conflict", "idempotency_conflict", "state_conflict", "approval_conflict",
                          "fence_conflict", "outcome_unknown", "file_conflict", "cursor_expired"}:
            status = 409
        elif exc.code == "internal_error":
            status = 500
        return error_response(exc.code, str(exc)[:4000], status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        if request.url.path.startswith("/api/studio"):
            return error_response("invalid_input", "Request does not match the Studio contract", 422)
        return await request_validation_exception_handler(request, exc)

    router = APIRouter(prefix="/api/studio")

    @router.get("/config")
    async def studio_config(request: Request, response: Response):
        result = public_profiles(config, environment)
        result["csrf_token"] = issue_session(request, response)
        result["ready"] = False
        result["unavailable_reason"] = unavailable if not config.enabled or repository is None else None
        if config.enabled and repository is not None:
            try:
                await asyncio.to_thread(repository.list_tasks, limit=1)
                result["ready"] = PI_CLI.is_file() and shutil.which("node") is not None
                if not result["ready"]:
                    result["unavailable_reason"] = "Real pinned Pi/Node is unavailable; run make studio-pi"
            except ContractViolation:
                result["unavailable_reason"] = "PostgreSQL is unavailable"
        return result

    @router.post("/tasks", status_code=202)
    async def create_task(body: TaskCreate, key: str | None = Header(default=None, alias="Idempotency-Key")):
        repo = backend()
        if not PI_CLI.is_file() or shutil.which("node") is None:
            raise ContractViolation("Real pinned Pi/Node is unavailable; run make studio-pi", "dependency_unavailable")
        key = idempotency_key(key)
        profile = config.profiles.get(body.profile_id)
        if profile is None:
            raise ContractViolation("Selected profile is unavailable", "profile_unavailable")
        ready = public_profiles(config, environment)["profiles"]
        if not next(item["ready"] for item in ready if item["profile_id"] == body.profile_id):
            raise ContractViolation("Selected profile credentials are not configured", "profile_unavailable")
        from production.policy import media_configuration_sha256
        media_hash = media_configuration_sha256()
        snapshot = snapshot_for(profile, body, media_models=config.media_models,
                                media_configuration_sha256=media_hash)
        task = await asyncio.to_thread(repo.create_task, body, snapshot, key)
        return task_view(task)

    @router.get("/tasks")
    async def tasks(limit: int = Query(default=50, ge=1, le=100), after: str | None = None):
        if after is not None:
            task_identifier(after)
        return [task_view(task) for task in await asyncio.to_thread(backend().list_tasks, limit=limit, after=after)]

    @router.get("/tasks/{task_id}")
    async def task(task_id: str):
        repo = backend()
        task = await asyncio.to_thread(repo.get_task, task_identifier(task_id))
        result = task_view(task)
        pending = await asyncio.to_thread(repo.unresolved_intents, task_id)
        result["pending_calls"] = [{"call_id": call.call_id, "kind": call.kind, "provider": call.provider,
                                   "model": call.model, "status": call.status, "price_status": call.price_status,
                                   "reserved_usd_micros": call.reserved_usd_micros,
                                   "external_job_id": call.external_job_id} for call in pending if isinstance(call, CallIntent)]
        return result

    def verified_video(task):
        if task.state != TaskState.SUCCEEDED or task.result is None:
            raise ContractViolation("A verified render result is not available", "state_conflict")
        project = (Path(app.state.studio_projects_dir) / task.project_id).resolve()
        if not project.is_relative_to(Path(app.state.studio_projects_dir).resolve()):
            raise ContractViolation("Result escaped the managed project", "forbidden")
        for reference in (task.result.video, task.result.render_report):
            path = (project / reference.path).resolve()
            if not path.is_relative_to(project) or not path.is_file():
                raise ContractViolation("Verified result is missing", "file_conflict")
            digest = sha256()
            with path.open("rb") as content:
                for chunk in iter(lambda: content.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != reference.sha256:
                raise ContractViolation("Verified result bytes changed", "file_conflict")
        video = (project / task.result.video.path).resolve()
        if video.stat().st_size != task.result.bytes:
            raise ContractViolation("Verified result size changed", "file_conflict")
        return video

    @router.get("/tasks/{task_id}/result")
    async def result(task_id: str, download: bool = False):
        task = await asyncio.to_thread(backend().get_task, task_identifier(task_id))
        video = await asyncio.to_thread(verified_video, task)
        return FileResponse(video, filename=video.name if download else None, media_type="video/mp4")

    @router.post("/tasks/{task_id}/cancel", status_code=202)
    async def cancel(task_id: str, body: CancelRequest, key: str | None = Header(default=None, alias="Idempotency-Key")):
        repo = backend()
        task = await asyncio.to_thread(repo.get_task, task_identifier(task_id))
        command = TaskCommand(command_id="cmd-" + uuid4().hex, task_id=task.task_id, run_id=task.run_id,
                              kind="cancel", expected_version=body.expected_version,
                              idempotency_key=idempotency_key(key), payload={"reason": body.reason})
        await asyncio.to_thread(repo.enqueue_command, command)
        return task_view(await asyncio.to_thread(repo.get_task, task.task_id))

    app.include_router(router)
    from production.api.events import events_router
    app.include_router(events_router(backend))
    from production.api.approvals import approvals_router
    app.include_router(approvals_router(app, backend))
