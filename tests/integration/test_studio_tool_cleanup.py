import asyncio
import os
import subprocess

from production.contracts import ToolCall
from tests.integration.test_studio_policy import running
from tests.integration.test_studio_repository import repository, repository_factory
from tools.base_tool import BaseTool, ToolResult, ToolRuntime


def test_completed_render_receipt_survives_disappeared_pid_lookup(repository, tmp_path, monkeypatch):
    from production import recovery

    class LocalRender(BaseTool):
        name, provider, runtime = "cleanup_render", "local", ToolRuntime.LOCAL
        side_effects = ["writes video"]
        input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}, "required": ["output_path"]}

        def execute(self, inputs):
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=0.3",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", inputs["output_path"]], capture_output=True, check=True, timeout=20)
            return ToolResult(success=True, data={"output_path": inputs["output_path"]}, cost_usd=0)

    bridge, context, _ = running(repository, tmp_path)
    bridge.registry.register(LocalRender())
    bridge.allowed_tools = frozenset({"cleanup_render"})
    actual_identity = recovery.process_identity

    def lookup_identity(pid):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            raise subprocess.TimeoutExpired("lsof", 5) from None
        return actual_identity(pid)

    monkeypatch.setattr(recovery, "process_identity", lookup_identity)
    receipt = asyncio.run(bridge.execute(ToolCall(call_id="completed-render", context=context, tool_name="cleanup_render",
                                               inputs={"output_path": "renders/final.mp4"})))
    assert receipt.success, receipt.error
    output = bridge.store.project(context) / "renders/final.mp4"
    assert output.is_file() and output.stat().st_size > 0
    assert repository.get_call("completed-render").status == "settled"
    assert list((tmp_path / "runtime/tool-processes").rglob("*.json")) == []
    assert bridge.stop() is True
