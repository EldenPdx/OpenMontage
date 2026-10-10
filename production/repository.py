"""PostgreSQL control plane; creative artifacts and session content remain files."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from production.contracts import (
    ApprovalRequest, CallIntent, ClaimedCommand, ConfigSnapshot, ContractViolation, CostSnapshot, ErrorDTO,
    FileWriteIntent,
    RunContext, SessionReference, StudioEvent, TaskCommand, TaskCreate, TaskRecord,
    TaskState, canonical_sha256, check_approval, check_transition,
)


def _json(value):
    value = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    _reject_credentials(value)
    return Jsonb(value)


def _reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"authorization", "headers", "env", "environment", "password", "secret", "access_token", "api_key"} or normalized.endswith("_api_key"):
                raise ContractViolation("Credentials cannot be stored in control-plane records", "forbidden")
            _reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credentials(item)


def _now():
    return datetime.now(timezone.utc).isoformat()


class PostgresRepository:
    def __init__(self, dsn: str, *, schema: str = "studio"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ContractViolation("Invalid trusted database schema")
        self._dsn = dsn
        self.schema = schema

    @contextmanager
    def _connection(self):
        try:
            connection = psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5,
                                         options="-c statement_timeout=5000 -c lock_timeout=5000")
            with connection:
                connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
                yield connection
        except psycopg.Error:
            raise ContractViolation("PostgreSQL operation unavailable", "dependency_unavailable") from None

    def migrate(self):
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema + ":migration",))
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY)")
            applied = {row["version"] for row in connection.execute("SELECT version FROM schema_migrations")}
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version not in applied:
                    connection.execute(path.read_text(encoding="utf-8"))
                    connection.execute("INSERT INTO schema_migrations VALUES (%s)", (version,))
            return max(row["version"] for row in connection.execute("SELECT version FROM schema_migrations"))

    def _task(self, connection, task_id, *, lock=False):
        row = connection.execute("SELECT record FROM tasks WHERE task_id = %s" + (" FOR UPDATE" if lock else ""), (task_id,)).fetchone()
        if row is None:
            raise ContractViolation("Task not found", "not_found")
        return TaskRecord.model_validate(row["record"])

    def _save_task(self, connection, task):
        connection.execute("UPDATE tasks SET version=%s, state=%s, record=%s, updated_at=clock_timestamp() WHERE task_id=%s", (task.version, task.state.value, _json(task), task.task_id))

    def _replay(self, connection, scope, key, fingerprint):
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (scope + ":" + key,))
        row = connection.execute("SELECT fingerprint, response FROM requests WHERE scope=%s AND idempotency_key=%s", (scope, key)).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise ContractViolation("Conflicting idempotency key", "idempotency_conflict")
        return row["response"] if row else None

    def create_task(self, request: TaskCreate, snapshot: ConfigSnapshot, idempotency_key: str):
        fingerprint = canonical_sha256({"request": request.model_dump(mode="json"), "snapshot": snapshot.model_dump(mode="json")})
        with self._connection() as connection:
            replay = self._replay(connection, "create", idempotency_key, fingerprint)
            if replay is not None:
                return TaskRecord.model_validate(replay)
            suffix = uuid4().hex
            task = TaskRecord(
                task_id="task-" + suffix, project_id="studio-" + suffix, run_id="run-" + uuid4().hex,
                request=request, config_snapshot=snapshot,
                cost=CostSnapshot(budget_usd_micros=request.budget_usd_micros, price_status=snapshot.price_status),
                created_at=_now(), updated_at=_now(),
            )
            connection.execute("INSERT INTO tasks(task_id,project_id,run_id,version,state,record) VALUES (%s,%s,%s,%s,%s,%s)", (task.task_id, task.project_id, task.run_id, task.version, task.state.value, _json(task)))
            session = SessionReference(path=f"{task.task_id}/{task.run_id}/session.jsonl")
            connection.execute("INSERT INTO runs(run_id,task_id,project_id,session) VALUES (%s,%s,%s,%s)", (task.run_id, task.task_id, task.project_id, _json(session)))
            connection.execute("INSERT INTO requests VALUES (%s,%s,%s,%s)", ("create", idempotency_key, fingerprint, _json(task)))
            self._insert_command(connection, TaskCommand(command_id="cmd-" + uuid4().hex, task_id=task.task_id, run_id=task.run_id, kind="start", expected_version=task.version, idempotency_key="start-" + suffix))
            self._event(connection, task, "state", {"state": task.state.value})
            return task

    def get_task(self, task_id: str):
        with self._connection() as connection:
            return self._task(connection, task_id)

    def list_tasks(self, *, limit=50, after=None):
        if not 1 <= limit <= 200:
            raise ContractViolation("Invalid task page size")
        with self._connection() as connection:
            rows = connection.execute("SELECT record FROM tasks WHERE (%s::text IS NULL OR task_id > %s) ORDER BY task_id LIMIT %s", (after, after, limit))
            return [TaskRecord.model_validate(row["record"]) for row in rows]

    def _insert_command(self, connection, command, *, fingerprint=None):
        content = command.model_dump(mode="json", exclude={"command_id", "idempotency_key", "status"})
        fingerprint = fingerprint or canonical_sha256(content)
        row = connection.execute("SELECT * FROM commands WHERE task_id=%s AND (idempotency_key=%s OR (run_id=%s AND kind=%s AND expected_version=%s))", (command.task_id, command.idempotency_key, command.run_id, command.kind, command.expected_version)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ContractViolation("Conflicting command idempotency key", "idempotency_conflict")
            return self._command(row)
        connection.execute("INSERT INTO commands(command_id,task_id,run_id,kind,expected_version,idempotency_key,payload,status,fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s)", (command.command_id, command.task_id, command.run_id, command.kind, command.expected_version, command.idempotency_key, _json(command.payload), fingerprint))
        return command.model_copy(update={"status": "pending"})

    @staticmethod
    def _command(row):
        return TaskCommand.model_validate({key: row[key] for key in TaskCommand.model_fields})

    def enqueue_command(self, command):
        with self._connection() as connection:
            task = self._task(connection, command.task_id, lock=True)
            if task.run_id != command.run_id:
                raise ContractViolation("Command run changed", "state_conflict")
            existing = connection.execute("SELECT * FROM commands WHERE task_id=%s AND idempotency_key=%s", (command.task_id, command.idempotency_key)).fetchone()
            fingerprint = canonical_sha256(command.model_dump(mode="json", exclude={"command_id", "idempotency_key", "status"}))
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ContractViolation("Conflicting command idempotency key", "idempotency_conflict")
                return self._command(existing)
            if task.version != command.expected_version:
                raise ContractViolation("Task version changed", "version_conflict")
            if command.kind in {"cancel", "resume"}:
                if command.kind == "resume":
                    if connection.execute("SELECT 1 FROM calls WHERE task_id=%s AND status IN ('prepared','submitted','outcome_unknown') AND (record->>'external_job_id') IS NULL LIMIT 1", (task.task_id,)).fetchone():
                        raise ContractViolation("Unknown call requires reconciliation", "outcome_unknown")
                    if connection.execute("SELECT 1 FROM file_intents WHERE task_id=%s AND status <> 'reconciled' LIMIT 1", (task.task_id,)).fetchone():
                        raise ContractViolation("File intent requires reconciliation", "file_conflict")
                target = TaskState.CANCEL_REQUESTED if command.kind == "cancel" else TaskState.QUEUED
                task = self._change(connection, task, target)
                if command.kind == "cancel":
                    connection.execute("UPDATE commands SET status='failed' WHERE task_id=%s AND status='pending'", (task.task_id,))
                command = command.model_copy(update={"expected_version": task.version})
            return self._insert_command(connection, command, fingerprint=fingerprint)

    def _context(self, connection, task, run=None):
        run = run or connection.execute("SELECT * FROM runs WHERE run_id=%s", (task.run_id,)).fetchone()
        return RunContext(task_id=task.task_id, project_id=task.project_id, run_id=task.run_id, attempt=run["attempt"], fence=run["fence"], config_snapshot=task.config_snapshot, session=SessionReference.model_validate(run["session"]))

    def _assert_fence(self, connection, task, fence):
        row = connection.execute("SELECT * FROM runs WHERE run_id=%s AND task_id=%s AND project_id=%s AND fence=%s AND lease_worker IS NOT NULL AND lease_expires_at>clock_timestamp() AND active FOR UPDATE", (task.run_id, task.task_id, task.project_id, fence)).fetchone()
        if row is None:
            raise ContractViolation("Worker fence is expired or replaced", "fence_conflict")
        return row

    def assert_fence(self, context):
        with self._connection() as connection:
            task = self._task(connection, context.task_id, lock=True)
            if (task.run_id, task.project_id, task.config_snapshot) != (context.run_id, context.project_id, context.config_snapshot):
                raise ContractViolation("Worker fence context changed", "fence_conflict")
            self._assert_fence(connection, task, context.fence)

    def claim_command(self, worker_id, *, lease_seconds=30):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", worker_id) or not 1 <= lease_seconds <= 3600:
            raise ContractViolation("Invalid worker lease")
        with self._connection() as connection:
            # One global slot; expired writers require reconciliation before handoff.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema + ":worker-slot",))
            if connection.execute("SELECT 1 FROM runs WHERE lease_worker IS NOT NULL LIMIT 1").fetchone():
                return None
            row = connection.execute("SELECT c.* FROM commands c JOIN tasks t USING (task_id) WHERE c.status='pending' AND c.expected_version=t.version AND t.state IN ('queued','cancel_requested') ORDER BY c.sequence FOR UPDATE OF t SKIP LOCKED LIMIT 1").fetchone()
            if row is None:
                return None
            task = self._task(connection, row["task_id"], lock=True)
            run = connection.execute("UPDATE runs SET fence=fence+1, lease_worker=%s, lease_expires_at=clock_timestamp()+(%s * interval '1 second') WHERE run_id=%s AND lease_worker IS NULL AND active RETURNING *", (worker_id, lease_seconds, task.run_id)).fetchone()
            if run is None:
                return None
            connection.execute("UPDATE commands SET status='claimed' WHERE command_id=%s", (row["command_id"],))
            row["status"] = "claimed"
            return ClaimedCommand(command=self._command(row), context=self._context(connection, task, run), worker_id=worker_id, lease_expires_at=run["lease_expires_at"].isoformat())

    def heartbeat(self, claim, *, lease_seconds=30):
        if not 1 <= lease_seconds <= 3600:
            raise ContractViolation("Invalid worker lease")
        with self._connection() as connection:
            task = self._task(connection, claim.context.task_id, lock=True)
            run = self._assert_fence(connection, task, claim.context.fence)
            if run["lease_worker"] != claim.worker_id:
                raise ContractViolation("Worker fence owner changed", "fence_conflict")
            row = connection.execute("UPDATE runs SET lease_expires_at=clock_timestamp()+(%s * interval '1 second') WHERE run_id=%s RETURNING lease_expires_at", (lease_seconds, task.run_id)).fetchone()
            return claim.model_copy(update={"lease_expires_at": row["lease_expires_at"].isoformat()})

    def finish_command(self, claim, *, error=None):
        with self._connection() as connection:
            task = self._task(connection, claim.context.task_id, lock=True)
            row = connection.execute("SELECT status FROM commands WHERE command_id=%s FOR UPDATE", (claim.command.command_id,)).fetchone()
            if row and row["status"] in {"completed", "failed"}:
                return
            run = self._assert_fence(connection, task, claim.context.fence)
            if row is None or run["lease_worker"] != claim.worker_id:
                raise ContractViolation("Worker fence owner changed", "fence_conflict")
            connection.execute("UPDATE commands SET status=%s WHERE command_id=%s", ("failed" if error else "completed", claim.command.command_id))
            connection.execute("UPDATE runs SET lease_worker=NULL,lease_expires_at=NULL WHERE run_id=%s", (task.run_id,))
            if task.state in {TaskState.CANCELLED, TaskState.SUCCEEDED}:
                connection.execute("UPDATE runs SET active=false WHERE run_id=%s", (task.run_id,))
                connection.execute("UPDATE commands SET status='failed' WHERE run_id=%s AND status='pending'", (task.run_id,))
            if error:
                self._event(connection, task, "error", error.model_dump(mode="json"))

    def _change(self, connection, task, state, updates=None):
        updates = dict(updates or {})
        if updates.keys() - {"current_stage", "approval", "error", "result"}:
            raise ContractViolation("Task updates contain immutable fields")
        data = task.model_dump(mode="json")
        data.update(updates, state=state, version=task.version + 1, updated_at=_now())
        previous_state = task.state
        task = TaskRecord.model_validate(data)
        check_transition(previous_state, state, result=task.result, updates=updates)
        self._save_task(connection, task)
        self._event(connection, task, "state", {"state": task.state.value})
        return task

    def transition(self, task_id, state, *, expected_version, fence=None, updates=None):
        with self._connection() as connection:
            task = self._task(connection, task_id, lock=True)
            if task.version != expected_version:
                raise ContractViolation("Task version changed", "version_conflict")
            if fence is not None:
                self._assert_fence(connection, task, fence)
            elif (task.state == TaskState.RUNNING and state != TaskState.CANCEL_REQUESTED) or state in {TaskState.RUNNING, TaskState.SUCCEEDED}:
                raise ContractViolation("Worker writes require a live fence", "fence_conflict")
            return self._change(connection, task, state, updates)

    def _event(self, connection, task, event_type, data):
        row = connection.execute("INSERT INTO events(task_id,record) VALUES (%s,%s) RETURNING event_id,created_at", (task.task_id, _json({}))).fetchone()
        event = StudioEvent(event_id=row["event_id"], task_id=task.task_id, run_id=task.run_id, task_version=task.version, type=event_type, data=data, created_at=row["created_at"].isoformat())
        connection.execute("UPDATE events SET record=%s WHERE event_id=%s", (_json(event), event.event_id))
        return event

    def append_event(self, event, *, fence=None):
        with self._connection() as connection:
            task = self._task(connection, event.task_id, lock=True)
            if event.run_id != task.run_id or event.task_version != task.version:
                raise ContractViolation("Event version changed", "version_conflict")
            if fence is not None:
                self._assert_fence(connection, task, fence)
            return self._event(connection, task, event.type, event.data)

    def events(self, task_id, *, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 500:
            raise ContractViolation("Invalid event page")
        with self._connection() as connection:
            self._task(connection, task_id)
            if after and connection.execute("SELECT 1 FROM events WHERE task_id=%s AND event_id=%s", (task_id, after)).fetchone() is None:
                raise ContractViolation("Event cursor requires a fresh snapshot", "cursor_expired")
            rows = connection.execute("SELECT record FROM events WHERE task_id=%s AND event_id>%s ORDER BY event_id LIMIT %s", (task_id, after, limit))
            return [StudioEvent.model_validate(row["record"]) for row in rows]

    def put_gate(self, request, *, expected_version, fence):
        binding = request.binding
        with self._connection() as connection:
            task = self._task(connection, binding.task_id, lock=True)
            self._assert_fence(connection, task, fence)
            if task.run_id != binding.run_id:
                raise ContractViolation("Approval run changed", "approval_conflict")
            old = connection.execute("SELECT request FROM gates WHERE task_id=%s AND run_id=%s AND gate_id=%s", (task.task_id, task.run_id, binding.gate_id)).fetchone()
            if old:
                if old["request"] == request.model_dump(mode="json") and task.approval == request:
                    return task
                raise ContractViolation("Approval gate is immutable; create a new revision gate", "approval_conflict")
            if task.version != expected_version:
                raise ContractViolation("Task version changed", "version_conflict")
            connection.execute("UPDATE gates SET status='superseded' WHERE task_id=%s AND status='pending'", (task.task_id,))
            connection.execute("INSERT INTO gates(task_id,run_id,gate_id,request,status) VALUES (%s,%s,%s,%s,'pending')", (task.task_id, task.run_id, binding.gate_id, _json(request)))
            task = self._change(connection, task, TaskState.AWAITING_APPROVAL, {"approval": request.model_dump(mode="json"), "current_stage": request.stage})
            self._event(connection, task, "approval", request.model_dump(mode="json"))
            return task

    def decide_gate(self, decision, *, idempotency_key, validate_evidence=None):
        task_id = decision.binding.task_id
        fingerprint = canonical_sha256(decision)
        with self._connection() as connection:
            replay = self._replay(connection, "decision:" + task_id, idempotency_key, fingerprint)
            if replay is not None:
                return TaskRecord.model_validate(replay)
            task = self._task(connection, task_id, lock=True)
            if task.approval is None or task.state != TaskState.AWAITING_APPROVAL:
                raise ContractViolation("No pending approval", "approval_conflict")
            check_approval(decision, task.approval.binding, current_version=task.version)
            if validate_evidence is not None:
                validate_evidence(task, task.approval)
            if not set(decision.selected_option_ids) <= {option.option_id for option in task.approval.options}:
                raise ContractViolation("Unknown approval option")
            row = connection.execute("SELECT status,consumed FROM gates WHERE task_id=%s AND run_id=%s AND gate_id=%s FOR UPDATE", (task_id, task.run_id, decision.binding.gate_id)).fetchone()
            if row is None or row["status"] != "pending" or row["consumed"]:
                raise ContractViolation("Approval already consumed", "approval_conflict")
            status = {"approve": "approved", "revise": "revised", "reject": "rejected", "abort": "aborted"}[decision.decision]
            connection.execute("UPDATE gates SET status=%s,decision=%s,consumed=true WHERE task_id=%s AND run_id=%s AND gate_id=%s", (status, _json(decision), task_id, task.run_id, decision.binding.gate_id))
            approval = task.approval.model_copy(update={"status": status})
            updates = {"approval": approval.model_dump(mode="json")}
            if decision.decision == "reject":
                target = TaskState.BLOCKED
                updates["error"] = ErrorDTO(code="approval_conflict", message="This plan was rejected. Resume to revise it and request fresh approval.", recovery_actions=["resume"]).model_dump(mode="json")
            else:
                target = TaskState.QUEUED if decision.decision in {"approve", "revise"} else TaskState.CANCEL_REQUESTED
            task = self._change(connection, task, target, updates)
            if decision.decision != "reject":
                kind = "continue" if decision.decision == "approve" else "revise" if decision.decision == "revise" else "cancel"
                self._insert_command(connection, TaskCommand(command_id="cmd-" + uuid4().hex, task_id=task_id, run_id=task.run_id, kind=kind, expected_version=task.version, idempotency_key=idempotency_key, payload={"gate_id": decision.binding.gate_id, "decision": decision.model_dump(mode="json")}))
            self._event(connection, task, "approval", {"gate_id": decision.binding.gate_id, "status": status})
            connection.execute("INSERT INTO requests VALUES (%s,%s,%s,%s)", ("decision:" + task_id, idempotency_key, fingerprint, _json(task)))
            return task

    def approved_gate(self, task_id, gate_id):
        with self._connection() as connection:
            task = self._task(connection, task_id)
            row = connection.execute("SELECT request FROM gates WHERE task_id=%s AND run_id=%s AND gate_id=%s AND status='approved'", (task_id, task.run_id, gate_id)).fetchone()
            return ApprovalRequest.model_validate(row["request"]).model_copy(update={"status": "approved"}) if row else None

    def _call(self, connection, call_id):
        row = connection.execute("SELECT record FROM calls WHERE call_id=%s", (call_id,)).fetchone()
        if row is None:
            raise ContractViolation("Call not found", "not_found")
        return CallIntent.model_validate(row["record"])

    def get_call(self, call_id):
        with self._connection() as connection:
            return self._call(connection, call_id)

    def _cost(self, connection, task):
        row = connection.execute("SELECT COALESCE(sum(reserved_usd_micros),0) AS reserved, COALESCE(sum(actual_usd_micros),0) AS spent, count(*) FILTER (WHERE status='outcome_unknown' OR (status IN ('receipted','settled') AND actual_usd_micros IS NULL)) AS unknown FROM calls WHERE task_id=%s", (task.task_id,)).fetchone()
        unquoted = connection.execute("SELECT 1 FROM calls WHERE task_id=%s AND (record->>'price_status'='unquoted' OR (status IN ('receipted','settled') AND actual_usd_micros IS NULL)) LIMIT 1", (task.task_id,)).fetchone()
        task = task.model_copy(update={"cost": CostSnapshot(budget_usd_micros=task.cost.budget_usd_micros, reserved_usd_micros=int(row["reserved"]), spent_usd_micros=int(row["spent"]), unknown_call_count=row["unknown"], price_status="unquoted" if unquoted else task.config_snapshot.price_status)})
        self._save_task(connection, task)
        self._event(connection, task, "cost", task.cost.model_dump(mode="json"))
        return task.cost

    @staticmethod
    def _same_call(previous, current):
        fields = ("task_id", "run_id", "kind", "operation", "provider", "model", "request_sha256", "price_status", "reserved_usd_micros")
        if any(getattr(previous, field) != getattr(current, field) for field in fields):
            raise ContractViolation("Conflicting call idempotency fingerprint", "idempotency_conflict")

    def reserve_call(self, intent):
        with self._connection() as connection:
            task = self._task(connection, intent.task_id, lock=True)
            if task.run_id != intent.run_id:
                raise ContractViolation("Call run changed", "fence_conflict")
            self._assert_fence(connection, task, intent.fence)
            if task.state != TaskState.RUNNING or (task.approval and task.approval.status == "pending"):
                raise ContractViolation("Task is not authorized to execute calls", "approval_conflict")
            row = connection.execute("SELECT record FROM calls WHERE call_id=%s", (intent.call_id,)).fetchone()
            if row:
                previous = CallIntent.model_validate(row["record"])
                self._same_call(previous, intent)
                return previous
            if connection.execute("SELECT 1 FROM calls WHERE task_id=%s AND status='outcome_unknown' AND record->>'external_job_id' IS NULL LIMIT 1", (task.task_id,)).fetchone():
                raise ContractViolation("A previous submission has an unknown result; reconcile before authorizing another call", "outcome_unknown")
            if intent.reserved_usd_micros is None:
                raise ContractViolation("Unknown price requires an authorized reservation", "quote_required")
            if intent.status not in {"prepared", "reserved"} or intent.actual_usd_micros is not None or intent.external_job_id is not None:
                raise ContractViolation("New call must begin with a pre-submit intent")
            if intent.kind == "model":
                count = connection.execute("SELECT count(*) AS total FROM calls WHERE task_id=%s AND record->>'kind'='model'", (task.task_id,)).fetchone()["total"]
                if count >= task.config_snapshot.max_turns:
                    raise ContractViolation("Model request turn limit reached", "forbidden")
                if (intent.provider, intent.model) != (task.config_snapshot.provider, task.config_snapshot.model):
                    raise ContractViolation("Model request differs from immutable profile", "forbidden")
            budget = task.cost
            if budget.spent_usd_micros + budget.reserved_usd_micros + intent.reserved_usd_micros > budget.budget_usd_micros:
                raise ContractViolation("Atomic reservation exceeds task budget", "budget_exceeded")
            connection.execute("INSERT INTO calls(call_id,task_id,run_id,request_sha256,record,reserved_usd_micros,status) VALUES (%s,%s,%s,%s,%s,%s,%s)", (intent.call_id, intent.task_id, intent.run_id, intent.request_sha256, _json(intent), intent.reserved_usd_micros, intent.status))
            self._cost(connection, task)
            return intent

    def update_call(self, intent):
        with self._connection() as connection:
            task = self._task(connection, intent.task_id, lock=True)
            if task.run_id != intent.run_id:
                raise ContractViolation("Call run changed", "fence_conflict")
            self._assert_intent_fence(connection, task, intent.fence, recovery_allowed=intent.status in {"receipted", "settled", "failed"})
            previous = self._call(connection, intent.call_id)
            self._same_call(previous, intent)
            if previous == intent:
                return previous
            allowed = {
                "reserved": {"prepared", "submitted", "receipted", "settled", "failed", "outcome_unknown"},
                "prepared": {"submitted", "receipted", "settled", "failed", "outcome_unknown"},
                "submitted": {"receipted", "settled", "failed", "outcome_unknown"},
                "receipted": {"settled", "failed", "outcome_unknown"},
                "outcome_unknown": {"receipted", "settled", "failed"},
                "settled": set(), "failed": set(),
            }
            if intent.status != previous.status and intent.status not in allowed[previous.status]:
                raise ContractViolation("Call cannot be resubmitted or unsettled", "outcome_unknown")
            if previous.status in {"settled", "failed"} and intent.model_dump(exclude={"fence"}) != previous.model_dump(exclude={"fence"}):
                raise ContractViolation("Call is already settled", "state_conflict")
            if intent.status == "outcome_unknown" and intent.actual_usd_micros is not None:
                raise ContractViolation("Unknown outcome retains its reservation", "outcome_unknown")
            if intent.status not in {"settled", "failed"} and intent.actual_usd_micros is not None:
                raise ContractViolation("Only terminal calls can settle actual cost")
            if previous.external_job_id and intent.external_job_id != previous.external_job_id:
                raise ContractViolation("External job receipt cannot be replaced", "state_conflict")
            reserved = 0 if intent.status in {"settled", "failed"} and intent.actual_usd_micros is not None else previous.reserved_usd_micros
            connection.execute("UPDATE calls SET record=%s,reserved_usd_micros=%s,actual_usd_micros=%s,status=%s WHERE call_id=%s", (_json(intent), reserved, intent.actual_usd_micros, intent.status, intent.call_id))
            self._cost(connection, task)
            return intent

    def _assert_intent_fence(self, connection, task, fence, *, recovery_allowed):
        if recovery_allowed and task.state == TaskState.RECOVERY_REQUIRED:
            row = connection.execute("SELECT 1 FROM runs WHERE run_id=%s AND fence=%s AND lease_worker IS NULL FOR UPDATE", (task.run_id, fence)).fetchone()
            if row:
                return
        self._assert_fence(connection, task, fence)

    def record_file_intent(self, intent):
        with self._connection() as connection:
            task = self._task(connection, intent.task_id, lock=True)
            if task.run_id != intent.run_id:
                raise ContractViolation("File run changed", "fence_conflict")
            self._assert_intent_fence(connection, task, intent.fence, recovery_allowed=intent.status in {"reconciled", "conflict"})
            row = connection.execute("SELECT record FROM file_intents WHERE intent_id=%s", (intent.intent_id,)).fetchone()
            if row:
                previous = FileWriteIntent.model_validate(row["record"])
                if previous.model_dump(exclude={"fence", "status"}) != intent.model_dump(exclude={"fence", "status"}):
                    raise ContractViolation("Conflicting file intent fingerprint", "idempotency_conflict")
                allowed = {"prepared": {"applied", "conflict"}, "applied": {"reconciled", "conflict"}, "conflict": {"reconciled"}, "reconciled": set()}
                if previous.status != intent.status and intent.status not in allowed[previous.status]:
                    raise ContractViolation("Invalid file reconciliation transition", "file_conflict")
                connection.execute("UPDATE file_intents SET record=%s,status=%s WHERE intent_id=%s", (_json(intent), intent.status, intent.intent_id))
            else:
                if intent.status != "prepared" or task.state not in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED}:
                    raise ContractViolation("New file write requires a running task", "state_conflict")
                overlap = connection.execute("SELECT 1 FROM file_intents WHERE task_id=%s AND record->'target'->>'path'=%s AND (status IN ('prepared','applied','conflict') OR (record->'target'->>'revision')::bigint >= %s) LIMIT 1", (task.task_id, intent.target.path, intent.target.revision)).fetchone()
                if overlap:
                    raise ContractViolation("File revision needs reconciliation", "file_conflict")
                connection.execute("INSERT INTO file_intents VALUES (%s,%s,%s,%s)", (intent.intent_id, intent.task_id, _json(intent), intent.status))
            return intent

    def unresolved_intents(self, task_id):
        with self._connection() as connection:
            self._task(connection, task_id)
            calls = connection.execute("SELECT record FROM calls WHERE task_id=%s AND status NOT IN ('settled','failed') ORDER BY call_id", (task_id,))
            result = [CallIntent.model_validate(row["record"]) for row in calls]
            files = connection.execute("SELECT record FROM file_intents WHERE task_id=%s AND status <> 'reconciled' ORDER BY intent_id", (task_id,))
            return result + [FileWriteIntent.model_validate(row["record"]) for row in files]

    def session(self, context):
        with self._connection() as connection:
            task = self._task(connection, context.task_id, lock=True)
            if (context.run_id, context.project_id) != (task.run_id, task.project_id):
                raise ContractViolation("Session context changed", "fence_conflict")
            run = self._assert_fence(connection, task, context.fence)
            return SessionReference.model_validate(run["session"])

    def bind_session(self, context, session):
        with self._connection() as connection:
            task = self._task(connection, context.task_id, lock=True)
            if (context.run_id, context.project_id) != (task.run_id, task.project_id) or not session.path.startswith(f"{task.task_id}/{task.run_id}/"):
                raise ContractViolation("Session is outside the managed run", "forbidden")
            run = self._assert_fence(connection, task, context.fence)
            old = SessionReference.model_validate(run["session"])
            if old.path != session.path or (old.session_id is not None and old.session_id != session.session_id):
                raise ContractViolation("Exact Pi session cannot be replaced", "state_conflict")
            connection.execute("UPDATE runs SET session=%s WHERE run_id=%s", (_json(session), task.run_id))
            return self._context(connection, task)

    def recover_expired_leases(self):
        count = 0
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema + ":worker-slot",))
            rows = connection.execute("SELECT t.task_id FROM tasks t JOIN runs r USING (run_id) WHERE r.lease_expires_at<=clock_timestamp() AND t.state<>'recovery_required' ORDER BY t.task_id FOR UPDATE OF t SKIP LOCKED").fetchall()
            for row in rows:
                task = self._task(connection, row["task_id"])
                connection.execute("UPDATE runs SET fence=fence+1 WHERE run_id=%s", (task.run_id,))
                connection.execute("UPDATE commands SET status='failed' WHERE run_id=%s AND status IN ('pending','claimed') AND kind<>'cancel'", (task.run_id,))
                calls = connection.execute("SELECT record FROM calls WHERE task_id=%s AND status IN ('reserved','prepared','submitted') AND (record->>'external_job_id') IS NULL", (task.task_id,)).fetchall()
                for record in calls:
                    call = CallIntent.model_validate(record["record"]).model_copy(update={"status": "outcome_unknown", "actual_usd_micros": None})
                    connection.execute("UPDATE calls SET record=%s,status='outcome_unknown',actual_usd_micros=NULL WHERE call_id=%s", (_json(call), call.call_id))
                # Recovery can override any lifecycle state after a writer died.
                data = task.model_dump(mode="json")
                data.update(state=TaskState.RECOVERY_REQUIRED, version=task.version + 1, updated_at=_now(), error=ErrorDTO(code="outcome_unknown", message="Worker lease expired; confirm process exit and reconcile persisted intents", recovery_actions=["reconcile", "cancel"]).model_dump(mode="json"))
                task = TaskRecord.model_validate(data)
                self._save_task(connection, task)
                self._cost(connection, task)
                self._event(connection, task, "recovery", {"state": task.state.value, "process_exit_required": True})
                count += 1
        return count

    def recovery_context(self, task_id):
        with self._connection() as connection:
            task = self._task(connection, task_id)
            if task.state != TaskState.RECOVERY_REQUIRED:
                raise ContractViolation("Task is not awaiting recovery", "state_conflict")
            return self._context(connection, task)

    def pending_cancel(self, task_id):
        with self._connection() as connection:
            self._task(connection, task_id)
            row = connection.execute("SELECT * FROM commands WHERE task_id=%s AND kind='cancel' AND status IN ('pending','claimed') ORDER BY sequence LIMIT 1", (task_id,)).fetchone()
            return self._command(row) if row else None

    def release_recovered_lease(self, task_id, *, fence, terminated):
        if terminated is not True:
            raise ContractViolation("Original worker process exit must be confirmed", "fence_conflict")
        with self._connection() as connection:
            task = self._task(connection, task_id, lock=True)
            if task.state != TaskState.RECOVERY_REQUIRED:
                raise ContractViolation("Task is not awaiting recovery", "state_conflict")
            row = connection.execute("UPDATE runs SET lease_worker=NULL,lease_expires_at=NULL WHERE run_id=%s AND fence=%s RETURNING run_id", (task.run_id, fence)).fetchone()
            if row is None:
                raise ContractViolation("Recovery fence changed", "fence_conflict")
            cancel = connection.execute("SELECT command_id FROM commands WHERE task_id=%s AND kind='cancel' AND status IN ('pending','claimed') ORDER BY sequence LIMIT 1", (task_id,)).fetchone()
            if cancel:
                task = self._change(connection, task, TaskState.CANCEL_REQUESTED)
                self._change(connection, task, TaskState.CANCELLED)
                connection.execute("UPDATE commands SET status='failed' WHERE run_id=%s AND status IN ('pending','claimed')", (task.run_id,))
                connection.execute("UPDATE commands SET status='completed' WHERE command_id=%s", (cancel["command_id"],))
                connection.execute("UPDATE runs SET active=false WHERE run_id=%s", (task.run_id,))
