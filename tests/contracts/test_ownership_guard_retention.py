"""Root-external protection survives canonical-files-only restoration offline."""

from pathlib import Path
from hashlib import sha256
import json
import os
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from production.ownership_guard import MARKER, WITNESS_PREFIX, GuardConflict, has_guard, mark_authenticated


def test_root_and_project_remain_protected_offline_after_both_internal_markers_are_lost(tmp_path):
    root = tmp_path / "projects"
    project = root / "protected-project"
    project.mkdir(parents=True)
    canonical = project / "project.json"
    canonical.write_bytes(b'{"project_id":"protected-project"}\n')
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    mark_authenticated(project, binding=binding, workspace={"kind": "personal", "owner_id": "user-a"})
    (root / MARKER).unlink()
    (project / MARKER).unlink()
    assert has_guard(root)
    assert has_guard(project)
    assert canonical.read_bytes() == b'{"project_id":"protected-project"}\n'
    script = (
        "from production.ownership_guard import has_guard; import sys; "
        "assert all(has_guard(path) for path in sys.argv[1:]); "
        "assert not any(name.startswith(('psycopg','cryptography','pydantic','logto')) for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-S", "-B", "-c", script, str(root), str(project)],
                   cwd=Path(__file__).resolve().parents[2], check=True)


def test_replacing_entire_canonical_root_retains_external_protection(tmp_path):
    root = tmp_path / "projects"
    project = root / "protected-project"
    project.mkdir(parents=True)
    canonical = project / "project.json"
    original = b'{"project_id":"protected-project"}\n'
    canonical.write_bytes(original)
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    mark_authenticated(project, binding=binding, workspace={"kind": "personal", "owner_id": "user-a"})
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    classification = witness.read_bytes()
    shutil.rmtree(root)
    assert has_guard(root)
    project.mkdir(parents=True)
    canonical.write_bytes(original)
    assert has_guard(root)
    assert not (root / MARKER).exists()
    assert witness.read_bytes() == classification
    mark_authenticated(root, binding=binding)
    mark_authenticated(project, binding=binding, workspace={"kind": "personal", "owner_id": "user-a"})
    assert has_guard(project)
    assert canonical.read_bytes() == original


def test_witness_only_repair_preserves_classification_and_rejects_rebinding(tmp_path):
    root = tmp_path / "projects"
    binding = str(uuid4())
    workspace = {"kind": "personal", "owner_id": "user-a"}
    mark_authenticated(root, binding=binding, workspace=workspace)
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    original = witness.read_bytes()
    assert witness.name == WITNESS_PREFIX + sha256(os.fsencode(root.resolve())).hexdigest() + ".json"
    assert witness.stat().st_mode & 0o777 == 0o600
    assert json.loads(original) == {"version": 1, "database_binding": binding, "workspace": workspace}
    (root / MARKER).unlink()
    mark_authenticated(root, binding=binding, workspace=workspace)
    assert (root / MARKER).read_bytes() == original == witness.read_bytes()
    (root / MARKER).unlink()
    for changed in ({"binding": str(uuid4()), "workspace": workspace},
                    {"binding": binding, "workspace": {"kind": "personal", "owner_id": "user-b"}}):
        with pytest.raises(GuardConflict):
            mark_authenticated(root, **changed)
        assert has_guard(root)
        assert not (root / MARKER).exists()
        assert witness.read_bytes() == original


def test_legacy_internal_marker_rejects_new_binding_before_publishing_a_witness(tmp_path):
    root = tmp_path / "projects"
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    marker = root / MARKER
    original = marker.read_bytes()
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    witness.unlink()
    with pytest.raises(GuardConflict):
        mark_authenticated(root, binding=str(uuid4()))
    assert marker.read_bytes() == original
    assert not witness.exists()
    mark_authenticated(root, binding=binding)
    assert witness.read_bytes() == original == marker.read_bytes()


@pytest.mark.parametrize("kind", ["malformed", "unreadable", "symlink", "directory", "public", "oversized", "classification"])
def test_unsafe_external_witness_always_protects_and_refuses_authenticated_activation(tmp_path, kind):
    root = tmp_path / "projects"
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    original = witness.read_bytes()
    (root / MARKER).unlink()
    witness.unlink()
    if kind == "symlink":
        witness.symlink_to(tmp_path / "missing-witness")
    elif kind == "directory":
        witness.mkdir()
    else:
        witness.write_bytes(original)
        witness.chmod(0o600)
        if kind in {"malformed", "unreadable"}:
            witness.write_bytes(b"not-json")
            if kind == "unreadable":
                witness.chmod(0)
        elif kind == "public":
            witness.chmod(0o644)
        elif kind == "oversized":
            witness.write_bytes(b"x" * 4097)
        else:
            witness.write_text(json.dumps({"version": True, "database_binding": binding, "workspace": None}))
    assert has_guard(root)
    with pytest.raises(GuardConflict):
        mark_authenticated(root, binding=binding)
    assert has_guard(root)
    assert not (root / MARKER).exists()


def test_fifo_witness_protects_and_refuses_activation_without_blocking(tmp_path):
    root = tmp_path / "projects"
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    (root / MARKER).unlink()
    witness.unlink()
    os.mkfifo(witness, 0o600)
    assert has_guard(root)
    script = (
        "from production.ownership_guard import GuardConflict, mark_authenticated; import sys\n"
        "try: mark_authenticated(sys.argv[1], binding=sys.argv[2])\n"
        "except GuardConflict: raise SystemExit(0)\n"
        "raise SystemExit('Unsafe witness must refuse authentication')\n"
    )
    subprocess.run([sys.executable, "-S", "-B", "-c", script, str(root), binding],
                   cwd=Path(__file__).resolve().parents[2], check=True, timeout=2)


def test_canonical_alias_uses_the_same_witness_and_other_roots_remain_legacy(tmp_path):
    root = tmp_path / "projects"
    binding = str(uuid4())
    mark_authenticated(root, binding=binding)
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    original = witness.read_bytes()
    (root / MARKER).unlink()
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    assert has_guard(alias)
    mark_authenticated(alias, binding=binding)
    assert witness.read_bytes() == original
    assert len(list(root.parent.glob(WITNESS_PREFIX + "*.json"))) == 1
    assert not has_guard(tmp_path / "unrelated-root")


def test_unwritable_witness_parent_fails_before_creating_an_internal_guard(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    canonical = root / "project.json"
    canonical.write_bytes(b'{"project_id":"legacy"}\n')
    open_file = os.open

    def read_only_parent(path, *args, **kwargs):
        if Path(path).parent == root.parent:
            raise PermissionError("Guard parent is read-only")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", read_only_parent)
    with pytest.raises(GuardConflict):
        mark_authenticated(root, binding=str(uuid4()))
    assert not (root / MARKER).exists()
    assert not list(root.parent.glob(WITNESS_PREFIX + "*.json"))
    assert canonical.read_bytes() == b'{"project_id":"legacy"}\n'


def test_failed_internal_marker_write_retains_external_protection(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    binding = str(uuid4())
    open_file = os.open

    def unwritable_root(path, *args, **kwargs):
        if Path(path).parent == root:
            raise PermissionError("Canonical root is read-only")
        return open_file(path, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(os, "open", unwritable_root)
        with pytest.raises(GuardConflict):
            mark_authenticated(root, binding=binding)
    assert has_guard(root)
    assert not (root / MARKER).exists()
    witness, = root.parent.glob(WITNESS_PREFIX + "*.json")
    before = witness.read_bytes()
    mark_authenticated(root, binding=binding)
    assert (root / MARKER).read_bytes() == before == witness.read_bytes()
