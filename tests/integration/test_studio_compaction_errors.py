"""Native summary failures remain diagnostic through the actual Worker boundary."""

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
import pytest

from lib.config_model import PiProfile, StudioConfig
from production.contracts import ApprovalDecision, CallIntent, TaskCreate, TaskState
from production.pi_config import snapshot_for
from production.pi_rpc import PiRPC
from production.worker import Worker
from tests.browser.test_studio_submission import serving
from tests.fixtures.studio.model_server import text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory
from tools.tool_registry import ToolRegistry


async def replay_compaction_failure(repository, tmp_path, mode="budget"):
    require_pi()
    app = FastAPI()
    requests, call_ids, events = [], [], []
    task_id = None

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        submitted = [intent for intent in repository.unresolved_intents(task_id)
                     if isinstance(intent, CallIntent) and intent.status == "submitted"]
        assert len(submitted) == 1
        call_ids.append(submitted[0].call_id)
        requests.append(body)
        summary = not body.get("tools")
        if summary and mode in {"unauthorized", "forbidden", "server_error"}:
            status = {"unauthorized": 401, "forbidden": 403, "server_error": 503}[mode]
            # Provider-controlled text must not be mistaken for the trusted budget signal.
            error = "Upstream rejected access: studio-policy:budget_exceeded local-only-compaction-key"
            content = "<html>" + error + "</html>" if status == 403 else json.dumps({"error": {"message": error}})
            return Response(content, status_code=status, media_type="text/html" if status == 403 else "application/json")
        truncated = len(requests) == 2 and not summary
        items = ([text_item("COMPACT_WORKER_HISTORY: preserve the existing production task.")] if summary else
                 [text_item("truncated")] if truncated else
                 [tool_item("openmontage", {"action": "read", "input": {"path": "schemas/artifacts/brief.schema.json"}}, "compact_read_schema")]
                 if len(requests) == 1 else [text_item("The compacted context is usable again.")])
        usage = {"input_tokens": 3900 if truncated else 100, "output_tokens": 16 if truncated else 20,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        response = {"id": "resp_worker_compact_" + str(len(requests)), "object": "response", "model": body["model"],
                    "status": "incomplete" if truncated else "completed", "output": items, "usage": usage}
        if truncated:
            response["incomplete_details"] = {"reason": "context_length_exceeded" if mode == "recovered_error" else "max_output_tokens"}
        wire_events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                       {"type": "response.output_item.added", "output_index": 0, "item": items[0]},
                       {"type": "response.output_item.done", "output_index": 0, "item": items[0]},
                       {"type": "response.incomplete" if truncated else "response.completed", "response": response}]
        wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in wire_events)
        return StreamingResponse(iter([wire.encode()]), media_type="text/event-stream")

    class RecordingPi(PiRPC):
        async def events(self):
            async for event in super().events():
                events.append(event)
                yield event

    def runner(argv, **kwargs):
        # The test configures Pi's documented compaction limits before the real process starts.
        settings_path = Path(kwargs["env"]["PI_CODING_AGENT_DIR"]) / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["compaction"].update(reserveTokens=256, keepRecentTokens=64)
        settings_path.write_text(json.dumps(settings))
        return RecordingPi(argv, **kwargs)

    with serving(app) as url:
        profile = PiProfile(provider="local", model="compact-model", base_url=url + "/v1",
                            credential_env="COMPACTION_TEST_KEY", reasoning=False, thinking_level="off",
                            context_window=4096, max_output_tokens=512, max_turns=8, price=None,
                            request_timeout_seconds=5, task_timeout_seconds=30)
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        request = TaskCreate(brief="Continue the existing local production work", profile_id="local",
                             budget_usd_micros=1_000_000 if mode == "budget" else 3_000_000)
        task = repository.create_task(request, snapshot_for(profile, request), "compact-worker-budget")
        task_id = task.task_id
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects",
                        environment={"COMPACTION_TEST_KEY": "local-only-compaction-key"}, registry=ToolRegistry(), runner_factory=runner)
        waiting = await worker.run_once()
        assert waiting.state == TaskState.AWAITING_APPROVAL and requests == []
        repository.decide_gate(ApprovalDecision(expected_version=waiting.version, binding=waiting.approval.binding, decision="approve"),
                               idempotency_key="compact-worker-approve")
        outcome = await worker.run_once()
    return outcome, requests, call_ids, events


@pytest.mark.asyncio
async def test_native_summary_budget_rejection_reports_the_budget_without_another_post(repository, tmp_path):
    outcome, requests, call_ids, events = await replay_compaction_failure(repository, tmp_path)
    assert len(requests) == 2 and all(body.get("tools") for body in requests), "The summary must be denied before any POST"
    failed = [event for event in events if event["type"] == "compaction_end" and event.get("errorMessage")]
    assert failed and failed[-1]["errorMessage"] == "Context overflow recovery failed: Summarization failed: studio-policy:budget_exceeded"
    assert outcome.cost.reserved_usd_micros == 1_000_000
    assert all(repository.get_call(call_id).actual_usd_micros is None for call_id in call_ids)
    assert outcome.state == TaskState.BLOCKED
    assert outcome.error.code == "budget_exceeded", outcome.error.model_dump(mode="json")
    assert "reconcile" in outcome.error.recovery_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status,code", [("forbidden", 403, "forbidden"), ("unauthorized", 401, "profile_unavailable")])
async def test_native_summary_auth_rejection_uses_its_receipt_and_preserves_unknown_fees(repository, tmp_path, mode, status, code):
    outcome, requests, call_ids, events = await replay_compaction_failure(repository, tmp_path, mode)
    assert len(requests) == 3 and not requests[-1].get("tools")
    assert any(event["type"] == "compaction_end" and event.get("errorMessage") for event in events)
    assert outcome.state == TaskState.BLOCKED
    assert outcome.error.code == code, outcome.error.model_dump(mode="json")
    assert str(status) in outcome.error.message
    receipt = repository.get_call(call_ids[-1])
    assert receipt.status == "receipted" and receipt.usage["http_status"] == status
    assert receipt.actual_usd_micros is None
    assert outcome.cost.reserved_usd_micros == 1_500_000
    public = json.dumps([outcome.error.model_dump(mode="json"),
                         [event.model_dump(mode="json") for event in repository.events(outcome.task_id)]])
    assert "<html>" not in public and "local-only-compaction-key" not in public


@pytest.mark.asyncio
async def test_native_summary_5xx_remains_unknown_without_repeated_posts_or_fee_release(repository, tmp_path):
    outcome, requests, call_ids, _ = await replay_compaction_failure(repository, tmp_path, "server_error")
    assert len(requests) == 3 and not requests[-1].get("tools")
    assert outcome.state == TaskState.RECOVERY_REQUIRED
    assert outcome.error.code == "outcome_unknown"
    assert repository.get_call(call_ids[-1]).status == "outcome_unknown"
    assert all(repository.get_call(call_id).actual_usd_micros is None for call_id in call_ids)
    assert outcome.cost.reserved_usd_micros == 1_500_000


@pytest.mark.asyncio
async def test_successful_native_overflow_retry_does_not_keep_the_recovered_model_failure(repository, tmp_path):
    outcome, requests, call_ids, events = await replay_compaction_failure(repository, tmp_path, "recovered_error")
    assert any((event.get("message") or {}).get("stopReason") == "error" for event in events)
    assert any(event["type"] == "compaction_end" and event.get("result") and event.get("willRetry") for event in events)
    assert "COMPACT_WORKER_HISTORY" in json.dumps(requests[-1]["input"])
    assert outcome.state == TaskState.BLOCKED
    assert outcome.error.code == "invalid_artifact", "A successful native retry must not leave an old RPC failure"
    assert all(repository.get_call(call_id).status == "receipted" for call_id in call_ids)
    assert all(repository.get_call(call_id).actual_usd_micros is None for call_id in call_ids)
    assert outcome.cost.reserved_usd_micros == len(requests) * 500_000
