"""One leased Pi turn at a time. The agent owns all creative pipeline decisions."""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from production.contracts import CallIntent, ContractViolation, ErrorDTO, TaskState
from production.pi_config import prepare_pi, profile_for_snapshot
from production.pi_rpc import PiRPC
from production.recovery import RecoveryService, process_path, record_process, write_private
from production.task_service import TaskService
from production.tool_bridge import BridgeServer, ProductionToolBridge


ROOT = Path(__file__).resolve().parents[1]


class Worker:
    def __init__(self, repository, config, runtime_root, projects_dir, *, environment=None,
                 registry=None, tool_quotes=None, runner_factory=PiRPC):
        self.repository, self.config = repository, config
        self.runtime_root, self.projects_dir = Path(runtime_root).resolve(), Path(projects_dir).resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self.registry, self.tool_quotes, self.runner_factory = registry, tool_quotes, runner_factory
        self.worker_id = "worker-" + uuid4().hex
        self.service = TaskService(self.projects_dir)
        self.stopping = None
        self.held_leases = {}

    def _prompt(self, task, command):
        return (
            "You are the OpenMontage production agent. Your only production tool is openmontage. "
            "First use read to load AGENT_GUIDE.md and catalog to discover real tools. "
            "Select an existing pipeline from the brief, read its manifest and initialize this task's project. "
            "For every stage read its director skill, and read Layer 3 skills before calling providers. "
            "Write schema-valid canonical artifacts and checkpoints through the bridge. "
            "Stop this turn immediately after a checkpoint returns paused=true; only the browser can approve. "
            "On continue/resume read_project for existing checkpoints and artifacts, and continue the exact session. "
            "Keep completed stages; resume known external jobs with zero new POSTs. "
            "Finish local delivery after canonical compose/render_report and final_review pass; do not publish externally. "
            "Do not change providers, models, runtime or budget without a fresh browser approval. "
            "The backend verifies the actual rendered video, so a text response is never success.\n"
            + json.dumps({"brief": task.request.model_dump(mode="json"), "project_id": task.project_id,
                          "command": command.kind, "feedback": command.payload,
                          "frozen_configuration": task.config_snapshot.model_dump(mode="json")}, ensure_ascii=False)
        )

    async def _settled(self, runner):
        error = False
        async for event in runner.events():
            message = event.get("message") or {}
            if message.get("stopReason") in {"error", "aborted"}:
                error = True
            if event["type"] == "agent_settled":
                return error
        raise ContractViolation("Pi exited before settling", "rpc_error")

    def _transition(self, context, state, *, updates=None):
        task = self.repository.get_task(context.task_id)
        return self.repository.transition(task.task_id, state, expected_version=task.version, fence=context.fence, updates=updates)

    def _has_unknown_submit(self, task_id):
        return any(isinstance(intent, CallIntent) and intent.status in {"prepared", "reserved", "submitted", "outcome_unknown"} and not intent.external_job_id for intent in self.repository.unresolved_intents(task_id))

    async def _keep_lease(self, claim):
        while True:
            await asyncio.sleep(5)
            claim = await asyncio.to_thread(self.repository.heartbeat, claim)

    async def _stop_tools(self, bridge, context):
        if not await asyncio.to_thread(bridge.stop):
            task = self.repository.get_task(context.task_id)
            if task.state in {TaskState.RUNNING, TaskState.AWAITING_APPROVAL, TaskState.CANCEL_REQUESTED}:
                self._transition(context, TaskState.RECOVERY_REQUIRED, updates={"error": ErrorDTO(code="fence_conflict", message="Managed tool writers have not confirmed exit; execution ownership remains held", recovery_actions=["reconcile"])})
            raise ContractViolation("Managed tool writers have not confirmed exit", "fence_conflict")

    async def run_once(self):
        claim = self.repository.claim_command(self.worker_id)
        if claim is None:
            return None
        context = claim.context
        runner = server = observation = lease_keeper = bridge = project_lease = None
        started = time.monotonic()
        error = None
        active_time_path = self.runtime_root / "runs" / context.task_id / context.run_id / "active-time.json"
        previous_time = json.loads(active_time_path.read_text()).get("seconds", 0) if active_time_path.exists() else 0
        try:
            task = self.repository.get_task(context.task_id)
            if task.state == TaskState.CANCEL_REQUESTED:
                return self._transition(context, TaskState.CANCELLED)
            task = self._transition(context, TaskState.RUNNING, updates={"error": None})
            lease_keeper = asyncio.create_task(self._keep_lease(claim))
            from lib.checkpoint import CheckpointValidationError, StudioProjectLease
            project = self.projects_dir / context.project_id
            if project.resolve() != project:
                raise ContractViolation("Task project is a filesystem alias", "file_conflict")
            try:
                project_lease = StudioProjectLease(project, f"{context.task_id}/{context.run_id}/{context.fence}").acquire()
            except CheckpointValidationError:
                raise ContractViolation("Another process owns the project writer lock", "file_conflict") from None
            profile = profile_for_snapshot(self.config, task.config_snapshot)
            remaining = profile.task_timeout_seconds - previous_time
            if remaining <= 0:
                raise ContractViolation("Task active execution time limit reached", "timeout")
            bridge = ProductionToolBridge(self.repository, self.projects_dir, model_profile=profile,
                                          registry=self.registry, tool_quotes=self.tool_quotes, runtime_root=self.runtime_root)
            bridge.bind(context)
            if claim.command.kind == "continue":
                bridge.apply_approval(context)
            server = BridgeServer(bridge, context, profile)
            managed = prepare_pi(profile, context, self.runtime_root, environment=self.environment,
                                 trusted_extension=ROOT / "pi-runtime/extensions/openmontage.ts")
            for attempt in range(profile.max_retries + 1):
                left = remaining - (time.monotonic() - started)
                if left <= 0:
                    raise ContractViolation("Task active execution time limit reached", "timeout")
                runner = self.runner_factory(managed.argv, cwd=managed.work_dir, env={**managed.env, **server.environment},
                                             session_root=managed.session_root,
                                             redact_values=(*managed.redact_values, server.token),
                                             startup_timeout=profile.startup_timeout_seconds,
                                             request_timeout=profile.request_timeout_seconds)
                try:
                    session = await asyncio.wait_for(runner.start(context), left)
                    break
                except asyncio.TimeoutError:
                    raise ContractViolation("Task active execution time limit reached", "timeout") from None
                except ContractViolation as failure:
                    await runner.close()
                    if attempt == profile.max_retries or failure.code not in {"rpc_error", "timeout"}:
                        raise
                if lease_keeper.done():
                    await lease_keeper
            context = self.repository.bind_session(context, session)
            record_process(self.runtime_root, context, runner)
            observation = asyncio.create_task(self._settled(runner))
            if time.monotonic() - started >= remaining:
                raise ContractViolation("Task active execution time limit reached", "timeout")
            await runner.prompt(self._prompt(task, claim.command), command_id=claim.command.command_id)
            while True:
                if lease_keeper.done():
                    await lease_keeper
                task = self.repository.get_task(context.task_id)
                if task.state == TaskState.CANCEL_REQUESTED:
                    await runner.close()
                    await self._stop_tools(bridge, context)
                    return self._transition(context, TaskState.CANCELLED)
                if task.state == TaskState.AWAITING_APPROVAL:
                    with suppress(ContractViolation, asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(observation), 2)
                    await self._stop_tools(bridge, context)
                    return task
                if self.stopping is not None and self.stopping.is_set():
                    raise ContractViolation("Worker is stopping; resume the exact session", "rpc_error")
                if time.monotonic() - started > remaining:
                    raise ContractViolation("Task active execution time limit reached", "timeout")
                if observation.done():
                    failed = await observation
                    await self._stop_tools(bridge, context)
                    if self._has_unknown_submit(context.task_id):
                        raise ContractViolation("A submitted call has an unknown result", "outcome_unknown")
                    result = self.service.completion(context, request=task.request)
                    if result is not None:
                        return self._transition(context, TaskState.SUCCEEDED, updates={"result": result})
                    code = "rpc_error" if failed else "invalid_artifact"
                    state = TaskState.FAILED if failed else TaskState.BLOCKED
                    return self._transition(context, state, updates={"error": ErrorDTO(code=code, message="Pi settled without a verified canonical video or pending browser gate", recovery_actions=["resume"])})
                await asyncio.sleep(0.1)
        except (ContractViolation, asyncio.CancelledError) as failure:
            if isinstance(failure, asyncio.CancelledError):
                error = ErrorDTO(code="rpc_error", message="Worker interrupted; resume the exact session", recovery_actions=["resume"])
            else:
                error = ErrorDTO(code=failure.code, message=str(failure), recovery_actions=["reconcile", "resume"])
            task = self.repository.get_task(context.task_id)
            if task.state == TaskState.RUNNING:
                state = TaskState.RECOVERY_REQUIRED if self._has_unknown_submit(context.task_id) or error.code in {"file_conflict", "outcome_unknown", "fence_conflict"} else TaskState.FAILED if error.code == "timeout" else TaskState.BLOCKED
                self._transition(context, state, updates={"error": error})
            if isinstance(failure, asyncio.CancelledError):
                raise
            return self.repository.get_task(context.task_id)
        except Exception:
            error = ErrorDTO(code="internal_error", message="Worker failed; inspect private diagnostics and resume after reconciliation", recovery_actions=["reconcile", "resume"])
            task = self.repository.get_task(context.task_id)
            if task.state == TaskState.RUNNING:
                self._transition(context, TaskState.RECOVERY_REQUIRED if self._has_unknown_submit(context.task_id) else TaskState.FAILED, updates={"error": error})
            return self.repository.get_task(context.task_id)
        finally:
            if runner is not None:
                await runner.close()
            tools_stopped = True if bridge is None else await asyncio.to_thread(bridge.stop)
            if observation is not None:
                observation.cancel()
                await asyncio.gather(observation, return_exceptions=True)
            if server is not None:
                await asyncio.to_thread(server.close)
            if lease_keeper is not None:
                lease_keeper.cancel()
                await asyncio.gather(lease_keeper, return_exceptions=True)
            write_private(active_time_path, {"seconds": previous_time + time.monotonic() - started, "last_fence": context.fence})
            if not tools_stopped:
                if project_lease is not None:
                    self.held_leases[context.task_id] = project_lease
            else:
                if project_lease is not None:
                    project_lease.close()
                try:
                    self.repository.finish_command(claim, error=error)
                except ContractViolation:
                    # Keep the identity record if the database could not acknowledge exit.
                    pass
                else:
                    process_path(self.runtime_root, context).unlink(missing_ok=True)

    async def run_forever(self, stop_event):
        self.stopping = stop_event
        recovery = RecoveryService(self.repository, self.runtime_root, self.projects_dir, held_leases=self.held_leases)
        recover_at = 0
        while not stop_event.is_set():
            try:
                if time.monotonic() >= recover_at:
                    await recovery.recover()
                    recover_at = time.monotonic() + 5
                result = await self.run_once()
            except ContractViolation as error:
                if error.code != "dependency_unavailable":
                    raise
                result = None
            if result is None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), 0.25)
