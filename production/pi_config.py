"""Generate isolated, credential-blind Pi v1.1.0 configuration from trusted profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping
import yaml

from lib.config_model import OpenMontageConfig, PiProfile, StudioConfig
from pydantic import ValidationError
from production.contracts import ConfigSnapshot, ContractViolation, RunContext, TaskCreate, canonical_sha256

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_studio_config(config_path: Path | None = None, *, environment: Mapping[str, str] | None = None) -> StudioConfig:
    """Defaults < trusted YAML < explicit backend enable/profile environment overrides."""
    environment = os.environ if environment is None else environment
    try:
        runtime = OpenMontageConfig.load(config_path)
        values = runtime.studio.model_dump(mode="json")
        if "single_action_approval_usd_micros" not in runtime.studio.model_fields_set:
            threshold = Decimal(str(runtime.budget.single_action_approval_usd)) * 1_000_000
            if not threshold.is_finite() or threshold < 0 or threshold != threshold.to_integral_value():
                raise ValueError("Action threshold must use nonnegative whole USD micros")
            values["single_action_approval_usd_micros"] = int(threshold)
        if "STUDIO_ENABLED" in environment:
            setting = environment["STUDIO_ENABLED"].lower()
            if setting not in {"true", "false", "1", "0"}:
                raise ValueError("Invalid backend Studio enable flag")
            values["enabled"] = setting in {"true", "1"}
        if environment.get("STUDIO_DEFAULT_PROFILE"):
            values["default_profile"] = environment["STUDIO_DEFAULT_PROFILE"]
        return StudioConfig.model_validate(values)
    except (ValidationError, ValueError, OSError, yaml.YAMLError):
        raise ContractViolation("Trusted Studio configuration is invalid", "profile_unavailable") from None


@dataclass
class ManagedPiConfig:
    agent_dir: Path
    session_root: Path
    work_dir: Path
    argv: list[str]
    limits: dict
    env: dict[str, str] = field(repr=False)
    redact_values: tuple[str, ...] = field(repr=False)


def snapshot_for(profile: PiProfile, request: TaskCreate, *, media_models: Mapping[str, str] | None = None,
                 media_configuration_sha256: str | None = None, single_action_approval_usd_micros: int = 500_000) -> ConfigSnapshot:
    return ConfigSnapshot(
        profile_id=request.profile_id, provider=profile.provider, model=profile.model, api=profile.api,
        configuration_sha256=canonical_sha256(profile.model_dump(mode="json")),
        media_configuration_sha256=media_configuration_sha256,
        budget_usd_micros=request.budget_usd_micros, max_output_tokens=profile.max_output_tokens,
        single_action_approval_usd_micros=single_action_approval_usd_micros,
        max_turns=profile.max_turns, task_timeout_seconds=math.ceil(profile.task_timeout_seconds),
        media_models=dict(media_models or {}), price_status="quoted" if profile.price is not None else "unquoted",
    )


def profile_for_snapshot(config: StudioConfig, snapshot: ConfigSnapshot) -> PiProfile:
    profile = config.profiles.get(snapshot.profile_id)
    if profile is None or canonical_sha256(profile.model_dump(mode="json")) != snapshot.configuration_sha256:
        raise ContractViolation("Trusted profile changed; reconcile the frozen run configuration", "profile_unavailable")
    return profile


def _credentials(profile: PiProfile, environment: Mapping[str, str]) -> dict[str, str]:
    references = {profile.credential_env, *profile.header_env.values()}
    if any(not environment.get(reference) for reference in references):
        raise ContractViolation("Required backend credential reference is not configured", "profile_unavailable")
    if any("\n" in environment[ref] or "\r" in environment[ref] for ref in references):
        raise ContractViolation("Backend credential contains an invalid header value", "profile_unavailable")
    return {reference: environment[reference] for reference in references}


def public_profiles(config: StudioConfig, environment: Mapping[str, str] | None = None) -> dict:
    environment = os.environ if environment is None else environment
    profiles = []
    for profile_id, profile in config.profiles.items():
        try:
            _credentials(profile, environment)
            ready = True
        except ContractViolation:
            ready = False
        profiles.append({
            "profile_id": profile_id, "provider": profile.provider, "model": profile.model, "api": profile.api,
            "input": profile.input, "context_window": profile.context_window,
            "max_output_tokens": profile.max_output_tokens,
            "price_status": "quoted" if profile.price is not None else "unquoted", "ready": ready,
        })
    return {"enabled": config.enabled, "default_profile": config.default_profile,
            "profiles": profiles, "media_models": config.media_models, "narration_default": False}


def estimated_cost_usd_micros(profile: PiProfile, usage: Mapping[str, int]) -> int | None:
    if profile.price is None:
        return None
    rates = profile.price
    total = 0
    for name, rate in (("input", rates.input), ("output", rates.output),
                       ("cacheRead", rates.cache_read), ("cacheWrite", rates.cache_write)):
        count = usage.get(name, 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ContractViolation("Invalid model usage")
        total += count * rate
    return (total + 999_999) // 1_000_000


def _write_private_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_pi(profile: PiProfile, context: RunContext, runtime_root: Path,
               *, environment: Mapping[str, str] | None = None,
               trusted_extension: Path | None = None) -> ManagedPiConfig:
    """Backend-only preparation. PiRPC appends exact --session-dir/--session flags."""
    environment = os.environ if environment is None else environment
    if canonical_sha256(profile.model_dump(mode="json")) != context.config_snapshot.configuration_sha256:
        raise ContractViolation("Run does not match its frozen profile", "profile_unavailable")
    credentials = _credentials(profile, environment)
    prefix = f"{context.task_id}/{context.run_id}"
    if not (context.session.path == prefix + ".jsonl" or context.session.path.startswith(prefix + "/")) or not context.session.path.endswith(".jsonl"):
        raise ContractViolation("Session reference must belong to this exact task/run", "forbidden")
    if profile.max_retries and trusted_extension is None:
        raise ContractViolation("Retries require the guarded provider extension", "profile_unavailable")
    root = runtime_root.resolve()
    agent_dir = root / "agents" / context.task_id / context.run_id
    work_dir = root / "runs" / context.task_id / context.run_id / "work"
    home = work_dir.parent / "home"
    session_root = root / "sessions"
    for directory in (root, agent_dir, work_dir, home, session_root):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    (session_root / context.task_id / context.run_id).mkdir(parents=True, exist_ok=True, mode=0o700)
    model = {
        "id": profile.model, "name": profile.model, "input": profile.input,
        "reasoning": profile.reasoning, "contextWindow": profile.context_window,
        "maxTokens": profile.max_output_tokens,
        "samplingParams": profile.sampling_params,
    }
    if profile.price is not None:
        model["cost"] = {"input": profile.price.input / 1_000_000, "output": profile.price.output / 1_000_000,
                         "cacheRead": profile.price.cache_read / 1_000_000, "cacheWrite": profile.price.cache_write / 1_000_000}
    models = {"providers": {profile.provider: {
        "baseUrl": profile.base_url, "api": "studio-guarded" if trusted_extension is not None else profile.api,
        "apiKey": "${" + profile.credential_env + "}",
        "headers": {header: "${" + ref + "}" for header, ref in profile.header_env.items()}, "models": [model],
    }}}
    settings = {
        "defaultProvider": profile.provider, "defaultModel": profile.model,
        "defaultThinkingLevel": profile.thinking_level, "enabledModels": [f"{profile.provider}/{profile.model}"],
        "defaultTools": ["openmontage"] if trusted_extension is not None else [],
        "defaultProjectTrust": "never", "cacheWarming": "off", "transport": "sse",
        "compaction": {"enabled": trusted_extension is not None}, "branchSummary": {"skipPrompt": True},
        "retry": {"enabled": bool(profile.max_retries), "maxRetries": profile.max_retries,
                  "provider": {"maxRetries": 0, "timeoutMs": int(profile.request_timeout_seconds * 1000)}},
        "httpIdleTimeoutMs": int(profile.idle_timeout_seconds * 1000),
        "enableInstallTelemetry": False, "enableAnalytics": False,
        "packages": [], "extensions": [], "skills": [], "prompts": [], "themes": [],
    }
    _write_private_json(agent_dir / "models.json", models)
    _write_private_json(agent_dir / "settings.json", settings)
    _write_private_json(agent_dir / "auth.json", {})
    node = shutil.which("node", path=environment.get("PATH") or os.environ.get("PATH"))
    cli = REPO_ROOT / ".runtime" / "pi" / "source" / "packages" / "coding-agent" / "dist" / "bundle" / "cli.js"
    if node is None or not cli.is_file():
        raise ContractViolation("Real pinned Pi/Node runtime is not installed; run make studio-pi", "dependency_unavailable")
    env = {"PATH": str(Path(node).parent) + os.pathsep + os.defpath, "HOME": str(home),
           "PI_CODING_AGENT_DIR": str(agent_dir), "PI_OFFLINE": "1",
           "LANG": "en_US.UTF-8", **credentials}
    argv = [node, str(cli), "--mode", "rpc", "--offline", "--no-builtin-tools", "--no-extensions",
            "--no-mcp", "--no-skills", "--no-themes", "--no-context-files", "--no-prompt-templates",
            "--no-approve", "--provider", profile.provider, "--model", profile.model,
            "--thinking", profile.thinking_level]
    if trusted_extension is not None:
        argv.extend(["--extension", str(trusted_extension.resolve(strict=True))])
    limits = {name: getattr(profile, name) for name in (
        "startup_timeout_seconds", "request_timeout_seconds", "idle_timeout_seconds", "task_timeout_seconds", "max_turns", "max_retries",
    )}
    return ManagedPiConfig(agent_dir, session_root, work_dir, argv, limits, env, tuple(credentials.values()))
