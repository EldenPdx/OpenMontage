"""Strict JSONL transport for one isolated, version-pinned Pi process."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import signal
from typing import AsyncIterator, Mapping, Sequence
from uuid import uuid4

from production.contracts import ContractViolation, RunContext, SessionReference


class PiRPC:
    """Command/environment are trusted backend configuration, never HTTP inputs."""

    FRAME_LIMIT = 4 * 1024 * 1024
    COMMANDS = frozenset({"prompt", "get_state", "get_messages", "get_session_stats", "get_commands",
                          "clear_queue", "abort_retry", "abort", "set_auto_retry", "set_auto_compaction"})

    def __init__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str], session_root: Path,
                 redact_values: Sequence[str] = (), startup_timeout: float = 30,
                 request_timeout: float = 60, shutdown_timeout: float = 5):
        if any(flag in command for flag in ("--no-session", "--continue", "--resume", "--session", "--session-id")):
            raise ContractViolation("Runner requires an explicit managed session", "rpc_error")
        self.command = list(command)
        self.cwd = Path(cwd).resolve()
        self.env = dict(env)
        self.session_root = Path(session_root).resolve()
        self.redact_values = tuple(value for value in redact_values if value)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.shutdown_timeout = shutdown_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.reader_tasks: list[asyncio.Task] = []
        self.diagnostics = ""
        self.failure: ContractViolation | None = None
        self.write_lock = asyncio.Lock()

    @property
    def returncode(self):
        return self.process.returncode if self.process else None

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.redact_values:
                value = value.replace(secret, "[redacted]")
            return re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", value)
        if isinstance(value, dict):
            return {key: "[redacted]" if re.search(r"(?i)authorization|api[_-]?key|password|secret", key)
                    else self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    async def start(self, context: RunContext) -> SessionReference:
        if self.process is not None:
            raise ContractViolation("Pi process already started", "rpc_error")
        prefix = f"{context.task_id}/{context.run_id}"
        if not (context.session.path == prefix + ".jsonl" or context.session.path.startswith(prefix + "/")):
            raise ContractViolation("Session reference belongs to another task/run", "forbidden")
        session_path = (self.session_root / context.session.path).resolve()
        if not session_path.is_relative_to(self.session_root):
            raise ContractViolation("Session escapes managed directory", "forbidden")
        session_path.parent.mkdir(parents=True, exist_ok=True)
        self.cwd.mkdir(parents=True, exist_ok=True)
        version_prefix = self.command[:2] if Path(self.command[0]).name.startswith(("node", "python")) else self.command[:1]
        version = await asyncio.create_subprocess_exec(*version_prefix, "--version", env=self.env,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(version.communicate(), self.startup_timeout)
        except BaseException:
            with suppress(ProcessLookupError):
                version.kill()
            await version.wait()
            raise
        if version.returncode != 0 or output.strip() != b"1.1.0":
            raise ContractViolation("Expected the pinned official Pi 1.1.0 runtime", "dependency_unavailable")
        self.process = await asyncio.create_subprocess_exec(
            *self.command, "--session-dir", str(self.session_root), "--session", str(session_path),
            cwd=self.cwd, env=self.env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=self.FRAME_LIMIT + 1,
        )
        self.reader_tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        try:
            state = await self.request("get_state", timeout=self.startup_timeout)
            actual = Path(state["sessionFile"]).resolve()
            if actual != session_path or (context.session.session_id and state["sessionId"] != context.session.session_id):
                raise ContractViolation("Pi restored a different session", "rpc_error")
            model = state.get("model") or {}
            if model.get("provider") != context.config_snapshot.provider or model.get("id") != context.config_snapshot.model:
                raise ContractViolation("Pi model differs from immutable run configuration", "rpc_error")
            await self.request("set_auto_retry", enabled=False)
            await self.request("set_auto_compaction", enabled=model.get("api") == "studio-guarded")
            return SessionReference(path=context.session.path, session_id=state["sessionId"])
        except BaseException:
            await self.close()
            raise

    def _failed(self, message):
        if self.failure is None:
            self.failure = ContractViolation(self.redact(message), "rpc_error")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)

    async def _read_stdout(self):
        try:
            while line := await self.process.stdout.readline():
                if len(line) > self.FRAME_LIMIT or not line.endswith(b"\n"):
                    raise ValueError("Pi RPC frame exceeds limit or is incomplete")
                record = json.loads(line.rstrip(b"\r\n").decode("utf-8"))
                if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                    raise ValueError("Pi RPC record must be a typed object")
                record = self.redact(record)
                if record["type"] == "response":
                    future = self.pending.get(record.get("id"))
                    if future is not None and not future.done():
                        if record.get("success") is True:
                            future.set_result(record.get("data") or {})
                        else:
                            future.set_exception(ContractViolation(record.get("error") or "Pi command failed", "rpc_error"))
                    elif record.get("command") == "parse":
                        raise ValueError("Pi rejected an RPC frame")
                else:
                    self.queue.put_nowait(record)
        except (ValueError, UnicodeError, asyncio.QueueFull) as exc:
            self._failed(str(exc))
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
        except asyncio.CancelledError:
            raise
        finally:
            self._failed("Pi RPC stream closed")
            # Keep the reader independent of a slow event consumer.
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(None)

    async def _read_stderr(self):
        while data := await self.process.stderr.read(4096):
            self.diagnostics = self.redact((self.diagnostics + data.decode("utf-8", "replace"))[-16_384:])

    async def request(self, command_type: str, *, command_id: str | None = None, timeout: float | None = None, **payload) -> dict:
        if command_type not in self.COMMANDS:
            raise ContractViolation("RPC command is outside the supervisor allowlist", "forbidden")
        if self.failure:
            raise self.failure
        if self.process is None or self.process.returncode is not None:
            raise ContractViolation("Pi is not running", "rpc_error")
        identifier = command_id or uuid4().hex
        if identifier in self.pending or len(self.pending) >= 32:
            raise ContractViolation("Too many outstanding or duplicate RPC commands", "rpc_error")
        record = json.dumps({**payload, "id": identifier, "type": command_type}, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(record) > self.FRAME_LIMIT:
            raise ContractViolation("RPC command exceeds frame limit", "invalid_input")
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            async with self.write_lock:
                self.process.stdin.write(record)
                await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout or self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise ContractViolation("Pi command timed out", "timeout") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ContractViolation("Pi RPC input closed", "rpc_error") from exc
        finally:
            self.pending.pop(identifier, None)

    async def prompt(self, message: str, *, command_id: str) -> dict:
        return await self.request("prompt", message=message, command_id=command_id)

    async def inspect(self) -> dict:
        return await self.request("get_state")

    async def clear_queue(self) -> dict:
        return await self.request("clear_queue")

    async def abort_retry(self) -> dict:
        return await self.request("abort_retry")

    async def abort(self) -> dict:
        return await self.request("abort")

    async def events(self) -> AsyncIterator[dict]:
        while (event := await self.queue.get()) is not None:
            yield event
        if self.failure and self.returncode not in (None, 0):
            raise self.failure

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None and not self.failure:
            for command in ("clear_queue", "abort_retry", "abort"):
                with suppress(ContractViolation, OSError):
                    await self.request(command, timeout=self.shutdown_timeout)
        if self.process.stdin:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), self.shutdown_timeout)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), self.shutdown_timeout)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        # EOF can exit Pi while one of its children still owns the process group.
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        await asyncio.sleep(0.05)
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        for task in self.reader_tasks:
            task.cancel()
        await asyncio.gather(*self.reader_tasks, return_exceptions=True)
