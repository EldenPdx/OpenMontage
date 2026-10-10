"""Video generation through the deployment's New API gateway."""
from tools._newapi.client import redact
from tools._newapi.config import load_settings
from tools._newapi.models import model_catalog
from tools._newapi.videos import execute_video
from tools.base_tool import BaseTool, ToolTier, ToolRuntime, ToolStability, ExecutionMode, ResumeSupport, ResourceProfile, RetryPolicy, ToolStatus
from tools.provider_pricing import PriceQuoteRequired


class NewAPIVideo(BaseTool):
    name = "newapi_video"
    capability = "video_generation"
    provider = "newapi"
    tier = ToolTier.GENERATE
    runtime = ToolRuntime.API
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    resume_support = ResumeSupport.FROM_CHECKPOINT
    dependencies = ["env:NEW_API_KEY", "cmd:ffprobe"]
    install_instructions = "The administrator configures the New API gateway and video profiles; set NEW_API_KEY."
    resource_profile = ResourceProfile(network_required=True)
    retry_policy = RetryPolicy(max_retries=0)
    agent_skills = ["ai-video-gen"]
    side_effects = ["submits a billable gateway video job", "writes video and resume job files"]
    input_schema = {"type": "object", "properties": {
        "prompt": {"type": "string"}, "model": {"type": "string"},
        "operation": {"type": "string"}, "duration": {"type": "integer", "minimum": 1, "maximum": 3600},
        "request_mode": {"type": "string", "enum": ["async"]},
        "size": {"type": "string"}, "seed": {"type": "integer"},
        "seconds": {"oneOf": [{"type": "integer", "minimum": 1, "maximum": 3600}, {"type": "string", "pattern": "^[0-9]+$"}]},
        "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
        "ratio": {"type": "string"}, "aspect_ratio": {"type": "string"}, "resolution": {"type": "string"},
        "generate_audio": {"type": "boolean"}, "negative_prompt": {"type": "string"},
        "image_url": {"type": "string"}, "reference_image_url": {"type": "string"},
        "image_path": {"type": "string"}, "reference_image_path": {"type": "string"},
        "metadata": {"type": "object"}, "provider_options": {"type": "object"},
        "provider_params": {"type": "object"}, "output_path": {"type": "string"},
        "resume_job": {"type": "object"}, "job_path": {"type": "string"},
        "poll_timeout": {"type": "number", "exclusiveMinimum": 0},
        "poll_interval": {"type": "number", "exclusiveMinimum": 0},
    }}

    def __init__(self, config_path=None):
        self.config_path = config_path

    def _catalog(self):
        try:
            settings = load_settings(self.config_path)
            return redact({model: profile for model, profile in model_catalog(settings, self.capability).items() if profile["supports_async"]}, settings.api_key)
        except Exception:
            return {}

    @property
    def capabilities(self):
        return sorted({operation for profile in self._catalog().values() for operation in profile["operations"]})

    @property
    def supports(self):
        return {"local_reference_image": "image_to_video" in self.capabilities, **{operation: True for operation in self.capabilities}}

    def get_status(self):
        try:
            settings = load_settings(self.config_path)
            self.check_dependencies()
            return ToolStatus.AVAILABLE if settings.configured and self.capabilities else ToolStatus.UNAVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def get_info(self):
        info = super().get_info()
        info["hosting_provider"] = "newapi"
        info["model_catalog"] = self._catalog()
        return info

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired("A deployment gateway price quote is required for video generation")

    def execute(self, inputs):
        return execute_video(inputs, self.config_path)
