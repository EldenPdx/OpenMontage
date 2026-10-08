"""Image public-interface wire contracts, using real HTTP and image decoding."""

from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

from PIL import Image
import pytest
import requests
import yaml


FIXTURES = Path(__file__).parents[1] / "fixtures" / "newapi"
PNG = (FIXTURES / "image_sample.png").read_bytes()
IMAGE_RESULT = json.loads((FIXTURES / "image_result.json").read_text())


@contextmanager
def gateway(replies):
    records = []
    pending = list(replies)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            content_type = self.headers.get("Content-Type", "")
            body = json.loads(raw) if raw and "application/json" in content_type else raw
            records.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
            reply = pending.pop(0) if len(pending) > 1 else pending[0]
            if callable(reply):
                reply = reply(records[-1])
            if reply is None:
                self.close_connection = True
                return
            status, value, *extra = reply
            headers = extra[0] if extra else {}
            data = json.dumps(value).encode() if isinstance(value, dict) else value
            self.send_response(status)
            self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                if name != "Content-Type":
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        do_POST = respond
        do_GET = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.dict(os.environ, {"NEW_API_KEY": "image-test-secret", "NEW_API_BASE_URL": ""}):
            yield f"http://127.0.0.1:{server.server_port}/gateway", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configured_tool(tmp_path, address, profile=None):
    from tools.graphics.newapi_image import NewAPIImage

    image_profile = {
        "capabilities": ["image_generation"],
        "operations": ["generate", "edit", "async", "async_edit"],
        "supports_async": True,
        "supported_parameters": ["prompt", "n", "size", "quality", "response_format", "watermark", "output_compression", "image", "images", "image[]", "mask"],
        "defaults": {"n": 1},
        "limits": {"n": {"type": "integer", "minimum": 1, "maximum": 4}, "images": {"type": "array", "maxItems": 2}},
        "parameter_map": {"json_image": "image", "json_images": "images", "json_mask": "mask", "multipart_image": "image[]", "multipart_mask": "mask"},
    }
    image_profile.update(profile or {})
    path = tmp_path / "deployment.yaml"
    path.write_text(yaml.safe_dump({"newapi": {"base_url": address, "default_models": {"image_generation": "picture-alias"}, "models": {"picture-alias": image_profile}, "poll_interval": 0.005, "poll_timeout": 0.2, "read_timeout": 1, "get_retries": 0}}))
    return NewAPIImage(config_path=path)


def parts(record):
    message = BytesParser(policy=default).parsebytes(("Content-Type: " + record["headers"]["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + record["body"])
    return [(part.get_param("name", header="content-disposition"), part.get_filename(), part.get_content_type(), part.get_payload(decode=True)) for part in message.iter_parts()]


def test_sync_generate_uses_deployment_alias_and_downloads_valid_png(tmp_path):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        output = tmp_path / "projects" / "image-contract" / "assets" / "images" / "rain.png"
        result = tool.execute({"prompt": "A quiet rain scene.", "output_path": str(output), "provider_params": {"watermark": False, "output_compression": 0}})
    assert result.success, result.error
    assert records[0]["path"] == "/gateway/v1/images/generations"
    assert records[0]["method"] == "POST"
    assert records[0]["headers"]["Authorization"] == "Bearer image-test-secret"
    assert records[0]["body"] == {"model": "picture-alias", "prompt": "A quiet rain scene.", "n": 1, "watermark": False, "output_compression": 0}
    assert output.read_bytes() == PNG
    with Image.open(output) as image:
        assert image.size == (3, 2)
    assert result.data["output"] == str(output)
    assert result.data["outputs"] == [str(output)]
    assert result.artifacts == [str(output)]
    assert result.data["operation"] == "generate"
    assert result.data["model"] == "picture-alias"
    assert result.data["revised_prompts"] == ["A quiet rain scene."]
    assert result.cost_usd is None
    assert result.data["cost_status"] == "unquoted"


def test_async_receipt_is_saved_before_poll_and_completion_downloads_result(tmp_path):
    output = tmp_path / "async.png"
    job_path = Path(str(output) + ".job.json")

    def pending(record):
        saved = json.loads(job_path.read_text())
        assert saved["id"] == "task-image-1"
        assert saved["operation"] == "generate"
        assert "image-test-secret" not in job_path.read_text()
        return (200, {"id": "task-image-1", "object": "task", "status": "in_progress"})

    receipt = {"id": "task-image-1", "object": "task", "status": "queued"}
    completed = {"id": "task-image-1", "object": "task", "status": "completed", "result": IMAGE_RESULT}
    with gateway([(200, receipt), pending, (200, completed)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "request_mode": "async", "output_path": str(output)})
    assert result.success, result.error
    assert [(item["method"], item["path"]) for item in records] == [("POST", "/gateway/v1/async/images/generations"), ("GET", "/gateway/v1/tasks/task-image-1"), ("GET", "/gateway/v1/tasks/task-image-1")]
    assert result.data["resume_job"] == json.loads(job_path.read_text())
    assert result.data["request_mode"] == "async"
    assert output.read_bytes() == PNG


def test_local_edit_sends_repeated_multipart_images_and_mask_with_declared_fields(tmp_path):
    first, second, mask = [tmp_path / name for name in ("first.png", "second.png", "mask.png")]
    for path in (first, second, mask):
        path.write_bytes(PNG)
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "generation_mode": "edit", "image_paths": [str(first), str(second)], "mask_path": str(mask), "output_path": str(tmp_path / "edit.png"), "provider_params": {"watermark": False, "output_compression": 0}})
    assert result.success, result.error
    assert records[0]["path"] == "/gateway/v1/images/edits"
    payload = parts(records[0])
    assert [(name, filename, mime, value) for name, filename, mime, value in payload if filename] == [("image[]", "first.png", "image/png", PNG), ("image[]", "second.png", "image/png", PNG), ("mask", "mask.png", "image/png", PNG)]
    assert {name: value.decode() for name, filename, mime, value in payload if not filename} == {"model": "picture-alias", "prompt": "Rain", "n": "1", "watermark": "false", "output_compression": "0"}
    assert str(first).encode() not in records[0]["body"]
    assert result.data["operation"] == "edit"


def test_json_edit_uses_declared_url_array_and_mask_without_uploading(tmp_path):
    refs = ["https://media.example/first.png", "data:image/png;base64," + IMAGE_RESULT["data"][0]["b64_json"]]
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "generation_mode": "edit", "image_urls": refs, "mask_url": refs[1], "output_path": str(tmp_path / "url-edit.png")})
    assert result.success, result.error
    assert records[0]["path"] == "/gateway/v1/images/edits"
    assert records[0]["body"] == {"model": "picture-alias", "prompt": "Rain", "n": 1, "images": refs, "mask": refs[1]}


def test_timeout_can_resume_from_saved_job_with_only_get_requests(tmp_path):
    output = tmp_path / "resume.png"
    complete = False

    def state(record):
        value = {"id": "task-resume", "object": "task", "status": "completed" if complete else "queued"}
        if complete:
            value["result"] = IMAGE_RESULT
        return 200, value

    with gateway([(200, {"id": "task-resume", "object": "task", "status": "queued"}), state]) as (address, records):
        tool = configured_tool(tmp_path, address)
        pending = tool.execute({"prompt": "Rain", "request_mode": "async", "poll_timeout": 0.025, "output_path": str(output)})
        assert not pending.success
        assert pending.data["resume_job"]["id"] == "task-resume"
        assert not output.exists()
        complete = True
        resumed = tool.execute({"job_path": pending.data["job_path"]})
    assert resumed.success, resumed.error
    assert sum(record["method"] == "POST" for record in records) == 1
    assert records[-1]["path"] == "/gateway/v1/tasks/task-resume"
    assert output.read_bytes() == PNG
    assert resumed.data["resume_job"]["base_url"] == address + "/v1"


def test_partial_multi_image_failure_preserves_successes_and_diagnoses_failed_entry(tmp_path):
    response = {"created": 1700000000, "data": [IMAGE_RESULT["data"][0], {"b64_json": "%%%broken%%", "revised_prompt": "Bad payload"}, IMAGE_RESULT["data"][0]], "usage": {"output_tokens": 24}}
    output = tmp_path / "partial.png"
    with gateway([(200, response)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "output_path": str(output)})
    assert not result.success
    assert result.data["error"]["code"] == "partial_media_failure"
    assert result.data["outputs"] == [str(output), str(tmp_path / "partial_3.png")]
    assert result.artifacts == result.data["outputs"]
    assert result.data["images_generated"] == 2
    assert result.data["failed_outputs"][0]["index"] == 1
    assert result.data["usage"] == {"output_tokens": 24}
    assert output.read_bytes() == PNG
    assert not (tmp_path / "partial_2.png").exists()
    assert len(records) == 1


@pytest.mark.parametrize("extra", [
    {"generation_mode": "edit"},
    {"width": 1024},
    {"provider_params": {"n": 129}},
    {"provider_params": {"model": "another-id"}},
    {"model": "picture-alias", "model_name": "different"},
    {"generation_mode": "edit", "operation": "generate"},
    {"stream": True},
])
def test_invalid_or_unsupported_requirements_are_rejected_before_paid_post(tmp_path, extra):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "output_path": str(tmp_path / "invalid.png"), **extra})
    assert not result.success
    assert records == []


def test_zero_image_count_has_the_upstream_paid_default_and_extensions_keep_zero(tmp_path):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "n": 0, "output_path": str(tmp_path / "zero.png"), "provider_params": {"output_compression": 0, "watermark": False}})
    assert result.success, result.error
    assert records[0]["body"]["n"] == 1
    assert records[0]["body"]["output_compression"] == 0
    assert records[0]["body"]["watermark"] is False


@pytest.mark.parametrize("params", [{"n": 129}, {"n": False}, {"n": 1.5}, {"parameters": {"n": 5}}, {"parameters": {"n": 0}}])
def test_provider_parameters_cannot_bypass_paid_count_bounds(tmp_path, params):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        profile = {"supported_parameters": ["prompt", "n", "parameters"], "limits": {"n": {"type": "integer", "minimum": 1, "maximum": 4}}}
        if params.get("n") == 129:
            profile["limits"] = {}
        result = configured_tool(tmp_path, address, profile).execute({"prompt": "Rain", "output_path": str(tmp_path / "bounded.png"), "provider_params": params})
    assert not result.success
    assert not records


@pytest.mark.parametrize("identifier", ["image-test-secret", "https://foreign.example/task", 123, ""])
def test_unsafe_submission_id_never_creates_a_fake_or_secret_resume_job(tmp_path, identifier):
    output = tmp_path / "unsafe.png"
    with gateway([(200, {"id": identifier, "object": "task", "status": "queued"})]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "request_mode": "async", "output_path": str(output)})
    assert not result.success
    assert result.data["error"]["outcome_unknown"] is True
    assert "resume_job" not in result.data
    assert not Path(str(output) + ".job.json").exists()
    assert "image-test-secret" not in repr(result)
    assert len(records) == 1


@pytest.mark.parametrize("extra", [
    {"image_url": "/tmp/local-file.png"},
    {"image_url": "file:///tmp/local-file.png"},
    {"image_url": "https://media.example:bad-port/image.png"},
    {"image_url": "https://user:password@media.example/image.png"},
    {"image_url": "data:image/png;base64,not-base64"},
    {"image_url": "data:image/unknown;base64," + IMAGE_RESULT["data"][0]["b64_json"]},
    {"image_url": "data:image/jpeg;base64," + IMAGE_RESULT["data"][0]["b64_json"]},
    {"provider_params": {"images": [{"url": "https://media.example/image.png"}]}},
    {"provider_params": {"image": ""}},
])
def test_json_references_require_the_declared_string_representation(tmp_path, extra):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "generation_mode": "edit", "output_path": str(tmp_path / "invalid-ref.png"), **extra})
    assert not result.success
    assert records == []


@pytest.mark.parametrize("local", [False, True])
def test_async_edit_uses_the_async_edit_endpoint_for_json_and_multipart(tmp_path, local):
    receipt = {"id": "task-edit", "object": "task", "status": "queued"}
    completed = {"id": "task-edit", "object": "task", "status": "completed", "result": IMAGE_RESULT}
    image = tmp_path / "reference.png"
    image.write_bytes(PNG)
    extra = {"image_path": str(image)} if local else {"image_url": "https://media.example/image.png"}
    with gateway([(200, receipt), (200, completed)]) as (address, records):
        result = configured_tool(tmp_path, address, {"parameter_map": {"json_image": "image", "multipart_image": "image"}}).execute({"prompt": "Rain", "generation_mode": "edit", "request_mode": "async", "output_path": str(tmp_path / "async-edit.png"), **extra})
    assert result.success, result.error
    assert [(record["method"], record["path"]) for record in records] == [("POST", "/gateway/v1/async/images/edits"), ("GET", "/gateway/v1/tasks/task-edit")]
    if local:
        assert [(name, filename) for name, filename, mime, value in parts(records[0]) if filename] == [("image", "reference.png")]
    else:
        assert records[0]["body"] == {"model": "picture-alias", "prompt": "Rain", "n": 1, "image": "https://media.example/image.png"}
    assert result.data["resume_job"]["operation"] == "edit"


@pytest.mark.parametrize("status,body,code", [
    (200, {"id": "task-failure", "object": "task", "status": "failed", "error": {"code": "generation_failed", "message": "Failed image-test-secret"}}, "generation_failed"),
    (200, {"id": "task-failure", "object": "task", "status": "mystery"}, "invalid_response"),
    (500, {"error": {"code": "result_data_unavailable", "message": "Result expired"}}, "result_data_unavailable"),
    (200, {"id": "task-failure", "object": "task", "status": "completed"}, "invalid_response"),
])
def test_failed_unknown_and_expired_tasks_keep_the_job_without_resubmitting(tmp_path, status, body, code):
    output = tmp_path / "failed.png"
    receipt = {"id": "task-failure", "object": "task", "status": "queued"}
    with gateway([(200, receipt), (status, body)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        result = tool.execute({"prompt": "Rain", "request_mode": "async", "output_path": str(output)})
        assert not result.success
        assert result.data["error"]["code"] == code
        assert result.data["resume_job"]["id"] == "task-failure"
        again = tool.execute({"resume_job": result.data["resume_job"]})
    assert not again.success
    assert [(record["method"], record["path"]) for record in records] == [("POST", "/gateway/v1/async/images/generations"), ("GET", "/gateway/v1/tasks/task-failure"), ("GET", "/gateway/v1/tasks/task-failure")]
    assert not output.exists()
    assert "image-test-secret" not in repr(result)


@pytest.mark.parametrize("response", [
    {"data": []},
    {"data": [{"revised_prompt": "No media"}]},
    {"data": [{"b64_json": "%%%"}]},
    {"data": [{"b64_json": "bm90LWFuLWltYWdl"}]},
    {"data": [None]},
])
def test_invalid_image_response_does_not_replace_an_existing_valid_asset(tmp_path, response):
    output = tmp_path / "previous.png"
    output.write_bytes(PNG)
    with gateway([(200, response)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "output_path": str(output)})
    assert not result.success
    assert output.read_bytes() == PNG
    assert not list(tmp_path.glob("*.part"))
    assert result.artifacts == []
    assert len(records) == 1


def test_object_and_payloadless_data_preserve_actual_image_count_and_redact_echoes(tmp_path):
    image = {**IMAGE_RESULT["data"][0], "url": "https://cdn.example/unused.png", "revised_prompt": "Rain image-test-secret"}
    with gateway([(200, {"created": 1700000000, "data": image}), (200, {"data": [{"revised_prompt": "Only text"}, image]})]) as (address, records):
        tool = configured_tool(tmp_path, address)
        single = tool.execute({"prompt": "Rain", "output_path": str(tmp_path / "single.png")})
        mixed = tool.execute({"prompt": "Rain", "n": 4, "output_path": str(tmp_path / "mixed.png")})
    assert single.success and mixed.success
    assert single.data["images_generated"] == mixed.data["images_generated"] == 1
    assert mixed.data["outputs"] == [str(tmp_path / "mixed_2.png")]
    assert mixed.data["skipped_entries"] == 1
    assert "image-test-secret" not in repr(single)
    assert len(records) == 2


@pytest.mark.parametrize("reply", [None, (504, {"error": {"code": "task_timeout", "message": "Still running"}}), (200, b"not-json")])
def test_sync_submission_interruptions_have_unknown_outcome_and_no_retry(tmp_path, reply):
    with gateway([reply]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "output_path": str(tmp_path / "unknown.png")})
    assert not result.success
    assert result.data["error"]["outcome_unknown"] is True
    assert "resume_job" not in result.data
    assert len(records) == 1


@pytest.mark.parametrize("extra", [{"poll_timeout": float("inf")}, {"poll_timeout": float("nan")}, {"poll_interval": float("inf")}, {"poll_interval": float("nan")}])
def test_non_finite_wait_options_are_rejected_before_submission(tmp_path, extra):
    receipt = {"id": "task-finite", "object": "task", "status": "queued"}
    complete = {"id": "task-finite", "object": "task", "status": "completed", "result": IMAGE_RESULT}
    with gateway([(200, receipt), (200, complete)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "request_mode": "async", "output_path": str(tmp_path / "finite.png"), **extra})
    assert not result.success
    assert records == []


@pytest.mark.parametrize("nested", [{"size": "9999x9999"}, {"model": "different"}, {"stream": True}, {"url": "https://other.example/route"}])
def test_native_parameter_container_cannot_bypass_profile_or_transport_limits(tmp_path, nested):
    profile = {"supported_parameters": ["prompt", "n", "size", "parameters"], "limits": {"size": {"enum": ["1024x1024"]}}}
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address, profile).execute({"prompt": "Rain", "output_path": str(tmp_path / "native.png"), "provider_params": {"parameters": nested}})
    assert not result.success
    assert records == []


@pytest.mark.parametrize("profile", [{"operations": []}, {"supports_sync": False, "supports_async": False}])
def test_profiles_without_a_usable_image_operation_are_unavailable_offline(tmp_path, profile):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        tool = configured_tool(tmp_path, address, profile)
        assert tool.get_status().value == "unavailable"
        assert tool.get_info()["status"] == "unavailable"
    assert records == []


def test_failed_submission_receipt_is_saved_and_can_be_observed_without_repost(tmp_path):
    failed = {"id": "task-immediate-failure", "object": "task", "status": "failed", "error": {"code": "generation_failed", "message": "Failed image-test-secret"}}
    output = tmp_path / "immediate.png"
    with gateway([(200, failed)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        result = tool.execute({"prompt": "Rain", "request_mode": "async", "output_path": str(output)})
        assert not result.success
        assert result.data["resume_job"]["id"] == "task-immediate-failure"
        assert json.loads(Path(result.data["job_path"]).read_text())["id"] == "task-immediate-failure"
        again = tool.execute({"resume_job": result.data["resume_job"]})
    assert not again.success
    assert result.data["error"]["code"] == "generation_failed"
    assert [record["method"] for record in records] == ["POST", "GET"]
    assert "image-test-secret" not in repr(result)


def test_event_context_remains_local_and_resume_accepts_new_scene_context(tmp_path, monkeypatch):
    from lib import events
    monkeypatch.setattr(events, "PROJECTS_DIR", tmp_path / "projects")
    receipt = {"id": "task-context", "object": "task", "status": "queued"}
    completed = {"id": "task-context", "object": "task", "status": "completed", "result": IMAGE_RESULT}
    project = tmp_path / "projects" / "scene-context"
    project.mkdir(parents=True)
    output = project / "assets" / "images" / "rain.png"
    local = {"scene_id": "scene-1", "project_dir": str(project), "task_context": {"purpose": "storytelling"}}
    with gateway([(200, receipt), (200, completed)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        result = tool.execute({"prompt": "Rain", "request_mode": "async", "output_path": str(output), **local})
        assert result.success, result.error
        resumed = tool.execute({"resume_job": result.data["resume_job"], **{**local, "scene_id": "scene-2"}})
    assert resumed.success, resumed.error
    assert "scene_id" not in records[0]["body"]
    events = (project / "events.jsonl").read_text()
    assert "scene-1" in events and "scene-2" in events
    assert "image-test-secret" not in events


def test_authenticated_task_download_failure_resumes_without_post_and_cdn_has_no_key(tmp_path, monkeypatch):
    url = "https://cdn.example/image.png?token=private"
    image_result = {"created": 1700000000, "data": [{"url": url, "revised_prompt": "Rain"}]}
    receipt = {"id": "task-download", "object": "task", "status": "queued"}
    completed = {"id": "task-download", "object": "task", "status": "completed", "result": image_result}
    output = tmp_path / "download.png"
    Image.new("RGB", (3, 2), (2, 3, 4)).save(output)
    previous = output.read_bytes()
    external = []
    send = requests.Session.send

    def transport(session, request, **kwargs):
        if request.url != url:
            return send(session, request, **kwargs)
        external.append(dict(request.headers))
        response = requests.Response()
        response.status_code = 200
        response._content = PNG
        response._content_consumed = True
        response.headers = {"Content-Type": "image/png", "Content-Length": str(len(PNG))}
        if len(external) == 1:
            def interrupted(**kwargs):
                yield PNG[:8]
                raise requests.ConnectionError("Download interrupted image-test-secret")
            response.iter_content = interrupted
        return response

    monkeypatch.setattr(requests.Session, "send", transport)
    with gateway([(200, receipt), (200, completed)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        failed = tool.execute({"prompt": "Rain", "request_mode": "async", "output_path": str(output)})
        assert not failed.success
        assert failed.data["generation_completed"] is True
        assert output.read_bytes() == previous
        assert not list(tmp_path.glob("*.part"))
        resumed = tool.execute({"resume_job": failed.data["resume_job"]})
    assert resumed.success, resumed.error
    assert output.read_bytes() == PNG
    assert [record["method"] for record in records] == ["POST", "GET", "GET"]
    assert all("Authorization" not in headers for headers in external)
    assert "image-test-secret" not in repr(failed)
    assert url not in json.dumps(failed.data["resume_job"])


def test_one_valid_representation_per_entry_does_not_duplicate_split_entries(tmp_path):
    valid_uri = "data:image/png;base64," + IMAGE_RESULT["data"][0]["b64_json"]
    response = {"data": [{"b64_json": "%%bad%%", "url": valid_uri}, {"url": valid_uri}, {"b64_json": IMAGE_RESULT["data"][0]["b64_json"]}]}
    with gateway([(200, response)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "output_path": str(tmp_path / "representations.png")})
    assert result.success, result.error
    assert result.data["images_generated"] == 3
    assert len(result.artifacts) == 3
    assert len(records) == 1


def test_explicit_model_name_and_profile_dimension_mapping_are_sent_verbatim(tmp_path):
    from tools.provider_pricing import PriceQuoteRequired
    profile = {"supported_parameters": ["prompt", "n", "canvas_width", "canvas_height"], "parameter_map": {"width": "canvas_width", "height": "canvas_height"}, "limits": {"canvas_width": {"type": "integer", "maximum": 5}, "canvas_height": {"type": "integer", "maximum": 5}}}
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        tool = configured_tool(tmp_path, address, profile)
        config_path = tmp_path / "deployment.yaml"
        deployment = yaml.safe_load(config_path.read_text())
        deployment["newapi"]["models"]["explicit-picture"] = deployment["newapi"]["models"]["picture-alias"]
        config_path.write_text(yaml.safe_dump(deployment))
        assert set(tool.get_info()["model_catalog"]) == {"picture-alias", "explicit-picture"}
        with pytest.raises(PriceQuoteRequired, match="deployment quote"):
            tool.estimate_cost({})
        assert not records
        result = tool.execute({"prompt": "Rain", "model_name": "explicit-picture", "width": 3, "height": 2, "output_path": str(tmp_path / "mapped.png")})
    assert result.success, result.error
    assert records[0]["body"] == {"model": "explicit-picture", "prompt": "Rain", "n": 1, "canvas_width": 3, "canvas_height": 2}


@pytest.mark.parametrize("profile,extra", [
    ({"supports_async": False}, {"request_mode": "async"}),
    ({"operations": ["generate"]}, {"generation_mode": "edit", "image_url": "https://media.example/input.png"}),
    ({"parameter_map": {"json_image": "image"}}, {"generation_mode": "edit", "image_url": "https://media.example/input.png", "mask_url": "https://media.example/mask.png"}),
    ({}, {"generation_mode": "edit", "image_urls": ["https://media.example/a.png", "https://media.example/b.png", "https://media.example/c.png"]}),
])
def test_undeclared_modes_masks_and_reference_counts_fail_without_generation(tmp_path, profile, extra):
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address, profile).execute({"prompt": "Rain", "output_path": str(tmp_path / "unsupported.png"), **extra})
    assert not result.success
    assert not records


def test_resume_rejects_foreign_or_changed_jobs_before_http(tmp_path):
    receipt = {"id": "task-original", "object": "task", "status": "queued"}
    expired = {"error": {"code": "result_data_unavailable", "message": "Expired"}}
    with gateway([(200, receipt), (500, expired)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        original = tool.execute({"prompt": "Rain", "request_mode": "async", "output_path": str(tmp_path / "original.png")})
        job = original.data["resume_job"]
        for changed in ({"base_url": "https://foreign.example/v1"}, {"tool": "newapi_video"}, {"model": "missing-model"}, {"id": "https://foreign.example/task"}, {"url": "https://foreign.example/task"}, {"output_path": "https://foreign.example/asset.png"}, {"id": "image-test-secret"}):
            failed = tool.execute({"resume_job": {**job, **changed}})
            assert not failed.success
            assert "image-test-secret" not in repr(failed)
        assert not tool.execute({"resume_job": job, "generation_mode": "edit"}).success
        assert not tool.execute({"resume_job": job, "provider_params": {"n": 2}}).success
    assert [record["method"] for record in records] == ["POST", "GET"]


def test_transient_poll_get_can_retry_without_a_second_submission(tmp_path):
    receipt = {"id": "task-retry", "object": "task", "status": "queued"}
    completed = {"id": "task-retry", "object": "task", "status": "completed", "result": IMAGE_RESULT}
    with gateway([(200, receipt), (429, {"error": {"message": "Slow down"}}, {"Retry-After": "0"}), (200, completed)]) as (address, records):
        tool = configured_tool(tmp_path, address)
        path = tmp_path / "deployment.yaml"
        settings = yaml.safe_load(path.read_text())
        settings["newapi"]["get_retries"] = 1
        path.write_text(yaml.safe_dump(settings))
        result = tool.execute({"prompt": "Rain", "request_mode": "async", "output_path": str(tmp_path / "retry.png")})
    assert result.success, result.error
    assert [record["method"] for record in records] == ["POST", "GET", "GET"]


def test_multipart_content_type_matches_image_bytes_even_with_a_different_suffix(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(PNG)
    with gateway([(200, IMAGE_RESULT)]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "generation_mode": "edit", "image_path": str(source), "output_path": str(tmp_path / "mime.png")})
    assert result.success, result.error
    uploaded = [item for item in parts(records[0]) if item[1]]
    assert uploaded == [("image[]", "source.jpg", "image/png", PNG)]


@pytest.mark.parametrize("entry", [
    {"b64_json": 123, "url": "data:image/png;base64," + IMAGE_RESULT["data"][0]["b64_json"]},
    {"b64_json": IMAGE_RESULT["data"][0]["b64_json"], "url": {"not": "a URL"}},
])
def test_a_valid_image_representation_survives_a_malformed_alternative(tmp_path, entry):
    with gateway([(200, {"data": [entry]})]) as (address, records):
        result = configured_tool(tmp_path, address).execute({"prompt": "Rain", "output_path": str(tmp_path / "alternative.png")})
    assert result.success, result.error
    assert result.data["images_generated"] == 1
    assert Path(result.data["output"]).read_bytes() == PNG
    assert len(records) == 1
