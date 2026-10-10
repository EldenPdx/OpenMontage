"""The real agent can use the input examples it receives on the model wire."""

import json
import re

import pytest

from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.contracts import TaskCreate, TaskState
from production.pi_config import snapshot_for
from production.worker import Worker
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory
from tools.tool_registry import ToolRegistry


def published_input(description, action, observed_bad_input):
    example = re.search(r"\b" + action + r":\s*(\{)", description)
    if example is None:
        # These shapes reproduce the guesses in the failed production session.
        return observed_bad_input
    return json.JSONDecoder().raw_decode(description[example.start(1):])[0]


@pytest.mark.asyncio
async def test_real_pi_discovers_action_inputs_and_initializes_then_reads_its_project(repository, tmp_path):
    require_pi()

    def reply(body):
        tool = next(tool for tool in body["tools"] if tool["name"] == "openmontage")
        if len(requests) == 1:
            inputs = published_input(tool["description"], "initialize", {
                "title": "Video title", "pipeline_type": "cinematic", "project_id": "model-guessed-project",
            })
            return [tool_item("openmontage", {"action": "initialize", "input": inputs}, "discover_initialize")]
        if len(requests) == 2:
            inputs = published_input(tool["description"], "read_project", {"project_id": "model-guessed-project"})
            return [tool_item("openmontage", {"action": "read_project", "input": inputs}, "discover_read_project")]
        if len(requests) == 3:
            return [tool_item("openmontage", {"action": "read", "input": {
                "path": "schemas/artifacts/brief.schema.json",
            }}, "discover_artifact_schema")]
        return [text_item("The declared initialization and read inputs both worked.")]

    with model_server(reply) as (url, requests):
        profile = PiProfile(provider="local", model="test-model", base_url=url,
                            credential_env="STUDIO_LOCAL_KEY", reasoning=False, thinking_level="off",
                            max_output_tokens=256, max_turns=4, request_timeout_seconds=5,
                            idle_timeout_seconds=10, task_timeout_seconds=30,
                            price=PiPrice(input=0, output=0, cache_read=0, cache_write=0))
        config = StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})
        request = TaskCreate(brief="Initialize a cinematic project and read its project.json", profile_id="local")
        task = repository.create_task(request, snapshot_for(profile, request), "discover-action-inputs")
        projects = tmp_path / "projects"
        worker = Worker(repository, config, tmp_path / "runtime", projects,
                        environment={"STUDIO_LOCAL_KEY": "local-only-discovery-key"}, registry=ToolRegistry())
        result = await worker.run_once()

    assert len(requests) == 4, "Declared inputs and schema reads must work without an invalid_input retry loop"
    outputs = {item["call_id"]: item["output"] for item in requests[-1]["input"]
               if item.get("type") == "function_call_output"}
    assert "studio-policy:" not in outputs["discover_initialize"], outputs["discover_initialize"]
    assert "studio-policy:" not in outputs["discover_read_project"], outputs["discover_read_project"]
    marker = json.loads((projects / task.project_id / "project.json").read_text())
    assert marker["pipeline_type"] == "cinematic"
    assert marker["title"] == "Video title"
    assert "project.json" in outputs["discover_read_project"]
    assert "cinematic" in outputs["discover_read_project"]
    schema = json.loads(json.loads(outputs["discover_artifact_schema"])["content"])
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(schema)
    assert result.state == TaskState.BLOCKED
    assert result.error.code == "invalid_artifact", "Reading a project is not a finished video"
    assert repository.unresolved_intents(task.task_id) == []
