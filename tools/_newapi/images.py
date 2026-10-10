"""Image wire codec; model operations and parameters come from deployment facts."""

from pathlib import Path
from contextlib import ExitStack
import base64
from io import BytesIO
import json
import math
import time
from urllib.parse import urlsplit

from tools.base_tool import ToolResult
from tools._newapi.client import NewAPIClient, NewAPIError, redact, sanitize_error, task_path, validate_media
from tools._newapi.models import make_job, merge_params, persist_job, resolve_model, validate_resume
from tools.provider_jobs import poll


def checked_task(response, identifier):
    if response.get("object") != "task" or response.get("id") != identifier or response.get("status") not in {"queued", "in_progress", "completed", "failed"}:
        raise NewAPIError("invalid_response", "New API returned an invalid image task identity or status")
    return response


def validate_reference(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("JSON image references must be non-empty URL or image data URI strings")
    if value.startswith("data:"):
        try:
            from PIL import Image
            head, payload = value.split(",", 1)
            if not head.startswith("data:image/") or not head.endswith(";base64"):
                raise ValueError()
            with Image.open(BytesIO(base64.b64decode(payload, validate=True))) as image:
                declared_type = head[5:].split(";")[0].lower().replace("image/jpg", "image/jpeg")
                if declared_type != Image.MIME.get(image.format):
                    raise ValueError()
                image.verify()
        except Exception:
            raise ValueError("JSON image data URI is not a valid image") from None
    else:
        parsed = urlsplit(value)
        parsed.port
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("JSON image reference must be an absolute credential-free URL")


def image_parameters(resolved, inputs, operation):
    mapping = resolved.profile.parameter_map
    standard = {}
    for name in ("prompt", "n", "size", "quality", "response_format", "width", "height", "resolution", "aspect_ratio"):
        if name in inputs:
            field = mapping.get(name, name)
            if field in standard and standard[field] != inputs[name]:
                raise ValueError("Standard image parameters conflict on the same wire field")
            standard[field] = inputs[name]
    provider = dict(inputs.get("provider_params") or {})
    for supplied in (resolved.profile.defaults, standard, provider):
        if "n" in supplied and (type(supplied["n"]) is not int or not 0 <= supplied["n"] <= 128):
            raise ValueError("Image n must be an integer between 0 and 128; zero uses the paid default")
    # The upstream treats top-level n=0 as a paid default of one, never zero images.
    if standard.get("n") == 0:
        standard["n"] = 1
    if provider.get("n") == 0:
        provider["n"] = 1
    if "n" not in standard and "n" not in provider and resolved.profile.defaults.get("n") == 0:
        standard["n"] = 1
    paths = list(inputs.get("image_paths") or [])
    if inputs.get("image_path"):
        paths.insert(0, inputs["image_path"])
    refs = list(inputs.get("image_urls") or [])
    if inputs.get("image_url"):
        refs.insert(0, inputs["image_url"])
    if paths and refs:
        raise ValueError("Use local images or JSON references in one edit")
    uploads = []
    if paths:
        if operation != "edit":
            raise ValueError("Source images require generation_mode=edit")
        field = resolved.profile.parameter_map.get("multipart_image")
        if field not in {"image", "image[]"}:
            raise ValueError("Deployment must declare a multipart image field")
        standard[field] = paths
        uploads.extend((field, Path(path)) for path in paths)
        if inputs.get("mask_path"):
            mask_field = resolved.profile.parameter_map.get("multipart_mask")
            if mask_field != "mask":
                raise ValueError("Deployment does not support a multipart mask")
            standard[mask_field] = inputs["mask_path"]
            uploads.append((mask_field, Path(inputs["mask_path"])))
        if inputs.get("mask_url"):
            raise ValueError("Local image edits require a local mask")
    elif refs:
        if operation != "edit":
            raise ValueError("Source images require generation_mode=edit")
        field = mapping.get("json_image") if len(refs) == 1 and mapping.get("json_image") else mapping.get("json_images")
        if not field:
            raise ValueError("Deployment does not support this JSON image reference count")
        standard[field] = refs[0] if field == mapping.get("json_image") else refs
        if inputs.get("mask_url"):
            mask_field = mapping.get("json_mask")
            if not mask_field:
                raise ValueError("Deployment does not support a JSON mask")
            standard[mask_field] = inputs["mask_url"]
        if inputs.get("mask_path"):
            raise ValueError("JSON image edits require a JSON mask reference")
    elif inputs.get("mask_path") or inputs.get("mask_url"):
        raise ValueError("A mask requires a source image")
    if {name for name, path in uploads}.intersection(provider):
        raise ValueError("provider_params cannot override uploaded image or mask files")
    params = merge_params(resolved, standard, provider)
    if isinstance(params.get("size"), str) and "×" in params["size"]:
        raise ValueError("Image size must use 'x' instead of the multiplication sign '×'")
    if "parameters" in params:
        native = params["parameters"]
        if not isinstance(native, dict):
            raise ValueError("Native image parameters must be an object")
        pending = [native]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                merge_params(resolved, node)
                if "n" in node and (type(node["n"]) is not int or not 1 <= node["n"] <= 128):
                    raise ValueError("Native parameters.n must be an integer between 1 and 128")
                pending.extend(node.values())
    if not isinstance(params.get("prompt"), str) or not params["prompt"].strip():
        raise ValueError("Image prompt must not be empty")
    json_fields = {mapping[name] for name in ("json_image", "json_images") if name in mapping}
    upload_fields = {field for field, path in uploads}
    json_refs = [params[name] for name in json_fields if name in params and name not in upload_fields]
    declared_refs = json_fields | upload_fields | ({mapping["json_mask"]} if "json_mask" in mapping else set())
    if {"image", "images", "mask", "image[]"}.intersection(params) - declared_refs:
        raise ValueError("Image reference representation is not declared by the deployment")
    if uploads and json_refs:
        raise ValueError("Use local images or JSON references in one edit")
    for semantic in ("json_image", "json_images", "json_mask"):
        field = mapping.get(semantic)
        if field in params and field not in upload_fields:
            values = params[field] if semantic == "json_images" else [params[field]]
            if not isinstance(values, list) or not values:
                raise ValueError("JSON image arrays must contain URL or image data URI strings")
            for value in values:
                validate_reference(value)
    if operation == "edit" and not (uploads or json_refs):
        raise ValueError("Image edit requires source images")
    if operation == "generate" and json_refs:
        raise ValueError("Source images require generation_mode=edit")
    return params, uploads


def image_entries(items):
    # The gateway counts max(URL entries, Base64 entries). Keep two representations
    # of the same image together, including providers that split them across entries.
    entries, urls, encoded = [], [], []
    skipped = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            entries.append((index, item))
            continue
        has_url = isinstance(item.get("url"), str) and bool(item["url"])
        has_encoded = isinstance(item.get("b64_json"), str) and bool(item["b64_json"])
        if has_url and has_encoded:
            entries.append((index, item))
        elif has_url:
            urls.append((index, item))
        elif has_encoded:
            encoded.append((index, item))
        elif item.get("url") or item.get("b64_json"):
            entries.append((index, item))
        else:
            skipped += 1
    for ordinal in range(max(len(urls), len(encoded))):
        pair = ([urls[ordinal]] if ordinal < len(urls) else []) + ([encoded[ordinal]] if ordinal < len(encoded) else [])
        pair.sort(key=lambda entry: entry[0])
        index = pair[0][0]
        item = {"revised_prompt": next((entry.get("revised_prompt") for _, entry in pair if entry.get("revised_prompt")), None)}
        if ordinal < len(urls):
            item["url"] = urls[ordinal][1]["url"]
        if ordinal < len(encoded):
            item["b64_json"] = encoded[ordinal][1]["b64_json"]
        entries.append((index, item))
    return sorted(entries, key=lambda entry: entry[0]), skipped


def execute_images(settings, inputs):
    started = time.monotonic()
    outputs = []
    client = None
    data = {"provider": "newapi", "hosting_provider": "newapi", "outputs": outputs, "cost_status": "unquoted"}
    try:
        if inputs.get("stream"):
            raise ValueError("New API image SSE is not supported")
        timeout = inputs.get("poll_timeout", settings.config.poll_timeout)
        interval = inputs.get("poll_interval", settings.config.poll_interval)
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(interval) or interval <= 0:
            raise ValueError("Image poll_timeout and poll_interval must be finite and positive")
        if inputs.get("model") is not None and inputs.get("model_name") is not None and inputs["model"] != inputs["model_name"]:
            raise ValueError("model and model_name conflict")
        if inputs.get("generation_mode") is not None and inputs.get("operation") is not None and inputs["generation_mode"] != inputs["operation"]:
            raise ValueError("generation_mode and operation conflict")
        model = inputs.get("model") or inputs.get("model_name")
        requested_operation = inputs.get("generation_mode", inputs.get("operation"))
        job = inputs.get("resume_job")
        if job is None and inputs.get("job_path") and Path(inputs["job_path"]).is_file():
            job = json.loads(Path(inputs["job_path"]).read_text())
        if job is not None:
            job = validate_resume(settings, job, tool="newapi_image", model=model, operation=requested_operation, output_path=inputs.get("output_path"))
            if inputs.get("request_mode", "async") != "async":
                raise ValueError("An image task must resume in async mode")
            if set(inputs) - {"resume_job", "job_path", "model", "model_name", "operation", "generation_mode", "request_mode", "output_path", "poll_timeout", "poll_interval", "stream", "scene_id", "project_dir", "task_context"}:
                raise ValueError("Resume cannot change the saved image request parameters")
            data["resume_job"] = job
            data["job_path"] = inputs.get("job_path") or job["output_path"] + ".job.json"
            model, operation, mode = job["model"], job["operation"], "async"
            output_path = job["output_path"]
        else:
            operation, mode = requested_operation or "generate", inputs.get("request_mode", "sync")
            output_path = inputs.get("output_path")
        if not isinstance(output_path, str) or not output_path.strip() or "\0" in output_path or "://" in output_path or Path(output_path).is_dir():
            raise ValueError("An explicit local image output_path is required")
        resolved = resolve_model(settings, "image_generation", model, operation=operation, request_mode=mode)
        data.update(model=resolved.id, operation=operation, request_mode=mode)
        params, uploads = image_parameters(resolved, inputs, operation) if job is None else ({}, [])
        client = NewAPIClient(settings)
        deadline = time.monotonic() + (timeout if mode == "async" else settings.config.connect_timeout + settings.config.read_timeout)
        endpoint = "/v1/" + ("async/" if mode == "async" else "") + "images/" + ("edits" if operation == "edit" else "generations")
        if job is None:
            with ExitStack() as stack:
                if uploads:
                    files = []
                    for field, path in uploads:
                        if not path.is_file():
                            raise ValueError("Image input must be an existing local file")
                        validate_media(path, "image")
                        from PIL import Image
                        with Image.open(path) as image:
                            content_type = Image.MIME.get(image.format, "application/octet-stream")
                        files.append((field, (path.name, stack.enter_context(path.open("rb")), content_type)))
                    body = {"model": resolved.id, **{key: value for key, value in params.items() if key not in {name for name, path in uploads}}}
                    form = {key: value if isinstance(value, str) else json.dumps(value) for key, value in body.items()}
                    response = client.request_json("POST", endpoint, data=form, files=files, deadline=deadline, receipt=mode == "async")
                else:
                    response = client.request_json("POST", endpoint, json={"model": resolved.id, **params}, deadline=deadline, receipt=mode == "async")
            if mode == "async":
                if not isinstance(response.get("id"), str) or not response["id"].strip():
                    raise NewAPIError("invalid_response", "Image submission has no public task id; outcome unknown", outcome_unknown=True)
                try:
                    task_path("tasks", response["id"])
                    if redact(response["id"], settings.api_key) != response["id"]:
                        raise ValueError("Unsafe public id")
                    job = make_job(settings, tool="newapi_image", model=resolved.id, operation=operation, request_mode=mode, id=response["id"], output_path=output_path, params=params)
                except ValueError:
                    raise NewAPIError("invalid_response", "Image submission has no safe public task id; outcome unknown", outcome_unknown=True) from None
                data["resume_job"] = job
                data["job_path"] = persist_job(job, inputs.get("job_path"))
                checked_task(response, job["id"])
                if response["status"] == "failed":
                    error = response.get("error") if isinstance(response.get("error"), dict) else {}
                    raise NewAPIError(error.get("code") or "task_failed", error.get("message") or "Image task failed", status=200)
        if mode == "async":
            response = poll(lambda: checked_task(client.request_json("GET", task_path("tasks", job["id"]), deadline=deadline), job["id"]), timeout=max(deadline - time.monotonic(), 0.001), interval=interval)
            response = response.get("result")
            if not isinstance(response, dict):
                raise NewAPIError("invalid_response", "Completed image task has no image result")
        items = response.get("data")
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise NewAPIError("invalid_response", "New API returned no image outputs")
        data["generation_completed"] = True
        data.update(created=response.get("created"), usage=response.get("usage"))
        output = Path(output_path)
        revised = []
        failures = []
        entries, skipped = image_entries(items)
        for index, item in entries:
            target = output if not outputs else output.with_name(f"{output.stem}_{len(outputs) + 1}{output.suffix}")
            try:
                if not isinstance(item, dict):
                    raise NewAPIError("invalid_response", "New API image entry must be an object")
                sources = []
                if isinstance(item.get("b64_json"), str) and item["b64_json"]:
                    sources.append("data:image/png;base64," + item["b64_json"])
                if isinstance(item.get("url"), str) and item["url"]:
                    sources.append(item["url"])
                if not sources:
                    if item.get("b64_json") or item.get("url"):
                        raise NewAPIError("invalid_response", "Image URL or Base64 must be a string")
                    skipped += 1
                    continue
                last_error = None
                for source in sources:
                    try:
                        if time.monotonic() >= deadline:
                            raise NewAPIError("deadline_exceeded", "Image download deadline exceeded")
                        saved = client.download_media(source, target, kind="image", deadline=deadline)
                        break
                    except Exception as exc:
                        last_error = exc
                else:
                    raise last_error
                outputs.append(saved)
                revised.append(item.get("revised_prompt"))
            except Exception as exc:
                failures.append({"index": index, "error": sanitize_error(settings, exc).as_data()})
        data.update(images_generated=len(outputs), revised_prompts=revised, skipped_entries=skipped)
        if outputs:
            data["output"] = outputs[0]
        if failures:
            data["failed_outputs"] = failures
            raise NewAPIError("partial_media_failure" if outputs else "invalid_media", "Some returned image entries could not be saved; see failed_outputs")
        if not outputs:
            raise NewAPIError("invalid_response", "New API returned no image media")
        return ToolResult(success=True, data=redact(data, settings.api_key), artifacts=outputs, cost_usd=None, duration_seconds=time.monotonic() - started, model=resolved.id)
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            exc = NewAPIError("poll_timeout", "Image task is still pending; resume the saved job without resubmitting")
        safe = sanitize_error(settings, exc)
        data["error"] = safe.as_data()
        if outputs:
            data["output"] = outputs[0]
        return ToolResult(success=False, error=str(safe), data=redact(data, settings.api_key), artifacts=outputs, cost_usd=None, duration_seconds=time.monotonic() - started, model=data.get("model"))
    finally:
        if client is not None:
            client.session.close()
