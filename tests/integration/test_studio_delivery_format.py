import json
import subprocess

import pytest

from production.contracts import ContractViolation
from production.task_service import TaskService
from tests.integration.test_studio_repository import repository, repository_factory
from tests.integration.test_studio_worker import completed_project


def update_report(project, **fields):
    path = project / "artifacts/render_report.json"
    report = json.loads(path.read_text())
    report["outputs"][0].update(fields)
    path.write_text(json.dumps(report))
    checkpoint_path = project / "checkpoint_compose.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["artifacts"]["render_report"] = report
    checkpoint_path.write_text(json.dumps(checkpoint))


def test_mp4_delivery_rejects_an_actual_matroska_file_with_mp4_name(repository, tmp_path):
    context, project = completed_project(repository, tmp_path)
    service = TaskService(tmp_path / "projects")
    assert service.completion(context).verified
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1:r=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "matroska", str(project / "renders/final.mp4")],
                   check=True, timeout=20)
    with pytest.raises(ContractViolation, match="MP4") as failure:
        service.completion(context)
    assert failure.value.code == "invalid_artifact"


def test_delivery_rejects_a_declared_video_codec_that_does_not_match(repository, tmp_path):
    context, project = completed_project(repository, tmp_path)
    update_report(project, codec="libx265")
    with pytest.raises(ContractViolation, match="codec"):
        TaskService(tmp_path / "projects").completion(context)


def test_delivery_requires_the_audio_stream_declared_in_the_report(repository, tmp_path):
    context, project = completed_project(repository, tmp_path)
    update_report(project, audio_codec="aac")
    with pytest.raises(ContractViolation, match="audio codec"):
        TaskService(tmp_path / "projects").completion(context)


@pytest.mark.parametrize("codec", ["h264", "libx264"])
def test_mp4_with_declared_h264_alias_and_actual_aac_is_verified(repository, tmp_path, codec):
    context, project = completed_project(repository, tmp_path)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1:r=25",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(project / "renders/final.mp4")],
                   check=True, timeout=20)
    update_report(project, codec=codec, audio_codec="aac")
    result = TaskService(tmp_path / "projects").completion(context)
    assert result.verified and (result.width, result.height, result.duration_seconds) == (320, 180, 1)


@pytest.mark.parametrize("change", ["extension", "format"])
def test_studio_delivery_rejects_non_mp4_reference_or_declaration(repository, tmp_path, change):
    context, project = completed_project(repository, tmp_path)
    if change == "extension":
        (project / "renders/final.mp4").rename(project / "renders/final.mov")
        update_report(project, path="renders/final.mov")
    else:
        update_report(project, format="webm")
    with pytest.raises(ContractViolation, match="MP4"):
        TaskService(tmp_path / "projects").completion(context)


def test_every_reported_output_must_have_the_declared_codec(repository, tmp_path):
    context, project = completed_project(repository, tmp_path)
    path = project / "artifacts/render_report.json"
    report = json.loads(path.read_text())
    report["outputs"].append({**report["outputs"][0], "codec": "libx265"})
    path.write_text(json.dumps(report))
    checkpoint_path = project / "checkpoint_compose.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["artifacts"]["render_report"] = report
    checkpoint_path.write_text(json.dumps(checkpoint))
    with pytest.raises(ContractViolation, match="codec"):
        TaskService(tmp_path / "projects").completion(context)
