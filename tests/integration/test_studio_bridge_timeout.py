"""The private tool RPC deadline is independent of Undici's provider timers."""

import asyncio
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import pytest

from lib.config_model import PiPrice, PiProfile
from production.pi_config import prepare_pi
from production.pi_rpc import PiRPC
from production.tool_bridge import BridgeServer
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_policy import running
from tests.integration.test_studio_repository import repository, repository_factory
from tools.base_tool import BaseTool, ToolResult, ToolRuntime


@pytest.mark.asyncio
async def test_private_tool_reply_survives_the_provider_fetch_headers_deadline(repository, tmp_path):
    require_pi()
    root = Path(__file__).resolve().parents[2]

    class DelayedMedia(BaseTool):
        name, provider, runtime = "delayed_media", "local", ToolRuntime.LOCAL
        side_effects = ["writes media"]
        input_schema = {"type": "object", "required": ["output_path"], "properties": {"output_path": {"type": "string"}}}

        def execute(self, inputs):
            time.sleep(2.5)
            Path(inputs["output_path"]).write_bytes((root / "tests/fixtures/newapi/image_sample.png").read_bytes())
            return ToolResult(success=True, data={"marker": "delayed-media-completed"}, cost_usd=0)

    shim = tmp_path / "provider-timeout.ts"
    shim.write_text('''import {Agent} from ''' + json.dumps((root / ".runtime/pi/source/node_modules/undici/index.js").as_uri()) + ''';
export default function () {
const originalFetch = globalThis.fetch;
const agent = new Agent({headersTimeout:100, bodyTimeout:100});
const dispatcher = {dispatch(options, handler) {
  return agent.dispatch({...options, headersTimeout:100, bodyTimeout:100}, handler);
}};
globalThis.fetch = async (input, options) => {
  if (new URL(typeof input === "string" ? input : input.url).pathname !== "/execute") return originalFetch(input, options);
  try {return await originalFetch(input, {...options, dispatcher});}
  catch (error) {console.error("CONTROLLED_PRIVATE_DEADLINE=" + error.cause?.code); throw error;}
};
}
''')
    count = {"requests": 0}

    def reply(body):
        count["requests"] += 1
        if count["requests"] == 1:
            return [tool_item("openmontage", {"action": "execute", "input": {
                "tool_name": "delayed_media", "inputs": {"output_path": "assets/images/delayed.png"},
            }}, "slow-private-result")]
        return [text_item("The private result has arrived.")]

    with model_server(reply) as (endpoint, model_requests):
        profile = PiProfile(provider="local", model="deadline-model", base_url=endpoint,
                            credential_env="DEADLINE_TEST_KEY", reasoning=False, thinking_level="off",
                            task_timeout_seconds=30, price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        bridge.registry.register(DelayedMedia())
        bridge.allowed_tools = frozenset({"delayed_media"})
        with BridgeServer(bridge, context, profile) as control:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"DEADLINE_TEST_KEY": "local-only-key"},
                                 trusted_extension=root / "pi-runtime/extensions/openmontage.ts")
            managed.argv[-2:-2] = ["--extension", str(shim)]
            runner = PiRPC(managed.argv, cwd=managed.work_dir,
                           env={**managed.env, **control.environment},
                           session_root=managed.session_root)
            await runner.start(context)
            try:
                await runner.prompt("Run the controlled local tool and wait for its complete reply.", command_id="private-deadline")
                async def settled():
                    async for event in runner.events():
                        if event["type"] == "agent_settled":
                            return
                    raise AssertionError("Real Pi exited before settling")
                await asyncio.wait_for(settled(), 15)
                messages = (await runner.request("get_messages"))["messages"]
                result = next(message for message in messages if message.get("role") == "toolResult")
                text = result["content"][0]["text"]
                assert "delayed-media-completed" in text, runner.diagnostics
                assert "bridge_unavailable" not in text
                assert len(model_requests) == 2
            finally:
                await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,expected", [
    ("slow_body", "城市🌇完成"), ("split_utf8", "城市🌇完成"), ("non2xx", "studio-policy:forbidden"),
    ("not_ok", "studio-policy:invalid_input"), ("malformed", "studio-policy:bridge_unavailable"),
    ("truncated", "studio-policy:bridge_unavailable"), ("redirect", "studio-policy:bridge_unavailable"),
    ("deadline", "studio-policy:bridge_unavailable"), ("abort", None),
])
async def test_private_reply_decoding_errors_and_abort_are_bounded_without_retry(repository, tmp_path, fault, expected):
    require_pi()
    root = Path(__file__).resolve().parents[2]
    calls, paths, started = [], [], threading.Event()

    class ProbeTool(BaseTool):
        name, provider, runtime = "reply_probe", "local", ToolRuntime.LOCAL
        side_effects = []
        input_schema = {"type": "object", "properties": {"prompt": {"type": "string"}}}

        def execute(self, inputs):
            raise AssertionError("The controlled HTTP peer supplies this transport-only reply")

    count = {"requests": 0}

    def reply(body):
        count["requests"] += 1
        if count["requests"] == 1:
            return [tool_item("openmontage", {"action": "execute", "input": {
                "tool_name": "reply_probe", "inputs": {"prompt": "中文镜头🌇"},
            }}, "private-reply-probe")]
        return [text_item("The private request has finished.")]

    with model_server(reply) as (endpoint, model_requests):
        profile = PiProfile(provider="local", model="deadline-model", base_url=endpoint,
                            credential_env="DEADLINE_TEST_KEY", reasoning=False, thinking_level="off",
                            task_timeout_seconds=1 if fault == "deadline" else 30,
                            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        bridge.registry.register(ProbeTool())
        bridge.allowed_tools = frozenset({"reply_probe"})
        with BridgeServer(bridge, context, profile) as control:
            target = urlsplit(control.environment["STUDIO_BRIDGE_URL"])

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    paths.append(self.path)
                    assert self.headers.get("Authorization") == "Bearer " + control.token
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    if self.path != "/execute":
                        connection = HTTPConnection(target.hostname, target.port, timeout=5)
                        connection.request("POST", self.path, body=raw, headers={"Authorization": self.headers["Authorization"],
                                           "Content-Type": "application/json", "Content-Length": str(len(raw))})
                        response = connection.getresponse()
                        body, status = response.read(), response.status
                        connection.close()
                    else:
                        calls.append(self.path)
                        assert json.loads(raw)["inputs"]["prompt"] == "中文镜头🌇"
                        started.set()
                        if fault in {"abort", "deadline"}:
                            time.sleep(2)
                        result, status = {"ok": True, "data": {"marker": "城市🌇完成"}}, 200
                        if fault == "non2xx":
                            result, status = {"ok": False, "error": {"code": "forbidden"}}, 409
                        elif fault == "not_ok":
                            result = {"ok": False, "error": {"code": "invalid_input"}}
                        elif fault == "redirect":
                            status = 302
                        body = b"{not-json" if fault == "malformed" else json.dumps(result, ensure_ascii=False).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    if fault == "redirect" and self.path == "/execute":
                        self.send_header("Location", "/redirected")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        if self.path == "/execute" and fault == "truncated":
                            self.wfile.write(body[:10]); self.wfile.flush(); self.close_connection = True
                        elif self.path == "/execute" and fault in {"slow_body", "split_utf8"}:
                            split = body.index("城".encode()) + 1
                            self.wfile.write(body[:split]); self.wfile.flush()
                            time.sleep(2.1 if fault == "slow_body" else 0.02)
                            self.wfile.write(body[split:])
                        else:
                            self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            peer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=peer.serve_forever, daemon=True)
            thread.start()
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"DEADLINE_TEST_KEY": "local-only-key"},
                                 trusted_extension=root / "pi-runtime/extensions/openmontage.ts")
            runner = PiRPC(managed.argv, cwd=managed.work_dir,
                           env={**managed.env, **control.environment, "STUDIO_BRIDGE_URL": f"http://127.0.0.1:{peer.server_port}"},
                           session_root=managed.session_root)
            try:
                await runner.start(context)
                await runner.prompt("Use the controlled tool exactly once.", command_id="private-reply-case")
                if fault == "abort":
                    assert await asyncio.to_thread(started.wait, 5)
                    await runner.abort()
                async def settled():
                    async for event in runner.events():
                        if event["type"] == "agent_settled":
                            return
                    raise AssertionError("Real Pi exited before settling")
                await asyncio.wait_for(settled(), 8)
                messages = (await runner.request("get_messages"))["messages"]
                if expected is not None:
                    result = next(message for message in messages if message.get("role") == "toolResult")
                    assert expected in result["content"][0]["text"]
                assert calls == ["/execute"]
                assert "/redirected" not in paths
                assert len(model_requests) == (1 if fault == "abort" else 2)
            finally:
                await runner.close()
                peer.shutdown(); peer.server_close(); thread.join(timeout=5)
