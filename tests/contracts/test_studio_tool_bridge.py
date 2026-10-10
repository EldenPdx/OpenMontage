from pathlib import Path

import pytest

from production.contracts import ConfigSnapshot, ContractViolation, RunContext, SessionReference


def context():
    return RunContext(task_id="task-a", project_id="project-a", run_id="run-a", fence=1,
                      config_snapshot=ConfigSnapshot(profile_id="local", provider="local", model="test",
                                                     api="openai-responses", configuration_sha256="a" * 64),
                      session=SessionReference(path="task-a/run-a/session.jsonl"))


def test_controlled_paths_reject_escape_secrets_and_checkpoint_writes(tmp_path):
    from production.policy import ToolPolicy
    policy = ToolPolicy(tmp_path / "repo", tmp_path / "projects")
    run = context()
    safe = policy.project_path(run, "assets/video/clip.mp4", output=True)
    assert safe == (tmp_path / "projects/project-a/assets/video/clip.mp4").resolve()
    for path in ["../other/clip.mp4", "/etc/passwd", "checkpoint_idea.json", "project.json", ".env", "artifacts/brief.json"]:
        with pytest.raises(ContractViolation):
            policy.project_path(run, path, output=True)
    (tmp_path / "projects/project-a/assets").mkdir(parents=True)
    (tmp_path / "projects/project-a/assets/escape").symlink_to(tmp_path)
    with pytest.raises(ContractViolation):
        policy.project_path(run, "assets/escape/exposed.mp4", output=True)
    with pytest.raises(ContractViolation):
        policy.inputs(run, {"subtitle_style": {"font": "Arial';movie=https://invalid.example/video"}})
    for inputs in [{"image_paths": [str(tmp_path / "projects/other/assets/source.png")]},
                   {"mask_path": str(tmp_path / "projects/other/assets/mask.png")},
                   {"image_urls": ["https://invalid.example/image.png"]}]:
        with pytest.raises(ContractViolation):
            policy.inputs(run, inputs)


def test_instruction_reads_include_layer_three_but_exclude_credentials(tmp_path):
    from production.policy import ToolPolicy
    path = tmp_path / ".agents/skills/provider/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Pinned provider guidance")
    policy = ToolPolicy(tmp_path, tmp_path / "projects")
    assert policy.instruction_path(".agents/skills/provider/SKILL.md") == path.resolve()
    for name in [".env", "config.yaml", "skills/../config.yaml", "production/policy.py"]:
        with pytest.raises(ContractViolation):
            policy.instruction_path(name)


def test_canonical_artifact_schemas_are_readable_without_exposing_other_json(tmp_path):
    from production.policy import ToolPolicy
    policy = ToolPolicy(tmp_path, tmp_path / "projects")
    directory = tmp_path / "schemas/artifacts"
    directory.mkdir(parents=True)
    schema = directory / "brief.schema.json"
    schema.write_text('{"type":"object"}')
    assert policy.instruction_path("schemas/artifacts/brief.schema.json").read_text() == '{"type":"object"}'
    secret = tmp_path / "secret.json"
    secret.write_text('{"secret":"private"}')
    (directory / "escape.schema.json").symlink_to(secret)
    (directory / ".private.schema.json").write_text('{"secret":"private"}')
    for name in ("secret.json", "schemas/artifacts/secret.json", "schemas/artifacts/escape.schema.json",
                 "schemas/artifacts/.private.schema.json"):
        with pytest.raises(ContractViolation):
            policy.instruction_path(name)


def test_existing_cli_cannot_write_an_active_studio_project(tmp_path, monkeypatch):
    from lib.checkpoint import CheckpointValidationError, init_project
    project = tmp_path / "owned"
    project.mkdir()
    (project / ".studio-owner.json").write_text('{"task_id":"task-a"}')
    with pytest.raises(CheckpointValidationError, match="Studio"):
        init_project("owned", title="forged", pipeline_type="framework-smoke", pipeline_dir=tmp_path)
    from tools.base_tool import BaseTool, ToolResult
    class CLIWriter(BaseTool):
        name = "cli_writer"
        def execute(self, inputs):
            Path(inputs["output_path"]).write_bytes(b"unapproved")
            return ToolResult(success=True)
    with pytest.raises(CheckpointValidationError, match="Studio"):
        CLIWriter().execute({"project_dir": str(project), "output_path": str(project / "clip.mp4")})
    from tools.cost_tracker import CostTracker
    with pytest.raises(CheckpointValidationError, match="Studio"):
        CostTracker(cost_log_path=project / "cost_log.json").estimate("image", "generate", 1)
    class DefaultCLIWriter(BaseTool):
        name = "default_cli_writer"
        def execute(self, inputs):
            Path("default.mp4").write_bytes(b"unapproved default output")
            return ToolResult(success=True)
    monkeypatch.chdir(project)
    with pytest.raises(CheckpointValidationError, match="Studio"):
        DefaultCLIWriter().execute({})
    assert not (project / "default.mp4").exists()


def test_render_asset_ids_resolve_to_their_owned_media_paths(tmp_path):
    from production.policy import ToolPolicy
    policy = ToolPolicy(tmp_path, tmp_path / "projects")
    inputs = {"asset_manifest": {"assets": [{"id": "clip-1", "path": "assets/video/clip.mp4"}]},
              "edit_decisions": {"cuts": [{"source": "clip-1"}]}}
    resolved = policy.inputs(context(), inputs)
    assert resolved["edit_decisions"]["cuts"][0]["source"] == str(tmp_path / "projects/project-a/assets/video/clip.mp4")
