"""Runtime configuration model for OpenMontage.

Loads config.yaml, merges with env overrides, and provides typed access.
"""

from __future__ import annotations

from enum import Enum
import ipaddress
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import yaml
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
        return self


class NewAPIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    base_url: str = ""
    default_models: dict[str, str] = Field(default_factory=dict)
    models: dict[str, NewAPIModelProfile] = Field(default_factory=dict)
    default_llm_protocol: str = "responses"
    connect_timeout: float = Field(default=10, gt=0)
    read_timeout: float = Field(default=180, gt=0)
    poll_timeout: float = Field(default=600, gt=0)
    poll_interval: float = Field(default=2, gt=0)
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


class OpenMontageConfig(BaseModel):
    """Top-level runtime configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    newapi: NewAPIConfig = Field(default_factory=NewAPIConfig)

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
