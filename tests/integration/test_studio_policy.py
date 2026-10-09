import asyncio
import multiprocessing
import os
import subprocess
import time
from pathlib import Path

import pytest

from lib.config_model import PiPrice, PiProfile
from production.contracts import ApprovalDecision, CallIntent, ContractViolation, TaskCreate, TaskState, ToolCall
from production.pi_config import snapshot_for
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.integration.test_studio_repository import repository, repository_factory
from tests.fixtures.studio.model_server import model_server, tool_item
from tests.contracts.test_newapi_video import gateway, mp4


def running(repository, tmp_path, profile=None, *, media_models=None, media_hash=None):
    from production.tool_bridge import ProductionToolBridge
    from tools.tool_registry import ToolRegistry
    profile = profile or PiProfile(provider="local", model="test-model", credential_env="LOCAL_TEST_KEY",
                                   reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
    request = TaskCreate(brief="A local video", profile_id="local")
    task = repository.create_task(request, snapshot_for(profile, request, media_models=media_models,
                                                        media_configuration_sha256=media_hash), "policy-task-1")
    claim = repository.claim_command("policy-worker", lease_seconds=3600)
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
    bridge = ProductionToolBridge(repository, tmp_path / "projects", model_profile=profile, registry=ToolRegistry(),
                                  runtime_root=tmp_path / "runtime")
    bridge.bind(claim.context)
    bridge.store.initialize(claim.context, title="Local video", pipeline_type="framework-smoke")
    return bridge, claim.context, profile


def intent(context, identifier="model-1"):
    return CallIntent(call_id=identifier, task_id=context.task_id, run_id=context.run_id, fence=context.fence,
                      kind="model", operation="inference", provider=context.config_snapshot.provider,
                      model=context.config_snapshot.model, request_sha256="b" * 64, price_status="quoted")


def test_atomic_artifact_and_gate_stop_model_calls_and_reject_forged_approval(repository, tmp_path):
    bridge, context, _ = running(repository, tmp_path)
    value = sample_artifact("research_brief")
    first = bridge.artifact(context, "research_brief", value)
    assert (tmp_path / "projects" / context.project_id / first["path"]).is_file()
    assert repository.unresolved_intents(context.task_id) == []
    with pytest.raises(ContractViolation, match="backend"):
        bridge.checkpoint(context, stage="research", status="completed", artifacts={"research_brief": value}, human_approved=True)
    response = bridge.checkpoint(context, stage="research", status="awaiting_human", artifacts={"research_brief": value})
    assert response["paused"] is True
    task = repository.get_task(context.task_id)
    assert task.state == TaskState.AWAITING_APPROVAL
    assert task.approval.artifact.revision == 2
    with pytest.raises(ContractViolation, match="paused"):
        asyncio.run(bridge.authorize_model(intent(context)))
    with pytest.raises(ContractViolation):
        bridge.artifact(context, "research_brief", value)


def test_model_intent_precedes_usage_and_unknown_reservation_is_retained(repository, tmp_path):
    profile = PiProfile(provider="local", model="test-model", credential_env="LOCAL_TEST_KEY",
                        reasoning=False, thinking_level="off", context_window=1000, max_output_tokens=100,
                        price=PiPrice(input=1000, output=2000, cache_read=0, cache_write=0))
    bridge, context, _ = running(repository, tmp_path, profile)
    submitted = asyncio.run(bridge.authorize_model(intent(context)))
    assert repository.get_call(submitted.call_id).status == "submitted"
    assert repository.get_task(context.task_id).cost.reserved_usd_micros == 2
    settled = asyncio.run(bridge.settle_model(submitted.model_copy(update={"usage": {"input": 10, "output": 5}})))
    assert settled.actual_usd_micros == 1
    assert asyncio.run(bridge.settle_model(submitted.model_copy(update={"usage": {"input": 999}}))) == settled
    unknown = asyncio.run(bridge.authorize_model(intent(context, "model-2")))
    asyncio.run(bridge.settle_model(unknown.model_copy(update={"status": "outcome_unknown"})))
    assert repository.get_task(context.task_id).cost.reserved_usd_micros == 2
    with pytest.raises(ContractViolation, match="replayed"):
        asyncio.run(bridge.authorize_model(intent(context, "model-2")))


def test_unknown_model_price_requires_explicit_scope_and_keeps_the_receipt_hold(repository, tmp_path):
    profile = PiProfile(provider="local", model="test-model", credential_env="LOCAL_TEST_KEY",
                        reasoning=False, thinking_level="off", price=None)
    bridge, context, _ = running(repository, tmp_path, profile)
    with pytest.raises(ContractViolation, match="approval"):
        asyncio.run(bridge.authorize_model(intent(context)))
    task = repository.get_task(context.task_id)
    assert task.approval.stage == "model_cost"
    assert task.approval.scope.unknown_price is True
    assert task.approval.scope.authorized_limit_usd_micros == 500_000
    approved = repository.decide_gate(ApprovalDecision(expected_version=task.version, binding=task.approval.binding,
                                                       decision="approve"), idempotency_key="unknown-price-consent")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=context.fence)
    bridge.apply_approval(context)
    submitted = asyncio.run(bridge.authorize_model(intent(context)))
    assert submitted.reserved_usd_micros == 500_000
    assert submitted.price_status == "unquoted"
    receipt = asyncio.run(bridge.settle_model(submitted.model_copy(update={"usage": {"input": 10, "output": 5}})))
    assert receipt.status == "receipted" and receipt.actual_usd_micros is None
    assert repository.get_task(context.task_id).cost.reserved_usd_micros == 500_000


def test_failed_media_tool_cannot_replace_existing_output(repository, tmp_path):
    from tools.base_tool import BaseTool, ToolResult, ToolRuntime
    class FailedMedia(BaseTool):
        name, provider, runtime = "test_media", "local", ToolRuntime.LOCAL
        side_effects = ["writes media"]
        input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}, "required": ["output_path"]}
        def execute(self, inputs):
            Path(inputs["output_path"]).write_bytes(b"partial download")
            return ToolResult(success=False, cost_usd=0)
    bridge, context, _ = running(repository, tmp_path)
    bridge.registry.register(FailedMedia())
    bridge.allowed_tools = frozenset({"test_media"})
    output = bridge.store.project(context) / "assets/video/existing.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"known good media")
    receipt = asyncio.run(bridge.execute(ToolCall(call_id="failed-media-1", context=context, tool_name="test_media",
                                                inputs={"output_path": "assets/video/existing.mp4"})))
    assert receipt.success is False
    assert output.read_bytes() == b"known good media"


@pytest.mark.asyncio
async def test_duplicate_tool_admission_executes_once_and_replays_the_receipt(repository, tmp_path):
    from tools.base_tool import BaseTool, ToolResult, ToolRuntime
    class LocalMedia(BaseTool):
        name, provider, runtime = "test_media", "local", ToolRuntime.LOCAL
        side_effects = ["writes media"]
        input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}, "required": ["output_path"]}
        count = multiprocessing.Value("i", 0)
        def execute(self, inputs):
            self.count.value += 1
            Path(inputs["output_path"]).write_bytes(b"new verified media")
            return ToolResult(success=True, data={"path": inputs["output_path"]}, cost_usd=0)
    bridge, context, _ = running(repository, tmp_path)
    tool = LocalMedia()
    bridge.registry.register(tool)
    bridge.allowed_tools = frozenset({"test_media"})
    call = ToolCall(call_id="same-media-1", context=context, tool_name="test_media", inputs={"output_path": "assets/video/clip.mp4"})
    results = await asyncio.gather(bridge.execute(call), bridge.execute(call))
    assert tool.count.value == 1
    result = next(item for item in results if item.success)
    replay = await bridge.execute(call)
    assert replay.success and replay.data == result.data
    assert tool.count.value == 1


def test_known_provider_job_resumes_without_a_second_submission(repository, tmp_path):
    from tools.base_tool import BaseTool, ToolResult, ToolRuntime
    class AsyncMedia(BaseTool):
        name, provider, runtime = "test_async_media", "local", ToolRuntime.API
        side_effects = ["writes media"]
        input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}, "required": ["output_path"]}
        posts, polls = multiprocessing.Value("i", 0), multiprocessing.Value("i", 0)
        def execute(self, inputs):
            if inputs.get("resume_job"):
                self.polls.value += 1
                Path(inputs["output_path"]).write_bytes(b"completed job media")
                return ToolResult(success=True, data={"job_id": "job-one"}, cost_usd=0)
            self.posts.value += 1
            return ToolResult(success=False, data={"job_id": "job-one", "resume_job": {"job_id": "job-one"}}, cost_usd=None)
    bridge, context, _ = running(repository, tmp_path)
    tool = AsyncMedia()
    bridge.registry.register(tool)
    bridge.allowed_tools = frozenset({tool.name})
    bridge.tool_quotes[(tool.name, None)] = 0
    first = asyncio.run(bridge.execute(ToolCall(call_id="known-job-1", context=context, tool_name=tool.name,
                                               inputs={"output_path": "assets/video/job.mp4"})))
    assert not first.success
    assert repository.get_call("known-job-1").status == "receipted"
    resumed = asyncio.run(bridge.resume(context, "known-job-1"))
    assert resumed.success
    assert (tool.posts.value, tool.polls.value) == (1, 1)
    assert repository.get_call("known-job-1").status == "settled"


def test_unknown_media_price_binds_one_exact_request_before_submission(repository, tmp_path):
    from tools.base_tool import BaseTool, ToolResult, ToolRuntime
    class UnquotedMedia(BaseTool):
        name, provider, runtime = "test_unquoted_media", "local", ToolRuntime.API
        side_effects = ["writes media"]
        input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}, "required": ["output_path"]}
        count = multiprocessing.Value("i", 0)
        def execute(self, inputs):
            self.count.value += 1
            Path(inputs["output_path"]).write_bytes(b"a completed media response")
            return ToolResult(success=True, cost_usd=None)
    bridge, context, _ = running(repository, tmp_path)
    tool = UnquotedMedia()
    bridge.registry.register(tool)
    bridge.allowed_tools = frozenset({tool.name})
    first = asyncio.run(bridge.execute(ToolCall(call_id="original-media-call", context=context, tool_name=tool.name,
                                               inputs={"output_path": "assets/video/unknown.mp4"})))
    assert not first.success and tool.count.value == 0
    task = repository.get_task(context.task_id)
    assert task.approval.stage == "media_cost"
    approved = repository.decide_gate(ApprovalDecision(expected_version=task.version, binding=task.approval.binding,
                                                       decision="approve"), idempotency_key="media-price-consent")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=context.fence)
    bridge.apply_approval(context)
    resumed = asyncio.run(bridge.execute(ToolCall(call_id="new-model-tool-id", context=context, tool_name=tool.name,
                                                 inputs={"output_path": "assets/video/unknown.mp4"})))
    assert resumed.success and tool.count.value == 1
    assert resumed.call_id == "original-media-call"
    assert repository.get_call("original-media-call").status == "receipted"
    assert repository.get_task(context.task_id).cost.reserved_usd_micros == 500_000
    again = asyncio.run(bridge.execute(ToolCall(call_id="third-model-tool-id", context=context, tool_name=tool.name,
                                               inputs={"output_path": "assets/video/unknown.mp4"})))
    assert again.success and tool.count.value == 1


def test_quoted_large_model_action_requires_its_own_cost_evidence(repository, tmp_path):
    profile = PiProfile(provider="local", model="test-model", credential_env="LOCAL_TEST_KEY",
                        reasoning=False, thinking_level="off", context_window=1_000_000, max_output_tokens=100,
                        price=PiPrice(input=1_000_000, output=0, cache_read=0, cache_write=0))
    bridge, context, _ = running(repository, tmp_path, profile)
    bridge.checkpoint(context, stage="research", status="awaiting_human", artifacts={"research_brief": sample_artifact("research_brief")})
    task = repository.get_task(context.task_id)
    approved = repository.decide_gate(ApprovalDecision(expected_version=task.version, binding=task.approval.binding,
                                                       decision="approve"), idempotency_key="research-only-approval")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=context.fence)
    bridge.apply_approval(context)
    with pytest.raises(ContractViolation, match="quoted model"):
        asyncio.run(bridge.authorize_model(intent(context)))
    task = repository.get_task(context.task_id)
    assert task.approval.stage == "model_cost"
    assert task.approval.scope.unknown_price is False
    assert task.approval.scope.authorized_limit_usd_micros == 1_000_000
    approved = repository.decide_gate(ApprovalDecision(expected_version=task.version, binding=task.approval.binding,
                                                       decision="approve"), idempotency_key="known-model-cost-approval")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=context.fence)
    bridge.apply_approval(context)
    submitted = asyncio.run(bridge.authorize_model(intent(context)))
    assert submitted.reserved_usd_micros == 1_000_000


@pytest.mark.asyncio
async def test_real_gateway_receipt_is_durable_during_poll_and_resume_has_no_post(repository, tmp_path, gateway):
    from production.policy import media_configuration_sha256
    from production.tool_bridge import ProductionToolBridge
    from tools.video.newapi_video import NewAPIVideo
    gateway["statuses"] = ["in_progress"]
    bridge, context, profile = running(repository, tmp_path, media_models={"video": "gateway-video"},
                                       media_hash=media_configuration_sha256(gateway["config_path"]))
    bridge.registry.register(NewAPIVideo(config_path=gateway["config_path"]))
    bridge.tool_quotes[("newapi_video", "gateway-video")] = 0
    bridge.checkpoint(context, stage="research", status="awaiting_human", artifacts={"research_brief": sample_artifact("research_brief")})
    task = repository.get_task(context.task_id)
    approved = repository.decide_gate(ApprovalDecision(expected_version=task.version, binding=task.approval.binding,
                                                       decision="approve"), idempotency_key="approve-video-plan")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=approved.version, fence=context.fence)
    bridge.apply_approval(context)
    call = ToolCall(call_id="real-video-job", context=context, tool_name="newapi_video", inputs={
        "model": "gateway-video", "prompt": "A blue room", "output_path": "assets/video/real.mp4",
        "poll_timeout": 30, "poll_interval": 0.05})
    execution = asyncio.create_task(bridge.execute(call))
    for _ in range(100):
        await asyncio.sleep(0.02)
        try:
            receipt = repository.get_call(call.call_id)
        except ContractViolation:
            continue
        if receipt.status == "receipted":
            break
    assert receipt.external_job_id == "video-public"
    assert not execution.done()
    assert bridge.stop() is True
    await execution
    assert repository.get_call(call.call_id).status == "receipted"
    gateway["statuses"] = ["completed"]
    restarted = ProductionToolBridge(repository, tmp_path / "projects", model_profile=profile, registry=bridge.registry,
                                     tool_quotes=bridge.tool_quotes, runtime_root=tmp_path / "runtime")
    restarted.bind(context)
    result = await restarted.resume(context, call.call_id)
    assert result.success, result.error
    assert sum(method == "POST" for method, *_ in gateway["calls"]) == 1
    assert (bridge.store.project(context) / "assets/video/real.mp4").is_file()


@pytest.mark.asyncio
async def test_stopping_managed_tool_kills_ffmpeg_descendant_and_retains_intent(repository, tmp_path):
    from production.recovery import _group_exists
    from tools.base_tool import BaseTool, ToolResult, ToolRuntime
    class LongRender(BaseTool):
        name, provider, runtime = "test_long_render", "local", ToolRuntime.LOCAL
        side_effects = ["writes media"]
        input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}, "required": ["output_path"]}
        def execute(self, inputs):
            result = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-re", "-f", "lavfi",
                                     "-i", "testsrc2=size=64x48:rate=1", "-t", "60", "-c:v", "libx264", inputs["output_path"]],
                                    capture_output=True)
            return ToolResult(success=result.returncode == 0, cost_usd=0)
    bridge, context, _ = running(repository, tmp_path)
    bridge.registry.register(LongRender())
    bridge.allowed_tools = frozenset({"test_long_render"})
    execution = asyncio.create_task(bridge.execute(ToolCall(call_id="long-render-1", context=context,
        tool_name="test_long_render", inputs={"output_path": "assets/video/long.mp4"})))
    for _ in range(100):
        await asyncio.sleep(0.02)
        processes = list(bridge.jobs.values())
        if processes and getattr(processes[0], "studio_identity", None):
            break
    await asyncio.sleep(0.2)
    started = time.monotonic()
    assert bridge.stop() is True
    assert time.monotonic() - started < 3
    result = await execution
    assert not result.success
    assert not _group_exists(processes[0].pid)
    assert repository.get_call("long-render-1").status == "outcome_unknown"


@pytest.mark.asyncio
async def test_real_pi_guard_authorizes_each_request_and_stops_at_turn_limit(repository, tmp_path):
    from production.pi_config import prepare_pi
    from production.pi_rpc import PiRPC
    from production.tool_bridge import BridgeServer
    root = Path(__file__).resolve().parents[2]
    if not (root / ".runtime/pi/source/packages/coding-agent/dist/bundle/cli.js").is_file():
        pytest.fail("Install the real pinned Pi before Studio integration")
    with model_server() as (url, requests):
        profile = PiProfile(provider="local", model="test-model", base_url=url, credential_env="LOCAL_TEST_KEY",
                            reasoning=False, thinking_level="off", context_window=1000, max_output_tokens=100,
                            max_turns=1, price=PiPrice(input=1000, output=2000, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        with BridgeServer(bridge, context, profile) as control:
            managed = prepare_pi(profile, context, tmp_path / "runtime",
                                 environment={"LOCAL_TEST_KEY": "local-fixture-secret"},
                                 trusted_extension=root / "pi-runtime/extensions/openmontage.ts")
            runner = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **control.environment},
                           session_root=managed.session_root,
                           redact_values=(*managed.redact_values, control.token))
            try:
                await runner.start(context)
                await runner.prompt("Say hello", command_id="guarded-first")
                async for event in runner.events():
                    if event["type"] == "agent_settled":
                        break
                assert len(requests) == 1
                assert repository.get_task(context.task_id).cost.spent_usd_micros == 1
                assert repository.get_task(context.task_id).cost.reserved_usd_micros == 0
                await runner.prompt("A second request must be denied", command_id="guarded-second")
                async for event in runner.events():
                    if event["type"] == "agent_settled":
                        break
                assert len(requests) == 1
            finally:
                await runner.close()


@pytest.mark.asyncio
async def test_real_pi_tool_gate_pauses_before_the_next_provider_request(repository, tmp_path):
    from production.pi_config import prepare_pi
    from production.pi_rpc import PiRPC
    from production.tool_bridge import BridgeServer
    root = Path(__file__).resolve().parents[2]
    artifact = sample_artifact("research_brief")
    reply = lambda body: [tool_item("openmontage", {"action": "checkpoint", "input": {
        "stage": "research", "status": "awaiting_human", "artifacts": {"research_brief": artifact}}})]
    with model_server(reply) as (url, requests):
        profile = PiProfile(provider="local", model="test-model", base_url=url, credential_env="LOCAL_TEST_KEY",
                            reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile)
        with BridgeServer(bridge, context, profile) as control:
            managed = prepare_pi(profile, context, tmp_path / "runtime",
                                 environment={"LOCAL_TEST_KEY": "local-fixture-secret"},
                                 trusted_extension=root / "pi-runtime/extensions/openmontage.ts")
            runner = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **control.environment},
                           session_root=managed.session_root, redact_values=(*managed.redact_values, control.token))
            try:
                await runner.start(context)
                await runner.prompt("Open the research review", command_id="gate-first")
                async for event in runner.events():
                    if event["type"] == "agent_settled":
                        break
                assert repository.get_task(context.task_id).state == TaskState.AWAITING_APPROVAL
                assert len(requests) == 1
                assert [tool["name"] for tool in requests[0]["tools"]] == ["openmontage"]
            finally:
                await runner.close()
