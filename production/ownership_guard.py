"""Durable noncanonical protection markers; safe to import without a database."""

import json
import os
from hashlib import sha256
from pathlib import Path
import secrets
import stat
from uuid import UUID


MARKER = ".openmontage-authenticated"
WITNESS_PREFIX = ".openmontage-auth-root-"


class GuardConflict(RuntimeError):
    """Protection cannot be safely established or matched to this data binding."""


def _witness(directory):
    return directory.parent / (WITNESS_PREFIX + sha256(os.fsencode(directory)).hexdigest() + ".json")


def has_guard(directory):
    try:
        directory = Path(directory).resolve()
        for marker in (directory / MARKER, _witness(directory)):
            try:
                os.lstat(marker)
                return True
            except FileNotFoundError:
                pass
        return False
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
            temporary = marker.parent / (marker.name + ".pending-" + secrets.token_hex(16))
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
        descriptor = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
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
        try:
            os.lstat(marker)
        except FileNotFoundError:
            pass
        else:
            _existing(marker, expected)
        _persist(_witness(directory), expected)
        _persist(marker, expected)
    except (OSError, RuntimeError, UnicodeError) as error:
        if isinstance(error, GuardConflict):
            raise
        raise GuardConflict("Authenticated protection could not be persisted") from None
