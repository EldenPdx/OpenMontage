"""Loopback media provider with actual image bytes and FFmpeg video."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading

import httpx
from PIL import Image

from tests.fixtures.studio.production_flow import DeterministicImage, DeterministicVideo
from tools.base_tool import ToolResult


class LoopbackImage(DeterministicImage):
    def __init__(self, address, submissions):
        super().__init__(submissions)
        self.address = address

    def execute(self, inputs):
        with httpx.Client(trust_env=False, timeout=15) as client:
            response = client.post(self.address + "/" + self.name, json={"model": inputs["model"], "prompt": inputs["prompt"]})
            response.raise_for_status()
        Path(inputs["output_path"]).write_bytes(response.content)
        self.submissions.append((self.name, inputs["model"]))
        return ToolResult(success=True, data={"output_path": inputs["output_path"]}, cost_usd=0)


class LoopbackVideo(LoopbackImage):
    name = DeterministicVideo.name
    capability = DeterministicVideo.capability
    input_schema = DeterministicVideo.input_schema


class EnvironmentProbeImage(LoopbackImage):
    def __init__(self, address, submissions, observations, configuration):
        super().__init__(address, submissions)
        self.observations, self.configuration = observations, configuration

    def execute(self, inputs):
        from lib.env_loader import load_env
        load_env(self.configuration)
        self.observations.append(sorted(os.environ))
        return super().execute(inputs)


@contextmanager
def loopback_media():
    image = BytesIO()
    Image.new("RGB", (1280, 720), "#1c638a").save(image, format="PNG")
    with TemporaryDirectory(prefix="m-") as directory:
        video = Path(directory) / "scene.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-c:v", "libx264",
            "-preset", "ultrafast", "-c:a", "aac", "-pix_fmt", "yuv420p", str(video)],
            capture_output=True, check=True, timeout=20)
        assets = {"/newapi_image": image.getvalue(), "/newapi_video": video.read_bytes()}
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path not in assets or set(body) != {"model", "prompt"}:
                    self.send_error(400)
                    return
                requests.append((self.path[1:], body["model"]))
                data = assets[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "image/png" if self.path == "/newapi_image" else "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
