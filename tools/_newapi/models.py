"""Deployment model contracts, explicitly refreshed visibility, and safe jobs."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from jsonschema import validate, ValidationError

from lib.config_model import normalize_newapi_url, NewAPIModelProfile
from tools.provider_jobs import save_job

# Memory-only: a credential fingerprint isolates permissions without persisting keys.
_VISIBLE = {}
_RESERVED = {'model', 'protocol', 'operation', 'request_mode', 'async', 'stream', 'background', 'api_key', 'key', 'authorization', 'headers', 'base_url', 'url', 'endpoint', 'timeout', 'resume_job', 'job_path', 'output_path'}
_ENDPOINT_PROTOCOLS = {'anthropic': 'anthropic', 'openai-response': 'responses'}


@dataclass(frozen=True)
class ResolvedModel:
    id: str
    profile: NewAPIModelProfile
    protocol: str | None = None


def cache_scope(settings):
    return (settings.config.base_url, hashlib.sha256(settings.api_key.encode()).hexdigest())


def model_catalog(settings, capability):
    visible = _VISIBLE.get(cache_scope(settings))
    return {
        model: {**profile.model_dump(), 'provider': 'newapi', 'hosting_provider': 'newapi', 'pricing_status': 'quote_required', **({'supported_endpoint_types': visible[model].get('supported_endpoint_types', [])} if visible is not None else {})}
        for model, profile in settings.config.models.items()
        if capability in profile.capabilities and (visible is None or model in visible)
    }


def refresh_models(settings, client=None):
    if client is None:
        from tools._newapi.client import NewAPIClient
        client = NewAPIClient(settings)
    response = client.request_json('GET', '/v1/models')
    data = response.get('data')
    if not isinstance(data, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in data):
        raise ValueError('New API models response must contain model IDs')
    _VISIBLE[cache_scope(settings)] = {item['id']: {'supported_endpoint_types': item.get('supported_endpoint_types', [])} for item in data}
    return model_catalog(settings, 'text_generation')


def resolve_model(settings, capability, model=None, protocol=None, operation=None, request_mode='sync'):
    selected = model or (settings.llm.model if capability == 'text_generation' and settings.llm.provider == 'newapi' else None) or settings.config.default_models.get(capability)
    if not selected:
        raise ValueError(f'No deployment default model for {capability}')
    profile = settings.config.models.get(selected)
    if profile is None or capability not in profile.capabilities:
        raise ValueError(f'Model {selected!r} has no declared {capability} capability')
    visible = _VISIBLE.get(cache_scope(settings))
    if visible is not None and selected not in visible:
        raise ValueError(f'Model {selected!r} is not visible to this gateway credential')
    if request_mode not in {'sync', 'async'} or not (profile.supports_async if request_mode == 'async' else profile.supports_sync):
        raise ValueError(f'Model {selected!r} does not support request_mode={request_mode}')
    profile_operation = {'generate': 'async', 'edit': 'async_edit'}.get(operation, operation) if capability == 'image_generation' and request_mode == 'async' else operation
    if profile_operation and profile_operation not in profile.operations:
        raise ValueError(f'Model {selected!r} does not support operation={operation}')
    chosen = None
    if capability == 'text_generation':
        supported = set(profile.protocols) & {'anthropic', 'responses'}
        if visible is not None:
            metadata = set(visible[selected].get('supported_endpoint_types') or [])
            known = {_ENDPOINT_PROTOCOLS[name] for name in metadata if name in _ENDPOINT_PROTOCOLS}
            if metadata:
                supported &= known
        desired = protocol or settings.config.default_llm_protocol
        desired = 'anthropic' if desired == 'messages' else desired
        if desired not in {'auto', 'anthropic', 'responses'}:
            raise ValueError('Unknown LLM protocol')
        if desired != 'auto':
            if desired not in supported:
                raise ValueError(f'Model {selected!r} conflicts with LLM protocol={desired}')
            chosen = desired
        elif len(supported) == 1:
            chosen = next(iter(supported))
        elif len(supported) > 1 and settings.config.default_llm_protocol in supported:
            chosen = settings.config.default_llm_protocol
        else:
            raise ValueError(f'Model {selected!r} has missing or ambiguous LLM protocol metadata')
    return ResolvedModel(selected, profile, chosen)


def merge_params(resolved, standard=None, provider_params=None):
    """Only declared wire parameters; codecs own aliases and transport controls."""
    standard, provider_params = standard or {}, provider_params or {}
    if not isinstance(standard, dict) or not isinstance(provider_params, dict):
        raise ValueError('provider_params must be an object')
    if _RESERVED.intersection(provider_params) or _RESERVED.intersection(standard):
        raise ValueError('Parameters cannot override routing, transport or credentials')
    merged = {**resolved.profile.defaults, **{k: v for k, v in standard.items() if v is not None}, **provider_params}
    if set(merged) - set(resolved.profile.supported_parameters):
        raise ValueError('Parameters are not declared by the deployment profile: ' + ', '.join(sorted(set(merged) - set(resolved.profile.supported_parameters))))
    for name, schema in resolved.profile.limits.items():
        if name in merged:
            try:
                validate(merged[name], schema)
            except ValidationError:
                raise ValueError(f'Parameter {name!r} violates deployment limits') from None
    return merged


def make_job(settings, *, tool, model, operation, request_mode, id, output_path, params=None):
    if not isinstance(id, str) or not id.strip():
        raise ValueError('Submitted task has no public id; outcome unknown')
    from tools._newapi.client import task_path
    task_path('tasks', id)
    if settings.api_key and any(settings.api_key in str(value) for value in (tool,model,operation,request_mode,id,settings.config.base_url,output_path)):
        raise ValueError('Job identity contains a credential; refusing to persist it')
    # Keep a useful, small summary; prompts and reference media do not belong in jobs.
    safe_params = {key: value for key, value in (params or {}).items() if key in {'seconds', 'size', 'n', 'quality', 'response_format', 'voice', 'speed'} and isinstance(value, (str, int, float, bool))}
    from tools._newapi.client import redact
    return {'tool': tool, 'model': model, 'operation': operation, 'request_mode': request_mode, 'id': id, 'base_url': settings.config.base_url, 'output_path': str(output_path), 'submitted_at': datetime.now(timezone.utc).isoformat(), 'params': redact(safe_params, settings.api_key)}


def validate_resume(settings, job, *, tool, model=None, operation=None, output_path=None):
    if not isinstance(job, dict) or any(not isinstance(job.get(field), str) or not job[field].strip() for field in ('tool', 'model', 'operation', 'request_mode', 'id', 'base_url', 'output_path', 'submitted_at')):
        raise ValueError('Resume job is missing required string fields')
    if set(job) - {'tool', 'model', 'operation', 'request_mode', 'id', 'base_url', 'output_path', 'submitted_at', 'params'}:
        raise ValueError('Resume job contains unsupported fields')
    from tools._newapi.client import task_path
    task_path('tasks', job['id'])
    if settings.api_key and any(settings.api_key in str(job[field]) for field in ('tool','model','operation','request_mode','id','base_url','output_path','submitted_at')):
        raise ValueError('Resume job identity contains a credential')
    if job['tool'] != tool or (model and job['model'] != model) or (operation and job['operation'] != operation) or job['request_mode'] != 'async':
        raise ValueError('Resume job does not match this tool, model or operation')
    if normalize_newapi_url(job['base_url']) != settings.config.base_url:
        raise ValueError('Resume job belongs to a different gateway')
    if '\0' in job['output_path'] or '://' in job['output_path']:
        raise ValueError('Resume output_path must be a local file path')
    if output_path is not None and Path(output_path).resolve() != Path(job['output_path']).resolve():
        raise ValueError('Resume output_path conflicts with the saved job')
    profile = settings.config.models.get(job['model'])
    profile_operation = {'generate': 'async', 'edit': 'async_edit'}.get(job['operation'], job['operation'])
    if not profile or profile_operation not in profile.operations or not profile.supports_async:
        raise ValueError('Resume model or operation is not supported by this deployment')
    from tools._newapi.client import redact
    return redact(dict(job), settings.api_key)


def persist_job(job, job_path=None):
    path = job_path or str(job['output_path']) + '.job.json'
    save_job(path, job)
    return str(path)
