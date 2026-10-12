"""Persistent filesystem protection is usable by genuinely offline local callers."""

from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest


def test_guard_survives_reuse_and_all_marker_entries_protect_anonymous_access(tmp_path):
    from production.ownership_guard import MARKER, GuardConflict, has_guard, mark_authenticated
    root = tmp_path / "projects"
    root.mkdir()
    canonical = root / "project.json"
    canonical.write_bytes(b'{"project_id":"legacy"}\n')
    binding = str(uuid4())
    assert not has_guard(root)
    mark_authenticated(root, binding=binding)
    assert has_guard(root)
    marker = root / MARKER
    original = marker.read_bytes()
    mark_authenticated(root, binding=binding)
    assert marker.read_bytes() == original
    assert canonical.read_bytes() == b'{"project_id":"legacy"}\n'
    with pytest.raises(GuardConflict):
        mark_authenticated(root, binding=str(uuid4()))
    assert has_guard(root)
    for kind in ("malformed", "symlink", "directory"):
        directory = tmp_path / kind
        directory.mkdir()
        entry = directory / MARKER
        if kind == "malformed":
            entry.write_bytes(b"not-json")
            entry.chmod(0)
        elif kind == "symlink":
            entry.symlink_to(tmp_path / "missing-marker")
        else:
            entry.mkdir()
        assert has_guard(directory)
        with pytest.raises(GuardConflict):
            mark_authenticated(directory, binding=binding)


def test_guard_import_has_no_database_crypto_or_identity_dependency(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = "from production.ownership_guard import has_guard; import sys; assert not has_guard(sys.argv[1]); assert not any(name.startswith(('psycopg','cryptography','pydantic','logto')) for name in sys.modules)"
    subprocess.run([sys.executable, "-S", "-B", "-c", script, str(tmp_path)], cwd=root, check=True)
