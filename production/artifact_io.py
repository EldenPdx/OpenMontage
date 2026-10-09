"""Schema-valid atomic file writes, preceded by durable PostgreSQL intents."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from uuid import uuid4

from production.contracts import ContractViolation, FileReference, FileWriteIntent
from schemas.artifacts import ARTIFACT_NAMES, validate_artifact


def file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, repository, projects_dir: Path):
        self.repository = repository
        self.projects_dir = Path(projects_dir).resolve()
        self.lock = threading.RLock()

    def project(self, context):
        expected = self.projects_dir / context.project_id
        path = expected.resolve()
        if path != expected or not path.is_relative_to(self.projects_dir):
            raise ContractViolation("Project escapes managed directory", "forbidden")
        return path

    def initialize(self, context, *, title, pipeline_type):
        from lib.checkpoint import init_project, studio_checkpoint_authority
        self.repository.assert_fence(context)
        project = self.project(context)
        with studio_checkpoint_authority(lambda: self.repository.assert_fence(context)):
            init_project(context.project_id, title=title, pipeline_type=pipeline_type, pipeline_dir=self.projects_dir)
        owner = project / ".studio-owner.json"
        if owner.exists() and json.loads(owner.read_text()).get("task_id") != context.task_id:
            raise ContractViolation("Project already belongs to another task", "file_conflict")
        owner.write_text(json.dumps({"task_id": context.task_id, "run_id": context.run_id}))
        owner.chmod(0o600)
        return project

    def reference(self, context, relative):
        project = self.project(context)
        path = (project / relative).resolve()
        if not path.is_relative_to(project) or not path.is_file():
            raise ContractViolation("Artifact not found", "not_found")
        revisions = project / ".studio-revisions.json"
        revision = json.loads(revisions.read_text()).get(relative, 1) if revisions.exists() else 1
        return FileReference(path=relative, revision=revision, sha256=file_sha256(path))

    def write_json(self, context, relative, value):
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > 4 * 1024 * 1024:
            raise ContractViolation("Artifact is too large", "invalid_artifact")
        return self.write_file(context, relative, encoded=encoded)

    def write_file(self, context, relative, *, encoded=None, source=None):
        with self.lock:
            self.repository.assert_fence(context)
            project = self.project(context)
            path = project / relative
            if not path.resolve().is_relative_to(project) or any(part in {"..", "."} for part in Path(relative).parts):
                raise ContractViolation("Artifact escaped the managed project", "forbidden")
            previous = file_sha256(path) if path.exists() else None
            revisions_path = project / ".studio-revisions.json"
            revisions = json.loads(revisions_path.read_text()) if revisions_path.exists() else {}
            revision = revisions.get(relative, 0) + 1
            digest = sha256(encoded).hexdigest() if encoded is not None else file_sha256(source)
            reference = FileReference(path=relative, revision=revision, sha256=digest)
            intent = FileWriteIntent(intent_id="file-" + uuid4().hex, task_id=context.task_id, run_id=context.run_id,
                                     fence=context.fence, target=reference, previous_sha256=previous)
            self.repository.record_file_intent(intent)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".studio-write-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    if encoded is not None:
                        stream.write(encoded)
                    else:
                        with Path(source).open("rb") as media:
                            shutil.copyfileobj(media, stream, 1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.repository.assert_fence(context)
                if (file_sha256(path) if path.exists() else None) != previous:
                    self.repository.record_file_intent(intent.model_copy(update={"status": "conflict"}))
                    raise ContractViolation("Artifact changed during write", "file_conflict")
                if relative.startswith("checkpoint_"):
                    from lib.checkpoint import _archive_superseded_checkpoint
                    _archive_superseded_checkpoint(path, relative.removeprefix("checkpoint_").removesuffix(".json"))
                os.replace(temporary, path)
                revisions[relative] = revision
                revision_temp = revisions_path.with_suffix(".tmp")
                revision_temp.write_text(json.dumps(revisions), encoding="utf-8")
                os.replace(revision_temp, revisions_path)
                applied = self.repository.record_file_intent(intent.model_copy(update={"status": "applied"}))
                self.repository.record_file_intent(applied.model_copy(update={"status": "reconciled"}))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return reference

    def artifact(self, context, name, value):
        if name not in ARTIFACT_NAMES:
            raise ContractViolation("Unknown canonical artifact", "invalid_artifact")
        try:
            validate_artifact(name, value)
        except Exception:
            raise ContractViolation("Artifact failed its canonical schema", "invalid_artifact") from None
        return self.write_json(context, f"artifacts/{name}.json", value)

    @contextmanager
    def checkpoint_writer(self, context):
        from lib.checkpoint import studio_checkpoint_authority
        with studio_checkpoint_authority(lambda: self.repository.assert_fence(context)):
            yield lambda path, value: self.write_json(context, path.name, value)
