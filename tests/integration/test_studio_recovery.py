"""Expired leases retain ownership until the actual official Pi has been stopped."""

import asyncio
import json

import pytest

from production.contracts import CallIntent, TaskCreate, TaskState
from production.pi_config import prepare_pi, snapshot_for
from production.pi_rpc import PiRPC
from production.recovery import process_path, record_process, write_private
from production.tool_bridge import BridgeServer, ProductionToolBridge
from tests.fixtures.studio.model_server import model_server
from tests.fixtures.studio.repository import require_pi
from tests.integration.test_studio_repository import repository, repository_factory
from tests.integration.test_studio_worker import local_config


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_identity", [False, True])
async def test_recovery_stops_original_real_pi_and_preserves_known_job_without_post(repository, tmp_path, mismatched_identity):
    require_pi()
    from production.recovery import RecoveryService

    with model_server() as (url, requests):
        config = local_config(url)
        profile = config.profiles["local"]
        request = TaskCreate(brief="A local recovery video", profile_id="local")
        task = repository.create_task(request, snapshot_for(profile, request), "recover-real-pi")
        claim = repository.claim_command("old-worker", lease_seconds=3)
        repository.transition(task.task_id, TaskState.RUNNING, expected_version=task.version, fence=claim.context.fence)
        bridge = ProductionToolBridge(repository, tmp_path / "projects", model_profile=profile)
        with BridgeServer(bridge, claim.context, profile) as server:
            managed = prepare_pi(profile, claim.context, tmp_path / "runtime", environment={"STUDIO_LOCAL_KEY": "local-test-key"})
            runner = PiRPC(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment}, session_root=managed.session_root, redact_values=managed.redact_values)
            session = await runner.start(claim.context)
            context = repository.bind_session(claim.context, session)
            record_process(tmp_path / "runtime", context, runner)
            ownership_path = process_path(tmp_path / "runtime", context)
            original_record = json.loads(ownership_path.read_text())
            if mismatched_identity:
                write_private(ownership_path, {**original_record, "start": "a different process start"})
            call = CallIntent(call_id="known-job-call", task_id=task.task_id, run_id=task.run_id, fence=context.fence, kind="tool", operation="generate", provider="local", request_sha256="a" * 64, price_status="quoted", reserved_usd_micros=10_000)
            repository.reserve_call(call)
            repository.update_call(call.model_copy(update={"status": "receipted", "external_job_id": "existing-external-job", "resume_reference": {"job_id": "existing-external-job"}}))
            try:
                await asyncio.sleep(3.1)
                service = RecoveryService(repository, tmp_path / "runtime", tmp_path / "projects")
                if mismatched_identity:
                    assert await service.recover() == 0
                    assert (await runner.inspect())["sessionId"] == session.session_id
                    assert repository.claim_command("another-worker") is None
                    write_private(ownership_path, original_record)
                assert await service.recover() == 1
                await asyncio.wait_for(runner.process.wait(), 5)
                assert runner.returncode is not None
                recovering = repository.get_task(task.task_id)
                assert recovering.state == TaskState.RECOVERY_REQUIRED
                assert repository.recovery_context(task.task_id).session == session
                assert repository.get_call(call.call_id).external_job_id == "existing-external-job"
                assert recovering.cost.reserved_usd_micros == 10_000
                assert requests == []
                assert await service.recover() == 0
            finally:
                await runner.close()
