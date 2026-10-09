"""A scripted model and deterministic media boundary for real Pi production E2E."""

from copy import deepcopy
import json
from multiprocessing import Manager
from pathlib import Path
import subprocess

from PIL import Image

from tests.contracts.test_phase0_contracts import sample_artifact
from tests.fixtures.studio.model_server import text_item, tool_item
from tools.base_tool import BaseTool, ToolResult
from tools.tool_registry import ToolRegistry
from tools.video.video_compose import VideoCompose


class DeterministicImage(BaseTool):
    name = "newapi_image"
    provider = "controlled-media"
    capability = "image_generation"
    side_effects = ["writes image"]
    input_schema = {"type": "object", "properties": {
        "model": {"type": "string"}, "prompt": {"type": "string"}, "output_path": {"type": "string"}},
        "required": ["model", "prompt", "output_path"]}

    def __init__(self, submissions):
        self.submissions = submissions

    def execute(self, inputs):
        self.submissions.append((self.name, inputs["model"]))
        Image.new("RGB", (1280, 720), "#1c638a").save(inputs["output_path"])
        return ToolResult(success=True, data={"output_path": inputs["output_path"]}, cost_usd=0)


class DeterministicVideo(DeterministicImage):
    name = "newapi_video"
    capability = "video_generation"
    input_schema = {"type": "object", "properties": {
        "model": {"type": "string"}, "prompt": {"type": "string"}, "output_path": {"type": "string"},
        "reference_image_path": {"type": "string"}}, "required": ["model", "prompt", "output_path"]}

    def execute(self, inputs):
        self.submissions.append((self.name, inputs["model"]))
        result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                                 "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                                 "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                                 "-pix_fmt", "yuv420p", inputs["output_path"]], capture_output=True, timeout=20)
        return ToolResult(success=result.returncode == 0, data={"output_path": inputs["output_path"]}, cost_usd=0)


def production_registry():
    registry = ToolRegistry()
    registry.fixture_manager = Manager()
    submissions = registry.fixture_manager.list()
    registry.register(DeterministicImage(submissions))
    registry.register(DeterministicVideo(submissions))
    registry.register(VideoCompose())
    return registry, submissions


def last_tool_receipt(body):
    for message in reversed(body.get("input", [])):
        if message.get("type") != "function_call_output":
            continue
        output = message["output"]
        decoded = json.loads(output) if isinstance(output, str) else output
        if isinstance(decoded, list):
            text = "".join(part.get("text", "") for part in decoded)
            return json.loads(text)
        return decoded
    raise AssertionError("Real Pi did not include the prior tool result")


class ProductionFlow:
    """The model fixture chooses stages; the production worker chooses no stages."""

    def __init__(self, repository, projects_dir, *, revise_script=True):
        self.repository, self.projects_dir = repository, Path(projects_dir)
        self.actions = None
        self.index = 0
        self.revise_script = revise_script
        self.errors = []

    def _plan(self):
        task = self.repository.list_tasks()[0]
        project = self.projects_dir / task.project_id
        image, clip, output = (project / "assets/images/scene-1.png", project / "assets/video/scene-1.mp4",
                               project / "renders/final.mp4")
        artifacts = {name: deepcopy(sample_artifact(name)) for name in (
            "research_brief", "proposal_packet", "script", "scene_plan", "asset_manifest", "edit_decisions")}
        proposal = artifacts["proposal_packet"]
        proposal["production_plan"]["render_runtime"] = "ffmpeg"
        proposal["production_plan"]["renderer_family"] = "animation-first"
        promise = {"promise_type": "motion_led", "motion_required": True, "source_required": False,
                   "tone_mode": "educational", "quality_floor": "presentable"}
        proposal["production_plan"]["delivery_promise"] = promise
        for concept in proposal["concept_options"]:
            concept["target_duration_seconds"] = 2
        proposal["production_plan"]["stages"] = [{"stage": "assets", "tools": [
            {"tool_name": "newapi_image", "role": "scene image", "available": True},
            {"tool_name": "newapi_video", "role": "moving footage", "available": True}], "approach": "Controlled media"}]
        proposal["cost_estimate"] = {"total_estimated_usd": 0, "line_items": [
            {"tool": "newapi_image", "operation": "image", "estimated_usd": 0},
            {"tool": "newapi_video", "operation": "video", "estimated_usd": 0}], "budget_verdict": "within_budget"}
        proposal["approval"]["status"] = "pending"
        artifacts["script"].update(title="Lighthouse", total_duration_seconds=2,
                                   sections=[{"id": "s1", "text": "A lighthouse guides ships.", "start_seconds": 0, "end_seconds": 2}])
        artifacts["scene_plan"]["scenes"] = [{"id": "scene-1", "type": "broll", "description": "Moving lighthouse scene",
                                                "start_seconds": 0, "end_seconds": 2}]
        artifacts["asset_manifest"]["assets"] = [
            {"id": "image-1", "type": "image", "path": str(image), "source_tool": "newapi_image", "scene_id": "scene-1"},
            {"id": "clip-1", "type": "video", "path": str(clip), "source_tool": "newapi_video", "scene_id": "scene-1"}]
        edit = artifacts["edit_decisions"]
        edit.update(render_runtime="ffmpeg", renderer_family="animation-first",
                    cuts=[{"id": "cut-1", "source": "clip-1", "in_seconds": 0, "out_seconds": 2}],
                    metadata={"compose_target": {"width": 1280, "height": 720, "fit": "pad"}, "delivery_promise": promise},
                    audio={"narration": {"enabled": False}}, subtitles={"enabled": False})
        decisions = {"version": "1.0", "project_id": task.project_id, "decisions": [{
            "decision_id": "runtime-initial", "category": "render_runtime_selection", "subject": "Composition runtime",
            "stage": "proposal", "selected": "ffmpeg", "reason": "Fixed, controlled FFmpeg rendering",
            "options_considered": [{"option_id": "ffmpeg", "label": "FFmpeg", "score": 1,
                                    "reason": "Approved supported runtime"}]}]}

        actions = []
        def action(kind, **inputs):
            actions.append((kind, inputs))
        def read(path):
            action("read", path=path)
        def checkpoint(stage, status="awaiting_human", **values):
            action("checkpoint", stage=stage, status=status, artifacts=values, summary=f"Review {stage} for the lighthouse video")
        read("AGENT_GUIDE.md")
        action("catalog")
        read("pipeline_defs/animated-explainer.yaml")
        action("initialize", title="Lighthouse", pipeline_type="animated-explainer")
        read("skills/pipelines/explainer/research-director.md")
        checkpoint("research", "completed", research_brief=artifacts["research_brief"])
        read("skills/pipelines/explainer/proposal-director.md")
        checkpoint("proposal", proposal_packet=proposal, decision_log=decisions)
        read("skills/pipelines/explainer/script-director.md")
        checkpoint("script", script=artifacts["script"])
        if self.revise_script:
            revised = deepcopy(artifacts["script"])
            revised["sections"][0]["text"] = "A blue lighthouse helps ships find the shore."
            checkpoint("script", script=revised)
        read("skills/pipelines/explainer/scene-director.md")
        checkpoint("scene_plan", scene_plan=artifacts["scene_plan"])
        read("skills/pipelines/explainer/asset-director.md")
        read("skills/core/newapi.md")
        action("execute", tool_name="newapi_image", inputs={"model": task.config_snapshot.media_models["image"],
                                                             "prompt": "A blue lighthouse", "output_path": str(image)})
        action("execute", tool_name="newapi_video", inputs={"model": task.config_snapshot.media_models["video"],
                                                             "prompt": "The lighthouse turns", "output_path": str(clip),
                                                             "reference_image_path": str(image)})
        checkpoint("assets", asset_manifest=artifacts["asset_manifest"])
        read("skills/pipelines/explainer/edit-director.md")
        checkpoint("edit", "completed", edit_decisions=edit)
        read("skills/pipelines/explainer/compose-director.md")
        read(".agents/skills/ffmpeg/SKILL.md")
        action("execute", tool_name="video_compose", inputs={"operation": "render", "edit_decisions": edit,
            "asset_manifest": artifacts["asset_manifest"], "proposal_packet": proposal, "output_path": str(output)})
        actions.append(("finish_render", {"output": output}))
        self.actions = actions

    def __call__(self, body):
        if self.actions is None:
            self._plan()
        if self.index and self.actions[self.index - 1][0] == "execute":
            prior = last_tool_receipt(body)
            if prior.get("success") is False:
                self.errors.append(prior)
                return [text_item("The controlled tool failed; stop production.")]
        if self.index >= len(self.actions):
            return [text_item("Production is complete; inspect the canonical compose checkpoint.")]
        action, inputs = self.actions[self.index]
        if action == "finish_render":
            receipt = last_tool_receipt(body)
            if not receipt.get("success"):
                self.errors.append(receipt)
                return [text_item("The fixed render failed; do not report success.")]
            review = receipt["data"]["final_review"]
            inputs = {"stage": "compose", "status": "completed", "artifacts": {
                "render_report": {"version": "1.0", "outputs": [{"path": str(inputs["output"]),
                    "format": "mp4", "resolution": "1280x720", "duration_seconds": 2}]},
                "final_review": review}}
            action = "checkpoint"
        self.index += 1
        return [tool_item("openmontage", {"action": action, "input": inputs}, call_id=f"call_step_{self.index}")]
