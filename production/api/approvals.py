"""Approval writes persist evidence/commands; the worker applies the checkpoint."""

import asyncio

from fastapi import APIRouter, Header

from production.approvals import ApprovalService
from production.api.tasks import idempotency_key, task_identifier, task_view
from production.contracts import ApprovalDecision, ResumeRequest


def approvals_router(app, backend):
    router = APIRouter(prefix="/api/studio")

    @router.post("/tasks/{task_id}/approvals/{gate_id}/decision", status_code=202)
    async def decide(task_id: str, gate_id: str, body: ApprovalDecision,
                     key: str | None = Header(default=None, alias="Idempotency-Key")):
        service = ApprovalService(backend(), app.state.studio_projects_dir, app.state.studio_config)
        result = await asyncio.to_thread(service.decide, task_identifier(task_id), task_identifier(gate_id),
                                         body, idempotency_key(key))
        return task_view(result)

    @router.post("/tasks/{task_id}/resume", status_code=202)
    async def resume(task_id: str, body: ResumeRequest,
                     key: str | None = Header(default=None, alias="Idempotency-Key")):
        service = ApprovalService(backend(), app.state.studio_projects_dir, app.state.studio_config)
        result = await asyncio.to_thread(service.resume, task_identifier(task_id), body, idempotency_key(key))
        return task_view(result)

    return router
