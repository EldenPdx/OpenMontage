"""Public video tool contracts against the audited gateway HTTP protocol."""
import json
import base64
from io import BytesIO
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading

import pytest
import yaml


@pytest.fixture(scope="module")
def mp4(tmp_path_factory):
    path = tmp_path_factory.mktemp("newapi-video") / "sample.mp4"
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for actual media validation")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=0.3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=True,
    )
    return path.read_bytes()


@pytest.fixture
def gateway(tmp_path, monkeypatch, mp4):
    state = {
        "calls": [], "receipt": {"id": "video-public", "object": "video", "status": "queued"},
        "statuses": ["in_progress", "completed"], "status_http": 200,
        "content": mp4, "content_http": 200, "content_type": "video/mp4",
        "content_length": None, "post_http": 200,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, content_type="application/json", length=None):
            body = json.dumps(data).encode() if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if length is None else length))
            if state.get("retry_after") is not None:
                self.send_header("Retry-After", str(state["retry_after"]))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            content_type = self.headers["Content-Type"]
            if content_type.startswith("multipart/"):
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
                )
                payload, files = {}, {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="Content-Disposition")
                    if part.get_filename():
                        files[name] = part.get_payload(decode=True)
                    else:
                        payload[name] = part.get_payload(decode=True).decode()
            else:
                payload, files = json.loads(body), {}
            state["calls"].append(("POST", self.path, dict(self.headers), payload, files))
            if state.get("disconnect_post"):
                self.close_connection = True
                return
            self.reply(state["post_http"], state["receipt"])

        def do_GET(self):
            state["calls"].append(("GET", self.path, dict(self.headers), None, {}))
            if self.path.endswith("/content"):
                if state.get("redirect"):
                    self.send_response(302)
                    self.send_header("Location", state["redirect"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.reply(state["content_http"], state["content"], state["content_type"], state["content_length"])
                return
            if state.get("status_errors"):
                code = state["status_errors"].pop(0)
                self.reply(code, {"error": {"code": "temporary", "message": "Try later"}})
                return
            current = state["statuses"].pop(0) if len(state["statuses"]) > 1 else state["statuses"][0]
            response = {"id": state.get("state_id", "video-public"), "object": "video", "status": current, "progress": 100}
            if current == "failed" or state["status_http"] >= 400:
                response["error"] = {"code": "model_error", "message": "Generation failed"}
            self.reply(state["status_http"], response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = tmp_path / "config.yaml"
    configuration = {"newapi": {
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "default_models": {"video_generation": "gateway-video"},
        "poll_timeout": 2, "poll_interval": 0.001, "get_retries": 0,
        "models": {"gateway-video": {
            "capabilities": ["video_generation"], "operations": ["text_to_video", "image_to_video"],
            "supports_async": True, "supported_parameters": ["prompt", "seconds", "size", "input_reference", "seed"],
            "parameter_map": {"duration": "seconds", "reference_image": "input_reference"},
            "limits": {"seconds": {"type": "number", "minimum": 1, "maximum": 30}},
        }},
    }}
    config_path.write_text(yaml.safe_dump(configuration), encoding="utf-8")
    monkeypatch.setenv("NEW_API_KEY", "fake-video-key")
    monkeypatch.delenv("NEW_API_BASE_URL", raising=False)
    output = tmp_path / "projects" / "video-contract" / "assets" / "video" / "clip.mp4"
    state.update(config_path=config_path, configuration=configuration, output=output)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def execute(state, **inputs):
    from tools.video.newapi_video import NewAPIVideo
    return NewAPIVideo(config_path=state["config_path"]).execute({
        "prompt": "A blue room", "output_path": str(state["output"]), **inputs,
    })


def test_text_video_downloads_authenticated_content_and_persists_public_job(gateway):
    result = execute(gateway, duration=4, size="1280x720", seed=0)
    assert result.success, result.error
    calls = gateway["calls"]
    assert [(call[0], call[1]) for call in calls] == [
        ("POST", "/v1/videos"), ("GET", "/v1/videos/video-public"),
        ("GET", "/v1/videos/video-public"), ("GET", "/v1/videos/video-public/content"),
    ]
    assert calls[0][3] == {"model": "gateway-video", "prompt": "A blue room", "seconds": "4", "size": "1280x720", "seed": 0}
    assert all(call[2]["Authorization"] == "Bearer fake-video-key" for call in calls)
    assert gateway["output"].read_bytes() == gateway["content"]
    assert result.data["video_width"] == 64 and result.data["video_height"] == 48
    assert result.data["duration_seconds"] == pytest.approx(0.3)
    assert result.cost_usd is None and result.data["cost_status"] == "unquoted"
    job_path = Path(str(gateway["output"]) + ".job.json")
    job = json.loads(job_path.read_text())
    assert job["id"] == "video-public" and job["model"] == "gateway-video"
    assert result.data["resume_job"] == job
    assert "fake-video-key" not in job_path.read_text()


def test_image_video_uploads_local_reference_without_another_provider(gateway, tmp_path):
    from PIL import Image
    reference = tmp_path / "reference.png"
    Image.new("RGB", (64, 48), "blue").save(reference)
    result = execute(gateway, operation="image_to_video", reference_image_path=str(reference), duration=4)
    assert result.success, result.error
    post = gateway["calls"][0]
    assert post[2]["Content-Type"].startswith("multipart/form-data;")
    assert post[3] == {"model": "gateway-video", "prompt": "A blue room", "seconds": "4"}
    assert post[4] == {"input_reference": reference.read_bytes()}


def test_non_sora_parameters_follow_deployment_profile_and_preserve_false(gateway):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/newapi/video-wire.json").read_text())
    gateway["configuration"]["newapi"]["models"]["independent-video"] = fixture["non_sora_profile"]
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, model="independent-video", duration=5, aspect_ratio="16:9", generate_audio=False)
    assert result.success, result.error
    assert gateway["calls"][0][3] == fixture["non_sora_request"]


def test_canonical_video_fields_and_declared_provider_options_reach_the_gateway(gateway):
    profile = gateway["configuration"]["newapi"]["models"]["gateway-video"]
    profile.update(
        supported_parameters=["prompt", "duration", "ratio", "resolution", "generate_audio", "seed", "camera_fixed", "watermark", "metadata", "provider_options"],
        parameter_map={}, limits={},
    )
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, duration=5, aspect_ratio="16:9", resolution="720p", generate_audio=False, seed=0,
                     provider_params={"camera_fixed": False, "watermark": True, "provider_options": {"draft": False}, "metadata": {"label": "reference-shot"}})
    assert result.success, result.error
    assert gateway["calls"][0][3] == {
        "model": "gateway-video", "prompt": "A blue room", "duration": 5, "ratio": "16:9", "resolution": "720p",
        "generate_audio": False, "seed": 0, "metadata": {"label": "reference-shot"},
        "provider_options": {"draft": False, "camera_fixed": False, "watermark": True},
    }


def test_declared_structured_image_references_use_json_instead_of_a_multipart_upload(gateway):
    profile = gateway["configuration"]["newapi"]["models"]["gateway-video"]
    profile.update(supported_parameters=["prompt", "duration", "metadata"], parameter_map={}, limits={})
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    content = [{"type": "image_url", "image_url": {"url": "https://media.example/reference.png"}, "role": "first_frame"}]
    result = execute(gateway, operation="image_to_video", duration=5, metadata={"content": content})
    assert result.success, result.error
    assert gateway["calls"][0][3] == {"model": "gateway-video", "prompt": "A blue room", "duration": 5, "metadata": {"content": content}}
    assert gateway["calls"][0][4] == {}


def test_structured_reference_profile_does_not_imply_multipart_upload_support(gateway, tmp_path):
    from PIL import Image
    reference = tmp_path / "reference.png"
    Image.new("RGB", (64, 48), "blue").save(reference)
    profile = gateway["configuration"]["newapi"]["models"]["gateway-video"]
    profile.update(supported_parameters=["prompt", "duration", "metadata"], parameter_map={}, limits={})
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, operation="image_to_video", duration=5, reference_image_path=str(reference))
    assert not result.success
    assert gateway["calls"] == []


def test_native_string_seconds_respects_the_declared_profile_and_round_trips(gateway):
    profile = gateway["configuration"]["newapi"]["models"]["gateway-video"]
    profile["limits"]["seconds"] = {"type": "string", "enum": ["4", "8", "12"]}
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, seconds="4")
    assert result.success, result.error
    assert gateway["calls"][0][3] == {"model": "gateway-video", "prompt": "A blue room", "seconds": "4"}


@pytest.mark.parametrize("parameters", [
    {"unknown_parameter": 1}, {"reference_video_url": "https://example.com/source.mp4"},
    {"duration": 4, "seconds": 5}, {"duration": 0},
    {"provider_params": {"model": "another-model"}},
])
def test_invalid_parameters_fail_before_a_billable_submission(gateway, parameters):
    result = execute(gateway, **parameters)
    assert not result.success
    assert gateway["calls"] == []
    assert result.cost_usd is None


@pytest.mark.parametrize("duration", [4.5, True, -1, 0, 3601, float("inf"), "4.5"])
def test_video_duration_is_bounded_before_submission_even_without_profile_limits(gateway, duration):
    gateway["configuration"]["newapi"]["models"]["gateway-video"].pop("limits")
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, duration=duration)
    assert not result.success
    assert gateway["calls"] == []


@pytest.mark.parametrize("parameters", [
    {"duration": 4, "seconds": 5},
    {"duration": 4, "metadata": {"duration": 5}},
    {"duration": 4, "provider_options": {"seconds": "5"}},
    {"metadata": {"duration": 3601}},
    {"provider_options": {"duration": 1.5}},
    {"resolution": "720p", "metadata": {"resolution": "1080p"}},
    {"generate_audio": False, "provider_options": {"generate_audio": True}},
])
def test_conflicting_billable_video_fields_cannot_bypass_canonical_values(gateway, parameters):
    profile = gateway["configuration"]["newapi"]["models"]["gateway-video"]
    profile.update(supported_parameters=["prompt", "duration", "seconds", "resolution", "generate_audio", "metadata", "provider_options"], parameter_map={}, limits={})
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, **parameters)
    assert not result.success
    assert gateway["calls"] == []


@pytest.mark.parametrize("parameters", [
    {"resolution": 720}, {"generate_audio": "false"}, {"seed": 1.5},
    {"metadata": {"generate_audio": "false"}}, {"provider_options": {"ratio": ["16:9"]}},
])
def test_video_billable_fields_preserve_their_canonical_types(gateway, parameters):
    profile = gateway["configuration"]["newapi"]["models"]["gateway-video"]
    profile.update(supported_parameters=["prompt", "resolution", "generate_audio", "seed", "metadata", "provider_options"], parameter_map={}, limits={})
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway, **parameters)
    assert not result.success
    assert gateway["calls"] == []


def test_poll_timeout_preserves_job_and_resume_only_reads(gateway):
    gateway["statuses"] = ["in_progress"]
    pending = execute(gateway, poll_timeout=0.02)
    assert not pending.success
    assert pending.data["generation_status"] == "pending"
    assert pending.data["download_status"] == "not_started"
    saved = json.loads(Path(str(gateway["output"]) + ".job.json").read_text())
    assert pending.data["resume_job"] == saved
    gateway["statuses"] = ["completed"]
    recovered = execute(gateway, resume_job=saved)
    assert recovered.success, recovered.error
    assert [call[0] for call in gateway["calls"]].count("POST") == 1


def test_unknown_submission_state_preserves_the_known_public_id(gateway):
    gateway["receipt"]["status"] = "unexpected"
    result = execute(gateway)
    assert not result.success
    assert result.data["error"]["code"] == "invalid_status"
    assert result.data["resume_job"]["id"] == "video-public"
    assert [call[0] for call in gateway["calls"]] == ["POST"]


@pytest.mark.parametrize("failure", ["gone", "json", "empty", "corrupt", "truncated"])
def test_content_failure_keeps_valid_output_and_resumes_without_post(gateway, failure, mp4):
    gateway["output"].parent.mkdir(parents=True)
    gateway["output"].write_bytes(mp4)
    if failure == "gone":
        gateway.update(content_http=410, content_type="application/json", content={"error": {"code": "artifact_gone", "message": "Artifact expired"}})
    elif failure == "json":
        gateway.update(content_type="application/json", content={"error": {"code": "upstream_error", "message": "Not a video"}})
    elif failure == "empty":
        gateway["content"] = b""
    elif failure == "corrupt":
        gateway["content"] = b"this is not a movie"
    else:
        gateway["content_length"] = len(mp4) + 30
    result = execute(gateway)
    assert not result.success
    assert result.data["generation_status"] == "completed" and result.data["download_status"] == "failed"
    assert gateway["output"].read_bytes() == mp4
    assert list(gateway["output"].parent.glob("*.part")) == []
    gateway.update(content_http=200, content_type="video/mp4", content=mp4, content_length=None)
    recovered = execute(gateway, resume_job=result.data["resume_job"])
    assert recovered.success, recovered.error
    assert [call[0] for call in gateway["calls"]].count("POST") == 1


@pytest.mark.parametrize("parameters", [
    {"output_path": "https://example.com/clip.mp4"}, {"job_path": "https://example.com/job.json"},
    {"poll_timeout": float("inf")}, {"poll_interval": float("nan")},
])
def test_invalid_local_paths_and_budgets_never_submit(gateway, parameters):
    result = execute(gateway, **parameters)
    assert not result.success
    assert gateway["calls"] == []


def test_public_discovery_is_offline_and_advertises_only_configured_operations(gateway):
    from tools.provider_pricing import PriceQuoteRequired
    from tools.video.newapi_video import NewAPIVideo
    tool = NewAPIVideo(config_path=gateway["config_path"])
    info = tool.get_info()
    assert info["status"] == "available"
    assert info["supports"]["text_to_video"] and info["supports"]["image_to_video"]
    assert info["supports"]["local_reference_image"]
    assert "reference_to_video" not in info["capabilities"]
    assert info["hosting_provider"] == "newapi" and list(info["model_catalog"]) == ["gateway-video"]
    with pytest.raises(PriceQuoteRequired):
        tool.estimate_cost({"prompt": "blue room"})
    assert gateway["calls"] == []
    gateway["configuration"]["newapi"]["models"]["gateway-video"]["operations"] = ["text_to_video"]
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    assert not tool.get_info()["supports"]["local_reference_image"]


@pytest.mark.parametrize("code", [429, 500])
def test_temporary_status_reads_retry_without_repeating_submission(gateway, code):
    gateway["configuration"]["newapi"]["get_retries"] = 1
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    gateway.update(status_errors=[code], retry_after=0)
    result = execute(gateway)
    assert result.success, result.error
    assert [call[0] for call in gateway["calls"]].count("POST") == 1
    assert [call[1] for call in gateway["calls"]].count("/v1/videos/video-public") == 3


@pytest.mark.parametrize("failure", ["failed", "unknown", "mismatched_id", 401, 403, 404, 410])
def test_failed_and_invalid_status_reads_preserve_job_without_content(gateway, failure):
    gateway["configuration"]["newapi"]["get_retries"] = 2
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    if isinstance(failure, int):
        gateway["status_http"] = failure
    elif failure == "mismatched_id":
        gateway["state_id"] = "other-video"
    else:
        gateway["statuses"] = [failure]
    result = execute(gateway)
    assert not result.success
    assert result.data["resume_job"]["id"] == "video-public"
    assert [call[0] for call in gateway["calls"]] == ["POST", "GET"]
    assert not gateway["output"].exists()
    if isinstance(failure, int):
        assert result.data["error"]["status"] == failure


@pytest.mark.parametrize("identifier", [None, 42, "https://evil.example/task", "fake-video-key"])
def test_unusable_submission_id_is_outcome_unknown_without_leaking_credentials(gateway, identifier):
    gateway["receipt"]["id"] = identifier
    result = execute(gateway)
    assert not result.success
    assert result.data["error"]["outcome_unknown"]
    assert "resume_job" not in result.data
    assert "fake-video-key" not in json.dumps(result.data)
    assert not Path(str(gateway["output"]) + ".job.json").exists()
    assert [call[0] for call in gateway["calls"]] == ["POST"]


@pytest.mark.parametrize("reference", ["", "file:///tmp/reference.png", "https://user:password@example.com/ref.png", "data:image/png;base64,bad"])
def test_invalid_reference_urls_are_rejected_before_submission(gateway, reference):
    result = execute(gateway, operation="image_to_video", image_url=reference)
    assert not result.success
    assert gateway["calls"] == []


def test_job_path_cannot_overwrite_the_requested_video(gateway, mp4):
    gateway["output"].parent.mkdir(parents=True)
    gateway["output"].write_bytes(mp4)
    result = execute(gateway, job_path=str(gateway["output"]))
    assert not result.success
    assert gateway["output"].read_bytes() == mp4
    assert gateway["calls"] == []


@pytest.mark.parametrize("mode", ["url", "data"])
def test_reference_urls_and_data_use_the_declared_json_field(gateway, mode):
    from PIL import Image
    image = BytesIO()
    Image.new("RGB", (64, 48), "blue").save(image, format="PNG")
    reference = "https://images.example/ref.png" if mode == "url" else "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    result = execute(gateway, operation="image_to_video", image_url=reference)
    assert result.success, result.error
    assert gateway["calls"][0][3]["input_reference"] == reference
    assert gateway["calls"][0][4] == {}


def test_paid_submission_disconnect_is_never_retried(gateway):
    gateway["disconnect_post"] = True
    gateway["configuration"]["newapi"]["get_retries"] = 2
    gateway["config_path"].write_text(yaml.safe_dump(gateway["configuration"]), encoding="utf-8")
    result = execute(gateway)
    assert not result.success and result.data["error"]["outcome_unknown"]
    assert "resume_job" not in result.data
    assert [call[0] for call in gateway["calls"]] == ["POST"]


@pytest.mark.parametrize("changes", [
    {"base_url": "https://other.example/v1"}, {"tool": "another-tool"}, {"model": "another-model"},
    {"operation": "unsupported"}, {"request_mode": "sync"}, {"id": "https://other.example/job"},
    {"output_path": "/different-output.mp4"}, {"status_url": "https://other.example/status"},
])
def test_invalid_resume_identity_never_sends_another_request(gateway, changes):
    first = execute(gateway)
    assert first.success, first.error
    before = len(gateway["calls"])
    result = execute(gateway, resume_job={**first.data["resume_job"], **changes})
    assert not result.success
    assert len(gateway["calls"]) == before


def test_content_cdn_redirect_does_not_receive_gateway_auth(gateway, monkeypatch, mp4):
    import requests
    gateway["redirect"] = "https://cdn.example/clip.mp4?temporary=token"
    captured = []

    def get(session, url, **kwargs):
        captured.append((url, dict(session.headers), session.auth, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.headers.update({"Content-Type": "video/mp4", "Content-Length": str(len(mp4))})
        response._content = mp4
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "get", get)
    result = execute(gateway)
    assert result.success, result.error
    assert len(captured) == 1
    assert captured[0][0] == gateway["redirect"]
    assert "Authorization" not in captured[0][1] and captured[0][2] is None
    assert "headers" not in captured[0][3]
    assert gateway["calls"][-1][2]["Authorization"] == "Bearer fake-video-key"


def test_failed_submission_receipt_preserves_public_job_without_polling(gateway):
    gateway["receipt"].update(status="failed", error={"code": "model_failed", "message": "Generation failed"})
    result = execute(gateway)
    assert not result.success
    assert result.data["resume_job"]["id"] == "video-public"
    assert result.data["error"]["code"] == "model_failed"
    assert result.data["generation_status"] == "failed" and result.data["download_status"] == "not_started"
    assert json.loads(Path(str(gateway["output"]) + ".job.json").read_text())["id"] == "video-public"
    assert [call[0] for call in gateway["calls"]] == ["POST"]


def test_new_submission_requires_explicit_output_before_charging(gateway, monkeypatch, tmp_path):
    from tools.video.newapi_video import NewAPIVideo
    monkeypatch.chdir(tmp_path)
    result = NewAPIVideo(config_path=gateway["config_path"]).execute({"prompt": "A blue room"})
    assert not result.success
    assert gateway["calls"] == []
    assert not (tmp_path / "newapi_video.mp4").exists()


def test_resume_uses_saved_output_without_requiring_it_again(gateway):
    from tools.video.newapi_video import NewAPIVideo
    first = execute(gateway)
    assert first.success, first.error
    resumed = NewAPIVideo(config_path=gateway["config_path"]).execute({"resume_job": first.data["resume_job"]})
    assert resumed.success, resumed.error
    assert resumed.data["output"] == str(gateway["output"])
    assert [call[0] for call in gateway["calls"]].count("POST") == 1
