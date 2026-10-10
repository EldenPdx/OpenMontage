"""Recorded media codecs are descriptions; render encoder options remain guarded."""

import asyncio
from copy import deepcopy
import json
import subprocess

from fastapi.testclient import TestClient
import pytest

from backlot.server import create_app
from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.contracts import ContractViolation
from production.worker import Worker
from tests.fixtures.studio.model_server import model_server
from tests.fixtures.studio.production_flow import ProductionFlow, production_registry
from tests.fixtures.studio.repository import require_pi, test_repository as isolated_repository
from tests.integration.test_studio_policy import running
from tests.integration.test_studio_repository import repository, repository_factory


class ProbedMediaFlow(ProductionFlow):
    def __call__(self, body):
        if self.actions is None:
            self._plan()
        action, inputs = self.actions[self.index] if self.index < len(self.actions) else (None, {})
        if action == "checkpoint" and inputs["stage"] == "assets":
            manifest = inputs["artifacts"]["asset_manifest"]
            clip = next(asset["path"] for asset in manifest["assets"] if asset["type"] == "video")
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", clip],
                                   capture_output=True, text=True, check=True, timeout=10)
            streams = {stream["codec_type"]: stream for stream in json.loads(probe.stdout)["streams"]}
            assert streams["audio"]["codec_name"] == "aac"
            assert streams["video"]["codec_name"] == "h264"
            manifest["metadata"] = {"technical_probe": {kind: {"codec": streams[kind]["codec_name"]}
                                                         for kind in ("audio", "video")}}
        return super().__call__(body)


def test_real_pi_checkpoints_edit_with_canonical_aac_probe_and_renders_verified_ffmpeg_video(tmp_path, request):
    require_pi()
    projects, runtime = tmp_path / "projects", tmp_path / "runtime"
    with isolated_repository() as repo:
        flow = ProbedMediaFlow(repo, projects, revise_script=False)
        with model_server(flow) as (url, _):
            profile = PiProfile(provider="controlled", base_url=url, model="local-model", credential_env="CODEC_MODEL_KEY",
                                reasoning=False, thinking_level="off", context_window=200000, max_output_tokens=8192,
                                price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
            config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
            environment = {"CODEC_MODEL_KEY": "local-codec-fixture-key"}
            registry, _ = production_registry()
            request.addfinalizer(registry.fixture_manager.shutdown)
            worker = Worker(repo, config, runtime, projects, environment=environment, registry=registry)
            app = create_app(studio_repository=repo, studio_config=config, studio_environment=environment)
            app.state.studio_projects_dir = projects
            with TestClient(app, base_url="http://127.0.0.1") as client:
                token = client.get("/api/studio/config").json()["csrf_token"]
                headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": token, "Idempotency-Key": "codec-create-0001"}
                response = client.post("/api/studio/tasks", headers=headers,
                                       json={"brief": "A two second moving lighthouse with ambient tone", "profile_id": "local",
                                             "duration_seconds": 2, "narration": False})
                assert response.status_code == 202
                task_id = response.json()["task_id"]
                for turn in range(5):
                    asyncio.run(worker.run_once())
                    task = client.get(f"/api/studio/tasks/{task_id}").json()
                    if task["state"] == "succeeded":
                        break
                    assert task["state"] == "awaiting_approval", (task.get("error"), flow.errors)
                    gate = task["approval"]
                    approved = client.post(f"/api/studio/tasks/{task_id}/approvals/{gate['binding']['gate_id']}/decision",
                                           headers={**headers, "Idempotency-Key": f"codec-approve-{turn:04d}"},
                                           json={"binding": gate["binding"], "expected_version": task["version"], "decision": "approve"})
                    assert approved.status_code == 202, approved.text
                assert task["state"] == "succeeded" and task["result"]["verified"] is True
                project = projects / task["project_id"]
                manifest = json.loads((project / "artifacts/asset_manifest.json").read_text())
                assert manifest["metadata"]["technical_probe"]["audio"]["codec"] == "aac"
                assert json.loads((project / "checkpoint_edit.json").read_text())["status"] == "completed"
                output = project / task["result"]["video"]["path"]
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
                               capture_output=True, check=True, timeout=20)


@pytest.mark.parametrize("codec", ["aac", "h264"])
@pytest.mark.parametrize("placement", ["top", "compose_options", "nested_lookalike"])
def test_descriptive_manifest_codec_does_not_authorize_active_encoder_options(repository, tmp_path, codec, placement):
    bridge, context, _ = running(repository, tmp_path)
    inputs = {"asset_manifest": {"assets": [], "metadata": {"technical_probe": {"audio": {"codec": "aac"}}}}}
    if placement == "top":
        inputs["codec"] = codec
    elif placement == "compose_options":
        inputs["edit_decisions"] = {"metadata": {"compose_options": {"codec": codec}}}
    else:
        inputs["edit_decisions"] = {"metadata": {"asset_manifest": {"codec": codec}}}
    with pytest.raises(ContractViolation) as failure:
        bridge.policy.inputs(context, inputs)
    assert failure.value.code == "forbidden"


@pytest.mark.parametrize("codec", ["aac", "h264", "pcm_s16le"])
def test_manifest_probe_can_describe_existing_audio_and_video_codecs(repository, tmp_path, codec):
    bridge, context, _ = running(repository, tmp_path)
    inputs = {"asset_manifest": {"assets": [], "metadata": {"technical_probe": {"audio": {"codec": codec}}}}}
    assert bridge.policy.inputs(context, inputs) == inputs


@pytest.mark.parametrize("unsafe", [
    {"path": "../other-project/assets/clip.mp4"}, {"path": ".studio-owner.json"},
    {"url": "https://unapproved.invalid/media.mp4"}, {"api_key": "must-not-enter-tools"}, {"code": "run()"},
    {"codec": {"headers": {"Authorization": "forbidden"}}}, {"codec": [{"shell": "forbidden"}]},
])
def test_descriptive_manifest_namespace_still_rejects_unsafe_paths_urls_credentials_and_code(repository, tmp_path, unsafe):
    bridge, context, _ = running(repository, tmp_path)
    metadata = {"technical_probe": {"audio": {"codec": "aac"}}, "untrusted": deepcopy(unsafe)}
    with pytest.raises(ContractViolation) as failure:
        bridge.policy.inputs(context, {"asset_manifest": {"assets": [], "metadata": metadata}})
    assert failure.value.code == "forbidden"
