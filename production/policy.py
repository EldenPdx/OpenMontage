"""The structured tool boundary; model instructions never grant permissions."""

from pathlib import Path
import re

from production.contracts import ContractViolation, canonical_sha256


def media_configuration_sha256(config_path=None):
    """Freeze effective non-secret gateway URL, codecs, model profiles and limits."""
    from tools._newapi.config import load_settings
    return canonical_sha256(load_settings(config_path).config.model_dump(mode="json"))


class ToolPolicy:
    ALLOWED = frozenset({"video_compose", "video_stitch", "video_trimmer", "audio_probe",
                         "audio_energy", "composition_validator", "newapi_image", "newapi_video"})
    FORBIDDEN_KEYS = frozenset({"command", "argv", "shell", "code", "html", "javascript",
                                "workflow_json", "workflow_path", "composition_path", "entry_point",
                                "provider_params", "headers", "api_key", "base_url", "config_path",
                                "human_approved", "approval_policy", "system_prompt", "resume_job", "job_path"})
    PATH_KEYS = frozenset({"path", "source", "input_path", "output_path", "audio_path", "video_path",
                           "image_path", "reference_image_path", "asset_path", "subtitle_path",
                           "script_path", "narration_transcript_path", "job_path", "clips", "image_paths",
                           "mask_path", "reference_image_paths", "reference_video_paths", "input_paths"})

    def __init__(self, repo_root: Path, projects_dir: Path):
        self.repo_root = Path(repo_root).resolve()
        self.projects_dir = Path(projects_dir).resolve()

    def project_path(self, context, value, *, output=False):
        if not isinstance(value, str) or not value or "\x00" in value or "://" in value:
            raise ContractViolation("A managed project file is required", "forbidden")
        project = self.projects_dir / context.project_id
        if project.resolve() != project or not project.resolve().is_relative_to(self.projects_dir):
            raise ContractViolation("Project directory escaped the managed root", "forbidden")
        candidate = Path(value)
        if not candidate.is_absolute():
            # Canonical artifacts may use repository-relative project paths.
            candidate = self.repo_root / candidate if candidate.parts[0] == "projects" else project / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(project.resolve()):
            raise ContractViolation("File escapes the task project", "forbidden")
        relative = candidate.relative_to(project.resolve())
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*", relative.as_posix()):
            raise ContractViolation("File path must match the managed reference contract", "forbidden")
        if not relative.parts or any(part.startswith(".") for part in relative.parts):
            raise ContractViolation("Private project files are not exposed", "forbidden")
        if output and (relative.parts[0] not in {"assets", "renders"} or candidate.suffix.lower() in {".py", ".js", ".ts", ".mjs", ".html", ".sh", ".json"}):
            raise ContractViolation("Tool output must be a managed media file", "forbidden")
        if not output and relative.parts[0] not in {"assets", "renders", "artifacts"}:
            raise ContractViolation("Only task artifacts and media may be read", "forbidden")
        return candidate

    def instruction_path(self, value):
        if not isinstance(value, str) or not value or Path(value).is_absolute() or any(part in {"..", "."} for part in value.split("/")):
            raise ContractViolation("Invalid instruction path", "forbidden")
        relative = value
        path = (self.repo_root / relative).resolve()
        resolved_relative = path.relative_to(self.repo_root).as_posix() if path.is_relative_to(self.repo_root) else ""
        allowed = resolved_relative == "AGENT_GUIDE.md" or resolved_relative.startswith(("pipeline_defs/", "skills/", ".agents/skills/"))
        if not allowed or not path.is_relative_to(self.repo_root) or path.suffix not in {".md", ".yaml"}:
            raise ContractViolation("Only production instructions may be read", "forbidden")
        if not path.is_file() or path.stat().st_size > 256 * 1024:
            raise ContractViolation("Instruction file is missing or too large", "not_found")
        return path

    def inputs(self, context, inputs):
        assets = {asset["id"]: str(self.project_path(context, asset["path"]))
                  for asset in inputs.get("asset_manifest", {}).get("assets", [])
                  if isinstance(asset, dict) and asset.get("id") and asset.get("path")}
        def visit(value, key=""):
            if key in self.FORBIDDEN_KEYS or key.endswith(("_url", "_urls")) or key == "url":
                raise ContractViolation("This tool parameter is not authorized", "forbidden")
            if key == "font" and (not isinstance(value, str) or not re.fullmatch(r"[\w -]{1,100}", value)):
                raise ContractViolation("Unsafe subtitle font", "forbidden")
            if key in {"primary_color", "outline_color"} and (not isinstance(value, str) or not re.fullmatch(r"(?:&H|#)[A-Fa-f0-9]{6,8}", value)):
                raise ContractViolation("Unsafe subtitle color", "forbidden")
            if key == "codec" and value not in {"libx264", "libx265", "copy"}:
                raise ContractViolation("Encoder is outside the fixed render policy", "forbidden")
            if key == "preset" and value not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}:
                raise ContractViolation("Encoder preset is outside the fixed render policy", "forbidden")
            if isinstance(value, dict):
                return {name: visit(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [visit(item, key) for item in value]
            if key == "source" and isinstance(value, str) and value in assets:
                return assets[value]
            if key in self.PATH_KEYS and isinstance(value, str):
                return str(self.project_path(context, value, output=key == "output_path"))
            return value
        return visit(inputs)
