"""Durable noncanonical protection markers; safe to import without a database."""

import json
import os
from hashlib import sha256
from pathlib import Path
import re
import secrets
import stat
from uuid import UUID


MARKER = ".openmontage-authenticated"
WITNESS_PREFIX = ".openmontage-auth-root-"
WITNESS_DIRECTORY = WITNESS_PREFIX + "records"


class GuardConflict(RuntimeError):
    """Protection cannot be safely established or matched to this data binding."""


def _actual_path(directory):
    # resolve() retains case aliases on case-insensitive filesystems.
    canonical = Path(directory.anchor)
    for name in directory.parts[1:]:
        requested = canonical / name
        try:
            entries = list(canonical.iterdir())
            if name in {entry.name for entry in entries}:
                canonical = requested
                continue
            info = requested.stat()
            canonical = next((entry for entry in entries if not entry.is_symlink() and
                              os.path.samestat(entry.stat(), info)), None)
            if canonical is None:
                raise GuardConflict("Authenticated directory spelling cannot be verified")
        except FileNotFoundError:
            canonical = requested
    return canonical


def _legacy_witness(directory):
    return directory.parent / (WITNESS_PREFIX + sha256(os.fsencode(directory)).hexdigest() + ".json")


def _legacy_entries(directory):
    try:
        return [entry for entry in directory.iterdir()
                if re.fullmatch(re.escape(WITNESS_PREFIX) + r"[0-9a-f]{64}\.json", entry.name)]
    except FileNotFoundError:
        return []


def _witness(directory):
    return directory.parent / WITNESS_DIRECTORY / directory.name


def has_guard(directory):
    try:
        directory = Path(directory).resolve()
        for marker in (directory / MARKER, _legacy_witness(directory)):
            try:
                os.lstat(marker)
                return True
            except FileNotFoundError:
                pass
        witness = _witness(directory)
        try:
            info = os.lstat(witness.parent)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                return True
            try:
                os.lstat(witness)
                return True
            except FileNotFoundError:
                pass
        # An opaque old path hash cannot prove an unmatched case alias is legacy.
        return bool(_legacy_entries(directory.parent))
    except (OSError, RuntimeError):
        return True


def _classification(binding, workspace):
    try:
        binding = str(UUID(binding))
    except (ValueError, TypeError, AttributeError):
        raise GuardConflict("Invalid authenticated data binding") from None
    if workspace is not None and (not isinstance(workspace, dict) or set(workspace) != {"kind", "owner_id"} or
                                  workspace["kind"] not in {"personal", "organization"} or
                                  not isinstance(workspace["owner_id"], str) or not 1 <= len(workspace["owner_id"]) <= 256):
        raise GuardConflict("Invalid authenticated workspace classification")
    return {"version": 1, "database_binding": binding, "workspace": workspace}


def _existing(marker, expected):
    try:
        # Reject an unsafe FIFO without blocking before fstat validation.
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as content:
            info = os.fstat(content.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o077:
                raise GuardConflict("Authenticated marker is unsafe")
            actual = json.loads(content.read())
            if actual != expected or type(actual.get("version")) is not int:
                raise GuardConflict("Authenticated marker classification does not match")
            os.fsync(content.fileno())
    except (OSError, ValueError, AttributeError):
        raise GuardConflict("Authenticated marker cannot be verified") from None


def _persist(marker, expected):
    temporary = None
    try:
        try:
            os.lstat(marker)
        except FileNotFoundError:
            temporary = marker.parent / (MARKER + ".pending-" + secrets.token_hex(16))
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as content:
                content.write((json.dumps(expected, sort_keys=True) + "\n").encode("utf-8"))
                content.flush()
                os.fsync(content.fileno())
            try:
                os.link(temporary, marker)
            except FileExistsError:
                _existing(marker, expected)
        else:
            _existing(marker, expected)
        descriptor = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError, UnicodeError) as error:
        if isinstance(error, GuardConflict):
            raise
        raise GuardConflict("Authenticated protection could not be persisted") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mark_authenticated(directory, *, binding, workspace=None):
    expected = _classification(binding, workspace)
    try:
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / MARKER
        witness = _witness(directory)
        try:
            info = os.lstat(witness.parent)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise GuardConflict("Authenticated witness directory is unsafe")
        legacy = _legacy_entries(directory.parent)
        proof = False
        entries = [marker, witness, _legacy_witness(directory)]
        if legacy:
            entries.append(_legacy_witness(_actual_path(directory)))
        for entry in entries:
            try:
                os.lstat(entry)
            except FileNotFoundError:
                continue
            _existing(entry, expected)
            proof = True
        if legacy and not proof:
            raise GuardConflict("Historical authenticated directory binding cannot be verified")
        witness.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(witness.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise GuardConflict("Authenticated witness directory is unsafe")
        finally:
            os.close(descriptor)
        _persist(witness, expected)
        descriptor = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _persist(marker, expected)
    except (OSError, RuntimeError, UnicodeError) as error:
        if isinstance(error, GuardConflict):
            raise
        raise GuardConflict("Authenticated protection could not be persisted") from None
