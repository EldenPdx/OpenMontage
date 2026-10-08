"""Speech through a deployment-configured New API gateway."""

from tools._newapi.client import redact, sanitize_error
from tools._newapi.config import load_settings
from tools._newapi.models import model_catalog
from tools._newapi.speech import synthesize
from tools.base_tool import BaseTool, Determinism, ResourceProfile, ToolResult, ToolRuntime, ToolStatus, ToolTier
from tools.provider_pricing import PriceQuoteRequired


class NewAPITTS(BaseTool):
    name = "newapi_tts"
    capability = "tts"
    provider = hosting_provider = "newapi"
    tier = ToolTier.VOICE
    runtime = ToolRuntime.API
    determinism = Determinism.STOCHASTIC
    dependencies = ["env:NEW_API_KEY", "python:requests", "binary:ffprobe"]
    install_instructions = "Administrator: configure newapi base_url and speech model profile; user: set NEW_API_KEY. Install FFmpeg/ffprobe for media validation."
    capabilities = ["text_to_speech"]
    best_for = ["narration through a deployment-configured gateway"]
    resource_profile = ResourceProfile(network_required=True)
    input_schema = {"type": "object", "required": ["text", "output_path"], "properties": {
        "text": {"type": "string"}, "model": {"type": "string"},
        "model_name": {"type": "string"}, "model_id": {"type": "string"},
        "voice": {"type": "string"}, "voice_id": {"type": "string"},
        "response_format": {"type": "string"}, "format": {"type": "string"},
        "output_format": {"type": "string"}, "speed": {"type": "number"},
        "speaking_rate": {"type": "number"}, "instructions": {"type": "string"},
        "stream": {"type": "boolean", "enum": [False]},
        "stream_format": {"type": "string", "enum": ["audio"]},
        "provider_params": {"type": "object"}, "output_path": {"type": "string"},
    }}

    def __init__(self, config_path=None):
        self.config_path = config_path

    def get_status(self):
        try:
            settings = load_settings(self.config_path)
            self.check_dependencies()
            enabled = any(profile.get("supports_sync") and "speech" in profile.get("operations", []) for profile in model_catalog(settings, self.capability).values())
            return ToolStatus.AVAILABLE if settings.configured and enabled else ToolStatus.UNAVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def get_info(self):
        info = super().get_info()
        try:
            info["model_catalog"] = model_catalog(load_settings(self.config_path), self.capability)
        except Exception as exc:
            info.update(model_catalog={}, configuration_error=str(sanitize_error(None, exc)))
        info["hosting_provider"] = self.hosting_provider
        return info

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired("New API speech pricing is set by the gateway administrator")

    def execute(self, inputs):
        settings = None
        try:
            settings = load_settings(self.config_path)
            self.check_dependencies()
            data = redact(synthesize(settings, inputs), settings.api_key)
            return ToolResult(success=True, data=data, artifacts=[data["output"]], model=data["model"], cost_usd=None)
        except Exception as exc:
            error = sanitize_error(settings, exc)
            return ToolResult(success=False, data={"error": error.as_data(), "cost_status": "unquoted"}, error=str(error), cost_usd=None)
