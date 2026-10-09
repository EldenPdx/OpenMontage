"""Human decisions verify current file evidence before durable queueing."""

from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from lib.checkpoint import validate_checkpoint
from production.contracts import CallIntent, ContractViolation, FileWriteIntent, TaskCommand
from production.pi_config import profile_for_snapshot


class ApprovalService:
    def __init__(self, repository, projects_dir, config):
        self.repository = repository
        self.projects_dir = Path(projects_dir).resolve()
        self.config = config

    def _evidence(self, task, reference):
        project = (self.projects_dir / task.project_id).resolve()
        path = (project / reference.path).resolve()
        if not project.is_relative_to(self.projects_dir) or not path.is_relative_to(project):
            raise ContractViolation("Approval evidence escaped the managed project", "forbidden")
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ContractViolation("Approval evidence is missing or too large", "file_conflict")
        content = path.read_bytes()
        if sha256(content).hexdigest() != reference.sha256:
            raise ContractViolation("Displayed approval content changed; refresh the current gate", "approval_conflict")
        return content

    def decide(self, task_id, gate_id, decision, key):
        if decision.binding.task_id != task_id or decision.binding.gate_id != gate_id:
            raise ContractViolation("Decision path differs from its bound gate", "approval_conflict")
        return self.repository.decide_gate(decision, idempotency_key=key, validate_evidence=self._verify_evidence)

    def _verify_evidence(self, task, gate):
        profile_for_snapshot(self.config, task.config_snapshot)
        if (gate.scope.provider, gate.scope.model, gate.scope.configuration_sha256,
                gate.scope.budget_usd_micros) != (task.config_snapshot.provider, task.config_snapshot.model,
                task.config_snapshot.configuration_sha256, task.config_snapshot.budget_usd_micros):
            raise ContractViolation("Approval scope differs from the frozen task", "approval_conflict")
        self._evidence(task, gate.artifact)
        content = self._evidence(task, gate.checkpoint)
        if gate.stage not in {"model_cost", "media_cost"}:
            try:
                checkpoint = json.loads(content)
                validate_checkpoint(checkpoint)
                if (checkpoint["project_id"], checkpoint["stage"], checkpoint["status"]) != (
                        task.project_id, gate.stage, "awaiting_human"):
                    raise ValueError("Wrong pending checkpoint")
            except Exception:
                raise ContractViolation("Approval checkpoint is invalid", "invalid_artifact") from None

    def resume(self, task_id, request, key):
        task = self.repository.get_task(task_id)
        profile_for_snapshot(self.config, task.config_snapshot)
        for intent in self.repository.unresolved_intents(task_id):
            if isinstance(intent, CallIntent) and intent.status in {"submitted", "outcome_unknown"}:
                raise ContractViolation("An external submission needs trusted reconciliation before resuming", "outcome_unknown")
            if isinstance(intent, FileWriteIntent):
                raise ContractViolation("Pending file writes need reconciliation before resuming", "file_conflict")
        command = TaskCommand(command_id="cmd-" + uuid4().hex, task_id=task_id, run_id=task.run_id, kind="resume",
                              expected_version=request.expected_version, idempotency_key=key,
                              payload={"comment": request.comment})
        self.repository.enqueue_command(command)
        return self.repository.get_task(task_id)
