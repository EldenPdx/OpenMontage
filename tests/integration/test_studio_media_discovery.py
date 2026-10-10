"""Studio exposes the exact deployment profile used to validate media inputs."""

import asyncio
import json
from pathlib import Path

import pytest
import requests
import yaml

from lib.config_model import PiPrice, PiProfile
from production.contracts import ContractViolation
from production.pi_config import prepare_pi
from production.pi_rpc import PiRPC
from production.policy import media_configuration_sha256
from production.tool_bridge import BridgeServer
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_policy import running
from tests.integration.test_studio_repository import repository, repository_factory
from tools._newapi.config import load_settings
from tools._newapi.models import merge_params, resolve_model
from tools.graphics.newapi_image import NewAPIImage
from tools.video.newapi_video import NewAPIVideo


@pytest.fixture
def configured_media(tmp_path, monkeypatch):
    monkeypatch.setenv("NEW_API_KEY", "media-discovery-fixture-key")
    monkeypatch.delenv("NEW_API_BASE_URL", raising=False)
    monkeypatch.setattr(requests.Session, "request", lambda *args, **kwargs: pytest.fail("Catalog discovery must not send HTTP"))
    selected = {"image": "Images2.5-Flare", "video": "dreamina-seedance-2-5-260628"}
    video = {
        "capabilities": ["video_generation"], "operations": ["text_to_video"], "supports_sync": False, "supports_async": True,
        "supported_parameters": ["prompt", "seconds", "metadata"], "parameter_map": {"duration": "seconds"},
        "defaults": {"metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": False}},
        "limits": {"seconds": {"type": "integer", "minimum": 4, "maximum": 15},
                   "metadata": {"type": "object", "properties": {
                       "resolution": {"enum": ["720p"]}, "ratio": {"enum": ["16:9"]}, "generate_audio": {"type": "boolean"}},
                       "required": ["resolution", "ratio", "generate_audio"], "additionalProperties": False}},
    }
    image = {"capabilities": ["image_generation"], "operations": ["generate"], "supports_sync": True,
             "supported_parameters": ["prompt", "size"], "defaults": {"size": "1024x1024"},
             "limits": {"size": {"enum": ["1024x1024"]}}}
    values = {"newapi": {"base_url": "https://gateway.private.example/v1", "models": {
        selected["image"]: image, selected["video"]: video, "different-video": video},
        "default_models": {"image_generation": selected["image"], "video_generation": selected["video"]}}}
    path = tmp_path / "media.yaml"
    path.write_text(yaml.safe_dump(values))
    return path, selected, video, image


def test_catalog_exposes_only_selected_frozen_media_profile_and_its_legal_request_shape(repository, tmp_path, configured_media):
    path, selected, video, image = configured_media
    bridge, context, _ = running(repository, tmp_path, media_models=selected, media_hash=media_configuration_sha256(path))
    bridge.registry.register(NewAPIVideo(path))
    bridge.registry.register(NewAPIImage(path))
    catalog = bridge.catalog(context)
    rows = {row["name"]: row for row in catalog["tools"]}
    assert set(rows["newapi_video"].get("model_catalog", {})) == {selected["video"]}
    assert set(rows["newapi_image"].get("model_catalog", {})) == {selected["image"]}
    profile = rows["newapi_video"]["model_catalog"][selected["video"]]
    assert profile["supported_parameters"] == ["prompt", "seconds", "metadata"]
    assert profile["defaults"] == video["defaults"]
    assert profile["limits"] == video["limits"]
    assert profile["parameter_map"] == {"duration": "seconds"}
    assert rows["newapi_image"]["model_catalog"][selected["image"]]["defaults"] == image["defaults"]
    assert "gateway.private.example" not in json.dumps(catalog)
    assert "media-discovery-fixture-key" not in json.dumps(catalog)
    settings = load_settings(path)
    resolved = resolve_model(settings, "video_generation", model=selected["video"], operation="text_to_video", request_mode="async")
    params = merge_params(resolved, {"prompt": "A blue city at night", "seconds": 12,
                                    "metadata": {**profile["defaults"]["metadata"], "generate_audio": True}})
    assert params == {"prompt": "A blue city at night", "seconds": 12,
                      "metadata": {"resolution": "720p", "ratio": "16:9", "generate_audio": True}}
    assert repository.unresolved_intents(context.task_id) == []


def test_catalog_refuses_changed_media_configuration_before_advertising_new_defaults(repository, tmp_path, configured_media):
    path, selected, _, _ = configured_media
    bridge, context, _ = running(repository, tmp_path, media_models=selected, media_hash=media_configuration_sha256(path))
    bridge.registry.register(NewAPIVideo(path))
    changed = yaml.safe_load(path.read_text())
    changed["newapi"]["models"][selected["video"]]["defaults"]["metadata"]["generate_audio"] = True
    path.write_text(yaml.safe_dump(changed))
    with pytest.raises(ContractViolation) as failure:
        bridge.catalog(context)
    assert failure.value.code == "profile_unavailable"
    assert repository.unresolved_intents(context.task_id) == []


@pytest.mark.asyncio
async def test_real_pi_receives_selected_media_defaults_limits_and_mapping_from_its_catalog_tool(repository, tmp_path, configured_media):
    require_pi()
    path, selected, video, _ = configured_media
    def reply(body):
        return [tool_item("openmontage", {"action": "catalog", "input": {}}, "media_discovery")] if len(requests) == 1 else [text_item("The selected deployment metadata has been read.")]

    with model_server(reply) as (url, requests):
        profile = PiProfile(provider="local", model="test-model", base_url=url, credential_env="LOCAL_TEST_KEY",
                            reasoning=False, thinking_level="off", price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        bridge, context, _ = running(repository, tmp_path, profile, media_models=selected, media_hash=media_configuration_sha256(path))
        bridge.registry.register(NewAPIVideo(path))
        bridge.registry.register(NewAPIImage(path))
        with BridgeServer(bridge, context, profile) as server:
            managed = prepare_pi(profile, context, tmp_path / "runtime", environment={"LOCAL_TEST_KEY": "local-media-discovery-key"},
                                 trusted_extension=Path(__file__).resolve().parents[2] / "pi-runtime/extensions/openmontage.ts")
            client = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment},
                           session_root=managed.session_root, redact_values=(*managed.redact_values, server.token))
            await client.start(context)
            try:
                await client.prompt("Read the selected image and video deployment profiles using the catalog action.", command_id="media-catalog-prompt")
                async def settled():
                    async for event in client.events():
                        if event["type"] == "agent_settled":
                            return
                    raise AssertionError("Real Pi exited before settling")
                await asyncio.wait_for(settled(), 15)
            finally:
                await client.close()
    assert len(requests) == 2
    result = next(item for item in requests[-1]["input"] if item.get("type") == "function_call_output")
    catalog = json.loads(result["output"])
    row = next(row for row in catalog["tools"] if row["name"] == "newapi_video")
    assert set(row["model_catalog"]) == {selected["video"]}
    received = row["model_catalog"][selected["video"]]
    assert received["defaults"] == video["defaults"]
    assert received["limits"] == video["limits"]
    assert received["parameter_map"] == {"duration": "seconds"}
    assert "gateway.private.example" not in result["output"]
    assert "media-discovery-fixture-key" not in result["output"]
