"""Frozen Web Studio v1 public DTOs and infrastructure ports.

Amounts are integer USD micros. Media, canonical checkpoints and Pi sessions stay
on disk; these DTOs hold references rather than introducing a second workflow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
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


class WorkspaceContext(ContractModel):
    kind: Literal["personal", "organization"]
    owner_id: str = Field(min_length=1, max_length=256)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.owner_id}"


class UserIdentity(ContractModel):
    user_id: str = Field(min_length=1, max_length=256)
    issuer: str = Field(min_length=1, max_length=2000)
    sub: str = Field(min_length=1, max_length=256)


class Actor(ContractModel):
    identity: UserIdentity
    workspace: WorkspaceContext

    @model_validator(mode="after")
    def personal_owner_matches_identity(self):
        if self.workspace.kind == "personal" and self.workspace.owner_id != self.identity.user_id:
            raise ValueError("Personal workspace must belong to the actor")
        return self


class ResourceOwnership(ContractModel):
    workspace: WorkspaceContext
    creator: UserIdentity


class OwnedRecord(ContractModel):
    ownership: ResourceOwnership | None = None
    actor: Actor | None = None

    @model_validator(mode="after")
    def ownership_matches_actor(self):
        if (self.ownership is None) != (self.actor is None):
            raise ValueError("Ownership and actor must be present together")
        if self.actor and self.actor.workspace != self.ownership.workspace:
            raise ValueError("Actor workspace differs from ownership")
        return self


class AuditEntry(ContractModel):
    operation_id: str
    actor: Actor
    operation: str
    target: str
    result: str
    created_at: str


class AuthContext(ContractModel):
    identity: UserIdentity
    workspace: WorkspaceContext
    role: Literal["admin", "editor", "viewer", "personal"] | None
    permissions: list[str]
    context_id: str
    session_ref: Sha256
    authority_verified_at: datetime


class AuthSession(ContractModel):
    session_ref: Sha256
    identity: UserIdentity
    workspace: WorkspaceContext
    context_id: str
    csrf_hash: Sha256
    created_at: datetime
    absolute_expires_at: datetime
    idle_expires_at: datetime
    revision: Version
    authority_verified_at: datetime | None = None


@dataclass(frozen=True)
class StoredSession:
    metadata: AuthSession
    sdk_storage: dict[str, str | None] = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class SessionHandle:
    cookie: str = field(repr=False)
    session: StoredSession


@dataclass(frozen=True, repr=False)
class ConsumedLoginInteraction:
    reference: str
    completion_token: str
    state: str
    nonce: str
    code_verifier: str
    return_to: str
    organization_hint: str | None
    sdk_storage: dict[str, str | None]


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
        "unauthenticated", "csrf_invalid", "auth_context_conflict", "invitation_invalid",
        "last_admin", "invalid_cursor", "identity_unavailable",
    ]
    message: str = Field(min_length=1, max_length=4000)
    retryable: bool = False
    recovery_actions: list[Literal["refresh", "resume", "reconcile", "cancel", "configure"]] = Field(default_factory=list)


OrganizationRole = Literal["admin", "editor", "viewer"]
OrganizationOperation = Literal["organization:create", "invitation:create", "invitation:cancel", "invitation:accept", "member:role", "member:remove"]


class OrganizationDTO(ContractModel):
    organization_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=100)
    roles: list[OrganizationRole]


class MemberDTO(ContractModel):
    user_id: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    roles: list[OrganizationRole]


class InvitationDTO(ContractModel):
    invitation_id: str = Field(min_length=1, max_length=256)
    organization_id: str = Field(min_length=1, max_length=256)
    email: str = Field(min_length=1, max_length=320)
    role: OrganizationRole
    status: Literal["pending", "accepted", "cancelled", "expired"] = "pending"
    expires_at: datetime

    @model_validator(mode="after")
    def expiry_is_utc_aware(self):
        if self.expires_at.tzinfo is None:
            raise ValueError("Invitation expiry needs a UTC timestamp")
        return self


class InvitationRecord(InvitationDTO):
    ticket_hash: Sha256
    intended_user_id: str | None = None
    issued_by: Actor

    def public_dict(self):
        return self.model_dump(mode="json", exclude={"ticket_hash", "intended_user_id", "issued_by"})


class InvitationTransition(ContractModel):
    invitation_id: str = Field(min_length=1, max_length=256)
    status: Literal["accepted", "cancelled"]
    accepted_at: datetime | None = None
    accepted_user_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def acceptance_evidence(self):
        if (self.accepted_at is None) != (self.accepted_user_id is None) or self.accepted_at is not None and (self.status != "accepted" or self.accepted_at.tzinfo is None):
            raise ValueError("Invalid authoritative acceptance evidence")
        return self


class InvitationCreationMaterial(ContractModel):
    ticket: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    expires_at: datetime
    organization_role_id: str = Field(min_length=1, max_length=256)
    prior_native_invitation_ids: tuple[str, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def private_creation_constraints(self):
        if self.expires_at.tzinfo is None or len(set(self.prior_native_invitation_ids)) != len(self.prior_native_invitation_ids) or any(not 1 <= len(value) <= 256 for value in self.prior_native_invitation_ids):
            raise ValueError("Invalid private invitation material")
        return self


class OperationScope(ContractModel):
    actor: Actor
    operation: OrganizationOperation
    resource_id: str = Field(min_length=1, max_length=1024)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class OrganizationOperationTarget(ContractModel):
    organization_id: str | None = Field(default=None, min_length=1, max_length=256)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    invitation_id: str | None = Field(default=None, min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=1, max_length=320)
    role: OrganizationRole | None = None


class OrganizationOperationResult(ContractModel):
    status_code: Literal[200, 201, 204, 401, 403, 404, 409, 422, 503]
    body: OrganizationDTO | MemberDTO | InvitationDTO | ErrorDTO | None = None

    @model_validator(mode="after")
    def status_matches_body(self):
        if (self.status_code >= 400) != isinstance(self.body, ErrorDTO):
            raise ValueError("Result status differs from response body")
        if (self.status_code == 204) != (self.body is None):
            raise ValueError("Only 204 results have no body")
        return self


class SafeRemoteRefs(ContractModel):
    organization_id: str | None = Field(default=None, min_length=1, max_length=256)
    invitation_id: str | None = Field(default=None, min_length=1, max_length=256)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)


class OrganizationIntent(ContractModel):
    operation_id: Identifier
    scope: OperationScope
    request_sha256: Sha256
    target: OrganizationOperationTarget
    organization_id: str | None = None
    stage: Identifier = "prepared"
    status: Literal["prepared", "submitted", "recovery_required", "committed", "failed"] = "prepared"
    revision: Version = 1
    remote_refs: SafeRemoteRefs = Field(default_factory=SafeRemoteRefs)
    result: OrganizationOperationResult | None = None
    created_at: str
    updated_at: str


class IntentAcceptance(ContractModel):
    intent: OrganizationIntent
    disposition: Literal["new", "pending", "replay"]


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


class TaskFields(ContractModel):
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


class PublicUserReference(ContractModel):
    user_id: str = Field(min_length=1, max_length=256)


class PublicResourceOwnership(ContractModel):
    workspace: WorkspaceContext
    creator: PublicUserReference


class PublicActor(PublicUserReference):
    workspace: WorkspaceContext


class PublicTaskRecord(TaskFields):
    ownership: PublicResourceOwnership | None = None
    actor: PublicActor | None = None

    @model_validator(mode="after")
    def public_ownership_matches_actor(self):
        if self.actor and (self.ownership is None or self.actor.workspace != self.ownership.workspace):
            raise ValueError("Public ownership and actor must match")
        return self


class TaskRecord(OwnedRecord, TaskFields):
    @model_validator(mode="after")
    def ownership_matches_actor(self):
        # Historical imports have a mapped creator without fabricating an old actor.
        if self.actor and (self.ownership is None or self.actor.workspace != self.ownership.workspace):
            raise ValueError("Actor workspace differs from ownership")
        return self

    def public_dict(self) -> dict:
        value = self.model_dump(mode="json")
        if self.ownership:
            value["ownership"]["creator"] = {"user_id": self.ownership.creator.user_id}
        if self.actor:
            value["actor"] = {"user_id": self.actor.identity.user_id, "workspace": self.actor.workspace.model_dump()}
        return PublicTaskRecord.model_validate(value).model_dump(mode="json")


class SessionReference(ContractModel):
    """Internal reference under .runtime/studio/sessions, never projects or public API."""

    path: str = Field(pattern=r"^[a-zA-Z0-9_-][a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*)*$")
    session_id: str | None = None


class RunContext(OwnedRecord):
    task_id: Identifier
    project_id: Identifier
    run_id: Identifier
    attempt: Version = 1
    fence: Version
    config_snapshot: ConfigSnapshot
    session: SessionReference


class TaskCommand(OwnedRecord):
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

    def create_task(self, request: TaskCreate, snapshot: ConfigSnapshot, idempotency_key: str, *, workspace: WorkspaceContext | None = None, actor: Actor | None = None) -> TaskRecord: ...
    def get_task(self, task_id: str) -> TaskRecord: ...
    def get_task_for_workspace(self, task_id: str, workspace: WorkspaceContext) -> TaskRecord: ...
    def list_tasks_for_workspace(self, workspace: WorkspaceContext, *, limit: int = 50, after: str | None = None) -> list[TaskRecord]: ...
    def project_ownership(self, project_id: str, workspace: WorkspaceContext) -> ResourceOwnership: ...
    def project_ids_for_workspace(self, workspace: WorkspaceContext) -> list[str]: ...
    def audit_for_workspace(self, workspace: WorkspaceContext, *, operation_id: str) -> list[AuditEntry]: ...
    def database_binding(self) -> str: ...
    def assert_legacy_root(self, *, expected_binding: str) -> None: ...
    def list_tasks(self, *, limit: int = 50, after: str | None = None) -> list[TaskRecord]: ...
    def enqueue_command(self, command: TaskCommand, *, workspace: WorkspaceContext | None = None, actor: Actor | None = None) -> TaskCommand: ...
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
    def decide_gate(self, decision: ApprovalDecision, *, idempotency_key: str, workspace: WorkspaceContext | None = None, actor: Actor | None = None, validate_evidence=None) -> TaskRecord: ...
    def approved_gate(self, task_id: str, gate_id: str) -> ApprovalRequest | None: ...
    def pending_cancel(self, task_id: str) -> TaskCommand | None: ...
    def reserve_call(self, intent: CallIntent) -> CallIntent: ...
    def get_call(self, call_id: str) -> CallIntent: ...
    def update_call(self, intent: CallIntent) -> CallIntent: ...
    def record_file_intent(self, intent: FileWriteIntent) -> FileWriteIntent: ...
    def unresolved_intents(self, task_id: str) -> list[CallIntent | FileWriteIntent]: ...


class PersistentStorage(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str | None) -> None: ...
    def delete(self, key: str) -> None: ...


class IdentityStore(Protocol):
    """Private synchronous state; consumers verify current Logto authority first."""

    def resolve_user(self, issuer: str, sub: str) -> UserIdentity: ...
    def get_user(self, user_id: str) -> UserIdentity: ...
    def remember_verified_organization(self, organization_id: str, name: str) -> None: ...
    def create_session(self, identity: UserIdentity, workspace: WorkspaceContext, *, sdk_storage: dict[str, str | None], absolute_seconds: int = 86400, idle_seconds: int = 28800) -> SessionHandle: ...
    def get_session(self, cookie: str) -> StoredSession | None: ...
    def touch_session(self, cookie: str) -> StoredSession: ...
    def update_session(self, cookie: str, *, expected_revision: int, sdk_storage: dict[str, str | None]) -> StoredSession: ...
    def rotate_session(self, cookie: str, *, expected_revision: int, workspace: WorkspaceContext) -> SessionHandle: ...
    def revoke_session(self, cookie: str, *, expected_revision: int | None = None) -> None: ...
    def revoke_user_sessions(self, user_id: str) -> int: ...
    def invalidate_authority(self, user_ids: list[str], workspace: WorkspaceContext) -> int: ...
    def record_authority_verification(self, cookie: str, *, expected_revision: int, verified_at: datetime) -> StoredSession: ...
    def validate_context(self, cookie: str, csrf_token: str, context_id: str) -> StoredSession: ...
    def storage(self, cookie: str) -> PersistentStorage: ...
    def create_login_interaction(self, binding_cookie: str, *, state: str, nonce: str, code_verifier: str, return_to: str, organization_hint: str | None, sdk_storage: dict[str, str | None], previous_session_cookie: str | None = None, lifetime_seconds: int = 600) -> str: ...
    def consume_login_interaction(self, binding_cookie: str, state: str) -> ConsumedLoginInteraction: ...
    def complete_login(self, interaction: ConsumedLoginInteraction, identity: UserIdentity, workspace: WorkspaceContext, *, sdk_storage: dict[str, str | None], absolute_seconds: int = 86400, idle_seconds: int = 28800) -> SessionHandle: ...
    def begin_organization_intent(self, scope: OperationScope, *, request_sha256: str, target: OrganizationOperationTarget) -> IntentAcceptance: ...
    def get_organization_intent(self, scope: OperationScope) -> OrganizationIntent | None: ...
    def advance_organization_intent(self, scope: OperationScope, *, expected_revision: int, stage: str, status: str, remote_refs: SafeRemoteRefs | None = None) -> OrganizationIntent: ...
    def complete_organization_intent(self, scope: OperationScope, *, expected_revision: int, result: OrganizationOperationResult, affected_user_ids=(), invitation_transition: InvitationTransition | None = None) -> OrganizationIntent: ...
    def reject_prepared_organization_intent(self, scope: OperationScope, *, expected_revision: int, result: OrganizationOperationResult) -> OrganizationIntent: ...
    def store_invitation(self, scope: OperationScope, *, expected_revision: int, invitation: InvitationDTO, ticket: str, intended_user_id: str | None = None) -> InvitationRecord: ...
    def invitation_for_ticket(self, ticket: str) -> InvitationRecord: ...
    def invitation_for_acceptance(self, scope: OperationScope, *, ticket: str, verified_email: str) -> InvitationRecord: ...
    def get_invitation(self, actor: Actor, invitation_id: str) -> InvitationRecord: ...
    def list_invitations(self, actor: Actor, *, after_id: str | None = None, limit: int = 101) -> list[InvitationRecord]: ...
    def prepare_invitation_creation(self, scope: OperationScope, *, expected_revision: int, material: InvitationCreationMaterial) -> OrganizationIntent: ...
    def invitation_creation_material(self, scope: OperationScope) -> InvitationCreationMaterial | None: ...
    def active_organization_intent(self, actor: Actor) -> OrganizationIntent | None: ...


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
    "public_task_record": PublicTaskRecord,
    "user_identity": UserIdentity,
    "workspace": WorkspaceContext,
    "ownership": ResourceOwnership,
    "actor": Actor,
    "auth_context": AuthContext,
    "auth_session": AuthSession,
    "organization": OrganizationDTO,
    "member": MemberDTO,
    "invitation": InvitationDTO,
    "operation_scope": OperationScope,
    "organization_intent": OrganizationIntent,
    "organization_result": OrganizationOperationResult,
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
