"""Private process ownership records used to confirm an expired writer has exited."""

import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import signal
import tempfile
import time

from production.contracts import ContractViolation, FileWriteIntent, TaskState


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".studio-state-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_identity(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid=", "-o", "command="], capture_output=True, text=True, timeout=5)
    parts = result.stdout.strip().split(maxsplit=6)
    if result.returncode or not parts:
        return None
    if len(parts) != 7:
        raise ContractViolation("OS process identity could not be verified", "fence_conflict")
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        cwd = str(proc_cwd.resolve())
    else:
        directory = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5)
        cwd = next((line[1:] for line in directory.stdout.splitlines() if line.startswith("n")), None)
    if cwd is None:
        status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5)
        if status.returncode or status.stdout.strip().startswith("Z"):
            return None
        raise ContractViolation("Managed process working directory could not be verified", "fence_conflict")
    return {"start": " ".join(parts[:5]), "pgid": int(parts[5]), "command": parts[6], "cwd": cwd}


def process_path(runtime_root, context):
    return Path(runtime_root) / "processes" / context.task_id / (context.run_id + ".json")


def record_process(runtime_root, context, runner):
    identity = process_identity(runner.process.pid)
    if identity is None or identity["pgid"] != runner.process.pid or identity["cwd"] != str(runner.cwd):
        raise ContractViolation("Pi process does not match the managed session", "fence_conflict")
    write_private(process_path(runtime_root, context), {**identity, "pid": runner.process.pid,
                  "task_id": context.task_id, "run_id": context.run_id, "fence": context.fence,
                  "session": context.session.model_dump(mode="json"),
                  "started_at": time.time(),
                  "argv": [*runner.command, "--session-dir", str(runner.session_root), "--session", str(runner.session_root / context.session.path)]})


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        result = subprocess.run(["ps", "-eo", "pgid=,stat="], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode:
        return True
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            return True
        if int(fields[0]) == pgid:
            members.append(fields[1])
    if members:
        # Linux keeps a numeric process group until init reaps its terminated members.
        return any(not state.startswith(("Z", "X")) for state in members)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def confirm_process_exit(record, context, runtime_root):
    session = str(Path(runtime_root).resolve() / "sessions" / context.session.path)
    if (record.get("task_id"), record.get("run_id"), record.get("session")) != (context.task_id, context.run_id, context.session.model_dump(mode="json")):
        raise ContractViolation("Process record belongs to another session", "fence_conflict")
    if record.get("fence") not in {context.fence, context.fence - 1} or record.get("pid", 0) <= 1 or record.get("pgid") != record.get("pid"):
        raise ContractViolation("Process ownership fence is invalid", "fence_conflict")
    argv = record.get("argv") or []
    if "--session" not in argv or argv[argv.index("--session") + 1] != session:
        raise ContractViolation("Process session argument is invalid", "fence_conflict")
    pid, pgid = record["pid"], record["pgid"]
    expected = {key: record[key] for key in ("start", "pgid", "command", "cwd")}
    current = process_identity(pid)
    if current is None:
        if _group_exists(pgid):
            raise ContractViolation("Orphan process group identity requires manual reconciliation", "fence_conflict")
        return True
    if current != expected:
        raise ContractViolation("PID was replaced; refusing to signal another process", "fence_conflict")
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    killed = False
    while _group_exists(pgid):
        current = process_identity(pid)
        if current is not None and current != expected:
            raise ContractViolation("PID changed during shutdown", "fence_conflict")
        if time.monotonic() > deadline:
            raise ContractViolation("Original Pi process group did not exit", "fence_conflict")
        if not killed and time.monotonic() > deadline - 3:
            os.killpg(pgid, signal.SIGKILL)
            killed = True
        time.sleep(0.05)
    return True


class RecoveryService:
    def __init__(self, repository, runtime_root, projects_dir, *, held_leases=None):
        self.repository = repository
        self.runtime_root, self.projects_dir = Path(runtime_root).resolve(), Path(projects_dir).resolve()
        self.held_leases = held_leases if held_leases is not None else {}

    async def _db(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    async def recover(self):
        await self._db(self.repository.recover_expired_leases)
        recovered, after = 0, None
        while True:
            tasks = await self._db(self.repository.list_tasks, limit=200, after=after)
            if not tasks:
                return recovered
            for task in tasks:
                if task.state != TaskState.RECOVERY_REQUIRED:
                    continue
                context = await self._db(self.repository.recovery_context, task.task_id)
                active_path = process_path(self.runtime_root, context)
                archive = active_path.with_name(f"{context.run_id}.recovering-{context.fence}.json")
                path = active_path if active_path.is_file() else archive
                if not path.is_file():
                    continue
                try:
                    record = json.loads(path.read_text())
                    await asyncio.to_thread(confirm_process_exit, record, context, self.runtime_root)
                    from production.tool_bridge import confirm_tool_process_exit, tool_process_records
                    records = tool_process_records(self.runtime_root, context)
                    for tool_path, tool_record in records:
                        await asyncio.to_thread(confirm_tool_process_exit, self.repository, tool_record, context)
                        tool_path.unlink(missing_ok=True)
                    intents = await self._db(self.repository.unresolved_intents, task.task_id)
                    counter = self.runtime_root / "runs" / context.task_id / context.run_id / "active-time.json"
                    previous = json.loads(counter.read_text()) if counter.exists() else {}
                    seconds = previous.get("seconds", 0)
                    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0 or (not counter.exists() and record["fence"] > 2):
                        raise ContractViolation("Active-time evidence requires reconciliation", "file_conflict")
                    if previous.get("last_fence") != record["fence"]:
                        await asyncio.to_thread(write_private, counter, {"seconds": seconds + max(0, time.time() - record["started_at"]), "last_fence": record["fence"]})
                    if path != archive:
                        path.replace(archive)
                    lease = self.held_leases.pop(context.task_id, None)
                    if lease is not None:
                        lease.close()
                    from lib.checkpoint import StudioProjectLease
                    with StudioProjectLease(self.projects_dir / context.project_id, f"{context.task_id}/{context.run_id}/{context.fence}"):
                        await self._db(self.repository.release_recovered_lease, task.task_id, fence=context.fence, terminated=True)
                    if (await self._db(self.repository.get_task, task.task_id)).state != TaskState.CANCELLED:
                        await asyncio.to_thread(self._reconcile_files, context, intents)
                    archive.unlink()
                    recovered += 1
                except (ContractViolation, OSError, ValueError, KeyError, TypeError, IndexError):
                    # Retain ownership and journals when identity/evidence is ambiguous.
                    continue
            after = tasks[-1].task_id

    def _reconcile_files(self, context, intents):
        from production.task_service import file_sha256

        project = (self.projects_dir / context.project_id).resolve()
        if not project.is_relative_to(self.projects_dir):
            raise ContractViolation("Recovered project escaped its root", "file_conflict")
        revisions_path = project / ".studio-revisions.json"
        revisions = json.loads(revisions_path.read_text()) if revisions_path.exists() else {}
        for intent in intents:
            if not isinstance(intent, FileWriteIntent):
                continue
            target = (project / intent.target.path).resolve()
            matching = target.is_relative_to(project) and target.is_file() and file_sha256(target) == intent.target.sha256
            revision = revisions.get(intent.target.path, 0)
            if matching and revision <= intent.target.revision:
                revisions[intent.target.path] = intent.target.revision
                write_private(revisions_path, revisions)
                if intent.status == "prepared":
                    intent = self.repository.record_file_intent(intent.model_copy(update={"fence": context.fence, "status": "conflict"}))
                self.repository.record_file_intent(intent.model_copy(update={"fence": context.fence, "status": "reconciled"}))
            else:
                self.repository.record_file_intent(intent.model_copy(update={"fence": context.fence, "status": "conflict"}))
