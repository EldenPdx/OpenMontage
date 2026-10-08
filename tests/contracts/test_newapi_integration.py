"""Offline public registry → selector → gateway → real asset integration."""

from collections import defaultdict
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import ssl
import tempfile
import threading
import wave

from dotenv import dotenv_values
from PIL import Image
import pytest
import yaml


FIXTURES = Path(__file__).parents[1] / "fixtures" / "newapi"
KEY = "integration-gateway-key-only"
IMAGE = json.loads((FIXTURES / "image_result.json").read_text())
PNG = (FIXTURES / "image_sample.png").read_bytes()


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 1600)
    video = tmp_path_factory.mktemp("newapi-integration-media") / "sample.mp4"
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
    return {"png": PNG, "wav": buffer.getvalue(), "mp4": video.read_bytes() if video.exists() else None}


@pytest.fixture
def connected(tmp_path, monkeypatch, media):
    from tools.tool_registry import registry
    from tools.newapi_llm import NewAPILLM
    from tools.graphics.newapi_image import NewAPIImage
    from tools.video.newapi_video import NewAPIVideo
    from tools.audio.newapi_tts import NewAPITTS
    from lib.paths import PROJECTS_DIR, REPO_ROOT

    # Blank existing credentials too: the repository's custom dotenv loader
    # respects present process values, so it cannot restore a vendor credential.
    for name in set(os.environ) | set(dotenv_values(REPO_ROOT / ".env")):
        if re.search(r"KEY|TOKEN|SECRET|CREDENTIAL|BASE_URL", name, re.I):
            monkeypatch.setenv(name, "")
    monkeypatch.setenv("NEW_API_KEY", KEY)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("OPENMONTAGE_ALLOW_NETWORK", "0")
    state = {"calls": [], "overrides": {}, "jobs": {}, "counts": defaultdict(int), "image_statuses": ["in_progress", "completed"], "video_statuses": ["in_progress", "completed"], "media": media}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, reply):
            if reply is None:
                self.close_connection = True
                return
            status, value, *extra = reply
            headers = extra[0] if extra else {}
            body = json.dumps(value).encode() if isinstance(value, dict) else value
            self.send_response(status)
            self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(headers.get("Content-Length", len(body))))
            self.send_header("x-request-id", "integration-request")
            for name, value in headers.items():
                if name not in {"Content-Type", "Content-Length"}:
                    self.send_header(name, str(value))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            content_type = self.headers.get("Content-Type", "")
            files = []
            if raw and content_type.startswith("multipart/"):
                message = BytesParser(policy=default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
                body = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    value = part.get_payload(decode=True)
                    if part.get_filename():
                        files.append((name, part.get_filename(), part.get_content_type(), value))
                    else:
                        body[name] = value.decode()
            else:
                body = json.loads(raw) if raw else None
            state["calls"].append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body, "files": files})
            override = state["overrides"].get((self.command, self.path))
            if override:
                reply = override.pop(0) if len(override) > 1 else override[0]
                self.reply(reply(state["calls"][-1]) if callable(reply) else reply)
                return
            path = self.path.removeprefix("/gateway")
            if path == "/v1/models":
                self.reply((200, json.loads((FIXTURES / "models.json").read_text())))
            elif path in {"/v1/messages", "/v1/responses"}:
                stem = "llm_messages" if path.endswith("messages") else "llm_responses"
                if body.get("stream"):
                    self.reply((200, (FIXTURES / (stem + ".sse")).read_bytes(), {"Content-Type": "text/event-stream"}))
                else:
                    self.reply((200, json.loads((FIXTURES / (stem + ".json")).read_text())))
            elif path == "/v1/audio/speech":
                self.reply((200, media["wav"], {"Content-Type": "audio/wav"}))
            elif self.command == "POST" and path.startswith("/v1/images/"):
                self.reply((200, IMAGE))
            elif self.command == "POST" and (path.startswith("/v1/async/images/") or path == "/v1/videos"):
                kind = "image" if "images/" in path else "video"
                state["counts"][kind] += 1
                identifier = kind + "-" + str(state["counts"][kind])
                state["jobs"][identifier] = {"kind": kind, "statuses": list(state[kind + "_statuses"])}
                self.reply((200, {"id": identifier, "object": "task" if kind == "image" else "video", "status": "queued", "progress": 0}))
            elif self.command == "GET" and path.endswith("/content"):
                self.reply((200, media["mp4"], {"Content-Type": "video/mp4"}))
            elif self.command == "GET" and path.startswith(("/v1/tasks/", "/v1/videos/")):
                identifier = path.rsplit("/", 1)[-1]
                job = state["jobs"][identifier]
                status = job["statuses"].pop(0) if len(job["statuses"]) > 1 else job["statuses"][0]
                reply = {"id": identifier, "object": "task" if job["kind"] == "image" else "video", "status": status, "progress": 100 if status == "completed" else 25}
                if status == "completed" and job["kind"] == "image":
                    reply["result"] = IMAGE
                self.reply((200, reply))
            else:
                self.reply((404, {"error": {"code": "unexpected_route", "message": "Unexpected route"}}))

        do_POST = handle_request
        do_GET = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = f"http://127.0.0.1:{server.server_port}/gateway"
    deployment = yaml.safe_load((FIXTURES / "deployment.yaml").read_text())
    deployment["newapi"].update(base_url=address, poll_timeout=2, poll_interval=0.005, get_retries=1)
    config = tmp_path / "deployment.yaml"
    config.write_text(yaml.safe_dump(deployment))
    previous = [registry.get(name) for name in registry.list_all()]
    registry.clear()
    registry.discover()
    for tool in (NewAPILLM, NewAPIImage, NewAPIVideo, NewAPITTS):
        registry.register(tool(config_path=config))
    # Real canonical filesystem attribution is exercised without replacing
    # the event module or registry. Only this unique test project is removed.
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project_context = tempfile.TemporaryDirectory(prefix="newapi-integration-", dir=PROJECTS_DIR)
    project = Path(project_context.name)
    for category in ("images", "video", "audio"):
        (project / "assets" / category).mkdir(parents=True)
    state.update(registry=registry, config=config, deployment=deployment, project=project, address=address)
    try:
        yield state
    finally:
        registry.clear()
        for tool in previous:
            registry.register(tool)
        project_context.cleanup()
        server.shutdown()
        server.server_close()
        worker.join()


def selector(state, name):
    return state["registry"].get(name)


def refresh(state):
    from tools._newapi.config import load_settings
    from tools._newapi.models import refresh_models
    refresh_models(load_settings(state["config"]))


def assert_safe_result(result):
    assert KEY not in repr(result)
    assert result.cost_usd is None
    assert result.data["cost_status"] == "unquoted"


def test_key_only_discovery_catalog_rank_and_dry_run_are_offline_then_models_refresh_is_explicit(connected):
    from tools._newapi.config import load_settings
    from tools._newapi.models import refresh_models
    from lib.config_model import OpenMontageConfig

    registry = connected["registry"]
    for tool_name, capability in (("newapi_llm", "text_generation"), ("newapi_image", "image_generation"), ("newapi_video", "video_generation"), ("newapi_tts", "tts")):
        tool = registry.get(tool_name)
        assert tool in registry.get_by_capability(capability)
        assert tool.get_status().value == "available"
        assert tool.get_info()["model_catalog"]
        assert tool.dry_run({})["cost_status"] == "quote_required"
    for name in ("image_selector", "video_selector", "tts_selector"):
        result = selector(connected, name).execute({"operation": "rank", "hosting_provider": "newapi", "prompt": "Rain", "text": "Rain"})
        assert result.success, result.error
        assert result.data["rankings"]
    assert connected["calls"] == []
    refresh_models(load_settings(connected["config"]))
    assert [(call["method"], call["path"]) for call in connected["calls"]] == [("GET", "/gateway/v1/models")]
    headers = connected["calls"][0]["headers"]
    assert headers["Authorization"] == "Bearer " + KEY
    assert "anthropic-version" not in headers and "x-api-key" not in headers
    assert OpenMontageConfig().llm.provider == "anthropic"
    assert not os.environ.get("OPENAI_API_KEY") and not os.environ.get("FAL_KEY") and not os.environ.get("OPENAI_BASE_URL")


@pytest.mark.parametrize("protocol,stream", [("anthropic", False), ("anthropic", True), ("responses", False), ("responses", True)])
def test_registry_llm_both_protocols_and_streams_preserve_wire_and_native_content(connected, protocol, stream):
    refresh(connected)
    result = connected["registry"].get("newapi_llm").execute({"protocol": protocol, "system": "Plan a rain scene.", "messages": [{"role": "user", "content": "Rain"}], "temperature": 0, "max_tokens": 64, "stream": stream})
    assert result.success, result.error
    call = connected["calls"][-1]
    operation = "messages" if protocol == "anthropic" else "responses"
    assert call["method"] == "POST" and call["path"] == "/gateway/v1/" + operation
    assert call["headers"]["Authorization"] == "Bearer " + KEY
    content_fields = {"system": "Plan a rain scene.", "messages": [{"role": "user", "content": "Rain"}], "max_tokens": 64} if protocol == "anthropic" else {"instructions": "Plan a rain scene.", "input": [{"role": "user", "content": "Rain"}], "max_output_tokens": 64}
    assert call["body"] == {"model": "deployment-text", "stream": stream, "temperature": 0, **content_fields}
    assert call["headers"].get("anthropic-version") == ("2023-06-01" if protocol == "anthropic" else None)
    assert result.data["text"] == "Rain scene"
    assert result.data["tool_calls"][0]["name"] == "plan"
    assert result.data["usage"] == {"input_tokens": 10, "output_tokens": 12}
    assert result.data["status"] == "completed"
    assert result.data["request_id"] == "integration-request"
    assert_safe_result(result)
    assert len(connected["calls"]) == 2


@pytest.mark.parametrize("mode,operation,local", [("sync", "generate", False), ("async", "generate", False), ("sync", "edit", False), ("async", "edit", False), ("sync", "edit", True), ("async", "edit", True)])
def test_image_selector_four_endpoints_use_declared_json_or_native_multipart(connected, mode, operation, local):
    refresh(connected)
    project = connected["project"]
    source = project / "reference.png"
    source.write_bytes(PNG)
    extra = {}
    if operation == "edit":
        extra = {"image_path": str(source), "mask_path": str(source)} if local else {"image_urls": ["https://media.example/source.png"]}
    output = project / "assets" / "images" / "generated.png"
    result = selector(connected, "image_selector").execute({"hosting_provider": "newapi", "allowed_providers": ["newapi"], "model_name": "deployment-image", "generation_mode": operation, "request_mode": mode, "prompt": "Rain", "output_path": str(output), "scene_id": "scene-1", **extra})
    assert result.success, result.error
    post = connected["calls"][1]
    endpoint = "/gateway/v1/" + ("async/" if mode == "async" else "") + "images/" + ("edits" if operation == "edit" else "generations")
    assert (post["method"], post["path"]) == ("POST", endpoint)
    if local:
        assert post["body"] == {"model": "deployment-image", "prompt": "Rain", "n": "1", "size": "1024x1024"}
        assert post["files"] == [("image[]", "reference.png", "image/png", PNG), ("mask", "reference.png", "image/png", PNG)]
    else:
        assert post["body"] == {"model": "deployment-image", "prompt": "Rain", "n": 1, "size": "1024x1024", **({"image": "https://media.example/source.png"} if operation == "edit" else {})}
    assert all(call["headers"]["Authorization"] == "Bearer " + KEY for call in connected["calls"])
    assert output.read_bytes() == PNG
    with Image.open(output) as image:
        assert image.size == (3, 2)
    assert result.data["selected_tool"] == "newapi_image"
    assert result.artifacts == [str(output)]
    assert_safe_result(result)
    if mode == "async":
        assert [call["path"] for call in connected["calls"][2:]] == ["/gateway/v1/tasks/image-1", "/gateway/v1/tasks/image-1"]
        saved = json.loads(Path(result.data["job_path"]).read_text())
        assert saved["id"] == "image-1" and saved["operation"] == operation
        assert KEY not in json.dumps(saved)
    assert KEY not in (project / "events.jsonl").read_text()


@pytest.mark.parametrize("local", [False, True])
def test_video_selector_json_and_native_reference_produce_probed_authenticated_content(connected, local):
    from tools.video._shared import probe_output
    if connected["media"]["mp4"] is None:
        pytest.skip("FFmpeg is required to build real video fixtures")
    refresh(connected)
    source = connected["project"] / "reference.png"
    source.write_bytes(PNG)
    output = connected["project"] / "assets" / "video" / "clip.mp4"
    result = selector(connected, "video_selector").execute({"hosting_provider": "newapi", "model": "deployment-video", "operation": "image_to_video" if local else "text_to_video", "prompt": "Rain", "duration": 4, "output_path": str(output), **({"reference_image_path": str(source)} if local else {})})
    assert result.success, result.error
    post = connected["calls"][1]
    assert post["path"] == "/gateway/v1/videos" and post["method"] == "POST"
    assert post["body"] == {"model": "deployment-video", "prompt": "Rain", "seconds": "4" if local else 4, "size": "1280x720"}
    assert post["files"] == ([("input_reference", "reference.png", "image/png", PNG)] if local else [])
    assert [(call["method"], call["path"]) for call in connected["calls"][2:]] == [("GET", "/gateway/v1/videos/video-1"), ("GET", "/gateway/v1/videos/video-1"), ("GET", "/gateway/v1/videos/video-1/content")]
    assert all(call["headers"]["Authorization"] == "Bearer " + KEY for call in connected["calls"])
    assert output.read_bytes() == connected["media"]["mp4"]
    probe = probe_output(output)
    assert probe["duration_seconds"] == pytest.approx(0.3)
    assert (probe["video_width"], probe["video_height"]) == (64, 48)
    assert result.data["selected_tool"] == "newapi_video"
    assert not os.environ.get("FAL_KEY")
    assert_safe_result(result)


def test_tts_selector_voice_format_speed_aliases_produce_real_wav(connected):
    from tools.analysis.audio_probe import AudioProbe, probe_duration
    refresh(connected)
    output = connected["project"] / "assets" / "audio" / "voice.wav"
    result = selector(connected, "tts_selector").execute({"hosting_provider": "newapi", "model_name": "deployment-speech", "text": "Rain", "voice_id": "alloy", "format": "wav", "speaking_rate": 0.8, "output_path": str(output)})
    assert result.success, result.error
    assert connected["calls"][-1]["path"] == "/gateway/v1/audio/speech"
    assert connected["calls"][-1]["body"] == {"model": "deployment-speech", "input": "Rain", "voice": "alloy", "response_format": "wav", "speed": 0.8}
    assert output.read_bytes() == connected["media"]["wav"]
    assert probe_duration(output) == pytest.approx(0.2)
    assert AudioProbe().execute({"input_path": str(output)}).data["audio"]["sample_rate"] == 8000
    assert result.data["selected_tool"] == "newapi_tts"
    assert_safe_result(result)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_selector_resume_only_infers_saved_tool_model_and_mode_without_paid_repost(connected, kind):
    if kind == "video" and connected["media"]["mp4"] is None:
        pytest.skip("FFmpeg is required for video integration")
    refresh(connected)
    connected[kind + "_statuses"] = ["queued"]
    output = connected["project"] / "assets" / ("images" if kind == "image" else "video") / ("resume.png" if kind == "image" else "resume.mp4")
    selector_name = kind + "_selector"
    inputs = {"hosting_provider": "newapi", "prompt": "Rain", "output_path": str(output), "poll_timeout": 0.035}
    if kind == "image":
        source = connected["project"] / "reference.png"
        source.write_bytes(PNG)
        inputs.update(generation_mode="edit", image_path=str(source), request_mode="async")
    else:
        inputs["operation"] = "text_to_video"
    pending = selector(connected, selector_name).execute(inputs)
    assert not pending.success
    job = pending.data["resume_job"]
    assert job["id"] == kind + "-1"
    assert Path(str(output) + ".job.json").exists()
    assert not output.exists()
    connected["jobs"][job["id"]]["statuses"] = ["completed"]
    before = len(connected["calls"])
    resumed = selector(connected, selector_name).execute({"resume_job": job})
    assert resumed.success, resumed.error
    assert resumed.data["selected_tool"] == "newapi_" + kind
    assert all(call["method"] == "GET" for call in connected["calls"][before:])
    assert sum(call["method"] == "POST" for call in connected["calls"]) == 1
    assert output.exists()
    assert_safe_result(resumed)


@pytest.mark.parametrize("kind,http,payload,expected", [
    ("image", 500, {"error": {"code": "result_data_unavailable", "message": "Expired"}}, "result_data_unavailable"),
    ("image", 200, {"id": "image-1", "object": "task", "status": "mystery"}, "invalid_response"),
    ("image", 200, {"id": "image-1", "object": "task", "status": "failed", "error": {"code": "task_failed", "message": "Failed"}}, "task_failed"),
    ("video", 200, {"id": "video-1", "object": "video", "status": "mystery"}, "invalid_status"),
    ("video", 200, {"id": "video-1", "object": "video", "status": "failed", "error": {"code": "task_failed", "message": "Failed"}}, "task_failed"),
])
def test_selector_keeps_gateway_task_failures_and_safe_jobs_for_get_only_observation(connected, kind, http, payload, expected):
    refresh(connected)
    route = "/gateway/v1/" + ("tasks/image-1" if kind == "image" else "videos/video-1")
    connected["overrides"][("GET", route)] = [(http, payload)]
    output = connected["project"] / "assets" / ("images" if kind == "image" else "video") / ("failed.png" if kind == "image" else "failed.mp4")
    result = selector(connected, kind + "_selector").execute({"hosting_provider": "newapi", "request_mode": "async", "prompt": "Rain", "output_path": str(output)})
    assert not result.success
    assert result.data["error"]["code"] == expected
    assert result.data["resume_job"]["id"] == kind + "-1"
    observed = selector(connected, kind + "_selector").execute({"resume_job": result.data["resume_job"]})
    assert not observed.success and observed.data["error"]["code"] == expected
    assert sum(call["method"] == "POST" for call in connected["calls"]) == 1
    assert not output.exists()
    assert KEY not in repr(result) and KEY not in repr(observed)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_unknown_submission_without_id_never_retries_or_invents_a_resume_job(connected, kind):
    route = "/gateway/v1/async/images/generations" if kind == "image" else "/gateway/v1/videos"
    connected["overrides"][("POST", route)] = [None]
    output = connected["project"] / "assets" / ("images" if kind == "image" else "video") / ("unknown.png" if kind == "image" else "unknown.mp4")
    result = selector(connected, kind + "_selector").execute({"hosting_provider": "newapi", "request_mode": "async", "prompt": "Rain", "output_path": str(output)})
    assert not result.success
    assert result.data["error"]["outcome_unknown"] is True
    assert "resume_job" not in result.data
    assert [call["method"] for call in connected["calls"]] == ["POST"]
    assert not Path(str(output) + ".job.json").exists()


def test_video_content_gone_preserves_previous_asset_and_resumes_with_only_get(connected):
    output = connected["project"] / "assets" / "video" / "existing.mp4"
    output.write_bytes(connected["media"]["mp4"])
    gone = {"error": {"code": "artifact_gone", "message": "Gone " + KEY + " https://media.example/clip?token=private"}}
    route = "/gateway/v1/videos/video-1/content"
    connected["overrides"][("GET", route)] = [(410, gone)]
    tool = selector(connected, "video_selector")
    failed = tool.execute({"hosting_provider": "newapi", "prompt": "Rain", "output_path": str(output)})
    assert not failed.success and failed.data["error"]["code"] == "artifact_gone"
    assert output.read_bytes() == connected["media"]["mp4"]
    connected["overrides"].pop(("GET", route))
    restored = tool.execute({"resume_job": failed.data["resume_job"]})
    assert restored.success, restored.error
    assert sum(call["method"] == "POST" for call in connected["calls"]) == 1
    assert KEY not in repr(failed) and "token=private" not in repr(failed)


def test_transient_get_429_and_503_retry_within_budget_but_submission_is_once(connected):
    document = yaml.safe_load(connected["config"].read_text())
    document["newapi"]["get_retries"] = 2
    connected["config"].write_text(yaml.safe_dump(document))
    connected["overrides"][("GET", "/gateway/v1/tasks/image-1")] = [(429, {"error": {"code": "temporary", "message": "Wait"}}, {"Retry-After": "0"}), (503, {"error": {"code": "temporary", "message": "Wait"}}, {"Retry-After": "0"}), (200, {"id": "image-1", "object": "task", "status": "completed", "result": IMAGE})]
    result = selector(connected, "image_selector").execute({"hosting_provider": "newapi", "request_mode": "async", "prompt": "Rain", "output_path": str(connected["project"] / "assets" / "images" / "retry.png")})
    assert result.success, result.error
    assert [call["method"] for call in connected["calls"]] == ["POST", "GET", "GET", "GET"]


def test_arbitrary_cross_gateway_and_route_conflicting_jobs_are_rejected_without_http(connected):
    connected["image_statuses"] = ["queued"]
    tool = selector(connected, "image_selector")
    output = connected["project"] / "assets" / "images" / "pending.png"
    original = tool.execute({"hosting_provider": "newapi", "request_mode": "async", "prompt": "Rain", "poll_timeout": 0.035, "output_path": str(output)})
    job = original.data["resume_job"]
    before = len(connected["calls"])
    for changes in ({"base_url": "https://other.example/v1"}, {"url": "https://other.example/tasks/1"}, {"id": "https://other.example/tasks/1"}):
        assert not tool.execute({"resume_job": {**job, **changes}}).success
    assert not tool.execute({"resume_job": job, "preferred_tool": "openai_image"}).success
    assert not tool.execute({"resume_job": job, "hosting_provider": "openai"}).success
    assert len(connected["calls"]) == before


def test_partial_image_and_invalid_tts_remain_diagnostic_and_atomic_through_selectors(connected):
    connected["overrides"][("POST", "/gateway/v1/images/generations")] = [(200, {"data": [IMAGE["data"][0], {"b64_json": "%%bad%%"}, IMAGE["data"][0]]})]
    output = connected["project"] / "assets" / "images" / "partial.png"
    result = selector(connected, "image_selector").execute({"hosting_provider": "newapi", "prompt": "Rain", "output_path": str(output)})
    assert not result.success and result.data["error"]["code"] == "partial_media_failure"
    assert len(result.artifacts) == 2 and result.data["failed_outputs"][0]["index"] == 1
    audio = connected["project"] / "assets" / "audio" / "existing.wav"
    audio.write_bytes(connected["media"]["wav"])
    connected["overrides"][("POST", "/gateway/v1/audio/speech")] = [(200, {"error": {"code": "speech_error", "message": "Failed " + KEY}})]
    failed = selector(connected, "tts_selector").execute({"hosting_provider": "newapi", "text": "Rain", "format": "wav", "output_path": str(audio)})
    assert not failed.success and failed.data["error"]["code"] == "speech_error"
    assert audio.read_bytes() == connected["media"]["wav"]
    assert KEY not in repr(failed)
    assert not list(connected["project"].rglob("*.part"))


def test_strict_gateway_failure_does_not_fall_back_to_an_available_vendor(connected, monkeypatch, caplog):
    import socket
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-other-provider-key")
    assert connected["registry"].get("openai_image").get_status().value == "available"
    attempts = []
    connect = socket.socket.connect

    def observed(socket_, address):
        attempts.append(address)
        return connect(socket_, address)

    monkeypatch.setattr(socket.socket, "connect", observed)
    connected["overrides"][("POST", "/gateway/v1/images/generations")] = [(401, {"error": {"code": "gateway_denied", "message": "Denied " + KEY}})]
    result = selector(connected, "image_selector").execute({"hosting_provider": "newapi", "prompt": "Rain", "output_path": str(connected["project"] / "assets" / "images" / "locked.png")})
    assert not result.success and result.data["error"]["code"] == "gateway_denied"
    assert len(connected["calls"]) == 1
    assert attempts and all(isinstance(address, tuple) and address[0] in {"127.0.0.1", "localhost"} for address in attempts)
    assert KEY not in caplog.text and KEY not in repr(result)


def test_llm_and_real_assets_flow_into_schemas_gated_checkpoints_and_backlot_without_free_costs(connected):
    from backlot.state import load_board_state
    from lib.checkpoint import CheckpointValidationError, write_checkpoint
    from schemas.artifacts import validate_artifact

    project = connected["project"]
    (project / "artifacts").mkdir()
    script = {"version": "1.0", "title": "Synthetic rain script", "total_duration_seconds": 1, "sections": [{"id": "section-1", "text": "Rain falls softly.", "start_seconds": 0, "end_seconds": 1}]}
    native = deepcopy(json.loads((FIXTURES / "llm_responses.json").read_text()))
    native["output"] = [{"type": "message", "id": "script-message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(script)}]}]
    connected["overrides"][("POST", "/gateway/v1/responses")] = [(200, native)]
    text_path = project / "artifacts" / "script.json"
    llm = connected["registry"].get("newapi_llm").execute({"protocol": "responses", "messages": [{"role": "user", "content": "Write a script as JSON."}], "output_path": str(text_path), "scene_id": "scene-1"})
    assert llm.success, llm.error
    parsed = json.loads(llm.data["text"])
    assert parsed == script == json.loads(text_path.read_text())
    validate_artifact("script", parsed)
    image = selector(connected, "image_selector").execute({"hosting_provider": "newapi", "prompt": "Rain", "scene_id": "scene-1", "output_path": str(project / "assets" / "images" / "rain.png")})
    video = selector(connected, "video_selector").execute({"hosting_provider": "newapi", "prompt": "Rain", "scene_id": "scene-1", "output_path": str(project / "assets" / "video" / "rain.mp4")})
    voice = selector(connected, "tts_selector").execute({"hosting_provider": "newapi", "text": parsed["sections"][0]["text"], "format": "wav", "scene_id": "scene-1", "output_path": str(project / "assets" / "audio" / "rain.wav")})
    results = [image, video, voice]
    assert all(result.success for result in results), [result.error for result in results]
    assets = []
    for index, (kind, result) in enumerate(zip(("image", "video", "narration"), results)):
        assert_safe_result(result)
        assets.append({"id": "asset-" + str(index), "type": kind, "path": str(Path(result.data["output"]).relative_to(project)), "source_tool": result.data["selected_tool"], "scene_id": "scene-1", "provider": "newapi", "model": result.data["model"]})
    manifest = {"version": "1.0", "assets": assets, "metadata": {"cost_status": "unquoted", "acceptance": "offline synthetic gateway"}}
    scene_plan = {"version": "1.0", "scenes": [{"id": "scene-1", "type": "generated", "description": "Synthetic rain scene", "start_seconds": 0, "end_seconds": 1, "script_section_id": "section-1"}]}
    validate_artifact("asset_manifest", manifest)
    validate_artifact("scene_plan", scene_plan)
    (project / "artifacts" / "scene_plan.json").write_text(json.dumps(scene_plan))
    (project / "artifacts" / "asset_manifest.json").write_text(json.dumps(manifest))
    # Legacy standalone handoff uses the existing explicit gate. No preceding
    # creative stage is invented or approved just to make an integration pass.
    script_checkpoint = write_checkpoint(project.parent, project.name, "script", "awaiting_human", {"script": parsed}, human_approval_required=True)
    asset_checkpoint = write_checkpoint(project.parent, project.name, "assets", "awaiting_human", {"asset_manifest": manifest}, human_approval_required=True)
    with pytest.raises(CheckpointValidationError, match="GATE VIOLATION"):
        write_checkpoint(project.parent, project.name, "assets", "completed", {"asset_manifest": manifest}, human_approval_required=True)
    for path in (script_checkpoint, asset_checkpoint):
        checkpoint = json.loads(path.read_text())
        assert checkpoint["status"] == "awaiting_human" and checkpoint["human_approved"] is False
    board = load_board_state(project)
    assert board["cost"] is None
    assert board["artifacts"]["script"] == script
    scene = board["storyboard"]["scenes"][0]
    assert scene["narration"] == "Rain falls softly."
    assert len(scene["takes"]) == 2 and len(scene["audio"]) == 1
    assert all(asset["exists"] and asset["cost_usd"] is None for asset in scene["takes"] + scene["audio"])
    assert KEY not in json.dumps(board) and KEY not in (project / "events.jsonl").read_text()
    assert all("cost_usd" not in asset for asset in manifest["assets"])
    assert not any(path.name.startswith("checkpoint_") and json.loads(path.read_text())["status"] == "completed" for path in project.glob("checkpoint_*.json"))


def test_video_content_cross_origin_https_redirect_uses_no_gateway_authentication(connected, tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if openssl is None or connected["media"]["mp4"] is None:
        pytest.skip("OpenSSL and FFmpeg are required for a real local HTTPS redirect")
    certificate, private_key = tmp_path / "localhost.pem", tmp_path / "localhost.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(private_key), "-out", str(certificate), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certificate))
    requests = []

    class CDN(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            body = connected["media"]["mp4"]
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), CDN)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    target = f"https://localhost:{server.server_port}/movie.mp4?token=synthetic-url-secret"
    connected["overrides"][("GET", "/gateway/v1/videos/video-1/content")] = [(302, b"", {"Location": target})]
    output = connected["project"] / "assets" / "video" / "redirect.mp4"
    try:
        result = selector(connected, "video_selector").execute({"hosting_provider": "newapi", "prompt": "Rain", "output_path": str(output)})
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    assert result.success, result.error
    assert output.read_bytes() == connected["media"]["mp4"]
    assert requests[0][0] == "/movie.mp4?token=synthetic-url-secret"
    assert "Authorization" not in requests[0][1]
    assert all(call["headers"]["Authorization"] == "Bearer " + KEY for call in connected["calls"])
    assert KEY not in repr(result) and "synthetic-url-secret" not in repr(result)


def test_selector_download_interruption_keeps_valid_asset_and_resume_never_posts(connected):
    original = connected["media"]["mp4"]
    output = connected["project"] / "assets" / "video" / "interrupted.mp4"
    output.write_bytes(original)
    route = "/gateway/v1/videos/video-1/content"
    connected["overrides"][("GET", route)] = [(200, original[:len(original) // 2], {"Content-Type": "video/mp4", "Content-Length": len(original)})]
    tool = selector(connected, "video_selector")
    failed = tool.execute({"hosting_provider": "newapi", "prompt": "Rain", "output_path": str(output)})
    assert not failed.success
    assert failed.data["generation_status"] == "completed"
    assert failed.data["download_status"] == "failed"
    assert output.read_bytes() == original
    assert not list(output.parent.glob("*.part"))
    connected["overrides"].pop(("GET", route))
    before = len(connected["calls"])
    resumed = tool.execute({"resume_job": failed.data["resume_job"]})
    assert resumed.success, resumed.error
    assert all(call["method"] == "GET" for call in connected["calls"][before:])
    assert sum(call["method"] == "POST" for call in connected["calls"]) == 1
    assert output.read_bytes() == original
