"""Capability routing contracts through registry and selector public interfaces."""
import pytest

from tools.base_tool import BaseTool, ToolResult, ToolStatus, ToolStability
from tools.provider_pricing import PriceQuoteRequired
from tools.tool_registry import ToolRegistry


class RoutingProvider(BaseTool):
    stability = ToolStability.BETA
    quality_score = 0.8
    supports = {"text_to_video": True, "image_to_video": True, "image_edit": True, "local_reference_image": True}

    def __init__(self, capability, provider="newapi"):
        self.capability = capability
        self.provider = provider
        self.name = f"routing_{provider}_{capability}"
        self.calls = []
        self.status = ToolStatus.AVAILABLE
        self.operations = {"image_generation": ["generate", "edit", "async", "async_edit"], "video_generation": ["text_to_video", "image_to_video"], "tts": ["speech"]}[capability]
        self.input_schema = {"type": "object", "properties": {name: {} for name in (
            "model", "prompt", "text", "operation", "generation_mode", "request_mode", "provider_params",
            "image_url", "image_path", "mask_path", "reference_image_path", "resume_job", "job_path", "output_path",
            "size", "quality", "response_format", "voice", "voice_id", "format", "speed", "speaking_rate",
        )}}

    def get_status(self):
        return self.status

    def get_info(self):
        return {**super().get_info(), "hosting_provider": self.provider, "model_catalog": {"visible-alias": {"operations": self.operations, "supports_sync": True, "supports_async": True}}}

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired("No deployment quote")

    def execute(self, inputs):
        self.calls.append(dict(inputs))
        if "width" in inputs:
            return ToolResult(success=False, error="This deployment does not support width", cost_usd=None)
        return ToolResult(success=True, data={"received": dict(inputs)}, cost_usd=None)


@pytest.fixture
def routes(monkeypatch):
    registry = ToolRegistry()
    registry.discover()
    monkeypatch.setattr("tools.tool_registry.registry", registry)
    providers = {}
    for capability in ("image_generation", "video_generation", "tts"):
        tool = RoutingProvider(capability)
        registry.register(tool)
        registry.register(RoutingProvider(capability, provider="routing_vendor"))
        providers[capability] = tool
    return registry, providers


def test_newapi_image_requirement_is_preserved_and_rejected_by_provider(routes):
    from tools.graphics.image_selector import ImageSelector
    _, providers = routes
    tool = providers["image_generation"]
    result = ImageSelector().execute({"hosting_provider": "newapi", "preferred_tool": tool.name, "model_name": "visible-alias", "prompt": "rain", "width": 768})
    assert not result.success
    assert tool.calls[0]["width"] == 768


@pytest.mark.parametrize("capability", ["image_generation", "video_generation", "tts"])
def test_conflicting_model_aliases_never_execute_a_provider(routes, capability):
    from tools.graphics.image_selector import ImageSelector
    from tools.video.video_selector import VideoSelector
    from tools.audio.tts_selector import TTSSelector
    selector = {"image_generation": ImageSelector, "video_generation": VideoSelector, "tts": TTSSelector}[capability]()
    _, providers = routes
    tool = providers[capability]
    result = selector.execute({"preferred_tool": tool.name, "prompt": "rain", "text": "rain", "model": "visible-alias", "model_name": "other-alias"})
    assert not result.success and "conflict" in result.error.lower()
    assert tool.calls == []


@pytest.mark.parametrize("capability,alias", [("image_generation", "model_name"), ("video_generation", "model_name"), ("tts", "model_id")])
def test_newapi_model_alias_is_forwarded_as_exact_model(routes, capability, alias):
    from tools.graphics.image_selector import ImageSelector
    from tools.video.video_selector import VideoSelector
    from tools.audio.tts_selector import TTSSelector
    selector = {"image_generation": ImageSelector, "video_generation": VideoSelector, "tts": TTSSelector}[capability]()
    _, providers = routes
    tool = providers[capability]
    result = selector.execute({"hosting_provider": "newapi", "preferred_tool": tool.name, "prompt": "rain", "text": "rain", alias: "visible-alias"})
    assert result.success, result.error
    assert tool.calls[0]["model"] == "visible-alias"
    assert alias not in tool.calls[0]


def test_native_local_video_reference_reaches_provider_without_fal_upload(routes, tmp_path):
    from tools.video.video_selector import VideoSelector
    _, providers = routes
    tool = providers["video_generation"]
    reference = str(tmp_path / "local.png")
    result = VideoSelector().execute({"preferred_tool": tool.name, "operation": "image_to_video", "prompt": "rain", "reference_image_path": reference})
    assert result.success, result.error
    assert tool.calls[0]["reference_image_path"] == reference
    assert "image_url" not in tool.calls[0]


@pytest.mark.parametrize("capability", ["image_generation", "video_generation", "tts"])
def test_rank_respects_provider_allowlist_without_executing(routes, capability):
    from tools.graphics.image_selector import ImageSelector
    from tools.video.video_selector import VideoSelector
    from tools.audio.tts_selector import TTSSelector
    selector = {"image_generation": ImageSelector, "video_generation": VideoSelector, "tts": TTSSelector}[capability]()
    _, providers = routes
    result = selector.execute({"operation": "rank", "allowed_providers": ["newapi"], "prompt": "rain", "text": "rain"})
    assert result.success
    assert result.data["rankings"]
    assert all(item["provider"] == "newapi" for item in result.data["rankings"])
    assert all(tool.calls == [] for tool in providers.values())


def test_image_rank_target_filters_deployment_operations(routes):
    from tools.graphics.image_selector import ImageSelector
    _, providers = routes
    tool = providers["image_generation"]
    tool.operations = ["generate"]
    result = ImageSelector().execute({"preferred_tool": tool.name, "operation": "rank", "target_operation": "edit", "prompt": "rain"})
    assert result.success and result.data["rankings"] == []
    assert tool.calls == []


def test_async_image_resume_routes_by_its_saved_mode_and_operation(routes):
    from tools.graphics.image_selector import ImageSelector
    _, providers = routes
    tool = providers["image_generation"]
    tool.operations = ["async_edit"]
    saved = {"tool": tool.name, "model": "visible-alias", "operation": "edit", "request_mode": "async"}
    result = ImageSelector().execute({"preferred_tool": tool.name, "resume_job": saved})
    assert result.success, result.error
    assert tool.calls[0]["resume_job"] == saved


def test_image_reference_defaults_to_edit_and_keeps_mask_async_and_metadata(routes):
    from tools.graphics.image_selector import ImageSelector
    _, providers = routes
    tool = providers["image_generation"]
    inputs = {"preferred_tool": tool.name, "image_path": "source.png", "mask_path": "mask.png", "request_mode": "async", "provider_params": {"quality": "high"}, "job_path": "saved.job.json", "scene_id": "scene-1", "project_dir": "projects/rain", "prompt": "rain"}
    result = ImageSelector().execute(inputs)
    assert result.success, result.error
    assert tool.calls[0]["generation_mode"] == "edit"
    for field in ("image_path", "mask_path", "request_mode", "provider_params", "job_path", "scene_id", "project_dir"):
        assert tool.calls[0][field] == inputs[field]


def test_rank_context_describes_the_requested_image_operation(routes):
    from tools.graphics.image_selector import ImageSelector
    _, providers = routes
    result = ImageSelector().execute({"operation": "rank", "target_operation": "edit", "preferred_tool": providers["image_generation"].name, "prompt": "rain"})
    assert result.success
    assert result.data["normalized_task_context"]["wants_image_editing"] is True


@pytest.mark.parametrize("capability", ["image_generation", "video_generation", "tts"])
def test_locked_unavailable_gateway_does_not_fall_back_to_vendor(routes, capability):
    from tools.graphics.image_selector import ImageSelector
    from tools.video.video_selector import VideoSelector
    from tools.audio.tts_selector import TTSSelector
    selector = {"image_generation": ImageSelector, "video_generation": VideoSelector, "tts": TTSSelector}[capability]()
    registry, providers = routes
    tool = providers[capability]
    tool.status = ToolStatus.UNAVAILABLE
    vendor = registry.get(f"routing_routing_vendor_{capability}")
    result = selector.execute({"preferred_tool": tool.name, "prompt": "rain", "text": "rain"})
    assert not result.success
    assert tool.calls == [] and vendor.calls == []


def test_invalid_explicit_model_type_never_executes_provider(routes):
    from tools.video.video_selector import VideoSelector
    _, providers = routes
    tool = providers["video_generation"]
    result = VideoSelector().execute({"preferred_tool": tool.name, "model": 0, "prompt": "rain"})
    assert not result.success
    assert tool.calls == []


def test_registry_discovers_all_gateway_capabilities_without_special_registration(routes):
    registry, _ = routes
    for name, capability in (("newapi_llm", "text_generation"), ("newapi_image", "image_generation"), ("newapi_video", "video_generation"), ("newapi_tts", "tts")):
        tool = registry.get(name)
        assert tool is not None and tool.provider == "newapi" and tool.capability == capability
        assert tool in registry.get_by_capability(capability)


def test_selector_schemas_expose_gateway_controls(routes):
    from jsonschema import Draft202012Validator
    from tools.graphics.image_selector import ImageSelector
    from tools.video.video_selector import VideoSelector
    from tools.audio.tts_selector import TTSSelector
    for selector, required in (
        (ImageSelector, {"request_mode", "provider_params", "resume_job", "job_path", "mask_path", "mask_url", "size", "quality"}),
        (VideoSelector, {"provider_params", "resume_job", "job_path", "reference_image_path", "image_url", "size"}),
        (TTSSelector, {"model", "model_name", "model_id", "voice", "voice_id", "format", "output_format", "response_format", "provider_params"}),
    ):
        Draft202012Validator.check_schema(selector.input_schema)
        assert required <= set(selector.input_schema["properties"])


def test_resume_only_is_pinned_to_the_saved_gateway_tool_and_model(routes):
    from tools.graphics.image_selector import ImageSelector
    registry, providers = routes
    tool = providers["image_generation"]
    tool = RoutingProvider("image_generation")
    tool.name = "newapi_image"
    tool.operations = ["async_edit"]
    registry.register(tool)
    vendor = registry.get("routing_routing_vendor_image_generation")
    vendor.quality_score = 1.0
    saved = {"tool": "newapi_image", "model": "visible-alias", "operation": "edit", "request_mode": "async"}
    result = ImageSelector().execute({"resume_job": saved})
    assert result.success, result.error
    assert tool.calls and tool.calls[0]["model"] == "visible-alias"
    assert vendor.calls == []


def test_selector_generate_control_does_not_override_explicit_image_edit_mode(routes):
    from tools.graphics.image_selector import ImageSelector
    _, providers = routes
    tool = providers["image_generation"]
    result = ImageSelector().execute({"preferred_tool": tool.name, "operation": "generate", "generation_mode": "edit", "prompt": "rain", "image_path": "source.png"})
    assert result.success, result.error
    assert tool.calls[0]["generation_mode"] == "edit"
    assert "operation" not in tool.calls[0]


@pytest.mark.parametrize("controls", [
    {"preferred_tool": "routing_routing_vendor_image_generation"}, {"hosting_provider": "routing_vendor"},
    {"allowed_providers": ["routing_vendor"]}, {"model": "different-alias"},
])
def test_resume_route_conflicts_fail_before_any_provider_call(routes, controls):
    from tools.graphics.image_selector import ImageSelector
    registry, providers = routes
    saved = {"tool": "newapi_image", "model": "visible-alias", "operation": "edit", "request_mode": "async"}
    result = ImageSelector().execute({"resume_job": saved, **controls})
    assert not result.success
    assert all(tool.calls == [] for tool in providers.values())
    assert registry.get("routing_routing_vendor_image_generation").calls == []


def test_expired_resume_model_cannot_match_an_unrelated_profile(routes):
    from tools.graphics.image_selector import ImageSelector
    registry, _ = routes
    tool = RoutingProvider("image_generation")
    tool.name = "newapi_image"
    registry.register(tool)
    saved = {"tool": "newapi_image", "model": "expired-alias", "operation": "edit", "request_mode": "async"}
    result = ImageSelector().execute({"resume_job": saved})
    assert not result.success
    assert tool.calls == []
