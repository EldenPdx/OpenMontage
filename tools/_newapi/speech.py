"""New API binary speech protocol; independent of the OpenAI SDK."""

from pathlib import Path

from tools._newapi.client import NewAPIClient, NewAPIError
from tools._newapi.models import merge_params, resolve_model
from tools.analysis.audio_probe import AudioProbe, probe_duration


def _alias(inputs, *names):
    values = [inputs[name] for name in names if name in inputs and inputs[name] is not None]
    if values and any(value != values[0] for value in values[1:]):
        raise ValueError("Conflicting speech aliases: " + ", ".join(names))
    return values[0] if values else None


def _validate_audio(path, fmt):
    if fmt == "wav":
        with path.open("rb") as file:
            header = file.read(8)
        if header[:4] == b"RIFF" and int.from_bytes(header[4:8], "little") + 8 > path.stat().st_size:
            raise ValueError("Gateway WAV is truncated")
    probe = AudioProbe().execute({"input_path": str(path)})
    audio = probe.data.get("audio", {})
    expected = {"mp3": "mp3", "wav": "wav", "opus": "ogg", "aac": "aac", "flac": "flac"}
    if not probe.success or not audio or expected[fmt] not in str(probe.data.get("format_name", "")).split(","):
        raise ValueError("Gateway audio does not match the requested format")
    if fmt == "opus" and audio.get("codec") != "opus":
        raise ValueError("Gateway audio is not Opus")


def _validate_pcm(path, pcm):
    with path.open("rb") as file:
        prefix = file.read(16)
    if prefix.startswith((b"RIFF", b"ID3", b"OggS", b"fLaC")) or prefix.lstrip().startswith((b"{", b"[")):
        raise ValueError("Gateway returned a container or error instead of raw PCM")
    if path.stat().st_size % (pcm["channels"] * pcm["sample_width"]):
        raise ValueError("Gateway PCM has a truncated sample frame")


def synthesize(settings, inputs):
    allowed = {"text", "model", "model_name", "model_id", "voice", "voice_id", "response_format", "format", "output_format", "speed", "speaking_rate", "instructions", "provider_params", "output_path", "stream", "stream_format", "operation", "input_type", "scene_id", "project_dir", "task_context", "sample_mode", "preferred_tool", "hosting_provider", "preferred_provider", "allowed_providers"}
    if set(inputs) - allowed:
        raise ValueError("Unsupported speech controls: " + ", ".join(sorted(set(inputs) - allowed)))
    if inputs.get("stream") or inputs.get("stream_format", "audio") != "audio":
        raise ValueError("Speech SSE is not supported; use binary audio")
    if inputs.get("operation", "generate") not in {"generate", "speech"} or inputs.get("input_type", "text") != "text":
        raise ValueError("Speech requires plain-text synthesis")
    text = inputs.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Speech requires non-empty text")
    if not inputs.get("output_path"):
        raise ValueError("Speech requires an explicit project output_path")
    resolved = resolve_model(settings, "tts", model=_alias(inputs, "model", "model_name", "model_id"), operation="speech")
    response_format = _alias(inputs, "response_format", "format", "output_format")
    if response_format is None and "response_format" not in resolved.profile.defaults:
        response_format = "mp3"
    payload = merge_params(resolved, {
        "input": text, "voice": _alias(inputs, "voice", "voice_id"),
        "response_format": response_format,
        "speed": _alias(inputs, "speed", "speaking_rate"), "instructions": inputs.get("instructions"),
    }, inputs.get("provider_params"))
    if payload.get("stream_format", "audio") != "audio":
        raise ValueError("Speech SSE is not supported; use binary audio")
    if not isinstance(payload.get("input"), str) or not payload["input"].strip():
        raise ValueError("Speech requires non-empty text")
    if not isinstance(payload.get("voice"), str) or not payload["voice"].strip():
        raise ValueError("Speech needs a voice in the deployment profile or request")
    fmt = payload["response_format"]
    if fmt not in {"mp3", "wav", "opus", "aac", "flac", "pcm"}:
        raise ValueError("Unsupported speech format")
    pcm = resolved.profile.limits.get("response_format", {}).get("x-pcm", {})
    if fmt == "pcm" and (any(isinstance(pcm.get(key), bool) or not isinstance(pcm.get(key), int) or pcm[key] <= 0 for key in ("sample_rate", "channels", "sample_width")) or pcm["sample_width"] not in {1, 2, 3, 4}):
        raise ValueError("PCM requires explicit sample_rate, channels and sample_width in the deployment profile")
    speed = payload.get("speed", 1)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.25 <= speed <= 4:
        raise ValueError("Speech speed must be between 0.25 and 4")
    if "instructions" in payload and not isinstance(payload["instructions"], str):
        raise ValueError("Speech instructions must be text")
    output_path = Path(inputs["output_path"])
    if output_path.suffix.lower() != "." + fmt:
        raise ValueError("Speech output_path extension must match response_format")
    payload["model"] = resolved.id
    client = NewAPIClient(settings)
    response = client.request("POST", "/v1/audio/speech", json=payload, stream=True)
    request_id = response.headers.get("x-request-id")
    mime = response.headers.get("Content-Type", "").split(";")[0].lower()
    if not (mime.startswith("audio/") or mime in {"application/octet-stream", "application/ogg"}):
        response.close()
        raise NewAPIError("invalid_media", "Gateway speech did not return an audio content type", request_id=request_id)
    validator = (lambda path: _validate_pcm(path, pcm)) if fmt == "pcm" else (lambda path: _validate_audio(path, fmt))
    output = client.write_binary(response, output_path, kind="pcm" if fmt == "pcm" else "audio", validator=validator)
    duration = Path(output).stat().st_size / (pcm["sample_rate"] * pcm["channels"] * pcm["sample_width"]) if fmt == "pcm" else probe_duration(output)
    return {
        "provider": "newapi", "hosting_provider": "newapi", "model": resolved.id,
        "voice": payload.get("voice"), "format": payload.get("response_format"),
        "response_format": payload.get("response_format"), "speed": payload.get("speed"),
        "output": output, "output_path": output,
        "audio_duration_seconds": duration, "cost_status": "unquoted", "request_id": request_id,
        "duration_note": "Duration unavailable from media probe" if duration is None else None,
        **({"pcm": pcm} if fmt == "pcm" else {}),
    }
