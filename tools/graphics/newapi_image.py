"""Generate and edit images through a deployment-configured New API gateway."""

from tools.base_tool import BaseTool, ResourceProfile, ResumeSupport, RetryPolicy, ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier
from tools._newapi.client import redact, sanitize_error
from tools._newapi.config import load_settings
from tools._newapi.images import execute_images
from tools._newapi.models import model_catalog
from tools.provider_pricing import PriceQuoteRequired


class NewAPIImage(BaseTool):
    name = "newapi_image"
    version = "0.1.0"
    capability = "image_generation"
    provider = "newapi"
    tier = ToolTier.GENERATE
    runtime = ToolRuntime.API
    stability = ToolStability.BETA
    description = "New API image generation/editing with explicit deployment model contracts"
    best_for = ["gateway image generation", "gateway image editing", "recoverable asynchronous images"]
    dependencies = ["env:NEW_API_KEY", "python:requests", "python:PIL"]
    install_instructions = "Administrator: provision New API address, model profiles and permissions. User: set NEW_API_KEY."
    agent_skills = []
    usage_location = "skills/core/newapi.md"
    resource_profile = ResourceProfile(network_required=True)
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_CHECKPOINT
    side_effects = ["paid image submission", "local image assets", "local task receipt"]
    idempotency_key_fields = ["model", "model_name", "prompt", "generation_mode", "request_mode", "provider_params"]
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "model_name": {"type": "string"},
            "operation": {"type": "string", "enum": ["generate", "edit"]},
            "generation_mode": {"type": "string", "enum": ["generate", "edit"], "default": "generate"},
            "request_mode": {"type": "string", "enum": ["sync", "async"], "default": "sync"},
            "n": {"type": "integer", "minimum": 0, "maximum": 128},
            "size": {"type": "string"},
            "quality": {"type": "string"},
            "response_format": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "resolution": {"type": ["string", "integer"]},
            "aspect_ratio": {"type": "string"},
            "image_path": {"type": "string"},
            "image_paths": {"type": "array", "items": {"type": "string"}},
            "image_url": {"type": "string"},
            "image_urls": {"type": "array", "items": {"type": "string"}},
            "mask_path": {"type": "string"},
            "mask_url": {"type": "string"},
            "stream": {"type": "boolean"},
            "provider_params": {"type": "object"},
            "resume_job": {"type": "object"},
            "job_path": {"type": "string"},
            "output_path": {"type": "string"},
            "poll_timeout": {"type": "number", "exclusiveMinimum": 0},
            "poll_interval": {"type": "number", "exclusiveMinimum": 0},
            "scene_id": {"type": "string"},
            "project_dir": {"type": "string"},
            "task_context": {"type": "object"},
        },
        "additionalProperties": False,
    }

    def __init__(self, config_path=None):
        self.config_path = config_path

    def get_status(self):
        try:
            settings = load_settings(self.config_path)
            usable = any((profile["supports_sync"] and {"generate", "edit"}.intersection(profile["operations"])) or (profile["supports_async"] and {"async", "async_edit"}.intersection(profile["operations"])) for profile in model_catalog(settings, self.capability).values())
            return ToolStatus.AVAILABLE if settings.configured and usable else ToolStatus.UNAVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def get_info(self):
        info = super().get_info()
        info["hosting_provider"] = self.provider
        try:
            settings = load_settings(self.config_path)
            info["model_catalog"] = redact(model_catalog(settings, self.capability), settings.api_key)
        except Exception as exc:
            info["model_catalog"] = {}
            info["configuration_error"] = str(sanitize_error(None, exc))
        return info

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired("New API image pricing requires a deployment quote")

    def execute(self, inputs):
        settings = None
        try:
            from jsonschema import validate
            validate(inputs, self.input_schema)
            settings = load_settings(self.config_path)
            return execute_images(settings, inputs)
        except Exception as exc:
            safe = sanitize_error(settings, exc)
            return ToolResult(success=False, error=str(safe), data={"error": safe.as_data(), "provider": self.provider, "cost_status": "unquoted"}, cost_usd=None)
