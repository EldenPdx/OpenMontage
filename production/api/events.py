"""SSE delivery from committed PostgreSQL events; browser disconnects change no task."""

import asyncio
import json
import time

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from production.api.tasks import task_identifier
from production.contracts import ContractViolation, TERMINAL_STATES


def event_record(event):
    content = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {content}\n\n"


def events_router(backend):
    router = APIRouter(prefix="/api/studio")

    @router.get("/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0,
                     last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
        task_identifier(task_id)
        if last_event_id is not None:
            if not last_event_id.isascii() or not last_event_id.isdigit() or len(last_event_id) > 18:
                raise ContractViolation("Invalid event cursor")
            last = int(last_event_id)
            # Native EventSource reconnects keep the bootstrap query unchanged.
            after = last
        if after < 0 or after > 2**63 - 1:
            raise ContractViolation("Invalid event cursor")
        repo = backend()
        await asyncio.to_thread(repo.get_task, task_id)
        initial = await asyncio.to_thread(repo.events, task_id, after=after, limit=100)

        async def stream():
            cursor = after
            batch = initial
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                for event in batch:
                    yield event_record(event)
                    cursor = event.event_id
                if len(batch) < 100:
                    task = await asyncio.to_thread(repo.get_task, task_id)
                    if task.state in TERMINAL_STATES:
                        # Check once more after reading state to avoid losing its commit event.
                        final = await asyncio.to_thread(repo.events, task_id, after=cursor, limit=100)
                        if not final:
                            return
                        batch = final
                        continue
                    if time.monotonic() - heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.25)
                try:
                    batch = await asyncio.to_thread(repo.events, task_id, after=cursor, limit=100)
                except ContractViolation:
                    yield 'event: resync\ndata: {"reason":"Event storage changed; refresh the task"}\n\n'
                    return

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Content-Encoding": "identity",
        })

    return router
