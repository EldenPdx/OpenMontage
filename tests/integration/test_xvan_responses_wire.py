"""Production xvan Responses fields through real Pi, HTTP and the shared journal."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import pytest

from lib.config_model import PiPrice, PiProfile
from production.pi_config import prepare_pi
from production.pi_rpc import PiRPC
from production.tool_bridge import BridgeServer
from tests.browser.test_studio_submission import serving
from tests.fixtures.studio.model_server import text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_policy import running
from tests.integration.test_studio_repository import repository, repository_factory


@pytest.mark.asyncio
async def test_default_xvan_alias_native_two_turn_tools_and_usage_match_production_contract(repository, tmp_path):
    require_pi()
    requests, affinity = [], []
    app = FastAPI()
    task_id = None
    usage = {"input_tokens": 100, "output_tokens": 7, "total_tokens": 107,
             "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
             "output_tokens_details": {"reasoning_tokens": 2}}

    @app.post("/v1/responses")
    async def responses(request: Request):
        assert request.headers["authorization"] == "Bearer wire-test-key"
        body = await request.json()
        requests.append(body)
        affinity.append({name: request.headers.get(name) for name in ("session_id", "x-client-request-id")})
        submitted = repository.unresolved_intents(task_id)
        assert any(intent.kind == "model" and intent.status == "submitted" for intent in submitted)
        items = [tool_item("openmontage", {"action": "read", "input": {"path": "AGENT_GUIDE.md"}}, "wire_read")] if len(requests) == 1 else [text_item("The exact tool result has been read.")]
        response = {"id": "resp_wire_" + str(len(requests)), "object": "response", "model": body["model"],
                    "status": "completed", "output": items, "usage": usage}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
        for index, item in enumerate(items):
            events.extend([{"type": "response.output_item.added", "output_index": index, "item": item},
                           {"type": "response.output_item.done", "output_index": index, "item": item}])
        events.append({"type": "response.completed", "response": response})
        wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return StreamingResponse(iter([wire.encode()]), media_type="text/event-stream")

    with serving(app) as endpoint:
        profile = PiProfile(provider="xvan", model="gpt-5.6-sol", base_url=endpoint + "/v1",
                            credential_env="NEW_API_KEY", reasoning=True, thinking_level="medium",
                            input=["text", "image"], context_window=200000, max_output_tokens=512, max_turns=2,
                            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        task_id = context.task_id
        with BridgeServer(bridge, context, profile) as server:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"NEW_API_KEY": "wire-test-key"},
                                 trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
            client = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment},
                           session_root=managed.session_root, redact_values=(*managed.redact_values, server.token))
            session = await client.start(context)
            try:
                await client.prompt("Use openmontage to read AGENT_GUIDE.md, then reply with one short sentence.", command_id="wire-prompt")
                async def settled():
                    async for event in client.events():
                        if event["type"] == "agent_settled":
                            return
                    raise AssertionError("Real Pi exited before settling")
                await asyncio.wait_for(settled(), 20)
                messages = (await client.request("get_messages"))["messages"]
                assert messages[-1]["stopReason"] == "stop"
                assert messages[-1]["usage"]["input"] == 70
                assert messages[-1]["usage"]["cacheRead"] == 20
                assert messages[-1]["usage"]["cacheWrite"] == 10
                assert messages[-1]["usage"]["output"] == 7
                assert messages[-1]["usage"]["reasoning"] == 2
            finally:
                await client.close()
        assert len(requests) == 2
        assert [body.get("prompt_cache_key") for body in requests] == [session.session_id] * 2
        assert affinity == [{"session_id": session.session_id, "x-client-request-id": session.session_id}] * 2
        assert json.loads((managed.agent_dir / "settings.json").read_text())["cacheWarming"] == "off"
        for body in requests:
            assert body["model"] == "gpt-5.6-sol"
            assert body["max_output_tokens"] == 512
            assert body["reasoning"]["effort"] == "medium"
            assert body["stream"] is True
            assert body["store"] is False
            assert isinstance(body["input"], list)
            definition = next(tool for tool in body["tools"] if tool["name"] == "openmontage")
            assert definition["type"] == "function"
            assert definition["parameters"]["type"] == "object"
            assert "function" not in definition
        history = requests[1]["input"]
        function_call = next(item for item in history if item.get("type") == "function_call")
        result = next(item for item in history if item.get("type") == "function_call_output")
        assert result["call_id"] == function_call["call_id"]
        assert function_call["name"] == "openmontage"
        assert "AGENT_GUIDE.md" in result["output"]
        assert repository.unresolved_intents(task_id) == []
        assert repository.get_task(task_id).cost.unknown_call_count == 0
