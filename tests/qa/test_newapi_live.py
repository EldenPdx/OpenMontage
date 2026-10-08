"""Explicit, billable deployment smoke. Default CI skips every live request."""

import json
import math
import os
import uuid
from pathlib import Path

import pytest

from tools._newapi.client import redact
from tools._newapi.config import load_settings
from tools._newapi.models import refresh_models, resolve_model


pytestmark = pytest.mark.live_api


@pytest.fixture(scope="module")
def live_gateway():
    if os.environ.get("OPENMONTAGE_NEWAPI_LIVE_SMOKE") != "1":
        pytest.skip("Set OPENMONTAGE_NEWAPI_LIVE_SMOKE=1 after deployment preflight")
    settings = load_settings()
    if not settings.configured:
        pytest.skip("New API deployment profile or NEW_API_KEY is missing")
    # This is the only preflight HTTP request; it does not submit or charge a job.
    refresh_models(settings)
    workspace = Path(__file__).resolve().parents[2] / "projects" / ("newapi-live-smoke-" + uuid.uuid4().hex[:12])
    workspace.mkdir(parents=True)
    report = {"kind": "live", "gateway": settings.config.base_url, "models_endpoint": "/v1/models", "models_preflight": "passed", "samples": []}
    try:
        yield settings, workspace, report
    finally:
        target = workspace / "live-report.json"
        target.write_text(json.dumps(redact(report, settings.api_key), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"New API live evidence: {target}")


def test_newapi_live_readonly_models(live_gateway):
    settings, _, report = live_gateway
    assert settings.config.models and report["models_preflight"] == "passed"


@pytest.mark.parametrize("capability,tool_name", [
    ("text_generation", "newapi_llm"),
    ("image_generation", "image_selector"),
    ("video_generation", "video_selector"),
    ("tts", "tts_selector"),
])
def test_newapi_live_minimum_approved_sample(live_gateway, capability, tool_name):
    if os.environ.get("NEW_API_LIVE_COST_APPROVED") != "1":
        pytest.skip("Confirm model permissions, balance and quoted sample costs under the existing budget policy, then set NEW_API_LIVE_COST_APPROVED=1")
    from tools.tool_registry import registry
    from tools.analysis.audio_probe import probe_duration

    settings, workspace, report = live_gateway
    mode = "async" if capability == "video_generation" else "sync"
    candidate = settings.config.models.get(settings.config.default_models.get(capability))
    if capability == "image_generation" and candidate and not candidate.supports_sync and candidate.supports_async:
        mode = "async"
    operation = {"image_generation": "generate", "video_generation": "text_to_video", "tts": "speech"}.get(capability)
    resolved = resolve_model(settings, capability, operation=operation, request_mode=mode)
    profile = resolved.profile
    registry.discover()
    inputs = {"hosting_provider": "newapi", "model": resolved.id}
    if capability == "text_generation":
        inputs = {"model": resolved.id, "messages": [{"role": "user", "content": "Reply with OK."}]}
        token_field = "max_tokens" if resolved.protocol == "anthropic" else "max_output_tokens"
        if token_field in profile.supported_parameters:
            limit = profile.limits.get(token_field, {})
            tokens = min(limit["enum"]) if limit.get("enum") else limit.get("minimum", 16)
            inputs["max_tokens"] = math.ceil(max(1, tokens))
            if "maximum" in limit:
                inputs["max_tokens"] = min(inputs["max_tokens"], math.floor(limit["maximum"]))
    elif capability == "image_generation":
        inputs.update(prompt="A plain blue square.", request_mode=mode, output_path=str(workspace / "assets/images/sample.png"))
        if "n" in profile.supported_parameters:
            limit = profile.limits.get("n", {})
            inputs["n"] = min(limit["enum"]) if limit.get("enum") else max(1, limit.get("minimum", 1))
    elif capability == "video_generation":
        inputs.update(operation="text_to_video", prompt="A blue square moving slowly on a white background.", output_path=str(workspace / "assets/video/sample.mp4"))
        duration_field = profile.parameter_map.get("duration", "duration")
        choices = profile.limits.get(duration_field, {}).get("enum")
        if choices:
            inputs["duration"] = min(choices)
        elif "minimum" in profile.limits.get(duration_field, {}):
            inputs["duration"] = profile.limits[duration_field]["minimum"]
    else:
        fmt = profile.defaults.get("response_format", "mp3")
        inputs.update(text="Hello.", output_path=str(workspace / ("assets/audio/sample." + fmt)))
    result = registry.get(tool_name).execute(inputs)
    endpoint = {
        "text_generation": "/v1/messages" if resolved.protocol == "anthropic" else "/v1/responses",
        "image_generation": "/v1/" + ("async/" if mode == "async" else "") + "images/generations",
        "video_generation": "/v1/videos", "tts": "/v1/audio/speech",
    }[capability]
    sample = {"capability": capability, "tool": tool_name, "model": resolved.id, "success": result.success,
              "endpoint": endpoint, "status": result.data.get("status") or result.data.get("generation_status") or ("completed" if result.success else "failed"),
              "request_id": result.data.get("request_id"), "operation": result.data.get("operation"),
              "protocol": result.data.get("protocol"), "error": result.data.get("error"),
              "task_id": result.data.get("task_id") or result.data.get("resume_job", {}).get("id"),
              "artifacts": result.artifacts, "cost_status": result.data.get("cost_status"),
              "media_checks_passed": None if capability == "text_generation" else False, "verified": False}
    report["samples"].append(sample)
    assert result.success, result.error
    assert result.cost_usd is None and result.data["cost_status"] == "unquoted"
    if capability == "text_generation":
        assert result.data["status"] == "completed" and result.data["text"].strip()
    else:
        assert all(Path(path).is_file() and Path(path).stat().st_size for path in result.artifacts)
        if capability == "image_generation":
            from PIL import Image
            with Image.open(result.data["output"]) as image:
                image.verify()
        elif capability == "video_generation":
            assert result.data["duration_seconds"] > 0 and result.data["video_width"] > 0 and result.data["video_height"] > 0
        elif fmt != "pcm":
            assert probe_duration(result.data["output"]) > 0
        sample["media_checks_passed"] = True
    sample["verified"] = True
