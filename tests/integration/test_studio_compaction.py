"""Native Pi compaction uses the same run-bound model receipt and budget guard."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import pytest

from lib.config_model import PiPrice, PiProfile
from production.contracts import ApprovalDecision, ContractViolation, TaskCreate, TaskState
from production.pi_config import prepare_pi, snapshot_for
from production.pi_rpc import PiRPC
from production.tool_bridge import BridgeServer, ProductionToolBridge
from tests.browser.test_studio_submission import serving
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.integration.test_studio_policy import intent, running
from tests.integration.test_studio_repository import repository, repository_factory
from tools.tool_registry import ToolRegistry


async def settled(client):
    events = []
    async for event in client.events():
        events.append(event)
        if event["type"] == "agent_settled":
            return events
    raise AssertionError("Real Pi exited before settling")


@pytest.mark.asyncio
async def test_native_guarded_compaction_recovers_length_and_accounts_for_every_summary_post(repository, tmp_path):
    require_pi()
    app = FastAPI()
    requests, call_ids, usages, affinity = [], [], [], []
    task_id = None

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        submitted = [intent for intent in repository.unresolved_intents(task_id)
                     if intent.kind == "model" and intent.status == "submitted"]
        assert len(submitted) == 1, "Every actual POST, including compaction, must already have a PG reservation"
        call_ids.append(submitted[0].call_id)
        requests.append(body)
        affinity.append({name: request.headers.get(name) for name in ("session_id", "x-client-request-id")})
        summary = not body.get("tools")
        truncated = len(requests) == 2 and not summary
        text = ("BLUE_CITY_COMPACT_SUMMARY: keep the existing city project and fixed model choices." if summary else
                "Historical city context. " * 100 if len(requests) == 1 else
                "truncated" if truncated else "Recovered BLUE_CITY with a short final reply.")
        usage = {"input_tokens": 3900 if truncated else 100, "output_tokens": 16 if truncated else 20,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        usages.append(usage)
        items = [text_item(text)]
        response = {"id": "resp_compact_" + str(len(requests)), "object": "response", "model": body["model"],
                    "status": "incomplete" if truncated else "completed", "output": items, "usage": usage}
        if truncated:
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                  {"type": "response.output_item.added", "output_index": 0, "item": items[0]},
                  {"type": "response.output_item.done", "output_index": 0, "item": items[0]},
                  {"type": "response.incomplete" if truncated else "response.completed", "response": response}]
        wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return StreamingResponse(iter([wire.encode()]), media_type="text/event-stream")

    with serving(app) as endpoint:
        profile = PiProfile(provider="local", model="compact-model", base_url=endpoint + "/v1",
                            credential_env="COMPACTION_TEST_KEY", reasoning=False, thinking_level="off",
                            context_window=4096, max_output_tokens=512, max_turns=8,
                            price=PiPrice(input=1_000_000, output=2_000_000, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        task_id = context.task_id
        with BridgeServer(bridge, context, profile) as server:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"COMPACTION_TEST_KEY": "local-only-key"},
                                 trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
            settings_path = managed.agent_dir / "settings.json"
            settings = json.loads(settings_path.read_text())
            settings["compaction"].update(reserveTokens=256, keepRecentTokens=64)
            settings_path.write_text(json.dumps(settings))
            client = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment},
                           session_root=managed.session_root, redact_values=(*managed.redact_values, server.token))
            session = await client.start(context)
            try:
                assert (await client.inspect())["autoCompactionEnabled"] is True
                await client.prompt("Remember BLUE_CITY and its existing approved choices.", command_id="compact-history")
                await asyncio.wait_for(settled(client), 15)
                assert len(requests) == 1
                await client.prompt("Continue the existing BLUE_CITY task.", command_id="compact-length")
                events = await asyncio.wait_for(settled(client), 15)
                assert any(event["type"] == "compaction_end" and not event.get("aborted") and event.get("result") for event in events)
                assert "BLUE_CITY_COMPACT_SUMMARY" in json.dumps(requests[-1]["input"])
                messages = (await client.request("get_messages"))["messages"]
                assert messages[-1]["stopReason"] == "stop"
                assert (await client.inspect())["sessionId"] == session.session_id
            finally:
                await client.close()
    assert len(requests) >= 4 and len(set(call_ids)) == len(requests)
    for body, headers in zip(requests, affinity):
        if body.get("tools"):
            assert body.get("prompt_cache_key") == session.session_id
            assert headers == {"session_id": session.session_id, "x-client-request-id": session.session_id}
        else:
            assert "prompt_cache_key" not in body
            assert headers == {"session_id": None, "x-client-request-id": None}
    expected_spend = sum(usage["input_tokens"] + 2 * usage["output_tokens"] for usage in usages)
    for call_id, usage in zip(call_ids, usages):
        receipt = repository.get_call(call_id)
        assert receipt.status == "settled"
        assert receipt.actual_usd_micros == usage["input_tokens"] + 2 * usage["output_tokens"]
    task = repository.get_task(task_id)
    assert task.cost.spent_usd_micros == expected_spend and task.cost.reserved_usd_micros == 0
    entries = [json.loads(line) for line in (managed.session_root / session.path).read_text().splitlines()]
    assert any(entry["type"] == "compaction" and "BLUE_CITY_COMPACT_SUMMARY" in entry["summary"] for entry in entries)


@pytest.mark.asyncio
async def test_native_compaction_at_a_pending_gate_has_no_post_and_cannot_save_a_pause_as_summary(repository, tmp_path):
    require_pi()
    app, requests = FastAPI(), []

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        requests.append(body)
        item = tool_item("openmontage", {"action": "checkpoint", "input": {
            "stage": "research", "status": "awaiting_human", "artifacts": {
                "research_brief": sample_artifact("research_brief"),
            },
        }}, "compact_pending_gate")
        response = {"id": "resp_gate", "object": "response", "model": body["model"], "status": "completed",
                    "output": [item], "usage": {"input_tokens": 3900, "output_tokens": 20, "total_tokens": 3920}}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                  {"type": "response.output_item.added", "output_index": 0, "item": item},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": response}]
        wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return StreamingResponse(iter([wire.encode()]), media_type="text/event-stream")

    with serving(app) as endpoint:
        profile = PiProfile(provider="local", model="compact-model", base_url=endpoint + "/v1",
                            credential_env="COMPACTION_TEST_KEY", reasoning=False, thinking_level="off",
                            context_window=4096, max_output_tokens=512, max_turns=8,
                            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        with BridgeServer(bridge, context, profile) as server:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"COMPACTION_TEST_KEY": "local-only-key"},
                                 trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
            path = managed.agent_dir / "settings.json"
            settings = json.loads(path.read_text())
            settings["compaction"].update(reserveTokens=256, keepRecentTokens=64)
            path.write_text(json.dumps(settings))
            client = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment},
                           session_root=managed.session_root)
            session = await client.start(context)
            try:
                await client.prompt("Restore BLUE_CITY, then checkpoint for the browser. " * 40, command_id="compact-gate")
                await asyncio.wait_for(settled(client), 15)
                assert repository.get_task(context.task_id).approval.status == "pending"
                messages = (await client.request("get_messages"))["messages"]
                assert messages[-1]["stopReason"] == "stop", "The ordinary agent still pauses cleanly at the gate"
            finally:
                await client.close()
    assert len(requests) == 1, "Neither continuation nor summarization may POST while the browser gate is pending"
    entries = [json.loads(line) for line in (managed.session_root / session.path).read_text().splitlines()]
    assert not any(entry["type"] == "compaction" for entry in entries), "A denied summary must preserve the old context"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["budget", "turns"])
async def test_native_summary_cannot_post_past_the_same_budget_or_turn_limit(repository, tmp_path, limit):
    require_pi()
    call_ids = []
    task_id = None

    def reply(body):
        submitted = [record for record in repository.unresolved_intents(task_id)
                     if record.kind == "model" and record.status == "submitted"]
        assert len(submitted) == 1
        call_ids.append(submitted[0].call_id)
        return [text_item("The existing city project and choices remain unchanged. " * 30)]

    with model_server(reply) as (base_url, requests):
        profile = PiProfile(provider="local", model="compact-model", base_url=base_url,
                            credential_env="COMPACTION_TEST_KEY", reasoning=False, thinking_level="off",
                            context_window=4096, max_output_tokens=512, max_turns=1 if limit == "turns" else 8,
                            price=None if limit == "budget" else PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        request = TaskCreate(brief="A local city test", profile_id="local", budget_usd_micros=500_000)
        task = repository.create_task(request, snapshot_for(profile, request), "summary-limit-test")
        task_id = task.task_id
        claim = repository.claim_command("summary-worker", lease_seconds=120)
        repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
        context = claim.context
        bridge = ProductionToolBridge(repository, tmp_path / "projects", model_profile=profile, registry=ToolRegistry())
        bridge.bind(context)
        bridge.initialize(context, title="Summary test", pipeline_type="framework-smoke")
        if limit == "budget":
            with pytest.raises(ContractViolation) as refusal:
                await bridge.authorize_model(intent(context, "cost-consent-probe"))
            assert refusal.value.code == "approval_conflict"
            gate = repository.get_task(task.task_id)
            approved = repository.decide_gate(ApprovalDecision(expected_version=gate.version,
                                                              binding=gate.approval.binding, decision="approve"),
                                               idempotency_key="approve-summary-test-price")
            repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=context.fence)
            assert bridge.apply_approval(context)
        with BridgeServer(bridge, context, profile) as server:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"COMPACTION_TEST_KEY": "local-only-key"},
                                 trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
            path = managed.agent_dir / "settings.json"
            settings = json.loads(path.read_text())
            settings["compaction"].update(reserveTokens=4095, keepRecentTokens=64)
            path.write_text(json.dumps(settings))
            client = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment},
                           session_root=managed.session_root)
            session = await client.start(context)
            try:
                await client.prompt("Remember BLUE_CITY before continuing its existing project.", command_id="summary-limit")
                events = await asyncio.wait_for(settled(client), 15)
                failure = next(event for event in events if event["type"] == "compaction_end" and event.get("errorMessage"))
                assert "budget_exceeded" in failure["errorMessage"] if limit == "budget" else "forbidden" in failure["errorMessage"]
            finally:
                await client.close()
        assert len(requests) == len(call_ids) == 1
        entries = [json.loads(line) for line in (managed.session_root / session.path).read_text().splitlines()]
        assert not any(entry["type"] == "compaction" for entry in entries)
        record = repository.get_call(call_ids[0])
        assert record.usage["input"] == 10 and record.usage["output"] == 5
        if limit == "budget":
            assert record.status == "receipted" and record.actual_usd_micros is None
            assert repository.get_task(task_id).cost.reserved_usd_micros == 500_000
        else:
            assert record.status == "settled" and record.actual_usd_micros == 0


@pytest.mark.asyncio
async def test_auto_compaction_remains_disabled_without_the_run_bound_provider_guard(repository, tmp_path):
    require_pi()
    with model_server() as (base_url, requests):
        profile = PiProfile(provider="local", model="compact-model", base_url=base_url,
                            credential_env="COMPACTION_TEST_KEY", reasoning=False, thinking_level="off",
                            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        _, context, _ = running(repository, tmp_path, profile)
        managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"COMPACTION_TEST_KEY": "local-only-key"})
        path = managed.agent_dir / "settings.json"
        settings = json.loads(path.read_text())
        assert settings["compaction"]["enabled"] is False
        settings["compaction"]["enabled"] = True
        path.write_text(json.dumps(settings))
        client = PiRPC(managed.argv, cwd=managed.work_dir, env=managed.env, session_root=managed.session_root)
        await client.start(context)
        try:
            assert (await client.inspect())["autoCompactionEnabled"] is False
            assert requests == []
        finally:
            await client.close()
