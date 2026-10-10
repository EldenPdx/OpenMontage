"""Local replay of the real HTML 403 failure, through official Pi and PostgreSQL."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

import pytest

from lib.config_model import PiProfile, StudioConfig
from production.approvals import ApprovalService
from production.contracts import ApprovalDecision, CallIntent, ResumeRequest, TaskCreate, TaskState
from production.pi_config import snapshot_for
from production.worker import Worker
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory
from tools.tool_registry import ToolRegistry


@contextmanager
def rejecting_provider(repository, task_id, *, mode):
    calls = []
    state = {"mode": mode}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            submitted = [intent for intent in repository.unresolved_intents(task_id())
                         if isinstance(intent, CallIntent) and intent.status == "submitted"]
            assert len(submitted) == 1
            calls.append(submitted[0].call_id)
            current = state["mode"]
            if current == "disconnect":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            statuses = {"forbidden": 403, "unauthorized": 401, "timeout": 408, "server_error": 503, "malformed": 200, "success": 200}
            status = statuses[current]
            if current == "success":
                response = {"id": "recovered-response", "object": "response", "status": "completed", "model": "gpt-5.6-sol",
                            "output": [{"id": "recovered-message", "type": "message", "role": "assistant", "status": "completed",
                                        "content": [{"type": "output_text", "text": "The manual network repair succeeded.", "annotations": []}]}],
                            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
                events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.added", "output_index": 0, "item": response["output"][0]},
                          {"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
                          {"type": "response.completed", "response": response}]
                payload = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
            elif current == "malformed":
                payload = b"event: response.completed\ndata: {not valid JSON}\n\n"
            else:
                payload = (b"<html><body><h1>403 Forbidden</h1></body></html>" if status == 403
                           else b'{"error":{"code":"denied","message":"Controlled provider rejected request"}}')
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if current in {"success", "malformed"} else "text/html" if status == 403 else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def replay_rejection(repository, tmp_path, mode, *, resume=False):
    require_pi()
    task_id = None
    with rejecting_provider(repository, lambda: task_id, mode=mode) as (base_url, calls, state):
        profile = PiProfile(provider="xvan", base_url=base_url, model="gpt-5.6-sol",
                            credential_env="PROVIDER_TEST_KEY", reasoning=True, thinking_level="medium",
                            max_output_tokens=512, max_turns=2, request_timeout_seconds=5, price=None)
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        request = TaskCreate(brief="Read the production guide for this test", profile_id="local")
        task = repository.create_task(request, snapshot_for(profile, request), "provider-rejection-create")
        task_id = task.task_id
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects",
                        environment={"PROVIDER_TEST_KEY": "local-rejection-fixture-key"}, registry=ToolRegistry())
        waiting = await worker.run_once()
        assert waiting.state == TaskState.AWAITING_APPROVAL
        assert waiting.approval.stage == "model_cost"
        assert calls == []
        repository.decide_gate(ApprovalDecision(expected_version=waiting.version,
                                               binding=waiting.approval.binding, decision="approve"),
                               idempotency_key="provider-rejection-approve")
        outcome = await worker.run_once()
        assert len(calls) == 1, "A rejection or unknown response must not automatically repeat a POST"
        intent = repository.get_call(calls[0])
        assert intent.actual_usd_micros is None
        assert outcome.cost.reserved_usd_micros == 500_000
        assert outcome.cost.price_status == "unquoted"
        if not resume:
            return outcome, intent
        assert outcome.state == TaskState.BLOCKED
        session = tmp_path / "runtime" / "sessions" / task.task_id / task.run_id / "session.jsonl"
        before = json.loads(session.read_text(encoding="utf-8").splitlines()[0])["id"]
        state["mode"] = "success"
        ApprovalService(repository, tmp_path / "projects", config).resume(
            task.task_id, ResumeRequest(expected_version=outcome.version, comment="The local provider fixture is now repaired"), "provider-repaired-resume")
        recovered = await worker.run_once()
        assert len(calls) == 2
        assert json.loads(session.read_text(encoding="utf-8").splitlines()[0])["id"] == before
        assert recovered.state == TaskState.BLOCKED
        assert recovered.error.code == "invalid_artifact"
        assert "403" not in recovered.error.message
        assert repository.get_call(calls[0]).usage.get("http_status") == 403
        current = repository.get_call(calls[1])
        assert current.status == "receipted"
        assert current.usage["input"] == 10 and current.usage["output"] == 5
        assert current.actual_usd_micros is None
        assert recovered.cost.reserved_usd_micros == 1_000_000
        return recovered, current


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, status", [("forbidden", 403), ("unauthorized", 401)])
async def test_explicit_auth_rejection_is_diagnostic_and_recoverable_with_unknown_fee_hold(repository, tmp_path, mode, status):
    outcome, intent = await replay_rejection(repository, tmp_path, mode)
    assert outcome.state in {TaskState.FAILED, TaskState.BLOCKED}, outcome.model_dump(mode="json")
    assert str(status) in outcome.error.message
    assert intent.usage.get("http_status") == status
    assert "resume" in outcome.error.recovery_actions or "configure" in outcome.error.recovery_actions
    assert intent.status not in {"submitted", "outcome_unknown"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disconnect", "server_error", "timeout", "malformed"])
async def test_connection_loss_and_5xx_keep_unknown_submission_and_never_auto_retry(repository, tmp_path, mode):
    outcome, intent = await replay_rejection(repository, tmp_path, mode)
    assert outcome.state == TaskState.RECOVERY_REQUIRED
    assert intent.status == "outcome_unknown"
    assert outcome.error.code == "outcome_unknown"


@pytest.mark.asyncio
async def test_manual_same_session_resume_after_403_is_not_poisoned_by_the_old_rejection(repository, tmp_path):
    await replay_rejection(repository, tmp_path, "forbidden", resume=True)
