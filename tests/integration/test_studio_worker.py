"""Worker lifecycle exercised through the official Pi and a local model service."""

import json
import subprocess
import threading

import pytest

from lib.config_model import PiPrice, PiProfile, StudioConfig
from production.contracts import ApprovalDecision, ContractViolation, TaskCommand, TaskCreate, TaskState
from production.pi_config import snapshot_for
from tests.fixtures.studio.model_server import model_server, text_item, tool_item
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory


def local_config(base_url, **changes):
    values = dict(provider="local", model="test-model", base_url=base_url,
                  credential_env="STUDIO_LOCAL_KEY", reasoning=False, thinking_level="off",
                  input=["text"], context_window=32768, max_output_tokens=256,
                  price=PiPrice(input=0, output=0, cache_read=0, cache_write=0),
                  request_timeout_seconds=5, idle_timeout_seconds=10, task_timeout_seconds=30)
    profile = PiProfile(**{**values, **changes})
    return StudioConfig(enabled=True, default_profile="local", profiles={"local": profile})


@pytest.mark.asyncio
async def test_real_pi_settled_is_blocked_until_a_canonical_render_exists(repository, tmp_path):
    require_pi()
    from production.worker import Worker

    with model_server() as (url, requests):
        config = local_config(url)
        request = TaskCreate(brief="A short local video", profile_id="local")
        task = repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-start-1")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"})
        result = await worker.run_once()
        assert result.task_id == task.task_id
        assert result.state == TaskState.BLOCKED
        assert result.result is None
        assert len(requests) == 1
        assert result.cost.reserved_usd_micros == 0
        session = tmp_path / "runtime/sessions" / task.task_id / task.run_id / "session.jsonl"
        assert json.loads(session.read_text().splitlines()[0])["type"] == "session"
        assert "local-only-test-key" not in json.dumps([event.model_dump(mode="json") for event in repository.events(task.task_id)])
        assert await worker.run_once() is None


@pytest.mark.asyncio
async def test_real_pi_browser_gate_releases_slot_and_continues_exact_session(repository, tmp_path):
    require_pi()
    from production.worker import Worker
    from tests.contracts.test_phase0_contracts import sample_artifact

    actions = [
        {"action": "read", "input": {"path": "AGENT_GUIDE.md"}},
        {"action": "initialize", "input": {"title": "Local video", "pipeline_type": "framework-smoke"}},
        {"action": "checkpoint", "input": {"stage": "research", "status": "awaiting_human", "artifacts": {"research_brief": sample_artifact("research_brief")}}},
    ]
    index = 0

    def reply(body):
        nonlocal index
        if index < len(actions):
            action = actions[index]
            index += 1
            return [tool_item("openmontage", action, f"worker-action-{index}")]
        return [text_item("Continue from the approved research checkpoint.")]

    with model_server(reply) as (url, requests):
        config = local_config(url)
        request = TaskCreate(brief="A short local video", profile_id="local")
        task = repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-gate-1")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"})
        awaiting = await worker.run_once()
        assert awaiting.state == TaskState.AWAITING_APPROVAL
        assert len(requests) == 3
        session = tmp_path / "runtime/sessions" / task.task_id / task.run_id / "session.jsonl"
        session_id = json.loads(session.read_text().splitlines()[0])["id"]
        approved = repository.decide_gate(ApprovalDecision(expected_version=awaiting.version, binding=awaiting.approval.binding, decision="approve"), idempotency_key="worker-approve-1")
        assert approved.state == TaskState.QUEUED
        continued = await worker.run_once()
        assert continued.state == TaskState.BLOCKED
        checkpoint = json.loads((tmp_path / "projects" / task.project_id / "checkpoint_research.json").read_text())
        assert checkpoint["status"] == "completed"
        assert checkpoint["human_approved"] is True
        assert json.loads(session.read_text().splitlines()[0])["id"] == session_id
        assert len(requests) == 4


@pytest.mark.asyncio
async def test_task_deadline_prevents_model_request_after_slow_startup(repository, tmp_path):
    require_pi()
    from production.worker import Worker

    with model_server() as (url, requests):
        config = local_config(url, task_timeout_seconds=0.01)
        request = TaskCreate(brief="A short local video", profile_id="local")
        task = repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-deadline-1")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"})
        result = await worker.run_once()
        assert result.state == TaskState.FAILED
        assert result.error.code == "timeout"
        assert requests == []


@pytest.mark.asyncio
async def test_live_cancel_stops_real_pi_and_never_runs_a_followup(repository, tmp_path):
    import asyncio
    require_pi()
    from production.worker import Worker

    entered, release = threading.Event(), threading.Event()

    def reply(body):
        entered.set()
        release.wait(timeout=5)
        return [text_item("A result that arrives after local cancellation.")]

    with model_server(reply) as (url, requests):
        config = local_config(url)
        request = TaskCreate(brief="A local video to cancel", profile_id="local")
        task = repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-cancel-1")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"})
        execution = asyncio.create_task(worker.run_once())
        try:
            for _ in range(300):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            running = repository.get_task(task.task_id)
            repository.enqueue_command(TaskCommand(command_id="cancel-live", task_id=task.task_id, run_id=task.run_id, kind="cancel", expected_version=running.version, idempotency_key="cancel-live-client"))
            cancelled = await asyncio.wait_for(execution, 10)
            assert cancelled.state == TaskState.CANCELLED
            assert len(requests) == 1
            assert await worker.run_once() is None
            assert not list((tmp_path / "runtime/processes").rglob("*.json"))
        finally:
            release.set()
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)


def completed_project(repository, tmp_path):
    from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS, write_checkpoint
    from lib.pipeline_loader import load_pipeline_readonly
    from production.artifact_io import ArtifactStore
    from tests.contracts.test_phase0_contracts import sample_artifact

    request = TaskCreate(brief="A local rendered video", profile_id="local")
    profile = local_config("http://127.0.0.1:1/v1").profiles["local"]
    task = repository.create_task(request, snapshot_for(profile, request), "verify-canonical-render")
    claim = repository.claim_command("verify-worker")
    repository.transition(task.task_id, TaskState.RUNNING, expected_version=1, fence=claim.context.fence)
    store = ArtifactStore(repository, tmp_path / "projects")
    project = store.initialize(claim.context, title="Local render", pipeline_type="animated-explainer")
    video = project / "renders/final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1:r=25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, timeout=20)
    report = {"version": "1.0", "outputs": [{"path": "renders/final.mp4", "format": "mp4", "resolution": "320x180", "duration_seconds": 1}]}
    review = {"version": "1.0", "output_path": "renders/final.mp4", "status": "pass", "checks": {"technical_probe": {"valid_container": True}, "visual_spotcheck": {"frames_sampled": 4}, "audio_spotcheck": {}, "promise_preservation": {}, "subtitle_check": {}}}
    for stage in load_pipeline_readonly("animated-explainer")["stages"]:
        name = CANONICAL_STAGE_ARTIFACTS[stage["name"]]
        artifacts = {name: report if name == "render_report" else sample_artifact(name)}
        if name == "edit_decisions":
            artifacts[name]["render_runtime"] = "ffmpeg"
        if stage["name"] == "compose":
            artifacts["final_review"] = review
        for artifact_name, value in artifacts.items():
            store.artifact(claim.context, artifact_name, value)
        with store.checkpoint_writer(claim.context) as writer:
            write_checkpoint(store.projects_dir, task.project_id, stage["name"], "completed", artifacts, pipeline_type="animated-explainer", human_approved=True, _writer=writer)
        if stage["name"] == "compose":
            break
    return claim.context, project


@pytest.mark.parametrize("tamper", ["video", "review"])
def test_completion_requires_actual_media_and_passing_canonical_review(repository, tmp_path, tamper):
    from production.task_service import TaskService
    context, project = completed_project(repository, tmp_path)
    service = TaskService(tmp_path / "projects")
    result = service.completion(context)
    assert result.verified is True
    assert (result.width, result.height, result.duration_seconds) == (320, 180, 1)
    assert result.video.path == "renders/final.mp4"
    if tamper == "video":
        for request in (TaskCreate(brief="Requested portrait", duration_seconds=1, aspect_ratio="9:16"), TaskCreate(brief="Requested longer video", duration_seconds=30)):
            with pytest.raises(ContractViolation, match="browser request"):
                service.completion(context, request=request)
    if tamper == "video":
        (project / "renders/final.mp4").write_bytes(b"not a video")
    else:
        path = project / "artifacts/final_review.json"
        review = json.loads(path.read_text())
        review["status"] = "revise"
        path.write_text(json.dumps(review))
    with pytest.raises(ContractViolation):
        service.completion(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("retries,expected_requests", [(0, 0), (1, 1)])
async def test_only_pre_prompt_startup_is_retried_and_model_posts_once(repository, tmp_path, retries, expected_requests):
    require_pi()
    from production.pi_rpc import PiRPC
    from production.worker import Worker

    attempts = []

    def runner_factory(*args, **kwargs):
        runner = PiRPC(*args, **kwargs)
        original_start = runner.start

        async def start(context):
            session = await original_start(context)
            attempts.append(runner.process.pid)
            if len(attempts) == 1:
                raise ContractViolation("Injected pre-prompt startup timeout", "timeout")
            return session

        runner.start = start
        return runner

    with model_server() as (url, requests):
        config = local_config(url, max_retries=retries)
        request = TaskCreate(brief="A local startup retry", profile_id="local")
        repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-startup-retry")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"}, runner_factory=runner_factory)
        result = await worker.run_once()
        assert len(attempts) == retries + 1
        assert len(requests) == expected_requests
        assert result.state == (TaskState.BLOCKED if retries else TaskState.FAILED)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["corrupt", "missing_after_run"])
async def test_invalid_active_time_evidence_is_not_reset_or_sent_to_model(repository, tmp_path, evidence):
    require_pi()
    from production.worker import Worker

    with model_server() as (url, requests):
        config = local_config(url)
        request = TaskCreate(brief="A local timer recovery", profile_id="local")
        task = repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-bad-counter")
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"})
        counter = tmp_path / "runtime/runs" / task.task_id / task.run_id / "active-time.json"
        if evidence == "corrupt":
            counter.parent.mkdir(parents=True)
            counter.write_text("{")
        else:
            blocked = await worker.run_once()
            counter.unlink()
            repository.enqueue_command(TaskCommand(command_id="resume-missing-counter", task_id=task.task_id, run_id=task.run_id, kind="resume", expected_version=blocked.version, idempotency_key="missing-counter-client"))
        result = await worker.run_once()
        assert result.state == TaskState.RECOVERY_REQUIRED
        assert result.error.code == "file_conflict"
        assert len(requests) == (1 if evidence == "missing_after_run" else 0)
        if evidence == "corrupt":
            assert counter.read_text() == "{"
        else:
            assert not counter.exists()


@pytest.mark.asyncio
async def test_slow_repository_does_not_block_worker_event_loop(repository, tmp_path, monkeypatch):
    import asyncio
    require_pi()
    from production.worker import Worker

    entered, release = threading.Event(), threading.Event()
    original_get = repository.get_task
    observed = []

    def delayed_get(task_id):
        if not entered.is_set():
            entered.set()
            observed.append(release.wait(timeout=1))
        return original_get(task_id)

    with model_server() as (url, requests):
        config = local_config(url)
        request = TaskCreate(brief="A local responsive worker", profile_id="local")
        repository.create_task(request, snapshot_for(config.profiles["local"], request), "worker-slow-repository")
        monkeypatch.setattr(repository, "get_task", delayed_get)
        worker = Worker(repository, config, tmp_path / "runtime", tmp_path / "projects", environment={"STUDIO_LOCAL_KEY": "local-only-test-key"})
        execution = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(entered.wait, 3)
        release.set()
        result = await execution
        assert observed == [True]
        assert result.state == TaskState.BLOCKED
        assert len(requests) == 1
