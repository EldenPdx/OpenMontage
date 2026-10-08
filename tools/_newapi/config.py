"""Non-secret deployment configuration and one environment credential."""
from dataclasses import dataclass, field
import os
from pathlib import Path

from lib.config_model import LLMConfig, NewAPIConfig, OpenMontageConfig
from lib.env_loader import load_env


@dataclass
class NewAPISettings:
    config: NewAPIConfig
    api_key: str = field(default='', repr=False)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def configured(self):
        return bool(self.config.base_url and self.api_key and self.config.models)


def load_settings(config_path=None):
    path = Path(config_path) if config_path is not None else None
    load_env(path.parent if path else None)
    runtime = OpenMontageConfig.load(path)
    config = runtime.newapi
    override = os.environ.get('NEW_API_BASE_URL')
    if override:
        config = config.model_copy(update={'base_url': NewAPIConfig(base_url=override).base_url})
    return NewAPISettings(config, os.environ.get('NEW_API_KEY', '').strip(), runtime.llm)
