"""A normally exited tool's pipe still carries its completed result."""

import asyncio
import multiprocessing
import os
import subprocess

import pytest

from lib.config_model import PiPrice, PiProfile
from production.contracts import TaskState, ToolCall
from tests.contracts.test_newapi_video import gateway, mp4
from tests.integration.test_studio_api import api
from tests.integration.test_studio_media_cost_revision import approve_plan, decide
from tools.video.newapi_video import NewAPIVideo


@pytest.mark.parametrize("fault", ["completed", "stopped", "abnormal_exit", "eof"])
def test_tool_receipt_at_process_exit_preserves_exact_consent_and_unknown_fee_hold(api, tmp_path, gateway, monkeypatch, fault):
    class GatewayVideo(NewAPIVideo):
        def execute(self, inputs):
            result = super().execute(inputs)
            if fault in {"abnormal_exit", "eof"}:
                os._exit(9 if fault == "abnormal_exit" else 0)
            return result

    gateway["statuses"] = ["completed"]
    profile = PiProfile(provider="controlled", model="local-model", credential_env="TEST_PI_KEY",
                        reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
    _, repository, _, bridge, context = approve_plan(api, tmp_path, gateway, profile)
    bridge.registry.register(GatewayVideo(config_path=gateway["config_path"]))
    call = ToolCall(call_id="completed-receipt", context=context, tool_name="newapi_video", inputs={
        "model": "gateway-video", "duration_seconds": 12, "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": True},
        "prompt": "An approved local receipt test", "output_path": "assets/video/final.mp4"})
    waiting = asyncio.run(bridge.execute(call))
    assert not waiting.success and waiting.error.code == "approval_conflict"
    assert gateway["calls"] == []
    approved = decide(api, context, "approve")
    assert approved.status_code == 202
    repository.transition(context.task_id, TaskState.RUNNING, expected_version=approved.json()["version"], fence=context.fence)
    assert bridge.apply_approval(context)
    original_alive = multiprocessing.process.BaseProcess.is_alive
    exits = []

    def finish_between_checks(process):
        if getattr(process, "studio_identity", None) and not exits and original_alive(process):
            process.join(5)
            assert process.exitcode == (9 if fault == "abnormal_exit" else 0)
            assert not bridge.stopped.is_set()
            if fault == "stopped":
                bridge.stopped.set()
            exits.append(process.exitcode)
        return original_alive(process)

    monkeypatch.setattr(multiprocessing.process.BaseProcess, "is_alive", finish_between_checks)
    receipt = asyncio.run(bridge.execute(call))
    assert exits == [9 if fault == "abnormal_exit" else 0]
    if fault == "completed":
        assert receipt.success, receipt.error
        output = bridge.store.project(context) / "assets/video/final.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
                       capture_output=True, check=True, timeout=20)
        assert asyncio.run(bridge.execute(call)).success
    else:
        assert not receipt.success and receipt.error.code == "outcome_unknown"
    call = repository.get_call("completed-receipt")
    if fault == "completed":
        assert call.status == "receipted" and call.external_job_id == "video-public"
    else:
        assert (call.status, call.external_job_id) in {("outcome_unknown", None), ("receipted", "video-public")}
    assert call.reserved_usd_micros == 500_000 and call.actual_usd_micros is None
    assert repository.get_task(context.task_id).cost.reserved_usd_micros == 500_000
    assert len([request for request in gateway["calls"] if request[0] == "POST"]) == 1
    assert bridge.stop() is True
