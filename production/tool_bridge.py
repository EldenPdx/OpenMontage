"""Authenticated run-bound structured bridge for the trusted Pi extension."""

import asyncio
import copy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
import multiprocessing
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from uuid import uuid4

import jsonschema

from production.artifact_io import ArtifactStore, file_sha256
from production.contracts import (
    ApprovalBinding, ApprovalRequest, ApprovalScope, CallIntent, ContractViolation,
    ErrorDTO, RunContext, TaskState, ToolCall, ToolReceipt, canonical_sha256,
)
from production.pi_config import estimated_cost_usd_micros
from production.policy import ToolPolicy, media_configuration_sha256

ROOT = Path(__file__).resolve().parents[1]


def _tool_child(tool, inputs, project, connection):
    """No request starts until its parent has durably recorded process ownership."""
    os.setsid()
    os.chdir(project)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.set_wakeup_fd(-1)
    if getattr(tool, "studio_base_url", None):
        os.environ["NEW_API_BASE_URL"] = tool.studio_base_url
    from lib import events
    events._write_lock = threading.Lock()
    connection.send({"ready": True})
    if connection.recv() != "execute":
        return
    try:
        connection.send({"result": tool.execute(inputs)})
    except BaseException:
        connection.send({"error": "Production tool process failed"})
    finally:
        connection.close()


def tool_process_records(runtime_root, context):
    directory = Path(runtime_root) / "tool-processes" / context.task_id / context.run_id
    return [(path, json.loads(path.read_text())) for path in directory.glob("*.json")]


def confirm_tool_process_exit(repository, record, context):
    from production.recovery import _group_exists, process_identity
    call = repository.get_call(record.get("call_id", ""))
    if (call.task_id, call.run_id, record.get("task_id"), record.get("run_id")) != (context.task_id, context.run_id, context.task_id, context.run_id):
        raise ContractViolation("Tool process belongs to another run", "fence_conflict")
    pid = record.get("pid", 0)
    if pid <= 1 or record.get("pgid") != pid or record.get("fence") not in {context.fence, context.fence - 1}:
        raise ContractViolation("Tool process ownership could not be verified", "fence_conflict")
    expected = {key: record[key] for key in ("start", "pgid", "command", "cwd")}
    current = process_identity(pid)
    if current is None:
        if _group_exists(pid):
            raise ContractViolation("Orphan tool process group requires reconciliation", "fence_conflict")
        return True
    if current != expected:
        raise ContractViolation("Tool PID was replaced; refusing to signal it", "fence_conflict")
    os.killpg(pid, signal.SIGTERM)
    deadline, killed = time.monotonic() + 3, False
    while _group_exists(pid):
        current = process_identity(pid)
        if current is not None and current != expected:
            raise ContractViolation("Tool PID changed during shutdown", "fence_conflict")
        if time.monotonic() > deadline:
            raise ContractViolation("Managed tool process group did not exit", "fence_conflict")
        if not killed and time.monotonic() > deadline - 2:
            os.killpg(pid, signal.SIGKILL)
            killed = True
        time.sleep(0.05)
    return True


class ProductionToolBridge:
    def __init__(self, repository, projects_dir, *, repo_root=ROOT, model_profile=None,
                 allowed_tools=None, registry=None, tool_quotes=None, runtime_root=None):
        if registry is None:
            from tools.tool_registry import registry as installed_registry
            registry = installed_registry
            registry.ensure_discovered()
        self.repository = repository
        self.registry = registry
        self.policy = ToolPolicy(repo_root, projects_dir)
        self.store = ArtifactStore(repository, projects_dir)
        self.model_profile = model_profile
        self.allowed_tools = frozenset(allowed_tools if allowed_tools is not None else ToolPolicy.ALLOWED)
        self.tool_quotes = dict(tool_quotes or {})
        self.contexts = {}
        self.call_lock = threading.RLock()
        self.runtime_root = Path(runtime_root or ROOT / ".runtime/studio").resolve()
        self.jobs = {}
        self.jobs_lock = threading.RLock()
        self.stopped = threading.Event()
        self.captured_jobs = set()
        self.started_calls = set()

    @staticmethod
    def _signal_tool(process, sig):
        expected = getattr(process, "studio_identity", None)
        if not process.is_alive():
            if expected is None:
                return
            from production.recovery import _group_exists, process_identity
            if not _group_exists(process.pid):
                return
            current = process_identity(process.pid)
            if current is not None and current != expected:
                return
        try:
            os.killpg(process.pid, sig)
        except PermissionError:
            process.join(0)
            from production.recovery import _group_exists
            if process.is_alive() or _group_exists(process.pid):
                raise
        except ProcessLookupError:
            if process.is_alive():
                try:
                    os.kill(process.pid, sig)
                except ProcessLookupError:
                    pass

    def stop(self):
        self.stopped.set()
        with self.jobs_lock:
            jobs = list(self.jobs.values())
        for process in jobs:
            self._signal_tool(process, signal.SIGTERM)
        deadline = time.monotonic() + 1
        for process in jobs:
            process.join(max(0, deadline - time.monotonic()))
        for process in jobs:
            self._signal_tool(process, signal.SIGKILL)
        deadline = time.monotonic() + 1
        for process in jobs:
            process.join(max(0, deadline - time.monotonic()))
        from production.recovery import _group_exists
        while True:
            stopped = all(not process.is_alive() and not _group_exists(process.pid) for process in jobs)
            if stopped or time.monotonic() >= deadline:
                return stopped
            time.sleep(0.02)

    def _capture_job(self, tool, inputs, call):
        if call.call_id in self.captured_jobs:
            return
        path = inputs.get("job_path")
        if not path or not Path(path).is_file() or Path(path).stat().st_size > 64 * 1024:
            return
        from tools._newapi.config import NewAPISettings
        from lib.config_model import OpenMontageConfig
        from tools._newapi.models import validate_resume
        settings = NewAPISettings(OpenMontageConfig.load(Path(tool.config_path)).newapi)
        job = validate_resume(settings, json.loads(Path(path).read_text()),
                              tool=tool.name, model=inputs.get("model"), output_path=inputs["output_path"])
        intent = self.repository.get_call(call.call_id)
        if intent.external_job_id and intent.external_job_id != job["id"]:
            raise ContractViolation("Provider job receipt changed", "file_conflict")
        if not intent.external_job_id:
            self.repository.update_call(intent.model_copy(update={"status": "receipted", "external_job_id": job["id"],
                                                                 "resume_reference": {**(intent.resume_reference or {}), "job": job}}))
        self.captured_jobs.add(call.call_id)

    async def _execute_tool(self, tool, inputs, call):
        from lib.checkpoint import StudioProjectLease, studio_checkpoint_authority
        from production.recovery import process_identity, write_private
        project = self.store.project(call.context)
        owner = f"{call.context.task_id}/{call.context.run_id}/{call.context.fence}"
        parent, child = multiprocessing.get_context("fork").Pipe()
        record = self.runtime_root / "tool-processes" / call.context.task_id / call.context.run_id / (call.call_id + ".json")
        with StudioProjectLease(project, owner), studio_checkpoint_authority(lambda: self.ready(call.context)):
            process = multiprocessing.get_context("fork").Process(target=_tool_child, args=(tool, inputs, str(project), child))
            with self.jobs_lock:
                if self.stopped.is_set():
                    raise ContractViolation("Tool supervisor is stopping", "state_conflict")
                process.start()
                self.jobs[call.call_id] = process
            child.close()
            try:
                deadline = time.monotonic() + 5
                while not parent.poll():
                    self._capture_job(tool, inputs, call)
                    if not process.is_alive() or time.monotonic() > deadline or self.stopped.is_set():
                        raise ContractViolation("Tool process did not become ready", "rpc_error")
                    await asyncio.sleep(0.05)
                if parent.recv() != {"ready": True}:
                    raise ContractViolation("Invalid tool process handshake", "rpc_error")
                identity = process_identity(process.pid)
                if identity is None or identity["pgid"] != process.pid or identity["cwd"] != str(project):
                    raise ContractViolation("Tool process ownership is invalid", "fence_conflict")
                process.studio_identity = identity
                write_private(record, {**identity, "pid": process.pid, "task_id": call.context.task_id,
                                      "run_id": call.context.run_id, "fence": call.context.fence, "call_id": call.call_id})
                current = self.repository.get_call(call.call_id)
                if current.status == "prepared":
                    self.repository.update_call(current.model_copy(update={"status": "submitted"}))
                self.started_calls.add(call.call_id)
                parent.send("execute")
                next_check = time.monotonic()
                while not parent.poll():
                    self._capture_job(tool, inputs, call)
                    if not process.is_alive() or self.stopped.is_set():
                        raise ContractViolation("Managed tool process was interrupted", "outcome_unknown")
                    if time.monotonic() >= next_check:
                        self.ready(call.context)
                        next_check = time.monotonic() + 0.5
                    await asyncio.sleep(0.05)
                message = parent.recv()
                self._capture_job(tool, inputs, call)
                if "result" not in message:
                    raise ContractViolation("Managed tool execution failed", "outcome_unknown")
                return message["result"]
            finally:
                self._signal_tool(process, signal.SIGTERM)
                await asyncio.to_thread(process.join, 0.5)
                self._signal_tool(process, signal.SIGKILL)
                await asyncio.to_thread(process.join, 0.5)
                parent.close()
                from production.recovery import _group_exists
                if not process.is_alive() and not _group_exists(process.pid):
                    record.unlink(missing_ok=True)
                    with self.jobs_lock:
                        self.jobs.pop(call.call_id, None)

    def bind(self, context):
        self.repository.assert_fence(context)
        if self.model_profile is not None and canonical_sha256(self.model_profile.model_dump(mode="json")) != context.config_snapshot.configuration_sha256:
            raise ContractViolation("Model profile does not match the immutable run", "profile_unavailable")
        self.contexts[context.run_id] = context
        project = self.store.project(context)
        project.mkdir(parents=True, exist_ok=True)
        owner = project / ".studio-owner.json"
        if owner.exists() and json.loads(owner.read_text()).get("task_id") != context.task_id:
            raise ContractViolation("Project belongs to another task", "file_conflict")
        owner.write_text(json.dumps({"task_id": context.task_id, "run_id": context.run_id}))
        owner.chmod(0o600)

    def initialize(self, context, *, title, pipeline_type):
        from lib.pipeline_loader import load_pipeline_readonly
        self.ready(context)
        if pipeline_type not in {path.stem for path in (self.policy.repo_root / "pipeline_defs").glob("*.yaml")}:
            raise ContractViolation("Choose a declared production pipeline", "invalid_input")
        try:
            load_pipeline_readonly(pipeline_type)
        except Exception:
            raise ContractViolation("Unknown production pipeline", "invalid_input") from None
        marker = self.store.project(context) / "project.json"
        if marker.exists() and json.loads(marker.read_text())["pipeline_type"] != pipeline_type:
            raise ContractViolation("Pipeline cannot change after project initialization", "file_conflict")
        self.store.initialize(context, title=title, pipeline_type=pipeline_type)
        return {"project_id": context.project_id, "pipeline_type": pipeline_type}

    def ready(self, context):
        self.repository.assert_fence(context)
        task = self.repository.get_task(context.task_id)
        if task.state != TaskState.RUNNING or (task.approval and task.approval.status == "pending"):
            raise ContractViolation("Task is paused or no longer running", "approval_conflict")
        return task

    def catalog(self, context):
        self.ready(context)
        tools = []
        for name in sorted(self.allowed_tools):
            tool = self.registry.get(name)
            if tool is not None:
                tools.append({"name": name, "provider": tool.provider, "capability": tool.capability,
                              "input_schema": tool.input_schema, "agent_skills": tool.agent_skills,
                              "status": tool.get_status().value})
        return {"tools": tools, "instructions": "AGENT_GUIDE.md", "render_runtimes": ["ffmpeg"],
                "composition_policy": "Fixed FFmpeg media operations; programmable HTML, scripts and custom workflows are disabled",
                "pipelines": sorted(path.name for path in (self.policy.repo_root / "pipeline_defs").glob("*.yaml"))}

    def read(self, context, path):
        self.ready(context)
        return {"path": path, "content": self.policy.instruction_path(path).read_text(encoding="utf-8")}

    def read_project(self, context, path):
        self.ready(context)
        project = self.store.project(context)
        candidate = (project / path).resolve()
        if not candidate.is_relative_to(project) or any(part.startswith(".") for part in Path(path).parts):
            raise ContractViolation("Private project file cannot be read", "forbidden")
        relative = candidate.relative_to(project).as_posix()
        if not (relative == "project.json" or relative.startswith("checkpoint_") or relative.startswith("artifacts/")) or candidate.suffix != ".json":
            raise ContractViolation("Only canonical project JSON is exposed", "forbidden")
        if not candidate.is_file() or candidate.stat().st_size > 256 * 1024:
            raise ContractViolation("Project JSON is missing or too large", "not_found")
        return {"path": relative, "content": json.loads(candidate.read_text())}

    def _validate_artifact(self, context, name, value):
        from schemas.artifacts import ARTIFACT_NAMES, validate_artifact
        task = self.ready(context)
        if name not in ARTIFACT_NAMES:
            raise ContractViolation("Unknown canonical artifact", "invalid_artifact")
        try:
            validate_artifact(name, value)
        except Exception:
            raise ContractViolation("Artifact failed its canonical schema", "invalid_artifact") from None
        approvals = self.store.project(context) / ".studio-approved-files.json"
        approved = json.loads(approvals.read_text()) if approvals.exists() else {}
        relative = f"artifacts/{name}.json"
        if relative in approved:
            encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            if sha256(encoded).hexdigest() != approved[relative]:
                raise ContractViolation("Approved artifacts require a new browser revision", "approval_conflict")
            return False
        # Schema references do not grant authority to read arbitrary filesystem paths.
        if name == "asset_manifest":
            for asset in value.get("assets", []):
                if asset.get("path"):
                    self.policy.project_path(context, asset["path"])
        if name == "edit_decisions":
            if value.get("render_runtime") not in {None, "ffmpeg"}:
                raise ContractViolation("Programmable render compositions are not enabled in this tool boundary", "forbidden")
        return True

    def artifact(self, context, name, value):
        if not self._validate_artifact(context, name, value):
            return self.store.reference(context, f"artifacts/{name}.json").model_dump(mode="json")
        return self.store.artifact(context, name, value).model_dump(mode="json")

    def _media_evidence(self, context, artifacts):
        project = self.store.project(context)
        manifest = artifacts.get("asset_manifest")
        if manifest is None and (project / "artifacts/asset_manifest.json").is_file():
            manifest = json.loads((project / "artifacts/asset_manifest.json").read_text())
        manifest = manifest or {"assets": []}
        paths = {str(asset["path"]) for asset in manifest["assets"] if asset.get("path")}
        edit = artifacts.get("edit_decisions")
        if edit:
            options = edit.get("metadata", {}).get("compose_options", {})
            resolved = self.policy.inputs(context, {"asset_manifest": manifest, "edit_decisions": edit, **options})
            def references(value, key=""):
                if isinstance(value, dict):
                    for name, item in value.items():
                        references(item, name)
                elif isinstance(value, list):
                    for item in value:
                        references(item, key)
                elif isinstance(value, str) and key in ToolPolicy.PATH_KEYS and key != "output_path":
                    paths.add(value)
            references(resolved)
        evidence = {}
        for value in paths:
            path = self.policy.project_path(context, value)
            if not path.is_file():
                raise ContractViolation("Review requires an actual media file", "invalid_artifact")
            evidence[path.relative_to(project).as_posix()] = file_sha256(path)
        return evidence

    def _canonical_render_inputs(self, context, inputs):
        project = self.store.project(context)
        result = dict(inputs)
        def semantic(value):
            if isinstance(value, dict):
                return {key: semantic(item) for key, item in value.items() if key != "decision_log_ref"}
            if isinstance(value, list):
                return [semantic(item) for item in value]
            return value
        for name in ("asset_manifest", "edit_decisions", "proposal_packet"):
            path = project / f"artifacts/{name}.json"
            if not path.is_file():
                if name == "proposal_packet":
                    continue
                raise ContractViolation("Rendering requires approved canonical artifacts", "approval_conflict")
            canonical = json.loads(path.read_text())
            if name in result and semantic(result[name]) != semantic(canonical):
                raise ContractViolation("Inline renderer inputs differ from approved canonical files", "approval_conflict")
            result[name] = canonical
        approved_options = result["edit_decisions"].get("metadata", {}).get("compose_options", {})
        if set(approved_options) & {"operation", "output_path", "asset_manifest", "edit_decisions", "proposal_packet"}:
            raise ContractViolation("Render metadata cannot replace canonical artifacts or managed output", "forbidden")
        for name in set(result) - {"operation", "output_path", "asset_manifest", "edit_decisions", "proposal_packet"}:
            if name not in approved_options or result[name] != approved_options[name]:
                raise ContractViolation("Render overrides require approved edit metadata", "approval_conflict")
        result.update(approved_options)
        media_map = project / ".studio-approved-media.json"
        approved = json.loads(media_map.read_text()) if media_map.is_file() else {}
        for relative, digest in self._media_evidence(context, result).items():
            if approved.get(relative) != digest:
                raise ContractViolation("Render media differs from the browser-approved content", "approval_conflict")
        return result

    def _scope(self, task):
        project = self.store.projects_dir / task.project_id
        proposal_path = project / "artifacts/proposal_packet.json"
        edit_path = project / "artifacts/edit_decisions.json"
        runtime = None
        if proposal_path.exists():
            runtime = json.loads(proposal_path.read_text()).get("production_plan", {}).get("render_runtime")
        if edit_path.exists():
            edit_runtime = json.loads(edit_path.read_text()).get("render_runtime")
            if runtime and edit_runtime and runtime != edit_runtime:
                raise ContractViolation("Render runtime differs from the approved proposal", "approval_conflict")
            runtime = runtime or edit_runtime
        if runtime not in {None, "ffmpeg"}:
            raise ContractViolation("Only explicitly selected FFmpeg rendering is enabled", "forbidden")
        scope = ApprovalScope(provider=task.config_snapshot.provider, model=task.config_snapshot.model,
                             render_runtime=runtime,
                             budget_usd_micros=task.request.budget_usd_micros,
                             configuration_sha256=task.config_snapshot.configuration_sha256,
                             media_models=task.config_snapshot.media_models, narration=task.request.narration)
        consent = self._unknown_consent(task)
        if consent:
            scope = scope.model_copy(update={"unknown_price": True, "authorized_limit_usd_micros": consent.scope.authorized_limit_usd_micros})
        return scope

    def _unknown_consent(self, task):
        consent = self._model_consent(task)
        return consent if consent and consent.scope.unknown_price else None

    def _model_consent(self, task):
        path = self.store.projects_dir / task.project_id / ".studio-cost-consent.json"
        gate_id = json.loads(path.read_text()).get("gate_id") if path.exists() else None
        consent = self.repository.approved_gate(task.task_id, gate_id) if gate_id else None
        if consent and (consent.scope.configuration_sha256 == task.config_snapshot.configuration_sha256
                        and consent.scope.budget_usd_micros == task.request.budget_usd_micros):
            return consent
        return None

    def _open_cost_gate(self, context, task, *, limit=None, unknown=True):
        limit = limit if limit is not None else min(task.config_snapshot.single_action_approval_usd_micros or 500_000, task.request.budget_usd_micros)
        if limit <= 0:
            raise ContractViolation("No budget is available for an unknown-price request", "budget_exceeded")
        if limit > task.request.budget_usd_micros:
            raise ContractViolation("Model reservation exceeds the task budget", "budget_exceeded")
        scope = self._scope(task).model_copy(update={"unknown_price": unknown, "authorized_limit_usd_micros": limit})
        evidence = {"version": "1.0", "kind": "model_cost", "provider": scope.provider, "model": scope.model,
                    "unknown_price": unknown, "reserved_per_request_usd_micros": limit,
                    "budget_usd_micros": scope.budget_usd_micros,
                    "notice": ("Actual gateway fees are unknown. This reservation estimate is not a gateway billing hard cap. Holds remain until trusted billing reconciliation."
                               if unknown else "Approve this quoted maximum model-request reservation. Actual usage is settled at the frozen configured rate.")}
        reference = self.store.write_json(context, "artifacts/model_cost.json", evidence)
        binding = ApprovalBinding(task_id=context.task_id, run_id=context.run_id, gate_id="gate-" + uuid4().hex,
                                  artifact_revision=reference.revision, artifact_sha256=reference.sha256,
                                  checkpoint_revision=reference.revision, checkpoint_sha256=reference.sha256,
                                  scope_sha256=canonical_sha256(scope))
        self.repository.put_gate(ApprovalRequest(binding=binding, stage="model_cost", artifact=reference, checkpoint=reference,
                                                scope=scope, summary=evidence["notice"]), expected_version=task.version, fence=context.fence)

    def _media_consent(self, task, fingerprint):
        path = self.store.projects_dir / task.project_id / ".studio-media-consents.json"
        pointer = json.loads(path.read_text()).get(fingerprint) if path.exists() else None
        consent = self.repository.approved_gate(task.task_id, pointer["gate_id"]) if pointer else None
        if consent and consent.scope.configuration_sha256 == task.config_snapshot.configuration_sha256 and consent.scope.budget_usd_micros == task.request.budget_usd_micros:
            return consent, pointer["call_id"]
        return None, None

    def _open_media_cost_gate(self, context, task, call, tool, fingerprint, *, amount=None):
        unknown = amount is None
        limit = min(task.config_snapshot.single_action_approval_usd_micros or 500_000, task.request.budget_usd_micros) if unknown else amount
        if limit <= 0 or limit > task.request.budget_usd_micros:
            raise ContractViolation("Media reservation exceeds the task budget", "budget_exceeded")
        scope = self._scope(task).model_copy(update={"unknown_price": unknown, "authorized_limit_usd_micros": limit})
        evidence = {"version": "1.0", "kind": "media_cost", "tool": tool.name, "provider": tool.provider,
                    "model": call.inputs.get("model"), "call_id": call.call_id, "request_sha256": fingerprint,
                    "inputs": call.inputs,
                    "unknown_price": unknown, "reserved_usd_micros": limit,
                    "notice": "Approve this exact media request. Unknown fees retain their reservation; the estimate is not a gateway billing hard cap."}
        reference = self.store.write_json(context, "artifacts/media_cost.json", evidence)
        binding = ApprovalBinding(task_id=context.task_id, run_id=context.run_id, gate_id="gate-" + uuid4().hex,
                                  artifact_revision=reference.revision, artifact_sha256=reference.sha256,
                                  checkpoint_revision=reference.revision, checkpoint_sha256=reference.sha256,
                                  scope_sha256=canonical_sha256(scope))
        self.repository.put_gate(ApprovalRequest(binding=binding, stage="media_cost", artifact=reference, checkpoint=reference,
                                                scope=scope, summary=evidence["notice"]), expected_version=task.version, fence=context.fence)

    def checkpoint(self, context, *, stage, status, artifacts, summary="Stage ready for review",
                   pipeline_type=None, human_approved=None, **extra):
        from lib.checkpoint import _stage_requires_approval, write_checkpoint
        if human_approved is not None or extra:
            raise ContractViolation("Approval is supplied only by the backend", "forbidden")
        task = self.ready(context)
        project = self.store.project(context)
        marker = json.loads((project / "project.json").read_text())
        pipeline = marker["pipeline_type"]
        if pipeline_type not in {None, pipeline}:
            raise ContractViolation("Pipeline differs from the managed project", "forbidden")
        if status not in {"in_progress", "awaiting_human", "completed", "failed"}:
            raise ContractViolation("Invalid checkpoint status")
        if _stage_requires_approval(pipeline, stage) and status == "completed":
            raise ContractViolation("A gated checkpoint must await the browser approval", "approval_conflict")
        for name, value in artifacts.items():
            self._validate_artifact(context, name, value)
        cost = task.cost
        snapshot = {"total_spent_usd": cost.spent_usd_micros / 1_000_000,
                    "total_reserved_usd": cost.reserved_usd_micros / 1_000_000,
                    "budget_remaining_usd": (cost.budget_usd_micros - cost.spent_usd_micros - cost.reserved_usd_micros) / 1_000_000,
                    "price_status": cost.price_status, "unknown_call_count": cost.unknown_call_count}
        with self.store.checkpoint_writer(context) as writer:
            def canonical_writer(path, checkpoint):
                for name, value in checkpoint["artifacts"].items():
                    self.artifact(context, name, value)
                writer(path, checkpoint)
            write_checkpoint(self.store.projects_dir, context.project_id, stage, status, artifacts,
                             pipeline_type=pipeline, cost_snapshot=snapshot,
                             metadata={"studio_asset_sha256": self._media_evidence(context, artifacts)} if status in {"awaiting_human", "completed"} else None,
                             _writer=canonical_writer)
        reference = self.store.reference(context, f"checkpoint_{stage}.json")
        if status == "awaiting_human":
            from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS
            artifact = self.store.reference(context, f"artifacts/{CANONICAL_STAGE_ARTIFACTS[stage]}.json")
            scope = self._scope(task)
            binding = ApprovalBinding(task_id=context.task_id, run_id=context.run_id, gate_id="gate-" + uuid4().hex,
                                      checkpoint_revision=reference.revision, checkpoint_sha256=reference.sha256,
                                      artifact_revision=artifact.revision, artifact_sha256=artifact.sha256,
                                      scope_sha256=canonical_sha256(scope))
            request = ApprovalRequest(binding=binding, stage=stage, artifact=artifact, checkpoint=reference,
                                      scope=scope, summary=summary)
            self.repository.put_gate(request, expected_version=task.version, fence=context.fence)
            return {"paused": True, "gate_id": binding.gate_id, "checkpoint": reference.model_dump(mode="json")}
        task = self.repository.get_task(context.task_id)
        self.repository.transition(task.task_id, task.state, expected_version=task.version, fence=context.fence,
                                   updates={"current_stage": stage})
        return {"paused": False, "checkpoint": reference.model_dump(mode="json")}

    def apply_approval(self, context):
        """Worker-only completion: compare exactly the files approved in the browser."""
        from lib.checkpoint import write_checkpoint
        self.repository.assert_fence(context)
        task = self.repository.get_task(context.task_id)
        gate = task.approval
        if gate is None or gate.status != "approved":
            return False
        if self.store.reference(context, gate.artifact.path) != gate.artifact or self.store.reference(context, gate.checkpoint.path) != gate.checkpoint:
            raise ContractViolation("Approved files changed; request a new review", "approval_conflict")
        if gate.stage in {"model_cost", "media_cost"}:
            expected = self._scope(task).model_copy(update={"unknown_price": gate.scope.unknown_price, "authorized_limit_usd_micros": gate.scope.authorized_limit_usd_micros})
            if gate.scope != expected:
                raise ContractViolation("Approved cost scope changed", "approval_conflict")
            if gate.stage == "model_cost":
                pointer = self.store.project(context) / ".studio-cost-consent.json"
                value = {"gate_id": gate.binding.gate_id}
            else:
                pointer = self.store.project(context) / ".studio-media-consents.json"
                value = json.loads(pointer.read_text()) if pointer.exists() else {}
                evidence = json.loads((self.store.project(context) / gate.artifact.path).read_text())
                value[evidence["request_sha256"]] = {"gate_id": gate.binding.gate_id, "call_id": evidence["call_id"]}
            temporary = pointer.with_suffix(".tmp")
            temporary.write_text(json.dumps(value))
            temporary.chmod(0o600)
            temporary.replace(pointer)
            return True
        if gate.scope != self._scope(task):
            raise ContractViolation("Approved configuration changed", "approval_conflict")
        checkpoint = json.loads((self.store.project(context) / gate.checkpoint.path).read_text())
        media = checkpoint.get("metadata", {}).get("studio_asset_sha256", {})
        for relative, digest in media.items():
            path = self.policy.project_path(context, relative)
            if not path.is_file() or file_sha256(path) != digest:
                raise ContractViolation("Reviewed media content changed", "approval_conflict")
        with self.store.checkpoint_writer(context) as writer:
            write_checkpoint(self.store.projects_dir, context.project_id, gate.stage, "completed", checkpoint["artifacts"],
                             pipeline_type=checkpoint["pipeline_type"], human_approved=True,
                             metadata=checkpoint.get("metadata"), _writer=writer)
        approvals = self.store.project(context) / ".studio-approved-files.json"
        protected = json.loads(approvals.read_text()) if approvals.exists() else {}
        protected[gate.artifact.path] = gate.artifact.sha256
        temporary = approvals.with_suffix(".tmp")
        temporary.write_text(json.dumps(protected))
        temporary.chmod(0o600)
        temporary.replace(approvals)
        media_map = self.store.project(context) / ".studio-approved-media.json"
        approved_media = json.loads(media_map.read_text()) if media_map.exists() else {}
        approved_media.update(media)
        temporary = media_map.with_suffix(".tmp")
        temporary.write_text(json.dumps(approved_media))
        temporary.chmod(0o600)
        temporary.replace(media_map)
        return True

    def _context_for(self, intent):
        context = self.contexts.get(intent.run_id)
        if context is None or (context.task_id, context.fence) != (intent.task_id, intent.fence):
            raise ContractViolation("Call is not bound to this worker", "fence_conflict")
        return context

    async def authorize_model(self, intent):
        context = self._context_for(intent)
        task = self.ready(context)
        profile = self.model_profile
        if profile is None or (intent.provider, intent.model, intent.kind) != (profile.provider, profile.model, "model"):
            raise ContractViolation("Model differs from the immutable profile", "forbidden")
        consent = self._model_consent(task)
        if profile.price is None and (consent is None or not consent.scope.unknown_price):
            self._open_cost_gate(context, task)
            raise ContractViolation("Unknown model fees require browser approval", "approval_conflict")
        # Reserve the largest possible input category plus bounded output, including cached tokens.
        rates = profile.price
        amount = consent.scope.authorized_limit_usd_micros if rates is None else (
            profile.context_window * max(rates.input, rates.cache_read, rates.cache_write)
            + profile.max_output_tokens * rates.output + 999_999) // 1_000_000
        if rates is not None and amount > task.config_snapshot.single_action_approval_usd_micros and (
                consent is None or consent.scope.unknown_price or consent.scope.authorized_limit_usd_micros < amount):
            self._open_cost_gate(context, task, limit=amount, unknown=False)
            raise ContractViolation("This quoted model action requires browser approval", "approval_conflict")
        prepared = intent.model_copy(update={"price_status": "unquoted" if rates is None else "quoted", "reserved_usd_micros": amount, "status": "prepared", "usage": {}})
        reserved = self.repository.reserve_call(prepared)
        if reserved.status != "prepared":
            raise ContractViolation("Model call was already submitted; it cannot be replayed", "outcome_unknown")
        return self.repository.update_call(reserved.model_copy(update={"status": "submitted"}))

    async def settle_model(self, intent):
        self._context_for(intent)
        existing = self.repository.get_call(intent.call_id)
        if existing.status == "settled":
            return existing
        if intent.status == "outcome_unknown":
            return self.repository.update_call(existing.model_copy(update={"status": "outcome_unknown"}))
        amount = estimated_cost_usd_micros(self.model_profile, intent.usage)
        if amount is None:
            return self.repository.update_call(existing.model_copy(update={"usage": intent.usage, "status": "receipted"}))
        return self.repository.update_call(existing.model_copy(update={"usage": intent.usage,
                                                                     "actual_usd_micros": amount, "status": "settled"}))

    async def execute(self, call):
        try:
            task = self.ready(call.context)
            if call.tool_name not in self.allowed_tools:
                raise ContractViolation("Tool is not enabled for browser production", "forbidden")
            tool = self.registry.get(call.tool_name)
            if tool is None:
                raise ContractViolation("Tool is unavailable", "dependency_unavailable")
            schema = dict(tool.input_schema, additionalProperties=False)
            jsonschema.validate(call.inputs, schema)
            raw_inputs = self._canonical_render_inputs(call.context, call.inputs) if tool.name == "video_compose" else call.inputs
            inputs = self.policy.inputs(call.context, raw_inputs)
            if tool.name == "video_compose":
                if inputs.get("operation") not in {"compose", "render", "encode", "burn_subtitles", "overlay"}:
                    raise ContractViolation("Only fixed media render operations are enabled", "forbidden")
                if inputs.get("edit_decisions", {}).get("render_runtime", "ffmpeg") != "ffmpeg":
                    raise ContractViolation("This boundary enables FFmpeg rendering only", "forbidden")
            if tool.name in {"newapi_image", "newapi_video"}:
                frozen = task.config_snapshot.media_configuration_sha256
                if frozen is None and getattr(tool, "config_path", None) is None:
                    raise ContractViolation("The media gateway was not frozen for this run", "profile_unavailable")
                if frozen is not None and media_configuration_sha256(getattr(tool, "config_path", None)) != frozen:
                    raise ContractViolation("The trusted media gateway configuration changed", "profile_unavailable")
                selection = task.config_snapshot.media_models.get(tool.capability) or task.config_snapshot.media_models.get({"image_generation": "image", "video_generation": "video"}.get(tool.capability))
                if not selection or inputs.get("model") != selection:
                    raise ContractViolation("Media model differs from the approved selection", "approval_conflict")
                if not task.approval or task.approval.status != "approved" or task.approval.stage == "model_cost":
                    raise ContractViolation("Paid media requires a browser-approved plan", "approval_conflict")
            stage = {"newapi_image": "assets", "newapi_video": "assets", "video_compose": "compose",
                     "video_stitch": "edit", "video_trimmer": "edit"}.get(tool.name)
            if stage:
                from lib.checkpoint import _enforce_stage_prerequisites
                marker = self.store.project(call.context) / "project.json"
                if not marker.is_file():
                    raise ContractViolation("Initialize a production pipeline before media execution", "approval_conflict")
                pipeline = json.loads(marker.read_text())["pipeline_type"]
                _enforce_stage_prerequisites(self.store.projects_dir, call.context.project_id, pipeline, stage, "completed")
            output = inputs.get("output_path")
            if not output and any("writes" in effect for effect in tool.side_effects):
                raise ContractViolation("A managed output_path is required before execution", "forbidden")
            if output:
                approved_media = self.store.project(call.context) / ".studio-approved-media.json"
                relative = Path(output).relative_to(self.store.project(call.context)).as_posix()
                if approved_media.exists() and relative in json.loads(approved_media.read_text()):
                    raise ContractViolation("Approved media cannot be overwritten by another tool", "approval_conflict")
            fingerprint = canonical_sha256({"tool": tool.name, "inputs": inputs})
            with self.call_lock:
                task = self.ready(call.context)
                if call.call_id in self.jobs:
                    raise ContractViolation("This call is already executing; await its durable receipt", "state_conflict")
                if (tool.name, inputs.get("model")) in self.tool_quotes:
                    amount, quoted = self.tool_quotes[(tool.name, inputs.get("model"))], True
                elif getattr(tool.runtime, "value", tool.runtime) in {"local", "local_gpu"}:
                    amount, quoted = 0, True
                else:
                    amount, quoted = None, False
                if not quoted or amount > task.config_snapshot.single_action_approval_usd_micros:
                    consent, original_id = self._media_consent(task, fingerprint)
                    if consent is None or consent.scope.unknown_price != (not quoted) or (quoted and consent.scope.authorized_limit_usd_micros < amount):
                        self._open_media_cost_gate(call.context, task, call, tool, fingerprint, amount=amount)
                        raise ContractViolation("Media fees require browser approval for this exact request", "approval_conflict")
                    amount = consent.scope.authorized_limit_usd_micros
                    call = call.model_copy(update={"call_id": original_id})
                    if call.call_id in self.jobs:
                        raise ContractViolation("This approved call is already executing", "state_conflict")
                try:
                    previous = self.repository.get_call(call.call_id)
                except ContractViolation as error:
                    if error.code != "not_found":
                        raise
                    previous = None
                if previous is not None:
                    if previous.request_sha256 != fingerprint:
                        raise ContractViolation("Call ID was reused for different inputs", "idempotency_conflict")
                    if previous.status in {"settled", "failed"}:
                        return ToolReceipt(call_id=call.call_id, success=previous.status == "settled",
                                           data=(previous.resume_reference or {}).get("result", previous.resume_reference or {}), cost=task.cost)
                    if previous.status == "receipted" and (previous.resume_reference or {}).get("complete"):
                        return ToolReceipt(call_id=call.call_id, success=True, data=previous.resume_reference["result"], cost=task.cost)
                    if previous.status == "outcome_unknown" or not previous.external_job_id:
                        raise ContractViolation("Previous submission needs reconciliation", "outcome_unknown")
                    # A known job resumes through GET/polling; no new paid submission is authorized.
                    inputs["resume_job"] = previous.resume_reference.get("job", previous.resume_reference)
                    intent = previous.model_copy(update={"fence": call.context.fence})
                else:
                    intent = CallIntent(call_id=call.call_id, task_id=call.context.task_id, run_id=call.context.run_id,
                                        fence=call.context.fence, kind="tool", operation=tool.name, provider=tool.provider or "local",
                                        model=inputs.get("model"), request_sha256=fingerprint, price_status="quoted" if quoted else "unquoted",
                                        reserved_usd_micros=amount, resume_reference={"inputs": dict(inputs),
                                            "job_path": str(self.runtime_root / "provider-jobs" / call.context.task_id / call.context.run_id / (call.call_id + ".json"))
                                            if tool.name in {"newapi_image", "newapi_video"} else None})
                    intent = self.repository.reserve_call(intent)
            staged = None
            if output:
                target = Path(output)
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = target.with_name(".studio-stage-" + call.call_id + target.suffix)
                inputs["output_path"] = str(staged)
            if tool.name in {"newapi_image", "newapi_video"}:
                from tools._newapi.config import load_settings
                import yaml
                settings = load_settings(getattr(tool, "config_path", None))
                frozen = call.context.config_snapshot.media_configuration_sha256
                if frozen is not None and canonical_sha256(settings.config.model_dump(mode="json")) != frozen:
                    raise ContractViolation("Media gateway changed before execution", "profile_unavailable")
                configuration = self.runtime_root / "tool-configs" / call.context.task_id / call.context.run_id / (call.call_id + ".yaml")
                configuration.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                configuration.write_text(yaml.safe_dump({"newapi": settings.config.model_dump(mode="json")}), encoding="utf-8")
                configuration.chmod(0o600)
                tool = copy.copy(tool)
                tool.config_path = configuration
                tool.studio_base_url = settings.config.base_url
                inputs["job_path"] = intent.resume_reference["job_path"]
                if "resume_job" in inputs:
                    inputs = {key: value for key, value in inputs.items() if key in {
                        "resume_job", "job_path", "model", "operation", "request_mode", "output_path", "poll_timeout", "poll_interval"}}
            try:
                result = await self._execute_tool(tool, inputs, call)
            except BaseException:
                current = self.repository.get_call(intent.call_id)
                if not current.external_job_id:
                    terminal = {"status": "failed", "actual_usd_micros": 0} if call.call_id not in self.started_calls else {"status": "outcome_unknown"}
                    self.repository.update_call(current.model_copy(update=terminal))
                raise
            intent = self.repository.get_call(intent.call_id).model_copy(update={"fence": call.context.fence})
            data = dict(result.data or {})
            if staged is not None and result.success:
                if not staged.is_file() or staged.stat().st_size == 0:
                    raise ContractViolation("Tool returned success without a media file", "invalid_artifact")
                reference = self.store.write_file(call.context, Path(output).relative_to(self.store.project(call.context)).as_posix(), source=staged)
                def relocate(value):
                    if isinstance(value, str):
                        return value.replace(str(staged), output)
                    if isinstance(value, dict):
                        return {key: relocate(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [relocate(item) for item in value]
                    return value
                data = relocate(data)
                data["file"] = reference.model_dump(mode="json")
                staged.unlink()
            elif staged is not None and result.cost_usd is not None:
                staged.unlink(missing_ok=True)
            job_id = data.get("job_id") or data.get("task_id") or data.get("resume_job", {}).get("id")
            if job_id:
                intent = self.repository.update_call(intent.model_copy(update={"status": "receipted", "external_job_id": str(job_id),
                                                                       "resume_reference": {**(intent.resume_reference or {}), "inputs": intent.resume_reference.get("inputs", {}),
                                                                                            "job": data.get("resume_job") or data, "result": data}}))
            if result.cost_usd is None:
                status = "receipted" if result.success or intent.external_job_id else "outcome_unknown"
                receipt = {**(intent.resume_reference or {}), "result": data, "complete": bool(result.success)}
                self.repository.update_call(intent.model_copy(update={"status": status, "resume_reference": receipt}))
            else:
                actual = max(0, math.ceil(result.cost_usd * 1_000_000))
                self.repository.update_call(intent.model_copy(update={"status": "settled" if result.success else "failed",
                                                                     "actual_usd_micros": actual, "resume_reference": {"result": data}}))
            return ToolReceipt(call_id=call.call_id, success=result.success, data=data,
                               error=None if result.success else ErrorDTO(code="internal_error", message=(result.error or "Production tool failed; inspect its safe receipt")[:4000]),
                               cost=self.repository.get_task(call.context.task_id).cost)
        except ContractViolation as error:
            return ToolReceipt(call_id=call.call_id, success=False, error=ErrorDTO(code=error.code, message=str(error)))
        except (jsonschema.ValidationError, TypeError, ValueError):
            return ToolReceipt(call_id=call.call_id, success=False, error=ErrorDTO(code="invalid_input", message="Tool input failed validation"))
        except Exception:
            return ToolReceipt(call_id=call.call_id, success=False, error=ErrorDTO(code="internal_error", message="Tool execution failed; reconcile its durable receipt"))

    async def resume(self, context, call_id):
        self.ready(context)
        previous = self.repository.get_call(call_id)
        if (previous.task_id, previous.run_id) != (context.task_id, context.run_id) or not previous.external_job_id:
            raise ContractViolation("No known provider job belongs to this run", "outcome_unknown")
        inputs = (previous.resume_reference or {}).get("inputs")
        if not inputs:
            raise ContractViolation("Original structured inputs require reconciliation", "outcome_unknown")
        return await self.execute(ToolCall(call_id=call_id, context=context, tool_name=previous.operation, inputs=inputs))


class BridgeServer:
    """Private control channel. Tokens and profile endpoints never enter public API DTOs."""

    def __init__(self, bridge, context, profile=None):
        self.bridge = bridge
        self.context = context
        self.profile = profile or bridge.model_profile
        if self.profile is None or canonical_sha256(self.profile.model_dump(mode="json")) != context.config_snapshot.configuration_sha256:
            raise ContractViolation("Private provider profile differs from this run", "profile_unavailable")
        self.token = secrets.token_urlsafe(32)
        bridge.bind(context)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4 * 1024 * 1024:
                        raise ContractViolation("Invalid bridge request size")
                    body = json.loads(self.rfile.read(size))
                    value = owner.dispatch(self.path, body)
                    result, code = {"ok": True, "data": value}, 200
                except ContractViolation as error:
                    result, code = {"ok": False, "error": {"code": error.code, "message": str(error)}}, 409
                except Exception:
                    result, code = {"ok": False, "error": {"code": "invalid_input", "message": "Invalid bridge request"}}, 400
                encoded = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="studio-private-bridge", daemon=True)
        self.thread.start()

    @property
    def environment(self):
        return {"STUDIO_BRIDGE_URL": f"http://127.0.0.1:{self.server.server_port}", "STUDIO_BRIDGE_TOKEN": self.token}

    def close(self):
        if not self.bridge.stop():
            raise ContractViolation("Managed tools did not exit; retain writer ownership", "fence_conflict")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def dispatch(self, route, body):
        if not isinstance(body, dict):
            raise ContractViolation("Bridge body must be an object")
        bridge, context = self.bridge, self.context
        if route == "/profile":
            bridge.repository.assert_fence(context)
            return self.profile.model_dump(mode="json")
        if route == "/catalog":
            return bridge.catalog(context)
        if route == "/read":
            return bridge.read(context, **body)
        if route == "/read_project":
            return bridge.read_project(context, **body)
        if route == "/initialize":
            return bridge.initialize(context, **body)
        if route == "/artifact":
            return bridge.artifact(context, **body)
        if route == "/checkpoint":
            return bridge.checkpoint(context, **body)
        if route == "/execute":
            call = ToolCall(context=context, **body)
            return asyncio.run(bridge.execute(call)).model_dump(mode="json")
        if route == "/resume":
            return asyncio.run(bridge.resume(context, **body)).model_dump(mode="json")
        if route in {"/authorize", "/settle"}:
            intent = CallIntent(task_id=context.task_id, run_id=context.run_id, fence=context.fence, **body)
            method = bridge.authorize_model if route == "/authorize" else bridge.settle_model
            return asyncio.run(method(intent)).model_dump(mode="json")
        raise ContractViolation("Unknown bridge action", "forbidden")
