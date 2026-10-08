"""Small gateway transport: paid POSTs are never retried."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from urllib3.util import Timeout

from tools.provider_jobs import download


class NewAPIError(ValueError):
    def __init__(self, code, message, *, status=None, request_id=None, outcome_unknown=False):
        self.code, self.status, self.request_id = code, status, request_id
        self.outcome_unknown = outcome_unknown
        super().__init__(message)

    def as_data(self):
        return {'code': self.code, 'message': str(self), 'status': self.status, 'request_id': self.request_id, 'outcome_unknown': self.outcome_unknown}


def redact(value, api_key=''):
    if isinstance(value, dict):
        return {key: redact(item, api_key) for key, item in value.items() if str(key).lower().replace('-', '_') not in {'key', 'api_key', 'new_api_key', 'authorization', 'x_api_key', 'headers', 'token', 'access_token', 'secret'}}
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if not isinstance(value, str):
        return value
    if api_key:
        value = value.replace(api_key, '[redacted]')
    value = re.sub(r'Bearer\s+[^\s,;]+', 'Bearer [redacted]', value, flags=re.I)
    return value


def safe_message(value, api_key=''):
    value = redact(str(value), api_key)
    def safe_url(match):
        try:
            parts = urlsplit(match.group(0))
            return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
        except ValueError:
            return '[invalid URL]'
    value = re.sub(r'https?://[^\s<>"\']+', safe_url, value)
    return value[:1000]


def sanitize_error(settings, exc):
    api_key = settings.api_key if settings is not None else os.environ.get('NEW_API_KEY', '')
    if isinstance(exc, NewAPIError):
        return NewAPIError(safe_message(exc.code, api_key), safe_message(exc, api_key), status=exc.status, request_id=safe_message(exc.request_id, api_key) if exc.request_id is not None else None, outcome_unknown=exc.outcome_unknown)
    # Third-party exceptions may contain request bodies or credentials; retain type only.
    return NewAPIError('invalid_request' if isinstance(exc, ValueError) else 'adapter_error', safe_message(exc, api_key) if isinstance(exc, ValueError) else f'New API adapter failed ({type(exc).__name__})')


def task_path(kind, identifier, content=False):
    if kind not in {'tasks', 'videos'} or not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
        raise ValueError('A public task/video ID is required')
    return '/v1/' + kind + '/' + quote(identifier, safe='') + ('/content' if content and kind == 'videos' else '')


def read_sse(response, deadline=None):
    """SSE transport frames; protocol-specific aggregation belongs in the codec."""
    event, lines = 'message', []
    try:
        if response.headers.get('Content-Type', '').split(';')[0].lower() != 'text/event-stream':
            raise NewAPIError('invalid_stream', 'New API did not return an SSE stream', outcome_unknown=getattr(response, '_newapi_paid', False))
        for raw in response.iter_lines():
            if deadline is not None and time.monotonic() >= deadline:
                raise NewAPIError('deadline_exceeded', 'New API stream deadline exceeded', outcome_unknown=True)
            line = raw.decode('utf8') if isinstance(raw, bytes) else raw
            if not line:
                if lines:
                    value = '\n'.join(lines)
                    yield event, '[DONE]' if value == '[DONE]' else json.loads(value)
                event, lines = 'message', []
            elif line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:'):
                lines.append(line[5:].lstrip())
        if lines:
            value = '\n'.join(lines)
            yield event, '[DONE]' if value == '[DONE]' else json.loads(value)
    except (requests.RequestException, UnicodeError, json.JSONDecodeError):
        raise NewAPIError('stream_interrupted', 'New API stream interrupted; outcome unknown', outcome_unknown=True) from None
    finally:
        response.close()


class NewAPIClient:
    def __init__(self, settings, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        if not settings.config.base_url or not settings.api_key:
            raise NewAPIError('not_configured', 'New API needs a deployment base URL and NEW_API_KEY')

    def _url(self, path):
        fixed = {'/v1/models', '/v1/messages', '/v1/responses', '/v1/images/generations', '/v1/images/edits', '/v1/async/images/generations', '/v1/async/images/edits', '/v1/videos', '/v1/audio/speech'}
        if path not in fixed and not re.fullmatch(r'/v1/(?:tasks/[A-Za-z0-9_-]+|videos/[A-Za-z0-9_-]+(?:/content)?)', path):
            raise NewAPIError('invalid_endpoint', 'New API endpoint must be a fixed adapter path')
        return self.settings.config.base_url + path[3:]

    def _error(self, response):
        payload = None
        try:
            payload = response.json()
        except (ValueError, requests.RequestException):
            pass
        failed = isinstance(payload, dict) and (payload.get('error') or payload.get('success') is False or payload.get('status') in {'failed', 'error', 'cancelled', 'canceled'})
        if response.status_code >= 400 or failed:
            error = payload.get('error') if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            code = error.get('code') or error.get('type') or ('http_error' if response.status_code >= 400 else 'business_error')
            message = error.get('message') or (payload.get('message') if isinstance(payload, dict) else None) or f'New API returned HTTP {response.status_code}'
            request_id = response.headers.get('x-request-id') or (payload.get('request_id') if isinstance(payload, dict) else None)
            raise NewAPIError(safe_message(code, self.settings.api_key), safe_message(message, self.settings.api_key), status=response.status_code, request_id=safe_message(request_id, self.settings.api_key) if request_id is not None else None)

    def request(self, method, path, *, json=None, data=None, files=None, stream=False, deadline=None, headers=None, _media_redirect=False):
        method = method.upper()
        if method not in {'GET', 'POST'}:
            raise NewAPIError('invalid_method', 'New API supports GET and POST adapter requests')
        url = self._url(path)
        config = self.settings.config
        deadline = deadline if deadline is not None else time.monotonic() + config.connect_timeout + config.read_timeout
        request_headers = {'Authorization': 'Bearer ' + self.settings.api_key}
        if headers:
            if set(headers) - {'anthropic-version', 'Accept'}:
                raise NewAPIError('invalid_headers', 'Only protocol headers may be supplied')
            request_headers.update(headers)
        for attempt in range(config.get_retries + 1 if method == 'GET' else 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NewAPIError('deadline_exceeded', 'New API request deadline exceeded')
            response = None
            try:
                response = self.session.request(method, url, headers=request_headers, json=json, data=data, files=files, stream=stream, allow_redirects=False, timeout=Timeout(total=remaining, connect=min(config.connect_timeout, remaining), read=min(config.read_timeout, remaining)))
                if 300 <= response.status_code < 400 and not _media_redirect:
                    raise NewAPIError('redirect_rejected', 'Gateway API redirect rejected', status=response.status_code)
                # Successful binary/SSE responses must remain streaming.
                if not stream or response.status_code >= 400 or 'json' in response.headers.get('Content-Type', '').lower():
                    self._error(response)
                response._newapi_paid = method == 'POST'
                response._newapi_deadline = deadline
                return response
            except requests.RequestException:
                error = NewAPIError('network_error', 'New API connection interrupted' + ('; outcome unknown' if method == 'POST' else ''), outcome_unknown=method == 'POST')
            except NewAPIError as exc:
                error = exc
            transient = error.code not in {'result_data_unavailable', 'artifact_gone', 'task_timeout'} and (error.code == 'network_error' or error.status == 429 or error.status is not None and 500 <= error.status < 600)
            if method != 'GET' or not transient or attempt >= config.get_retries:
                if response is not None:
                    response.close()
                if method == 'POST' and error.status == 504:
                    error.outcome_unknown = True
                raise error from None
            delay = min(2 ** attempt, 4)
            retry_after = response.headers.get('Retry-After') if response is not None else None
            if retry_after:
                try:
                    delay = max(0, float(retry_after))
                except ValueError:
                    try:
                        delay = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError):
                        pass
            if response is not None:
                response.close()
            remaining = deadline - time.monotonic()
            if delay >= remaining:
                raise NewAPIError('deadline_exceeded', 'New API retry would exceed request deadline') from None
            time.sleep(delay)

    def request_json(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError('Expected JSON object')
            return result
        except ValueError:
            raise NewAPIError('invalid_response', 'New API returned a non-JSON or malformed response', outcome_unknown=method.upper() == 'POST') from None
        finally:
            response.close()

    def write_binary(self, response, output_path, *, kind='audio', deadline=None, max_bytes=1024 * 1024 * 1024, validator=None):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        deadline = deadline if deadline is not None else getattr(response, '_newapi_deadline', None)
        try:
            self._error(response) if 'json' in response.headers.get('Content-Type', '').lower() else None
            mime = response.headers.get('Content-Type', '').split(';')[0].lower()
            if mime.startswith('text/') or 'json' in mime:
                raise NewAPIError('invalid_media', 'New API returned an error or stream instead of binary media')
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.part', delete=False) as file:
                temporary = Path(file.name)
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise NewAPIError('deadline_exceeded', 'Media download deadline exceeded')
                    size += len(chunk)
                    if size > max_bytes:
                        raise NewAPIError('invalid_media', 'Media exceeds the permitted size')
                    file.write(chunk)
            expected = response.headers.get('Content-Length')
            if not size or expected and not response.headers.get('Content-Encoding') and size != int(expected):
                raise NewAPIError('invalid_media', 'Media is empty or truncated')
            if kind == 'pcm':
                if validator is None:
                    raise NewAPIError('invalid_media', 'PCM requires an explicit sample/frame validator')
            else:
                validate_media(temporary, kind)
            if validator:
                validator(temporary)
            temporary.replace(path)
            return str(path)
        except requests.RequestException:
            raise NewAPIError('download_interrupted', 'Media download interrupted', outcome_unknown=getattr(response, '_newapi_paid', False)) from None
        finally:
            response.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def download(self, path, output_path, *, kind='video', deadline=None, **kwargs):
        url = self._url(path)
        # Downloads may redirect to an external HTTPS CDN. Follow manually without auth.
        response = self.request('GET', path, stream=True, deadline=deadline, _media_redirect=True)
        deadline = deadline if deadline is not None else response._newapi_deadline
        if 300 <= response.status_code < 400:
            location = urljoin(url, response.headers.get('Location', ''))
            response.close()
            return self.download_media(location, output_path, kind=kind, deadline=deadline, **kwargs)
        return self.write_binary(response, output_path, kind=kind, deadline=deadline, **kwargs)

    def download_media(self, url, output_path, *, kind='image', deadline=None, **kwargs):
        deadline = deadline if deadline is not None else time.monotonic() + self.settings.config.read_timeout
        if url.startswith('data:'):
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as directory:
                temporary = Path(directory) / 'media.part'
                download(url, temporary)
                if temporary.stat().st_size > kwargs.get('max_bytes', 1024 * 1024 * 1024):
                    raise NewAPIError('invalid_media', 'Media exceeds the permitted size')
                validate_media(temporary, kind)
                if kwargs.get('validator'):
                    kwargs['validator'](temporary)
                temporary.replace(target)
            return str(target)
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None:
            raise NewAPIError('invalid_media_url', 'Media URL must be credential-free HTTPS or base64 data')
        # A fresh session has no gateway Authorization, cookies, or session auth.
        with requests.Session() as anonymous:
            for _ in range(6):
                remaining = deadline - time.monotonic() if deadline is not None else self.settings.config.read_timeout
                if remaining <= 0:
                    raise NewAPIError('deadline_exceeded', 'Media download deadline exceeded')
                try:
                    response = anonymous.get(url, stream=True, allow_redirects=False, timeout=min(remaining, self.settings.config.read_timeout))
                except requests.RequestException:
                    raise NewAPIError('download_interrupted', 'Media download connection interrupted') from None
                if 300 <= response.status_code < 400:
                    url = urljoin(url, response.headers.get('Location', ''))
                    response.close()
                    parts = urlsplit(url)
                    if parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None:
                        raise NewAPIError('invalid_media_url', 'CDN redirect must be credential-free HTTPS')
                    continue
                if response.status_code >= 400:
                    try:
                        self._error(response)
                    finally:
                        response.close()
                return self.write_binary(response, output_path, kind=kind, deadline=deadline, **kwargs)
        raise NewAPIError('redirect_limit', 'Too many media redirects')


def validate_media(path, kind):
    if kind == 'image':
        from PIL import Image
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception:
            raise NewAPIError('invalid_media', 'Downloaded image is invalid or truncated') from None
    elif kind in {'audio', 'video'}:
        # Reuse the existing probe; requiring a positive duration rejects error bytes.
        from tools.analysis.audio_probe import probe_duration
        if not shutil.which('ffprobe'):
            raise NewAPIError('missing_dependency', 'ffprobe is required to validate generated media')
        duration = probe_duration(path)
        if duration is None or duration <= 0:
            raise NewAPIError('invalid_media', 'Downloaded media is invalid or truncated')
    else:
        raise NewAPIError('invalid_media', 'Expected media kind image, audio or video')
