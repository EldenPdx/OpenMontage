"""The deterministic endpoint must speak actual Responses SSE to the pinned Pi."""

import json
from urllib.request import Request, urlopen

from tests.fixtures.studio.model_server import model_server


def test_controlled_model_stream_has_unicode_usage_and_a_terminal_response():
    with model_server() as (base_url, requests):
        request = Request(base_url + "/responses", data=b'{"model":"controlled-model","stream":true}',
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            events = [json.loads(line[6:]) for line in response.read().decode().split("\n")
                      if line.startswith("data: ")]
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "真实 Pi 联调成功\u2028上下文已保留"
    assert events[-1]["response"]["usage"]["total_tokens"] == 15
    assert requests == [{"model": "controlled-model", "stream": True}]
