"""Runtime configuration model for OpenMontage.

Loads config.yaml, merges with env overrides, and provides typed access.
"""

from __future__ import annotations

from enum import Enum
import ipaddress
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import yaml
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


def normalize_newapi_url(value: str) -> str:
    """Keep a deployment prefix, with exactly one API version suffix."""
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("New API requires an absolute HTTP(S) URL")
    if parts.query or parts.fragment or parts.username is not None or parts.password is not None:
        raise ValueError("New API URL cannot contain credentials, query or fragment")
    try:
        parts.port
    except ValueError:
        raise ValueError("New API URL has an invalid port") from None
    try:
        private = ipaddress.ip_address(parts.hostname).is_private
    except ValueError:
        private = parts.hostname == "localhost" or "." not in parts.hostname or parts.hostname.endswith((".local", ".internal"))
    if parts.scheme == "http" and not private:
        raise ValueError("Use HTTPS for a public New API gateway")
    if any(segment in {".", ".."} for segment in parts.path.split("/")) or "%" in parts.path:
        raise ValueError("New API URL cannot contain encoded or relative path segments")
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


class NewAPIModelProfile(BaseModel):
    """Deployment facts; model names never imply a capability or protocol."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    capabilities: list[str] = Field(default_factory=list)
    protocols: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    supports_sync: bool = True
    supports_async: bool = False
    supported_parameters: list[str] = Field(default_factory=list)
    defaults: dict = Field(default_factory=dict)
    limits: dict[str, dict] = Field(default_factory=dict)
    parameter_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("protocols")
    @classmethod
    def normalize_protocols(cls, value):
        return ["anthropic" if item == "messages" else item for item in value]

    @model_validator(mode="after")
    def validate_parameters(self):
        reserved = {"api_key", "key", "authorization", "headers", "base_url", "url", "endpoint", "model", "protocol", "operation", "stream", "background", "request_mode", "async"}
        if reserved.intersection(self.supported_parameters) or set(self.defaults) - set(self.supported_parameters) or set(self.limits) - set(self.supported_parameters) or reserved.intersection(self.parameter_map.values()):
            raise ValueError("Profile parameters must be declared and cannot contain routing or credentials")
        for name, schema in self.limits.items():
            try:
                Draft202012Validator.check_schema(schema)
                if name in self.defaults:
                    Draft202012Validator(schema).validate(self.defaults[name])
            except (ValidationError, SchemaError):
                raise ValueError(f"Invalid deployment default or parameter schema for {name!r}") from None
        return self


class NewAPIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    base_url: str = ""
    default_models: dict[str, str] = Field(default_factory=dict)
    models: dict[str, NewAPIModelProfile] = Field(default_factory=dict)
    default_llm_protocol: str = "responses"
    connect_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    read_timeout: float = Field(default=180, gt=0, allow_inf_nan=False)
    poll_timeout: float = Field(default=600, gt=0, allow_inf_nan=False)
    poll_interval: float = Field(default=2, gt=0, allow_inf_nan=False)
    get_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        return normalize_newapi_url(value)

    @field_validator("default_llm_protocol")
    @classmethod
    def validate_protocol(cls, value):
        if value not in {"messages", "anthropic", "responses", "auto"}:
            raise ValueError("LLM protocol must be anthropic, responses or auto")
        return "anthropic" if value == "messages" else value


class BudgetMode(str, Enum):
    OBSERVE = "observe"
    WARN = "warn"
    CAP = "cap"


class CheckpointPolicy(str, Enum):
    GUIDED = "guided"
    MANUAL_ALL = "manual_all"
    AUTO_NONCREATIVE = "auto_noncreative"


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096


class BudgetConfig(BaseModel):
    mode: BudgetMode = BudgetMode.WARN
    total_usd: float = 10.0
    reserve_pct: float = 0.10
    single_action_approval_usd: float = 0.50
    require_approval_for_new_paid_tool: bool = True


class CheckpointConfig(BaseModel):
    policy: CheckpointPolicy = CheckpointPolicy.GUIDED
    storage_dir: str = "pipeline"


class OutputConfig(BaseModel):
    default_format: str = "mp4"
    default_codec: str = "libx264"
    default_audio_codec: str = "aac"
    default_resolution: str = "1920x1080"
    default_fps: int = 30
    default_crf: int = 23


class PathsConfig(BaseModel):
    pipeline_dir: str = "pipeline"
    library_dir: str = "library"
    styles_dir: str = "styles"
    skills_dir: str = "skills"
    output_dir: str = "output"


class PiPrice(BaseModel):
    """Estimated integer USD micros per million tokens, not gateway billing."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    input: int = Field(strict=True, ge=0)
    output: int = Field(strict=True, ge=0)
    cache_read: int = Field(strict=True, ge=0)
    cache_write: int = Field(strict=True, ge=0)


class PiProfile(BaseModel):
    """Administrator-selected agent transport; never accepted from task input."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: str = Field(default="xvan", pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    base_url: str = "https://xvan.ai/v1"
    api: Literal["openai-responses", "openai-completions", "anthropic-messages"] = "openai-responses"
    model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=200)
    credential_env: str = Field(default="NEW_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]*$")
    input: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text", "image"], min_length=1)
    context_window: int = Field(default=200_000, ge=32, strict=True)
    max_output_tokens: int = Field(default=16_384, ge=16, strict=True)
    reasoning: bool = True
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"
    sampling_params: dict[str, float] = Field(default_factory=dict)
    header_env: dict[str, str] = Field(default_factory=dict)
    startup_timeout_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    idle_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    task_timeout_seconds: float = Field(default=3600, gt=0, allow_inf_nan=False)
    max_turns: int = Field(default=100, strict=True, ge=1)
    max_retries: int = Field(default=0, strict=True, ge=0, le=5)
    price: PiPrice | None = None

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value):
        normalize_newapi_url(value)  # Reuse credential/path/public-HTTP checks without changing API prefixes.
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_agent_capabilities(self):
        import math
        import re

        if "text" not in self.input or self.max_output_tokens >= self.context_window:
            raise ValueError("Agent requires text input and output tokens below context window")
        protected = {"HOME", "PATH", "LANG", "LC_ALL", "NODE_OPTIONS", "NODE_PATH",
                     "PI_CODING_AGENT_DIR", "PI_OFFLINE", "PI_CACHE_RETENTION"}
        if protected.intersection({self.credential_env, *self.header_env.values()}):
            raise ValueError("Credential references cannot replace isolation environment")
        if not self.reasoning and self.thinking_level != "off":
            raise ValueError("Non-reasoning models require thinking_level=off")
        allowed = {"temperature": (0, 2), "top_p": (0, 1), "top_k": (0, None),
                   "min_p": (0, 1), "frequency_penalty": (-2, 2), "presence_penalty": (-2, 2),
                   "repetition_penalty": (0, None), "seed": (0, None)}
        if self.sampling_params and self.api == "anthropic-messages":
            raise ValueError("sampling_params require an OpenAI-compatible API")
        for name, value in self.sampling_params.items():
            if name not in allowed or not math.isfinite(value):
                raise ValueError("Unsupported sampling parameter")
            low, high = allowed[name]
            if value < low or (high is not None and value > high):
                raise ValueError("Sampling parameter is out of range")
            if name in {"seed", "top_k"} and not value.is_integer():
                raise ValueError("Sampling count/seed must be an integer")
        for header, reference in self.header_env.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", header) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", reference):
                raise ValueError("Headers require safe names and backend environment references")
            if header.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
                raise ValueError("Routing headers cannot be configured")
        return self


class StudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool = False
    default_profile: str = "xvan"
    profiles: dict[str, PiProfile] = Field(default_factory=lambda: {"xvan": PiProfile()})
    concurrency: Literal[1] = 1
    single_action_approval_usd_micros: int = Field(default=500_000, strict=True, ge=0)
    media_models: dict[str, str] = Field(default_factory=lambda: {
        "image": "Images2.5-Flare", "video": "dreamina-seedance-2-5-260628",
    })

    @model_validator(mode="after")
    def validate_profiles(self):
        import re

        if self.default_profile not in self.profiles:
            raise ValueError("Default Studio profile must exist")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name) for name in self.profiles):
            raise ValueError("Invalid Studio profile identifier")
        return self


class OpenMontageConfig(BaseModel):
    """Top-level runtime configuration."""

    model_config = ConfigDict(hide_input_in_errors=True)

    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    newapi: NewAPIConfig = Field(default_factory=NewAPIConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "OpenMontageConfig":
        """Load config from YAML file. Falls back to defaults if file missing."""
        if config_path is None:
            config_path = Path(__file__).resolve().parent.parent / "config.yaml"

        if config_path.exists():
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            return cls.model_validate(raw)

        return cls()

    def resolve_path(self, key: str, project_root: Optional[Path] = None) -> Path:
        """Resolve a relative path from PathsConfig against project root."""
        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent
        value = getattr(self.paths, key)
        return (project_root / value).resolve()
