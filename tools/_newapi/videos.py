"""OpenAI-style gateway video lifecycle and authenticated content retrieval."""
from contextlib import ExitStack
import base64
from io import BytesIO
import json
import math
import mimetypes
from pathlib import Path
import time
from urllib.parse import urlsplit

from tools._newapi.client import NewAPIClient, NewAPIError, redact, sanitize_error, task_path, validate_media
from tools._newapi.config import load_settings
from tools._newapi.models import make_job, merge_params, persist_job, resolve_model, validate_resume
from tools.base_tool import ToolResult
from tools.provider_jobs import poll
from tools.video._shared import probe_output


_CONTROLS = {"model", "operation", "request_mode", "output_path", "resume_job", "job_path", "poll_timeout", "poll_interval", "provider_params", "preferred_tool", "hosting_provider", "preferred_provider", "preferred_provider_gap", "allowed_providers", "target_operation", "scene_id", "project_dir", "task_context", "sample_mode"}
_STANDARD = {"prompt", "duration", "duration_seconds", "seconds", "size", "aspect_ratio", "resolution", "seed", "generate_audio", "negative_prompt"}
_REFERENCES = {"reference_image_path", "image_path", "reference_image_url", "image_url"}


def verify_video(path):
    info = probe_output(path)
    if not all(info.get(field, 0) > 0 for field in ("duration_seconds", "video_width", "video_height")):
        raise NewAPIError("invalid_media", "Downloaded media has no valid video stream")
    return info


def validate_state(state, identifier):
    if state.get("id") != identifier or state.get("object") != "video":
        raise NewAPIError("invalid_response", "Gateway video response does not match the public job")
    if state.get("status") not in {"queued", "in_progress", "completed", "failed"}:
        raise NewAPIError("invalid_status", "Gateway returned an unknown video status")
    if state.get("error") or state["status"] == "failed":
        error = state.get("error") if isinstance(state.get("error"), dict) else {}
        raise NewAPIError(error.get("code") or "video_failed", error.get("message") or "Gateway video generation failed", status=200, request_id=state.get("request_id"))
    return state


def local_output(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value) or "://" in str(value):
        raise ValueError("Video output and job paths must be local file paths")
    path = Path(value)
    if path.is_dir():
        raise ValueError("Video output and job paths must be files")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def validate_reference(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Reference image must be a URL or image data URI")
    if value.startswith("data:"):
        from PIL import Image
        try:
            header, body = value.split(",", 1)
            if not header.startswith("data:image/") or not header.endswith(";base64"):
                raise ValueError("Invalid image encoding")
            with Image.open(BytesIO(base64.b64decode(body, validate=True))) as image:
                image.verify()
        except Exception:
            raise ValueError("Reference image data URI is invalid") from None
    else:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Reference image URL must be absolute HTTP(S) without embedded credentials")
        parsed.port


def execute_video(inputs, config_path=None):
    settings, client, job = None, None, {}
    generation_status, download_status = "not_started", "not_started"
    try:
        settings = load_settings(config_path)
        if inputs.get("request_mode", "async") != "async":
            raise ValueError("Gateway video requests use request_mode=async")
        timeout = float(inputs.get("poll_timeout", settings.config.poll_timeout))
        interval = float(inputs.get("poll_interval", settings.config.poll_interval))
        if not math.isfinite(timeout) or not math.isfinite(interval) or timeout <= 0 or interval <= 0:
            raise ValueError("poll_timeout and poll_interval must be finite and positive")
        deadline = time.monotonic() + timeout
        output = inputs.get("output_path")
        if inputs.get("job_path") is not None:
            local_output(inputs["job_path"])
        operation = inputs.get("operation", "text_to_video")
        if inputs.get("resume_job") is not None:
            job = validate_resume(settings, inputs["resume_job"], tool="newapi_video", model=inputs.get("model"), operation=inputs.get("operation"), output_path=inputs.get("output_path"))
            output, operation = job["output_path"], job["operation"]
            output = local_output(output)
            if inputs.get("job_path") and Path(inputs["job_path"]).resolve() == Path(output).resolve():
                raise ValueError("job_path must not overwrite the video output")
            resolve_model(settings, "video_generation", job["model"], operation=operation, request_mode="async")
            generation_status = "pending"
        else:
            output = local_output(output)
            if inputs.get("job_path") and Path(inputs["job_path"]).resolve() == Path(output).resolve():
                raise ValueError("job_path must not overwrite the video output")
            resolved = resolve_model(settings, "video_generation", inputs.get("model"), operation=operation, request_mode="async")
            wire_inputs = (_STANDARD | set(resolved.profile.supported_parameters) | set(resolved.profile.parameter_map)) - {"reference_image"}
            unknown = set(inputs) - wire_inputs - _CONTROLS - _REFERENCES
            if unknown:
                raise ValueError("Unsupported video inputs: " + ", ".join(sorted(unknown)))
            standard = {}
            for name in wire_inputs:
                if name in inputs:
                    semantic = "duration" if name == "duration_seconds" else name
                    target = resolved.profile.parameter_map.get(semantic, semantic)
                    if target in standard:
                        raise ValueError(f"Conflicting aliases for {target}")
                    standard[target] = inputs[name]
            payload = {"model": resolved.id, **merge_params(resolved, standard, inputs.get("provider_params"))}
            if not isinstance(payload.get("prompt"), str) or not payload["prompt"].strip():
                raise ValueError("prompt is required")
            references = [(kind, inputs[name]) for kind, names in (
                ("file", ("reference_image_path", "image_path")),
                ("url", ("reference_image_url", "image_url")),
            ) for name in names if inputs.get(name) is not None]
            if len(references) > 1:
                raise ValueError("Conflicting reference image aliases")
            reference_field = resolved.profile.parameter_map.get("reference_image", "input_reference")
            if references and (operation != "image_to_video" or reference_field not in resolved.profile.supported_parameters or reference_field in payload):
                raise ValueError("Reference image is unsupported or conflicts with native parameters")
            if operation == "image_to_video" and not references and not payload.get(reference_field):
                raise ValueError("image_to_video requires a reference image")
            if operation == "text_to_video" and payload.get(reference_field) is not None:
                raise ValueError("text_to_video does not accept a reference image")
            if references and references[0][0] == "url":
                validate_reference(references[0][1])
            elif payload.get(reference_field) is not None:
                validate_reference(payload[reference_field])
            client = NewAPIClient(settings)
            with ExitStack() as files:
                if references and references[0][0] == "file":
                    reference = Path(references[0][1])
                    validate_media(reference, "image")
                    upload = files.enter_context(reference.open("rb"))
                    multipart = {reference_field: (reference.name, upload, mimetypes.guess_type(reference.name)[0] or "image/png")}
                    fields = {name: json.dumps(value) if isinstance(value, (dict, list, bool)) else value for name, value in payload.items()}
                    receipt = client.request_json("POST", "/v1/videos", data=fields, files=multipart, deadline=deadline, receipt=True)
                else:
                    if references:
                        payload = {"model": resolved.id, **merge_params(resolved, {**standard, reference_field: references[0][1]}, inputs.get("provider_params"))}
                    receipt = client.request_json("POST", "/v1/videos", json=payload, deadline=deadline, receipt=True)
            identifier = receipt.get("id")
            try:
                task_path("videos", identifier)
                if redact(identifier, settings.api_key) != identifier:
                    raise ValueError("Unsafe public id")
            except ValueError:
                raise NewAPIError("invalid_receipt", "Video submission has no usable public id; outcome unknown", outcome_unknown=True) from None
            try:
                job = make_job(settings, tool="newapi_video", model=resolved.id, operation=operation, request_mode="async", id=identifier, output_path=output, params=payload)
            except ValueError:
                raise NewAPIError("invalid_receipt", "Video submission has no safe resumable identity; outcome unknown", outcome_unknown=True) from None
            persist_job(job, inputs.get("job_path"))
            generation_status = "failed" if receipt.get("error") or receipt.get("status") == "failed" else "pending"
            validate_state(receipt, job["id"])
        client = client or NewAPIClient(settings)

        def fetch():
            nonlocal generation_status
            try:
                state = client.request_json("GET", task_path("videos", job["id"]), deadline=deadline)
            except NewAPIError as exc:
                if exc.status == 200:
                    generation_status = "failed"
                raise
            return validate_state(state, job["id"])

        poll(fetch, timeout=max(0, deadline - time.monotonic()), interval=interval)
        generation_status, download_status = "completed", "failed"
        path = client.download(task_path("videos", job["id"], content=True), output, kind="video", deadline=deadline, validator=verify_video)
        data = {"provider": "newapi", "hosting_provider": "newapi", "model": job["model"], "operation": operation, "output": path, "output_path": path, "task_id": job["id"], "resume_job": job, "generation_status": "completed", "download_status": "completed", "cost_status": "unquoted", **verify_video(Path(path))}
        return ToolResult(success=True, data=redact(data, settings.api_key), artifacts=[path], cost_usd=None, model=job["model"])
    except Exception as exc:
        safe = sanitize_error(settings, exc)
        if isinstance(exc, TimeoutError):
            safe = NewAPIError("deadline_exceeded", "Video job remains pending; resume the saved job without submitting again")
        data = {"error": safe.as_data(), "cost_status": "unquoted", "generation_status": generation_status, "download_status": download_status}
        if job:
            data["resume_job"] = job
        return ToolResult(success=False, error=str(safe), data=redact(data, settings.api_key if settings else ""), cost_usd=None)
    finally:
        if client is not None:
            client.session.close()
