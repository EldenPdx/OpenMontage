"""Observe existing canonical production evidence; never choose creative stages."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess

from production.contracts import ContractViolation, FileReference, RenderResult
from production.policy import ToolPolicy

CODEC_NAMES = {"libx264": "h264", "libx265": "hevc", "h265": "hevc", "libvpx": "vp8",
               "libvpx-vp9": "vp9", "libaom-av1": "av1", "libsvtav1": "av1",
               "libfdk_aac": "aac", "libmp3lame": "mp3", "libopus": "opus", "libvorbis": "vorbis"}


def file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TaskService:
    def __init__(self, projects_dir, *, repo_root=None):
        self.projects_dir = Path(projects_dir).resolve()
        self.policy = ToolPolicy(repo_root or Path(__file__).resolve().parents[1], self.projects_dir)

    def completion(self, context, *, request=None):
        from lib.checkpoint import read_checkpoint
        from lib.pipeline_loader import load_pipeline_readonly
        from schemas.artifacts import validate_artifact

        project = self.projects_dir / context.project_id
        compose_path = project / "checkpoint_compose.json"
        if not compose_path.is_file():
            return None
        try:
            checkpoint = read_checkpoint(self.projects_dir, context.project_id, "compose")
            if checkpoint["status"] != "completed":
                return None
            manifest = load_pipeline_readonly(checkpoint["pipeline_type"])
            for stage in manifest["stages"]:
                current = read_checkpoint(self.projects_dir, context.project_id, stage["name"])
                if not current or current["status"] != "completed" or (stage.get("human_approval_default") and not current.get("human_approved")):
                    raise ContractViolation("Canonical predecessor is incomplete or unapproved", "invalid_artifact")
                if stage["name"] == "compose":
                    break
            report = checkpoint["artifacts"]["render_report"]
            review = checkpoint["artifacts"].get("final_review")
            report_path = self.policy.project_path(context, "artifacts/render_report.json")
            if report_path.is_file() and json.loads(report_path.read_text(encoding="utf-8")) != report:
                raise ContractViolation("Canonical render report changed after its checkpoint", "file_conflict")
            review_path = self.policy.project_path(context, "artifacts/final_review.json")
            if review_path.is_file():
                current_review = json.loads(review_path.read_text(encoding="utf-8"))
                if review is not None and current_review != review:
                    raise ContractViolation("Canonical final review changed after its checkpoint", "file_conflict")
                review = current_review
            validate_artifact("final_review", review)
            if review["status"] != "pass":
                raise ContractViolation("Rendered video did not pass its final review", "invalid_artifact")
            outputs = report["outputs"]
            selected = None
            for output in outputs:
                video = self.policy.project_path(context, output["path"])
                if video.relative_to(project.resolve()).parts[0] != "renders" or not video.is_file() or video.stat().st_size <= 0:
                    raise ContractViolation("Canonical video is absent from managed renders", "invalid_artifact")
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)], capture_output=True, text=True, timeout=30, check=True)
                data = json.loads(probe.stdout)
                if output["format"] != "mp4" or video.suffix.lower() != ".mp4" or "mp4" not in data["format"].get("format_name", "").split(","):
                    raise ContractViolation("Studio delivery requires an actual MP4 matching the canonical report", "invalid_artifact")
                stream = next(item for item in data["streams"] if item["codec_type"] == "video")
                if "codec" in output:
                    declared = output["codec"].strip().lower()
                    if stream.get("codec_name") != CODEC_NAMES.get(declared, declared):
                        raise ContractViolation("Rendered video codec differs from the canonical report", "invalid_artifact")
                if "audio_codec" in output:
                    declared = output["audio_codec"].strip().lower()
                    audio = [item for item in data["streams"] if item["codec_type"] == "audio"]
                    if not audio or any(item.get("codec_name") != CODEC_NAMES.get(declared, declared) for item in audio):
                        raise ContractViolation("Rendered audio codec differs from the canonical report", "invalid_artifact")
                duration = float(data["format"]["duration"])
                width, height = int(stream["width"]), int(stream["height"])
                if duration <= 0 or width <= 0 or height <= 0 or abs(duration - output["duration_seconds"]) > 0.5 or output["resolution"] != f"{width}x{height}":
                    raise ContractViolation("Rendered media differs from the canonical report", "invalid_artifact")
                if selected is None:
                    selected = video, duration, width, height
            video, duration, width, height = selected
            if request is not None:
                requested_width, requested_height = map(int, request.aspect_ratio.split(":"))
                if abs(duration - request.duration_seconds) > 0.5 or abs(width - height * requested_width / requested_height) > 2:
                    raise ContractViolation("Rendered duration or aspect ratio differs from the browser request", "invalid_artifact")
            if self.policy.project_path(context, review["output_path"]) != video:
                raise ContractViolation("Final review refers to another video", "invalid_artifact")
            if not report_path.is_file():
                report_path = compose_path
            revisions_file = project / ".studio-revisions.json"
            revisions = json.loads(revisions_file.read_text()) if revisions_file.is_file() else {}

            def reference(path):
                relative = path.relative_to(project.resolve()).as_posix()
                return FileReference(path=relative, revision=revisions.get(relative, 1), sha256=file_sha256(path))

            return RenderResult(render_report=reference(report_path), video=reference(video), bytes=video.stat().st_size,
                                duration_seconds=duration, width=width, height=height, verified=True)
        except ContractViolation:
            raise
        except Exception:
            raise ContractViolation("Canonical render evidence failed media verification", "invalid_artifact") from None
