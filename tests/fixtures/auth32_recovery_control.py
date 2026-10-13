"""Private fixed-phase handshake; only the lifecycle owner operates Native services."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import time


def read_private(path):
    path = Path(path)
    if path.absolute() != path.resolve():
        raise RuntimeError("Recovery input must not contain aliases")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError("Recovery input must be a private regular file")
        return json.load(stream)


def write_private(path, value):
    path = Path(path)
    pending = path.with_name(path.name + ".pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def recovery_plan():
    name = os.environ.get("AUTH32_NATIVE_RECOVERY_PLAN")
    if not name:
        raise RuntimeError("Root must supply AUTH32_NATIVE_RECOVERY_PLAN for the exclusive recovery window")
    path = Path(name)
    value = read_private(path)
    if set(value) != {"schema_version", "run_id", "owner_state_dir"} or value["schema_version"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", str(value["run_id"])):
        raise RuntimeError("Recovery plan is not the fixed test-only contract")
    owner = Path(value["owner_state_dir"])
    if not owner.is_absolute() or owner.resolve() != owner or not owner.is_dir() or owner.stat().st_mode & 0o077:
        raise RuntimeError("Recovery owner state must be a private unaliased directory")
    directory = path.parent
    if not directory.is_dir() or directory.stat().st_mode & 0o077 or any((directory / name).exists() for name in ("request.json", "ack.json", "manifest.json", "receipt.json")):
        raise RuntimeError("Recovery control must be fresh and private; retain earlier recovery state")
    return directory, value


class RootRecoveryWindow:
    def __init__(self, directory, run_id, manifest_hash, *, timeout=180):
        self.directory = Path(directory)
        self.run_id, self.manifest_hash, self.timeout = run_id, manifest_hash, timeout
        self.original_ready = False

    def request(self, phase):
        write_private(self.directory / "request.json", {"schema_version": 1, "run_id": self.run_id,
                      "phase": phase, "manifest_sha256": self.manifest_hash})

    def wait(self, phase):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            path = self.directory / "ack.json"
            if path.exists():
                value = read_private(path)
                if set(value) != {"schema_version", "run_id", "phase", "manifest_sha256"} or value["schema_version"] != 1 or value["run_id"] != self.run_id or value["manifest_sha256"] != self.manifest_hash:
                    raise RuntimeError("Recovery acknowledgment does not match this exact request")
                if value["phase"] == phase:
                    return
                if value["phase"] == "operator_failed":
                    raise RuntimeError("Root recovery helper failed; retain private diagnostics and restore originals")
            time.sleep(0.02)
        raise RuntimeError("Root recovery helper did not acknowledge " + phase)

    @contextmanager
    def clones(self, observe_quiesced):
        try:
            self.request("source_quiesced")
            self.wait("native_quiesced")
            observe_quiesced()
            self.request("snapshot_allowed")
            self.wait("restored_clones_ready")
            yield
        finally:
            self.request("restore_original")
            # A stale/failed clone response must never prevent requesting restoration.
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                path = self.directory / "ack.json"
                if path.exists():
                    value = read_private(path)
                    if value == {"schema_version": 1, "run_id": self.run_id, "phase": "original_ready", "manifest_sha256": self.manifest_hash}:
                        self.original_ready = True
                        break
                time.sleep(0.02)
            else:
                raise RuntimeError("Root must restore and verify original Native services before ending the drill")


def manifest_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()
