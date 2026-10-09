"""Frozen Web Studio v1 public DTOs and infrastructure ports.

Amounts are integer USD micros. Media, canonical checkpoints and Pi sessions stay
on disk; these DTOs hold references rather than introducing a second workflow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from enum import Enum
from hashlib import sha256
import json
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]
UsdMicros = Annotated[int, Field(strict=True, ge=0)]
Version = Annotated[int, Field(strict=True, ge=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def canonical_sha256(value: ContractModel | dict) -> str:
    """Fingerprint non-secret DTO content; file hashes use exact persisted bytes."""
    content = value.model_dump(mode="json") if isinstance(value, ContractModel) else value
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


class TaskCreate(ContractModel):
    brief: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(min_length=2, max_length=10_000)
    profile_id: Identifier = "xvan"
    duration_seconds: int = Field(default=30, ge=1, le=600, strict=True)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    narration: bool = False
    budget_usd_micros: UsdMicros = 10_000_000


class ContractViolation(ValueError):
    """Safe contract failure; callers expose its code, never raw secret inputs."""

    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


ALLOWED_TRANSITIONS = {
    TaskState.QUEUED: {TaskState.RUNNING, TaskState.CANCEL_REQUESTED, TaskState.BLOCKED},
    TaskState.RUNNING: {
        TaskState.AWAITING_APPROVAL, TaskState.BLOCKED, TaskState.CANCEL_REQUESTED,
        TaskState.SUCCEEDED, TaskState.FAILED, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.QUEUED, TaskState.BLOCKED, TaskState.CANCEL_REQUESTED, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.BLOCKED: {
        TaskState.QUEUED, TaskState.CANCEL_REQUESTED, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.CANCEL_REQUESTED: {
        TaskState.CANCELLED, TaskState.FAILED, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.RECOVERY_REQUIRED: {
        TaskState.QUEUED, TaskState.CANCEL_REQUESTED, TaskState.FAILED,
    },
    TaskState.FAILED: {TaskState.QUEUED},
    TaskState.CANCELLED: set(),
    TaskState.SUCCEEDED: set(),
}
TERMINAL_STATES = frozenset({TaskState.CANCELLED, TaskState.SUCCEEDED, TaskState.FAILED})


def check_transition(current: TaskState | str, target: TaskState | str, *, result=None, updates: Mapping | None = None) -> None:
    try:
        current, target = TaskState(current), TaskState(target)
    except ValueError as exc:
        raise ContractViolation("Unknown task state") from exc
    if target not in ALLOWED_TRANSITIONS[current] and not (current == target and updates):
        raise ContractViolation("Invalid task state transition", "state_conflict")
    if target == TaskState.SUCCEEDED and (result is None or not result.verified):
        raise ContractViolation("Succeeded requires a verified canonical render result", "invalid_artifact")


class FileReference(ContractModel):
    """Path is relative to the task project, never a client-supplied host path."""

    path: str = Field(pattern=r"^[a-zA-Z0-9_-][a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*)*$")
    revision: Version
    sha256: Sha256

class ConfigSnapshot(ContractModel):
    """Non-secret immutable selection. Credentials/headers/endpoints stay backend-only."""

    profile_id: Identifier
    provider: Identifier
    model: str = Field(min_length=1, max_length=200)
    api: str = Field(min_length=1, max_length=80)
    configuration_sha256: Sha256
    media_configuration_sha256: Sha256 | None = None
    budget_usd_micros: UsdMicros = 10_000_000
    single_action_approval_usd_micros: UsdMicros = 500_000
    max_output_tokens: int = Field(default=16_384, strict=True, ge=1)
    max_turns: int = Field(default=100, strict=True, ge=1)
    task_timeout_seconds: int = Field(default=3600, strict=True, ge=1)
    media_models: dict[str, str] = Field(default_factory=dict)
    price_status: Literal["quoted", "unquoted"] = "unquoted"


class ApprovalScope(ContractModel):
    provider: Identifier
    model: str = Field(min_length=1, max_length=200)
    render_runtime: Literal["ffmpeg", "remotion", "hyperframes"] | None = None
    budget_usd_micros: UsdMicros
    configuration_sha256: Sha256
    media_models: dict[str, str] = Field(default_factory=dict)
    narration: bool = False
    unknown_price: bool = False
    authorized_limit_usd_micros: UsdMicros | None = None

    @model_validator(mode="after")
    def unquoted_authorization_is_bounded(self):
        if self.unknown_price and not self.authorized_limit_usd_micros:
            raise ValueError("Unquoted authorization requires a positive reservation limit")
        if self.authorized_limit_usd_micros is not None and self.authorized_limit_usd_micros > self.budget_usd_micros:
            raise ValueError("Authorized reservation cannot exceed the task budget")
        return self


class ApprovalBinding(ContractModel):
    task_id: Identifier
    run_id: Identifier
    gate_id: Identifier
    checkpoint_revision: Version
    checkpoint_sha256: Sha256
    artifact_revision: Version
    artifact_sha256: Sha256
    scope_sha256: Sha256


class ApprovalOption(ContractModel):
    option_id: Identifier
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class ApprovalRequest(ContractModel):
    binding: ApprovalBinding
    stage: Identifier
    artifact: FileReference
    checkpoint: FileReference
    scope: ApprovalScope
    summary: str = Field(min_length=1, max_length=20_000)
    options: list[ApprovalOption] = Field(default_factory=list, max_length=100)
    status: Literal["pending", "approved", "revised", "rejected", "aborted", "superseded"] = "pending"

    @model_validator(mode="after")
    def binding_matches_displayed_evidence(self):
        b = self.binding
        if (self.artifact.revision, self.artifact.sha256) != (b.artifact_revision, b.artifact_sha256):
            raise ValueError("Approval artifact does not match binding")
        if (self.checkpoint.revision, self.checkpoint.sha256) != (b.checkpoint_revision, b.checkpoint_sha256):
            raise ValueError("Approval checkpoint does not match binding")
        if canonical_sha256(self.scope) != b.scope_sha256:
            raise ValueError("Approval scope does not match binding")
        return self


class VersionedRequest(ContractModel):
    expected_version: Version


class CancelRequest(VersionedRequest):
    reason: str = Field(default="User requested cancellation", max_length=2000)


class ResumeRequest(VersionedRequest):
    """Resume performs reconciliation first; it never authorizes a fresh paid submit."""

    comment: str = Field(default="", max_length=4000)


class ApprovalDecision(VersionedRequest):
    binding: ApprovalBinding
    decision: Literal["approve", "revise", "reject", "abort"]
    comment: str = Field(default="", max_length=10_000)
    selected_option_ids: list[Identifier] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def revision_needs_feedback(self):
        if self.decision == "revise" and not self.comment.strip():
            raise ValueError("Revision requires feedback")
        return self


def check_approval(decision: ApprovalDecision, binding: ApprovalBinding, *, current_version: int) -> None:
    if decision.expected_version != current_version:
        raise ContractViolation("Task version changed; refresh current gate", "version_conflict")
    if decision.binding != binding:
        raise ContractViolation("Approval does not match the current gate/version/scope", "approval_conflict")


class ErrorDTO(ContractModel):
    code: Literal[
        "invalid_input", "not_found", "profile_unavailable", "dependency_unavailable",
        "version_conflict", "idempotency_conflict", "state_conflict", "approval_conflict",
        "fence_conflict", "budget_exceeded", "quote_required", "outcome_unknown",
        "file_conflict", "invalid_artifact", "cursor_expired", "forbidden", "rpc_error",
        "timeout", "internal_error",
    ]
    message: str = Field(min_length=1, max_length=4000)
    retryable: bool = False
    recovery_actions: list[Literal["refresh", "resume", "reconcile", "cancel", "configure"]] = Field(default_factory=list)


class CostSnapshot(ContractModel):
    currency: Literal["USD"] = "USD"
    budget_usd_micros: UsdMicros = 10_000_000
    reserved_usd_micros: UsdMicros = 0
    spent_usd_micros: UsdMicros = 0
    unknown_call_count: int = Field(default=0, strict=True, ge=0)
    price_status: Literal["quoted", "unquoted"] = "unquoted"


class RenderResult(ContractModel):
    render_report: FileReference
    video: FileReference
    bytes: int = Field(strict=True, gt=0)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    verified: Literal[True]


class TaskRecord(ContractModel):
    task_id: Identifier
    project_id: Identifier
    run_id: Identifier
    version: Version = 1
    state: TaskState = TaskState.QUEUED
    request: TaskCreate
    config_snapshot: ConfigSnapshot
    current_stage: Identifier | None = None
    approval: ApprovalRequest | None = None
    cost: CostSnapshot = Field(default_factory=CostSnapshot)
    error: ErrorDTO | None = None
    result: RenderResult | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def state_has_evidence(self):
        if self.state == TaskState.SUCCEEDED and self.result is None:
            raise ValueError("Succeeded requires a verified canonical render result")
        if self.state == TaskState.AWAITING_APPROVAL and self.approval is None:
            raise ValueError("Awaiting approval requires a persistent gate")
        return self


class SessionReference(ContractModel):
    """Internal reference under .runtime/studio/sessions, never projects or public API."""

    path: str = Field(pattern=r"^[a-zA-Z0-9_-][a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*)*$")
    session_id: str | None = None


class RunContext(ContractModel):
    task_id: Identifier
    project_id: Identifier
    run_id: Identifier
    attempt: Version = 1
    fence: Version
    config_snapshot: ConfigSnapshot
    session: SessionReference


class TaskCommand(ContractModel):
    command_id: Identifier
    task_id: Identifier
    run_id: Identifier
    kind: Literal["start", "continue", "revise", "cancel", "resume"]
    expected_version: Version
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    payload: dict = Field(default_factory=dict)
    status: Literal["pending", "claimed", "completed", "failed"] = "pending"


class ClaimedCommand(ContractModel):
    command: TaskCommand
    context: RunContext
    worker_id: Identifier
    lease_expires_at: str


class StudioEvent(ContractModel):
    event_id: int = Field(strict=True, ge=1)
    task_id: Identifier
    run_id: Identifier
    task_version: Version
    type: Literal["state", "stage", "approval", "progress", "cost", "error", "result", "recovery"]
    data: dict = Field(default_factory=dict)
    created_at: str


class CallIntent(ContractModel):
    call_id: Identifier
    task_id: Identifier
    run_id: Identifier
    fence: Version
    kind: Literal["model", "tool"]
    operation: Identifier
    provider: Identifier
    model: str | None = None
    request_sha256: Sha256
    price_status: Literal["quoted", "unquoted"]
    reserved_usd_micros: UsdMicros | None = None
    actual_usd_micros: UsdMicros | None = None
    status: Literal["reserved", "prepared", "submitted", "receipted", "settled", "failed", "outcome_unknown"] = "prepared"
    external_job_id: str | None = None
    resume_reference: dict | None = None
    usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unknown_outcome_is_not_settled(self):
        if self.status == "outcome_unknown" and self.actual_usd_micros is not None:
            raise ValueError("Unknown outcome cannot have a settled actual cost")
        return self


class FileWriteIntent(ContractModel):
    intent_id: Identifier
    task_id: Identifier
    run_id: Identifier
    fence: Version
    target: FileReference
    previous_sha256: Sha256 | None = None
    status: Literal["prepared", "applied", "reconciled", "conflict"] = "prepared"


class ToolCall(ContractModel):
    call_id: Identifier
    context: RunContext
    tool_name: Identifier
    inputs: dict = Field(default_factory=dict)
    approval_binding: ApprovalBinding | None = None


class ToolReceipt(ContractModel):
    call_id: Identifier
    success: bool
    data: dict = Field(default_factory=dict)
    error: ErrorDTO | None = None
    cost: CostSnapshot | None = None


class Repository(Protocol):
    """Each mutation is transactional, including its durable events/commands.

    Worker writes require a live fence. Public writes compare expected_version.
    Repeating an idempotency key with a different fingerprint is a conflict.
    """

    def create_task(self, request: TaskCreate, snapshot: ConfigSnapshot, idempotency_key: str) -> TaskRecord: ...
    def get_task(self, task_id: str) -> TaskRecord: ...
    def list_tasks(self, *, limit: int = 50, after: str | None = None) -> list[TaskRecord]: ...
    def enqueue_command(self, command: TaskCommand) -> TaskCommand: ...
    def claim_command(self, worker_id: str, *, lease_seconds: int = 30) -> ClaimedCommand | None: ...
    def heartbeat(self, claim: ClaimedCommand, *, lease_seconds: int = 30) -> ClaimedCommand: ...
    def finish_command(self, claim: ClaimedCommand, *, error: ErrorDTO | None = None) -> None: ...
    def assert_fence(self, context: RunContext) -> None: ...
    def bind_session(self, context: RunContext, session: SessionReference) -> RunContext: ...
    def session(self, context: RunContext) -> SessionReference: ...
    def recover_expired_leases(self) -> int: ...
    def recovery_context(self, task_id: str) -> RunContext: ...
    def release_recovered_lease(self, task_id: str, *, fence: int, terminated: bool) -> None: ...
    def transition(self, task_id: str, state: TaskState, *, expected_version: int, fence: int | None = None, updates: Mapping[str, Any] | None = None) -> TaskRecord: ...
    def append_event(self, event: StudioEvent, *, fence: int | None = None) -> StudioEvent: ...
    def events(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[StudioEvent]: ...
    def put_gate(self, request: ApprovalRequest, *, expected_version: int, fence: int) -> TaskRecord: ...
    def decide_gate(self, decision: ApprovalDecision, *, idempotency_key: str, validate_evidence=None) -> TaskRecord: ...
    def approved_gate(self, task_id: str, gate_id: str) -> ApprovalRequest | None: ...
    def pending_cancel(self, task_id: str) -> TaskCommand | None: ...
    def reserve_call(self, intent: CallIntent) -> CallIntent: ...
    def get_call(self, call_id: str) -> CallIntent: ...
    def update_call(self, intent: CallIntent) -> CallIntent: ...
    def record_file_intent(self, intent: FileWriteIntent) -> FileWriteIntent: ...
    def unresolved_intents(self, task_id: str) -> list[CallIntent | FileWriteIntent]: ...


class PiRunner(Protocol):
    """One bound, isolated real Pi process. Protocol acks never complete a task."""

    async def start(self, context: RunContext) -> SessionReference: ...
    async def prompt(self, message: str, *, command_id: str) -> dict: ...
    async def inspect(self) -> dict: ...
    async def clear_queue(self) -> dict: ...
    async def abort_retry(self) -> dict: ...
    async def abort(self) -> dict: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def close(self) -> None: ...


class ToolBridge(Protocol):
    """Backend validates schemas, current fence, gate, budget and managed paths."""

    def catalog(self, context: RunContext) -> dict: ...
    async def execute(self, call: ToolCall) -> ToolReceipt: ...
    async def authorize_model(self, intent: CallIntent) -> CallIntent: ...
    async def settle_model(self, intent: CallIntent) -> CallIntent: ...


API_CONTRACT = {
    "config": ("GET", "/api/studio/config"),
    "create_task": ("POST", "/api/studio/tasks"),
    "list_tasks": ("GET", "/api/studio/tasks"),
    "task": ("GET", "/api/studio/tasks/{task_id}"),
    "events": ("GET", "/api/studio/tasks/{task_id}/events"),
    "cancel": ("POST", "/api/studio/tasks/{task_id}/cancel"),
    "decision": ("POST", "/api/studio/tasks/{task_id}/approvals/{gate_id}/decision"),
    "resume": ("POST", "/api/studio/tasks/{task_id}/resume"),
}

SCHEMA_MODELS = {
    "task_create": TaskCreate,
    "task_record": TaskRecord,
    "config_snapshot": ConfigSnapshot,
    "approval_request": ApprovalRequest,
    "approval_decision": ApprovalDecision,
    "cancel_request": CancelRequest,
    "resume_request": ResumeRequest,
    "task_command": TaskCommand,
    "run_context": RunContext,
    "event": StudioEvent,
    "call_intent": CallIntent,
    "file_write_intent": FileWriteIntent,
    "tool_call": ToolCall,
    "tool_receipt": ToolReceipt,
    "error": ErrorDTO,
}
