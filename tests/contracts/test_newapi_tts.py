"""Speech wire contracts from xvanai-new 00e4e916 (see speech.json)."""

import io
import json
import subprocess
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from tools.analysis.audio_probe import probe_duration


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 1600)
    state = {"requests": [], "body": audio.getvalue(), "mime": "audio/wav", "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, self.headers.get("Authorization"), body))
            self.send_response(state["status"])
            self.send_header("Content-Type", state["mime"])
            self.send_header("Content-Length", str(state.get("length", len(state["body"]))))
            self.send_header("x-request-id", "speech-safe-request")
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    deployment = yaml.safe_load((Path(__file__).parents[1] / "fixtures/newapi/deployment.yaml").read_text())
    deployment["newapi"]["base_url"] = f"http://127.0.0.1:{server.server_port}/gateway"
    deployment["newapi"]["models"]["deployment-speech"]["defaults"]["response_format"] = "wav"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(deployment))
    monkeypatch.setenv("NEW_API_KEY", "speech-fake-key")
    monkeypatch.delenv("NEW_API_BASE_URL", raising=False)
    state.update(config_path=config_path, deployment=deployment, output=tmp_path / "projects/speech/assets/audio/narration.wav")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_gateway_speech_produces_valid_wav_with_only_gateway_key(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "output_path": str(gateway["output"])})

    assert result.success, result.error
    assert gateway["requests"] == [("/gateway/v1/audio/speech", "Bearer speech-fake-key", {
        "model": "deployment-speech", "input": "Hello.", "voice": "alloy", "response_format": "wav", "speed": 1,
    })]
    assert result.artifacts == [str(gateway["output"])]
    assert result.data["output"] == str(gateway["output"])
    assert result.data["audio_duration_seconds"] == pytest.approx(0.2)
    assert probe_duration(gateway["output"]) == pytest.approx(0.2)
    assert result.cost_usd is None and result.data["cost_status"] == "unquoted"


def test_speech_aliases_and_delivery_instructions_reach_gateway(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    result = NewAPITTS(config_path=gateway["config_path"]).execute({
        "text": "Good morning.", "model_name": "deployment-speech", "voice_id": "narrator",
        "format": "wav", "speaking_rate": 1.25, "instructions": "Speak calmly.",
        "output_path": str(gateway["output"]),
    })

    assert result.success, result.error
    assert gateway["requests"][0][2] == {
        "model": "deployment-speech", "input": "Good morning.", "voice": "narrator",
        "response_format": "wav", "speed": 1.25, "instructions": "Speak calmly.",
    }


@pytest.mark.parametrize("controls", [
    {"voice": "a", "voice_id": "b"},
    {"format": "mp3", "response_format": "wav"},
    {"speed": 1, "speaking_rate": 1.5},
    {"model": "deployment-speech", "model_name": "other"},
    {"speed": 0}, {"speed": True}, {"speed": 5},
    {"format": "unknown"}, {"stream_format": "sse"}, {"stream": True},
    {"pitch": 3}, {"provider_params": {"model": "other"}},
    {"provider_params": {"input": " "}},
])
def test_invalid_or_unsupported_speech_controls_fail_before_billing(gateway, controls):
    from tools.audio.newapi_tts import NewAPITTS

    result = NewAPITTS(config_path=gateway["config_path"]).execute({
        "text": "Hello.", "output_path": str(gateway["output"]), **controls,
    })

    assert not result.success
    assert gateway["requests"] == []
    assert not gateway["output"].exists()


def test_format_mismatch_preserves_existing_asset(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    target = gateway["output"].with_suffix(".mp3")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous-valid-asset")
    result = NewAPITTS(config_path=gateway["config_path"]).execute({
        "text": "Hello.", "response_format": "mp3", "output_path": str(target),
    })

    assert not result.success
    assert target.read_bytes() == b"previous-valid-asset"
    assert len(gateway["requests"]) == 1
    assert not list(target.parent.glob("*.part"))


def test_truncated_wav_cannot_replace_a_previous_asset(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    gateway["body"] = gateway["body"][:80]
    gateway["output"].parent.mkdir(parents=True)
    gateway["output"].write_bytes(b"previous-valid-asset")
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "output_path": str(gateway["output"])})

    assert not result.success
    assert gateway["output"].read_bytes() == b"previous-valid-asset"
    assert len(gateway["requests"]) == 1


def test_pcm_needs_sampling_facts_before_billing(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    profile = gateway["deployment"]["newapi"]["models"]["deployment-speech"]
    profile["limits"]["response_format"]["enum"].append("pcm")
    gateway["config_path"].write_text(yaml.safe_dump(gateway["deployment"]))
    result = NewAPITTS(config_path=gateway["config_path"]).execute({
        "text": "Hello.", "format": "pcm", "output_path": str(gateway["output"].with_suffix(".pcm")),
    })

    assert not result.success
    assert gateway["requests"] == []


def test_pcm_duration_uses_declared_sampling_facts(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    profile = gateway["deployment"]["newapi"]["models"]["deployment-speech"]
    profile["limits"]["response_format"].update(enum=["pcm"], **{"x-pcm": {"sample_rate": 8000, "channels": 1, "sample_width": 2}})
    gateway["config_path"].write_text(yaml.safe_dump(gateway["deployment"]))
    gateway.update(body=b"\0\0" * 1600, mime="application/octet-stream")
    output = gateway["output"].with_suffix(".pcm")
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "format": "pcm", "output_path": str(output)})

    assert result.success, result.error
    assert result.data["audio_duration_seconds"] == pytest.approx(0.2)
    assert output.read_bytes() == gateway["body"]
    assert gateway["requests"][0][2]["response_format"] == "pcm"
    assert "sample_rate" not in gateway["requests"][0][2]


def test_gateway_mp3_is_probed_and_saved_as_mp3(gateway, tmp_path):
    from tools.audio.newapi_tts import NewAPITTS

    source = tmp_path / "source.wav"
    source.write_bytes(gateway["body"])
    mp3 = tmp_path / "source.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), str(mp3)], check=True)
    gateway.update(body=mp3.read_bytes(), mime="audio/mpeg")
    target = gateway["output"].with_suffix(".mp3")
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "format": "mp3", "output_path": str(target)})

    assert result.success, result.error
    assert result.data["format"] == "mp3"
    assert probe_duration(target) > 0
    assert target.read_bytes() == mp3.read_bytes()


@pytest.mark.parametrize("failure", ["json", "empty", "sse", "corrupt", "interrupted"])
def test_speech_errors_never_retry_or_replace_a_previous_asset(gateway, failure):
    from tools.audio.newapi_tts import NewAPITTS

    if failure == "json":
        gateway.update(body=b'{"error":{"code":"invalid_voice","message":"speech-fake-key https://upstream.test/error?token=secret"}}', mime="application/json")
    elif failure == "empty":
        gateway["body"] = b""
    elif failure == "sse":
        gateway.update(body=b'data: {"event":"speech"}\n\n', mime="text/event-stream")
    elif failure == "corrupt":
        gateway["body"] = b"not audio"
    else:
        gateway["length"] = len(gateway["body"]) + 100
    gateway["output"].parent.mkdir(parents=True)
    gateway["output"].write_bytes(b"previous-valid-asset")
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "output_path": str(gateway["output"])})

    assert not result.success
    assert len(gateway["requests"]) == 1
    assert gateway["output"].read_bytes() == b"previous-valid-asset"
    assert not list(gateway["output"].parent.glob("*.part"))
    assert "speech-fake-key" not in str(result) and "token=secret" not in str(result)
    if failure == "interrupted":
        assert result.data["error"]["outcome_unknown"] is True


def test_profile_without_instructions_rejects_them_before_billing(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    gateway["deployment"]["newapi"]["models"]["deployment-speech"]["supported_parameters"].remove("instructions")
    gateway["config_path"].write_text(yaml.safe_dump(gateway["deployment"]))
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "instructions": "Calm", "output_path": str(gateway["output"])})

    assert not result.success and gateway["requests"] == []


def test_speech_discovery_and_dry_run_load_dotenv_without_network(gateway, monkeypatch):
    from tools.audio.newapi_tts import NewAPITTS
    from tools.base_tool import ToolStatus

    monkeypatch.delenv("NEW_API_KEY")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    gateway["config_path"].with_name(".env").write_text("NEW_API_KEY=speech-fake-key\n")
    tool = NewAPITTS(config_path=gateway["config_path"])

    assert tool.get_status() == ToolStatus.AVAILABLE
    assert "deployment-speech" in tool.get_info()["model_catalog"]
    assert tool.dry_run({"text": "Hello."})["estimated_cost_usd"] is None
    assert gateway["requests"] == []


def test_non_audio_content_type_is_not_saved(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    gateway["mime"] = "image/png"
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "output_path": str(gateway["output"])})

    assert not result.success and not gateway["output"].exists()
    assert len(gateway["requests"]) == 1


def test_speech_status_respects_declared_sync_operation(gateway):
    from tools.audio.newapi_tts import NewAPITTS
    from tools.base_tool import ToolStatus

    gateway["deployment"]["newapi"]["models"]["deployment-speech"]["operations"] = []
    gateway["config_path"].write_text(yaml.safe_dump(gateway["deployment"]))

    assert NewAPITTS(config_path=gateway["config_path"]).get_status() == ToolStatus.UNAVAILABLE
    assert gateway["requests"] == []


def test_sync_timeout_is_unknown_and_does_not_retry(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    gateway.update(status=504, mime="application/json", body=b'{"error":{"code":"task_timeout","message":"still running"}}')
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "output_path": str(gateway["output"])})

    assert not result.success
    assert result.data["error"]["outcome_unknown"] is True
    assert len(gateway["requests"]) == 1
    assert "resume_job" not in result.data


@pytest.mark.parametrize("text", ["", " \n", None])
def test_speech_requires_nonempty_text_before_billing(gateway, text):
    from tools.audio.newapi_tts import NewAPITTS

    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": text, "output_path": str(gateway["output"])})

    assert not result.success and gateway["requests"] == []


def test_default_format_obeys_profile_limits_before_billing(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    profile = gateway["deployment"]["newapi"]["models"]["deployment-speech"]
    del profile["defaults"]["response_format"]
    profile["limits"]["response_format"]["enum"] = ["wav"]
    gateway["config_path"].write_text(yaml.safe_dump(gateway["deployment"]))
    result = NewAPITTS(config_path=gateway["config_path"]).execute({"text": "Hello.", "output_path": str(gateway["output"].with_suffix(".mp3"))})

    assert not result.success and gateway["requests"] == []


def test_provider_parameters_cannot_enable_speech_sse(gateway):
    from tools.audio.newapi_tts import NewAPITTS

    profile = gateway["deployment"]["newapi"]["models"]["deployment-speech"]
    profile["supported_parameters"].append("stream_format")
    gateway["config_path"].write_text(yaml.safe_dump(gateway["deployment"]))
    result = NewAPITTS(config_path=gateway["config_path"]).execute({
        "text": "Hello.", "provider_params": {"stream_format": "sse"}, "output_path": str(gateway["output"]),
    })

    assert not result.success and gateway["requests"] == []
