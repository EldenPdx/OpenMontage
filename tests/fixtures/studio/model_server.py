"""Loopback-only model service consumed by the real, pinned Pi in integration tests."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from typing import Callable


def text_item(text: str) -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def tool_item(name: str, arguments: dict, call_id: str = "call_test") -> dict:
    return {"id": f"fc_{call_id}", "type": "function_call", "call_id": call_id,
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False), "status": "completed"}


@contextmanager
def model_server(reply: Callable[[dict], list[dict]] | None = None, *, expected_authorization=None, expected_headers=None):
    """Yield (base_url, request_bodies); never record authentication headers."""
    requests: list[dict] = []
    reply = reply or (lambda body: [text_item("真实 Pi 联调成功\u2028上下文已保留")])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            expected = dict(expected_headers or {})
            if expected_authorization is not None:
                expected["Authorization"] = expected_authorization
            if any(self.headers.get(name) != value for name, value in expected.items()):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Controlled fixture credential mismatch"}}')
                return
            size = int(self.headers.get("Content-Length", "0"))
            if size > 4 * 1024 * 1024:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(size))
            requests.append(body)
            items = reply(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.path.endswith("/chat/completions"):
                content = "".join(part.get("text", "") for item in items for part in item.get("content", []))
                chunks = [
                    {"id": "chat_test", "object": "chat.completion.chunk", "model": body["model"],
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
                    {"id": "chat_test", "object": "chat.completion.chunk", "model": body["model"],
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
                ]
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                response = {"id": "resp_test", "object": "response", "model": body["model"],
                            "status": "completed", "output": items,
                            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                      "input_tokens_details": {"cached_tokens": 0},
                                      "output_tokens_details": {"reasoning_tokens": 0}}}
                events = [{"type": "response.created", "response": {**response, "output": [], "status": "in_progress"}}]
                for index, item in enumerate(items):
                    events.append({"type": "response.output_item.added", "output_index": index, "item": item})
                    events.append({"type": "response.output_item.done", "output_index": index, "item": item})
                events.append({"type": "response.completed", "response": response})
                for event in events:
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            self.close_connection = True

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, address):
            if not isinstance(sys.exception() if hasattr(sys, "exception") else sys.exc_info()[1],
                              (BrokenPipeError, ConnectionResetError)):
                super().handle_error(request, address)

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
