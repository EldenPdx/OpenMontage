"""Explicit tool/host/model constraints shared by capability selectors."""

from __future__ import annotations


def newapi_resume(inputs):
    job = inputs.get("resume_job")
    if not isinstance(job, dict) or job.get("tool") not in {"newapi_image", "newapi_video"}:
        return {}
    if inputs.get("preferred_tool") and inputs["preferred_tool"] != job["tool"] or inputs.get("hosting_provider") and inputs["hosting_provider"] != "newapi":
        raise ValueError("Explicit route conflicts with the saved New API job")
    if inputs.get("allowed_providers") and "newapi" not in inputs["allowed_providers"]:
        raise ValueError("Allowed providers conflict with the saved New API job")
    return job


def explicit_model(inputs):
    aliases = [inputs[field] for field in ("model", "model_id", "model_name") if inputs.get(field) is not None and inputs[field] != ""]
    resume = newapi_resume(inputs)
    if resume:
        aliases.append(resume.get("model"))
    if any(not isinstance(value, str) for value in aliases):
        raise ValueError("Model aliases must be strings")
    if len(set(aliases)) > 1:
        raise ValueError("Model aliases conflict")
    return aliases[0] if aliases else None


def newapi_inputs(inputs, capability):
    adapted = {key: value for key, value in inputs.items() if key not in {"preferred_provider", "preferred_provider_gap", "allowed_providers", "preferred_tool", "hosting_provider", "target_operation", "model_name", "model_id"}}
    model = explicit_model(inputs)
    if model:
        adapted["model"] = model
    if capability == "image_generation" and adapted.get("operation") == "generate" and adapted.get("generation_mode") in {"generate", "edit"}:
        adapted.pop("operation")
    if capability == "image_generation" and not inputs.get("resume_job") and not inputs.get("operation") and not inputs.get("generation_mode") and any(inputs.get(field) for field in ("image_path", "image_paths", "image_url", "image_urls")):
        adapted["generation_mode"] = "edit"
    return adapted


def filter_explicit_route(inputs, candidates):
    saved_route = newapi_resume(inputs)
    tool_name = inputs.get("preferred_tool") or saved_route.get("tool")
    host = inputs.get("hosting_provider") or ("newapi" if saved_route else None)
    model = explicit_model(inputs)
    resume = inputs.get("resume_job") if isinstance(inputs.get("resume_job"), dict) else {}
    allowed = set(inputs.get("allowed_providers") or [])
    selected = []
    for tool in candidates:
        if allowed and tool.provider not in allowed:
            continue
        if tool_name and tool.name != tool_name:
            continue
        if host and getattr(tool, "hosting_provider", tool.provider) != host:
            continue
        if model:
            props = tool.input_schema.get("properties", {})
            models = set()
            for field in ("model", "model_id", "model_name"):
                models.update(props.get(field, {}).get("enum", []))
            models.update(getattr(tool, "_MODELS", {}))
            models.update(getattr(tool, "_MODEL_ALIASES", {}))
            models.update(tool.get_info().get("model_catalog", {}))
            if model not in models:
                continue
        operation = inputs.get("generation_mode") or inputs.get("operation")
        if operation == "rank":
            operation = inputs.get("target_operation") or {"image_generation": "generate", "video_generation": "text_to_video", "tts": "speech"}.get(tool.capability)
        if tool.provider == "newapi":
            operation = inputs.get("generation_mode") or inputs.get("operation") or resume.get("operation")
            if operation == "rank":
                operation = inputs.get("target_operation")
            if not operation and tool.capability == "image_generation" and any(inputs.get(field) for field in ("image_path", "image_paths", "image_url", "image_urls")):
                operation = "edit"
            operation = operation or {"image_generation": "generate", "video_generation": "text_to_video", "tts": "speech"}.get(tool.capability)
            if tool.capability == "tts" and operation == "generate":
                operation = "speech"
            mode = inputs.get("request_mode") or resume.get("request_mode") or ("async" if tool.capability == "video_generation" else "sync")
            profile_operation = {"generate": "async", "edit": "async_edit"}.get(operation, operation) if tool.capability == "image_generation" and mode == "async" else operation
            catalogue = tool.get_info().get("model_catalog", {})
            profiles = [catalogue[model]] if model in catalogue else list(catalogue.values())
            if mode not in {"sync", "async"} or not any(profile.get("supports_" + mode) and profile_operation in profile.get("operations", []) for profile in profiles):
                continue
        routes = getattr(tool, "routes", None) or getattr(tool, "models", None)
        if isinstance(routes, dict) and operation and operation != "rank":
            model_routes = (
                routes.get(model, {}) if model else next(iter(routes.values()), {})
            )
            normalized = (
                "text_to_video"
                if operation == "generate" and tool.capability == "video_generation"
                else operation
            )
            if normalized not in model_routes:
                continue
        if tool.capability == "image_generation":
            props = tool.input_schema.get("properties", {})
            if any(inputs.get(k) and k not in props for k in ("mask_path", "mask_url")):
                continue
            if any(
                inputs.get(k) and k not in props and "images" not in props
                for k in ("image_path", "image_paths", "image_url", "image_urls")
            ):
                continue
            if operation == "precise_edit" and not (
                getattr(tool, "supports", {}).get("precise_edit")
                or isinstance(routes, dict)
                and any("precise_edit" in r for r in routes.values())
            ):
                continue
        selected.append(tool)
    return selected
