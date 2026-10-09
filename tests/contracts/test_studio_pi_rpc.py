"""Public runner behavior against official Pi; fault transports are separate tests."""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import threading

import pytest

from production.contracts import ConfigSnapshot, RunContext, SessionReference
from production.contracts import ContractViolation
from production.pi_rpc import PiRPC
from tests.fixtures.studio.model_server import model_server, text_item
from tests.fixtures.studio.repository import require_pi

CLI = Path(__file__).resolve().parents[2] / ".runtime/pi/source/packages/coding-agent/dist/bundle/cli.js"


def context():
    return RunContext(task_id="task-rpc", project_id="project-rpc", run_id="run-rpc", fence=1,
                      config_snapshot=ConfigSnapshot(profile_id="test", provider="controlled", model="local-model",
                                                     api="openai-responses", configuration_sha256="a" * 64),
                      session=SessionReference(path="task-rpc/run-rpc.jsonl"))


def runner(tmp_path, base_url):
    agent = tmp_path / "agent"
    agent.mkdir(exist_ok=True)
    (agent / "models.json").write_text(json.dumps({"providers": {"controlled": {
        "baseUrl": base_url, "api": "openai-responses", "apiKey": "$TEST_PI_KEY",
        "models": [{"id": "local-model", "name": "Local model", "input": ["text"], "reasoning": False,
                    "contextWindow": 32768, "maxTokens": 1024,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
    (agent / "settings.json").write_text(json.dumps({"retry": {"enabled": False, "provider": {"maxRetries": 0}},
                                                   "compaction": {"enabled": False}, "cacheWarming": "off"}))
    command = ["node", str(CLI), "--mode", "rpc", "--offline", "--no-tools", "--no-extensions",
               "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
               "--no-approve", "--provider", "controlled", "--model", "local-model", "--thinking", "off"]
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "PI_OFFLINE": "1",
           "PI_CODING_AGENT_DIR": str(agent), "TEST_PI_KEY": "test-secret-never-public"}
    return PiRPC(command, cwd=tmp_path, env=env, session_root=tmp_path / "sessions",
                 redact_values=("test-secret-never-public",))


async def settled(client):
    async for event in client.events():
        if event["type"] == "agent_settled":
            return event
    raise AssertionError("Pi exited before agent_settled")


@pytest.mark.asyncio
async def test_real_pi_accepts_streaming_prompt_and_resumes_the_exact_session(tmp_path):
    require_pi()
    with model_server() as (base_url, requests):
        client = runner(tmp_path, base_url)
        session = await client.start(context())
        try:
            assert session.path == "task-rpc/run-rpc.jsonl"
            acknowledged = await client.prompt("记住蓝色", command_id="prompt-first")
            assert acknowledged["disposition"] == "started"
            await asyncio.wait_for(settled(client), timeout=15)
            assert len(requests) == 1
        finally:
            await client.close()
        assert (tmp_path / "sessions" / session.path).is_file()
        restored = runner(tmp_path, base_url)
        await restored.start(context().model_copy(update={"session": session}))
        try:
            state = await restored.inspect()
            assert state["sessionId"] == session.session_id
            await restored.prompt("继续上次对话", command_id="prompt-second")
            await asyncio.wait_for(settled(restored), timeout=15)
            assert "真实 Pi 联调成功" in json.dumps(requests[-1]["input"], ensure_ascii=False)
            assert "test-secret-never-public" not in json.dumps(state)
        finally:
            await restored.close()
        assert client.returncode == 0
        assert restored.returncode == 0


@pytest.mark.asyncio
async def test_transport_shutdown_reaps_children_even_when_the_parent_exits_normally(tmp_path):
    # This fault-injection transport creates a child that deliberately outlives EOF.
    script = tmp_path / "fault_transport.py"
    script.write_text('''import json, subprocess, sys
if "--version" in sys.argv:
    print("1.1.0"); raise SystemExit
for line in sys.stdin.buffer:
    request = json.loads(line)
    data = {}
    if request["type"] == "get_state":
        data = {"sessionFile":sys.argv[sys.argv.index("--session")+1], "sessionId":"fault-session",
                "model":{"provider":"controlled","id":"local-model"}}
    if request["type"] == "get_messages":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        data = {"child_pid":child.pid}
    print(json.dumps({"id":request["id"],"type":"response","success":True,"data":data}), flush=True)
''')
    client = PiRPC([sys.executable, str(script)], cwd=tmp_path, env={"PATH": os.environ["PATH"]},
                   session_root=tmp_path / "sessions", shutdown_timeout=1)
    await client.start(context())
    child_pid = (await client.request("get_messages"))["child_pid"]
    try:
        await client.close()
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("Managed process group retained an orphan after Pi exited")
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_fragmented_unicode_crlf_unknown_events_and_out_of_order_responses(tmp_path):
    script = tmp_path / "fault_transport.py"
    script.write_text('''import json, os, sys
if "--version" in sys.argv:
    print("1.1.0"); raise SystemExit
pending=[]
for line in sys.stdin.buffer:
    request=json.loads(line); data={}
    if request["type"]=="get_state":
        data={"sessionFile":sys.argv[sys.argv.index("--session")+1],"sessionId":"fault-session",
              "model":{"provider":"controlled","id":"local-model"}}
    if request["type"]=="get_messages":
        pending.append(request)
        if len(pending)<2: continue
        event=json.dumps({"type":"future_event","text":"汉\\u2028字\\u2029UTF-8"},ensure_ascii=False).encode()+b"\\r\\n"
        for byte in event: os.write(1,bytes([byte]))
        for item in reversed(pending):
            print(json.dumps({"type":"response","id":item["id"],"success":True,
                              "data":{"ordinal":item["ordinal"]}}),flush=True)
        pending=[]; continue
    print(json.dumps({"type":"response","id":request["id"],"success":True,"data":data}),flush=True)
''')
    client = PiRPC([sys.executable, str(script)], cwd=tmp_path, env={"PATH": os.environ["PATH"]},
                   session_root=tmp_path / "sessions")
    await client.start(context())
    try:
        results = await asyncio.gather(client.request("get_messages", ordinal=1),
                                       client.request("get_messages", ordinal=2))
        assert results == [{"ordinal": 1}, {"ordinal": 2}]
        event = await asyncio.wait_for(anext(client.events()), timeout=2)
        assert event == {"type": "future_event", "text": "汉\u2028字\u2029UTF-8"}
        with pytest.raises(ContractViolation, match="allowlist"):
            await client.request("bash", command="touch forbidden")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["invalid", "huge", "eof", "hang"])
async def test_malformed_oversize_eof_and_hung_processes_fail_without_orphans(tmp_path, fault):
    script = tmp_path / "fault_transport.py"
    script.write_text('''import json, sys, time
if "--version" in sys.argv:
    print("1.1.0"); raise SystemExit
for line in sys.stdin.buffer:
    request=json.loads(line); data={}
    if request["type"]=="get_state":
        data={"sessionFile":sys.argv[sys.argv.index("--session")+1],"sessionId":"fault-session",
              "model":{"provider":"controlled","id":"local-model"}}
    if request["type"]=="get_messages":
        fault=sys.argv[1]
        if fault=="invalid": print("not JSON",flush=True)
        elif fault=="huge": print("x"*5000000,flush=True)
        elif fault=="hang": time.sleep(120)
        raise SystemExit
    print(json.dumps({"type":"response","id":request["id"],"success":True,"data":data}),flush=True)
''')
    # Interpreter prefix has a fault selector after the script; version probe stays fixed.
    script.write_text(script.read_text().replace('fault=sys.argv[1]', f'fault={fault!r}'))
    client = PiRPC([sys.executable, str(script)], cwd=tmp_path, env={"PATH": os.environ["PATH"]},
                   session_root=tmp_path / "sessions", request_timeout=0.2, shutdown_timeout=0.1)
    await client.start(context())
    try:
        with pytest.raises(ContractViolation):
            await client.request("get_messages")
    finally:
        await client.close()
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_real_pi_cancellation_discards_a_queued_follow_up(tmp_path):
    require_pi()
    started, release = threading.Event(), threading.Event()

    def slow_response(body):
        started.set()
        release.wait(timeout=5)
        return [text_item("This response should be cancelled")]

    with model_server(slow_response) as (base_url, requests):
        client = runner(tmp_path, base_url)
        await client.start(context())
        try:
            await client.prompt("开始长请求", command_id="prompt-long")
            assert await asyncio.to_thread(started.wait, 2)
            queued = await client.request("prompt", message="不应执行的后续请求", streamingBehavior="followUp")
            assert queued["disposition"] == "queued"
            await client.close()
            release.set()
            await asyncio.sleep(0.1)
            assert len(requests) == 1
            assert client.returncode == 0
        finally:
            release.set()
            await client.close()


@pytest.mark.asyncio
async def test_session_reference_cannot_reuse_another_tasks_managed_session(tmp_path):
    require_pi()
    with model_server() as (base_url, requests):
        client = runner(tmp_path, base_url)
        other_session = SessionReference(path="another-task/another-run/session.jsonl")
        try:
            with pytest.raises(ContractViolation, match="task/run"):
                await client.start(context().model_copy(update={"session": other_session}))
            assert requests == []
        finally:
            await client.close()
