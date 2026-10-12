"""Public message and URL helpers shared by the platform pages."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


UI = Path(__file__).resolve().parents[2] / "backlot" / "ui"


def run_js(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser-module checks")
    result = subprocess.run(
        [node, "--input-type=module", "-e", f"""
import assert from 'node:assert/strict';
import {{ t, registerMessages, getLocale }} from {json.dumps((UI / 'i18n.js').as_uri())};
globalThis.location = {{ origin: 'http://localhost', pathname: '/zh' }};
{source}
"""], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_platform_messages_follow_url_language_and_interpolate_counts():
    run_js("""
registerMessages({
  zh: {'{count} projects': '{count} 个项目'},
  ja: {'{count} projects': '{count} 件のプロジェクト'},
});
assert.equal(getLocale(), 'zh');
assert.equal(t('{count} projects', {count: 3}), '3 个项目');
location.pathname = '/ja/studio';
assert.equal(t('{count} projects', {count: 3}), '3 件のプロジェクト');
location.pathname = '/en/p/example';
assert.equal(t('{count} projects', {count: 3}), '3 projects');
assert.equal(t('New message'), 'New message');
location.pathname = '/';
assert.equal(getLocale(), 'zh');
""")


def test_platform_links_replace_language_and_keep_page_query_and_fragment():
    run_js(f"""
const {{ localePath }} = await import({json.dumps((UI / 'i18n.js').as_uri())});
assert.equal(localePath('/en/studio?task=abc%2Fdef#review', 'ja'), '/ja/studio?task=abc%2Fdef#review');
assert.equal(localePath('/p/nested/project?static=1'), '/zh/p/nested/project?static=1');
assert.equal(localePath('/ja', 'en'), '/en');
assert.equal(localePath('/'), '/zh');
assert.equal(localePath('/api/projects'), '/api/projects');
assert.equal(localePath('/media/example/movie.mp4'), '/media/example/movie.mp4');
assert.equal(localePath('https://example.com/studio'), 'https://example.com/studio');
""")


def test_unknown_custom_labels_keep_their_text_even_for_object_property_names():
    run_js("""
assert.equal(t('constructor'), 'constructor');
assert.equal(t('toString'), 'toString');
assert.equal(t('__proto__'), '__proto__');
""")
