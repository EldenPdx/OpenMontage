"""Public Studio authorization with real Logto, browser cookies and PostgreSQL."""

from contextlib import ExitStack
import httpx
from hashlib import sha256
import json
import os
import socket
import subprocess
import time

import pytest

from tests.fixtures.auth28 import ORIGIN, authority_outage, complete_production, production_studio, studio_browser, studio_identity


def assert_no_remaining_sse_frames(lines):
    remaining = []
    try:
        remaining.extend(lines)
    except httpx.RemoteProtocolError:
        pass  # Revocation may close an already-started chunked HTTP transfer.
    assert remaining == []


def test_signed_in_studio_config_has_only_safe_profiles_and_current_context(studio_identity):
    studio = studio_identity
    anonymous = studio.browser.new_context()
    assert anonymous.request.get(ORIGIN + "/api/studio/config").status == 401
    owner = studio.sign_in()
    me = studio.me(owner)
    response = owner.request.get(ORIGIN + "/api/studio/config")
    assert response.status == 200
    config = response.json()
    assert config["ready"] is True
    assert config["csrf_token"] == me["csrf_token"]
    assert response.headers["x-auth-context"] == me["context_id"]
    assert response.headers["cache-control"] == "private, no-store"
    assert config["profiles"][0]["profile_id"] == "local"
    for private in ("private-auth28-model-credential", "app_secret", "issuer", "session_ref", "base_url"):
        assert private not in response.text()


def test_personal_same_key_creates_separate_owned_tasks_and_scoped_reads(studio_identity):
    studio = studio_identity
    first, second = studio.sign_in("u1"), studio.sign_in("u2")
    one = studio.create(first)
    two = studio.create(second)
    assert one["task_id"] != two["task_id"]
    assert studio.create(first) == one
    counts = studio.accepted_production_counts()
    changed = first.request.post(ORIGIN + "/api/studio/tasks", headers=studio.headers(first, "studio-auth28-create"),
        data={"brief": "Changed accepted request", "profile_id": "local", "duration_seconds": 2})
    assert changed.status == 409
    assert studio.accepted_production_counts() == counts
    for context, own, foreign in ((first, one, two), (second, two, one)):
        user_id = studio.me(context)["user"]["user_id"]
        workspace = {"kind": "personal", "owner_id": user_id}
        assert own["ownership"] == {"workspace": workspace, "creator": {"user_id": user_id}}
        assert own["actor"] == {"user_id": user_id, "workspace": workspace}
        listed = context.request.get(ORIGIN + "/api/studio/tasks")
        assert listed.status == 200
        assert [item["task_id"] for item in listed.json()] == [own["task_id"]]
        assert [item["task_id"] for item in context.request.get(ORIGIN + "/api/studio/tasks?limit=1").json()] == [own["task_id"]]
        detail = context.request.get(ORIGIN + "/api/studio/tasks/" + own["task_id"])
        assert detail.status == 200
        for field in ('"issuer"', '"sub"', '"session_ref"', "private-auth28-model-credential"):
            assert field not in detail.text()
        for path in ("/api/studio/tasks/" + foreign["task_id"], "/api/studio/tasks?after=" + foreign["task_id"]):
            denied = context.request.get(ORIGIN + path)
            assert denied.status == 404
            assert denied.json()["error"]["code"] == "not_found"
            assert foreign["task_id"] not in denied.text()
        assert context.request.get(ORIGIN + "/api/studio/tasks?after=" + own["task_id"]).json() == []


def test_organization_viewer_denials_precede_state_and_editor_cancel_records_actor(studio_identity):
    studio = studio_identity
    admin, foreign_admin, viewer, editor = (studio.sign_in(alias) for alias in ("u1", "u2", "viewer", "cross"))
    personal = studio.create(admin)
    studio.switch(admin, "A")
    own = studio.create(admin)
    assert own["task_id"] != personal["task_id"]
    studio.switch(foreign_admin, "B")
    foreign = studio.create(foreign_admin)
    studio.switch(viewer, "A")
    studio.switch(editor, "A")
    assert viewer.request.get(ORIGIN + "/api/studio/config").status == 200
    assert [task["task_id"] for task in viewer.request.get(ORIGIN + "/api/studio/tasks").json()] == [own["task_id"]]
    denied_create = viewer.request.post(ORIGIN + "/api/studio/tasks", headers=studio.headers(viewer, "denied-create-0001"), data={})
    assert denied_create.status == 403
    before = viewer.request.get(ORIGIN + "/api/studio/tasks").json()
    for suffix in ("cancel", "resume", "approvals/unknown-gate/decision"):
        for target, status in ((own["task_id"], 403), (foreign["task_id"], 404), ("task-absent", 404), (personal["task_id"], 404)):
            denied = viewer.request.post(f"{ORIGIN}/api/studio/tasks/{target}/{suffix}",
                headers=studio.headers(viewer, "denied-command-0001"), data={"expected_version": 99})
            assert denied.status == status, denied.text()
            assert denied.json()["error"]["code"] == ("forbidden" if status == 403 else "not_found")
            assert target not in denied.text()
    assert viewer.request.get(ORIGIN + "/api/studio/tasks").json() == before
    editor_task = studio.create(editor)
    assert editor_task["task_id"] != own["task_id"]
    accepted = editor.request.post(f"{ORIGIN}/api/studio/tasks/{own['task_id']}/cancel",
        headers=studio.headers(editor, "editor-cancel-0001"), data={"expected_version": own["version"]})
    assert accepted.status == 202, accepted.text()
    current = admin.request.get(ORIGIN + "/api/studio/tasks/" + own["task_id"]).json()
    assert current["state"] == "cancel_requested"
    assert current["ownership"] == own["ownership"]
    assert current["actor"]["user_id"] == studio.me(editor)["user"]["user_id"]
    repeat = editor.request.post(f"{ORIGIN}/api/studio/tasks/{own['task_id']}/cancel",
        headers=studio.headers(editor, "editor-cancel-0001"), data={"expected_version": own["version"]})
    assert repeat.json()["version"] == current["version"]
    studio.switch(editor, "B")
    foreign_replay = editor.request.post(f"{ORIGIN}/api/studio/tasks/{own['task_id']}/cancel",
        headers=studio.headers(editor, "editor-cancel-0001"), data={"expected_version": own["version"]})
    assert foreign_replay.status == 404


def test_owned_authenticated_version_conflict_retains_existing_http_status(studio_identity):
    studio = studio_identity
    owner = studio.sign_in()
    task = studio.create(owner)
    stale = owner.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/cancel",
        headers=studio.headers(owner, "stale-version-0001"), data={"expected_version": 99})
    assert stale.json()["error"]["code"] == "version_conflict"
    assert stale.status == 409
    assert owner.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()["version"] == task["version"]


def test_real_pi_ordinary_approval_is_owned_and_captures_current_editor(monkeypatch):
    with production_studio(monkeypatch) as studio:
        creator, editor = studio.sign_in("u1"), studio.sign_in("cross")
        studio.switch(creator, "A")
        studio.switch(editor, "A")
        created = studio.create(creator)
        studio.run_worker()
        task = editor.request.get(ORIGIN + "/api/studio/tasks/" + created["task_id"]).json()
        assert task["state"] == "awaiting_approval", task.get("error")
        assert task["approval"]["stage"] == "proposal"
        binding = task["approval"]["binding"]
        path = f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{binding['gate_id']}/decision"
        accepted = editor.request.post(path, headers=studio.headers(editor, "editor-approve-0001"),
            data={"binding": binding, "expected_version": task["version"], "decision": "approve"})
        assert accepted.status == 202, accepted.text()
        assert accepted.json()["ownership"] == created["ownership"]
        assert accepted.json()["actor"]["user_id"] == studio.me(editor)["user"]["user_id"]
        assert accepted.json()["approval"]["status"] == "approved"
        assert list(studio.media_submissions) == []
        duplicate = editor.request.post(path, headers=studio.headers(editor, "editor-approve-0001"),
            data={"binding": binding, "expected_version": task["version"], "decision": "approve"})
        assert duplicate.json() == accepted.json()
        before = studio.accepted_production_counts()
        other_actor = creator.request.post(path, headers=studio.headers(creator, "editor-approve-0001"),
            data={"binding": binding, "expected_version": task["version"], "decision": "approve"})
        assert other_actor.status == 409
        assert studio.accepted_production_counts() == before
        assert creator.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()["actor"] == accepted.json()["actor"]


def test_task_sse_checks_current_session_before_sending_a_queued_frame(studio_identity):
    studio = studio_identity
    reader, operator = studio.sign_in(), studio.sign_in()
    task = studio.create(operator)
    cookies = {cookie["name"]: cookie["value"] for cookie in reader.cookies(ORIGIN)}
    with httpx.Client(trust_env=False, cookies=cookies, timeout=35) as client:
        with client.stream("GET", f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events") as response:
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            lines = response.iter_lines()
            initial = []
            for line in lines:
                if not line:
                    break
                initial.append(line)
            assert any('"state":"queued"' in line for line in initial)
            logout = reader.request.post(ORIGIN + "/api/auth/logout", headers=studio.headers(reader, "logout-sse-0001"), data={})
            assert logout.status == 200
            start = time.monotonic()
            cancelled = operator.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/cancel",
                headers=studio.headers(operator, "cancel-sse-0001"), data={"expected_version": task["version"]})
            assert cancelled.status == 202
            assert next((line for line in lines if line.startswith("data:")), None) is None
            assert time.monotonic() - start < 30
        assert client.get(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events").status_code == 401


def test_rejected_real_pi_gate_resumes_only_with_current_owned_actor(monkeypatch):
    with production_studio(monkeypatch) as studio:
        owner = studio.sign_in()
        created = studio.create(owner)
        studio.run_worker()
        task = owner.request.get(ORIGIN + "/api/studio/tasks/" + created["task_id"]).json()
        assert task["state"] == "awaiting_approval", task.get("error")
        binding = task["approval"]["binding"]
        rejected = owner.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{binding['gate_id']}/decision",
            headers=studio.headers(owner, "reject-plan-0001"),
            data={"binding": binding, "expected_version": task["version"], "decision": "reject"})
        assert rejected.status == 202
        assert rejected.json()["state"] == "blocked"
        assert rejected.json()["approval"]["status"] == "rejected"
        resumed = owner.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/resume",
            headers=studio.headers(owner, "resume-rejected-0001"), data={"expected_version": rejected.json()["version"]})
        assert resumed.status == 202, resumed.text()
        assert resumed.json()["state"] == "queued"
        assert resumed.json()["actor"] == created["actor"]
        assert resumed.json()["cost"] == rejected.json()["cost"]
        repeat = owner.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/resume",
            headers=studio.headers(owner, "resume-rejected-0001"), data={"expected_version": rejected.json()["version"]})
        assert repeat.json()["version"] == resumed.json()["version"]
        assert list(studio.media_submissions) == []


@pytest.mark.parametrize("workspace", ["personal", "organization"])
def test_real_owned_video_preserves_accepted_actor_after_logout_and_protects_result(monkeypatch, workspace):
    with production_studio(monkeypatch, media_http=True) as studio:
        creator = studio.sign_in()
        if workspace == "organization":
            studio.switch(creator, "A")
        created = studio.create(creator)
        creator_id = studio.me(creator)["user"]["user_id"]
        logout = creator.request.post(ORIGIN + "/api/auth/logout", headers=studio.headers(creator, "logout-accepted-0001"), data={})
        assert logout.status == 200
        studio.run_worker()
        approver = studio.sign_in("cross" if workspace == "organization" else "u1")
        if workspace == "organization":
            studio.switch(approver, "A")
        original_actor = studio.committed_run_actor(created["task_id"])
        assert original_actor["identity"]["user_id"] == creator_id
        stages, revised = [], False
        for turn in range(8):
            response = approver.request.get(ORIGIN + "/api/studio/tasks/" + created["task_id"])
            assert response.status == 200
            task = response.json()
            if task["state"] == "succeeded":
                break
            assert task["state"] == "awaiting_approval", task.get("error")
            gate = task["approval"]
            stages.append(gate["stage"])
            binding = gate["binding"]
            path = f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{binding['gate_id']}/decision"
            invalid = approver.request.post(path, headers=studio.headers(approver, f"invalid-binding-{turn:04d}"),
                data={"binding": {**binding, "artifact_sha256": "0" * 64}, "expected_version": task["version"], "decision": "approve"})
            assert invalid.status == 409
            assert invalid.json()["error"]["code"] == "approval_conflict"
            decision = "revise" if gate["stage"] == "script" and not revised else "approve"
            revised = revised or decision == "revise"
            accepted = approver.request.post(path, headers=studio.headers(approver, f"pipeline-decision-{turn:04d}"),
                data={"binding": binding, "expected_version": task["version"], "decision": decision,
                      "comment": "Simplify the script" if decision == "revise" else ""})
            assert accepted.status == 202, accepted.text()
            assert accepted.json()["ownership"] == created["ownership"]
            if turn == 0:
                assert studio.committed_run_actor(task["task_id"]) == original_actor
            studio.run_worker()
        else:
            pytest.fail("Real owned Pi production did not reach a verified video")
        assert stages == ["proposal", "script", "script", "scene_plan", "assets"]
        assert task["result"]["verified"] is True
        assert studio.media_requests == [("newapi_image", "Images2.5-Flare"), ("newapi_video", "dreamina-seedance-2-5-260628")]
        result_url = ORIGIN + task["result"]["preview_url"]
        downloaded = approver.request.get(ORIGIN + task["result"]["download_url"])
        assert downloaded.status == 200
        video = downloaded.body()
        assert sha256(video).hexdigest() == task["result"]["video"]["sha256"]
        path = studio.repository.projects_dir / task["project_id"] / task["result"]["video"]["path"]
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=10)
        metadata = json.loads(probe.stdout)
        assert float(metadata["format"]["duration"]) == pytest.approx(2, abs=0.1)
        assert any(stream["codec_type"] == "video" and stream["width"] == 1280 for stream in metadata["streams"])
        assert approver.request.get(result_url, headers={"Range": "bytes=0-31"}).body() == video[:32]
        conditional = approver.request.get(result_url, headers={"If-None-Match": downloaded.headers["etag"]})
        assert conditional.status == 304
        foreign = studio.sign_in("u2")
        for headers in ({}, {"Range": "bytes=0-31"}, {"If-None-Match": downloaded.headers["etag"]}, {"Range": "bytes=999999999-"}):
            denied = foreign.request.get(result_url, headers=headers)
            assert denied.status == 404
            assert "etag" not in denied.headers and "content-range" not in denied.headers
            assert task["task_id"] not in denied.text()
        playback = approver.new_page()
        playback.goto(ORIGIN + "/en")
        playback.evaluate("url => {const video=document.createElement('video');video.src=url;video.muted=true;video.controls=true;document.body.replaceChildren(video);}", result_url)
        playback.locator("video").evaluate("video => video.play()")
        playback.wait_for_function("document.querySelector('video').currentTime > 0.3")
        assert playback.locator("video").evaluate("video => video.videoWidth") == 1280
        studio.record_evidence(workspace + "-production", {
            "task_id": task["task_id"], "ownership": task["ownership"], "latest_actor": task["actor"],
            "original_run_actor": {"user_id": original_actor["identity"]["user_id"], "workspace": original_actor["workspace"]},
            "final_run_actor_user_id": studio.committed_run_actor(task["task_id"])["identity"]["user_id"],
            "stages": stages, "result": task["result"], "download_sha256": sha256(video).hexdigest(),
            "ffprobe": metadata, "playhead_seconds": playback.locator("video").evaluate("video => video.currentTime"),
            "model_http_requests": len(studio.model_requests), "media_http_requests": studio.media_requests})


def test_owned_worker_refuses_unsafe_protection_before_initializing_project(monkeypatch):
    with production_studio(monkeypatch) as studio:
        owner = studio.sign_in()
        task = studio.create(owner)
        project = studio.repository.projects_dir / task["project_id"]
        (project / ".openmontage-authenticated").write_text("malformed protection")
        studio.run_worker()
        current = owner.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()
        assert current["state"] == "recovery_required"
        assert current["error"]["code"] == "file_conflict"
        assert not (project / "project.json").exists()
        assert studio.model_requests == []
        assert list(studio.media_submissions) == []


@pytest.mark.parametrize("revocation", ["logout", "role", "member", "account", "outage"])
def test_long_verified_result_stops_before_remaining_bytes_after_logout(monkeypatch, revocation):
    with production_studio(monkeypatch, media_http=True, slow_results=True) as studio:
        owner = studio.sign_in()
        if revocation != "logout":
            assert studio.fixture.path.name == "fixture-auth28.json"
            studio.switch(owner, "A")
        task = complete_production(studio, owner)
        reader = owner if revocation == "logout" else studio.sign_in("viewer")
        if revocation != "logout":
            studio.switch(reader, "A")
        headers = studio.headers(reader, "logout-download-0001")
        cookies = {cookie["name"]: cookie["value"] for cookie in reader.cookies(ORIGIN)}
        roles = studio.fixture.organization_roles("viewer", "A")
        with httpx.Client(trust_env=False, cookies=cookies, timeout=35) as client:
            with client.stream("GET", ORIGIN + task["result"]["download_url"]) as response:
                assert response.status_code == 200
                chunks = response.iter_bytes()
                first = next(chunks)
                assert first
                try:
                    with ExitStack() as controls:
                        if revocation == "logout":
                            assert reader.request.post(ORIGIN + "/api/auth/logout", headers=headers, data={}).status == 200
                        elif revocation == "role":
                            studio.fixture.set_roles("viewer", "A", [])
                        elif revocation == "member":
                            studio.fixture.remove_member("viewer", "A")
                        elif revocation == "account":
                            studio.fixture.suspend("viewer")
                        else:
                            controls.enter_context(authority_outage(studio))
                        started = time.monotonic()
                        received = len(first)
                        try:
                            received += sum(len(chunk) for chunk in chunks)
                        except httpx.RemoteProtocolError:
                            pass  # A fixed Content-Length transfer must close on revocation.
                        assert time.monotonic() - started < 30
                        assert received < task["result"]["bytes"]
                        denied = client.get(ORIGIN + task["result"]["preview_url"], headers={"Range": "bytes=0-31"})
                        assert denied.status_code == {"logout": 401, "role": 403, "member": 404, "account": 401, "outage": 503}[revocation]
                        assert "etag" not in denied.headers and "content-range" not in denied.headers
                finally:
                    if revocation == "role":
                        studio.fixture.set_roles("viewer", "A", roles)
                    elif revocation == "member":
                        studio.fixture.add_member("viewer", "A", roles)
                    elif revocation == "account":
                        studio.fixture.suspend("viewer", False)


def test_spoofed_identity_and_pending_old_context_do_not_accept_production(studio_identity):
    studio = studio_identity
    owner = studio.sign_in()
    created = studio.create(owner)
    counts = studio.accepted_production_counts()
    body = {"brief": "A private lighthouse", "profile_id": "local"}
    for field in ("actor", "creator", "owner", "ownership", "workspace", "organization_id", "issuer", "sub"):
        rejected = owner.request.post(ORIGIN + "/api/studio/tasks", headers=studio.headers(owner, "spoofed-create-0001"),
            data={**body, field: "spoofed-private-sentinel"})
        assert rejected.status == 422
        assert "spoofed-private-sentinel" not in rejected.text()
    invalid_csrf = owner.request.post(ORIGIN + "/api/studio/tasks", headers={**studio.headers(owner, "bad-csrf-0001"),
        "X-CSRF-Token": "private-invalid-csrf"}, data=body)
    assert invalid_csrf.status == 403
    old_headers = studio.headers(owner, "old-context-0001")
    studio.switch(owner, "A")
    pending = owner.request.post(ORIGIN + "/api/studio/tasks", headers=old_headers, data=body)
    assert pending.status == 409
    assert pending.json()["error"]["code"] == "auth_context_conflict"
    old_cancel = owner.request.post(f"{ORIGIN}/api/studio/tasks/{created['task_id']}/cancel", headers=old_headers,
        data={"expected_version": created["version"]})
    assert old_cancel.status == 409
    assert studio.accepted_production_counts() == counts


@pytest.mark.parametrize("revocation", ["role", "member", "account"])
def test_real_membership_revocation_closes_idle_sse_and_denies_all_entry_points(studio_identity, revocation):
    studio = studio_identity
    assert studio.fixture.path.name == "fixture-auth28.json", "Revocation needs the dedicated Issue #28 identity dataset"
    admin, viewer = studio.sign_in(), studio.sign_in("viewer")
    studio.switch(admin, "A")
    task = studio.create(admin)
    studio.switch(viewer, "A")
    headers = studio.headers(viewer, "revoked-write-0001")
    cookies = {cookie["name"]: cookie["value"] for cookie in viewer.cookies(ORIGIN)}
    roles = studio.fixture.organization_roles("viewer", "A")
    with httpx.Client(trust_env=False, cookies=cookies, timeout=35) as client:
        with client.stream("GET", f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events") as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            for line in lines:
                if not line:
                    break
            try:
                if revocation == "role":
                    studio.fixture.set_roles("viewer", "A", [])
                elif revocation == "member":
                    studio.fixture.remove_member("viewer", "A")
                else:
                    studio.fixture.suspend("viewer")
                started = time.monotonic()
                status = {"role": 403, "member": 404, "account": 401}[revocation]
                counts = studio.accepted_production_counts()
                for suffix in ("/api/studio/config", "/api/studio/tasks", f"/api/studio/tasks/{task['task_id']}",
                               f"/api/studio/tasks/{task['task_id']}/result", f"/api/studio/tasks/{task['task_id']}/events"):
                    denied = client.get(ORIGIN + suffix, headers={"Range": "bytes=0-31", "If-None-Match": '"prior"'})
                    assert denied.status_code == status, denied.text
                    assert task["task_id"] not in denied.text
                    assert "etag" not in denied.headers and "content-range" not in denied.headers
                for suffix in ("", f"/{task['task_id']}/cancel", f"/{task['task_id']}/resume", f"/{task['task_id']}/approvals/unknown/decision"):
                    denied = client.post(ORIGIN + "/api/studio/tasks" + suffix, headers=headers, json={"expected_version": 99})
                    assert denied.status_code == status
                assert studio.accepted_production_counts() == counts
                assert_no_remaining_sse_frames(lines)
                assert time.monotonic() - started < 30
            finally:
                if revocation == "role":
                    studio.fixture.set_roles("viewer", "A", roles)
                elif revocation == "member":
                    studio.fixture.add_member("viewer", "A", roles)
                else:
                    studio.fixture.suspend("viewer", False)
            if revocation == "account":
                assert client.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status_code == 401
                fresh = studio.sign_in("viewer")
                studio.switch(fresh, "A")
                assert fresh.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status == 200
            else:
                assert client.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status_code == 200


def test_unquoted_model_cost_gate_retains_exact_authorization_and_unknown_fee_hold(monkeypatch):
    with production_studio(monkeypatch, unquoted_model=True) as studio:
        admin, editor, viewer = (studio.sign_in(alias) for alias in ("u1", "cross", "viewer"))
        for context in (admin, editor, viewer):
            studio.switch(context, "A")
        created = studio.create(admin, budget_usd_micros=500_000)
        studio.run_worker()
        task = editor.request.get(ORIGIN + "/api/studio/tasks/" + created["task_id"]).json()
        assert task["state"] == "awaiting_approval" and task["approval"]["stage"] == "model_cost"
        assert studio.model_requests == []
        gate = task["approval"]
        assert gate["scope"]["unknown_price"] is True
        assert gate["scope"]["authorized_limit_usd_micros"] == 500_000
        path = f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{gate['binding']['gate_id']}/decision"
        body = {"binding": gate["binding"], "expected_version": task["version"], "decision": "approve"}
        before = studio.accepted_production_counts()
        denied = viewer.request.post(path, headers=studio.headers(viewer, "viewer-cost-0001"), data=body)
        assert denied.status == 403
        invalid = editor.request.post(path, headers=studio.headers(editor, "invalid-cost-0001"),
            data={**body, "binding": {**gate["binding"], "scope_sha256": "0" * 64}})
        assert invalid.status == 409
        assert studio.accepted_production_counts() == before
        accepted = editor.request.post(path, headers=studio.headers(editor, "approve-cost-0001"), data=body)
        assert accepted.status == 202
        assert accepted.json()["actor"]["user_id"] == studio.me(editor)["user"]["user_id"]
        studio.run_worker()
        current = editor.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()
        assert len(studio.model_requests) == 1
        assert current["state"] == "blocked" and current["error"]["code"] == "budget_exceeded"
        assert current["cost"]["reserved_usd_micros"] == 500_000
        assert current["cost"]["spent_usd_micros"] == 0 and current["cost"]["price_status"] == "unquoted"
        assert list(studio.media_submissions) == []


def test_quoted_media_cost_gate_rejects_viewer_and_preserves_exact_revision(monkeypatch):
    with production_studio(monkeypatch, media_http=True, media_quote=True) as studio:
        admin, editor, viewer, foreign = (studio.sign_in(alias) for alias in ("u1", "cross", "viewer", "u2"))
        for context in (admin, editor, viewer):
            studio.switch(context, "A")
        studio.switch(foreign, "B")
        task = studio.create(admin)
        for turn in range(6):
            studio.run_worker()
            task = editor.request.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).json()
            assert task["state"] == "awaiting_approval", task.get("error")
            gate = task["approval"]
            if gate["stage"] == "media_cost":
                break
            approved = editor.request.post(f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{gate['binding']['gate_id']}/decision",
                headers=studio.headers(editor, f"media-plan-{turn:04d}"),
                data={"binding": gate["binding"], "expected_version": task["version"], "decision": "approve"})
            assert approved.status == 202
        else:
            pytest.fail("Real Pi did not expose its exact media quote")
        assert studio.media_requests == []
        assert gate["scope"]["unknown_price"] is False
        assert gate["scope"]["authorized_limit_usd_micros"] == 1_000_000
        path = f"{ORIGIN}/api/studio/tasks/{task['task_id']}/approvals/{gate['binding']['gate_id']}/decision"
        body = {"binding": gate["binding"], "expected_version": task["version"], "decision": "revise", "comment": "Change this exact media request"}
        before = studio.accepted_production_counts()
        for context, status in ((viewer, 403), (foreign, 404)):
            denied = context.request.post(path, headers=studio.headers(context, "media-denied-0001"), data=body)
            assert denied.status == status
        stale = editor.request.post(path, headers=studio.headers(editor, "media-stale-0001"),
            data={**body, "binding": {**gate["binding"], "checkpoint_sha256": "0" * 64}})
        assert stale.status == 409
        assert studio.accepted_production_counts() == before
        revised = editor.request.post(path, headers=studio.headers(editor, "media-revise-0001"), data=body)
        assert revised.status == 202
        assert revised.json()["approval"]["status"] == "revised"
        assert revised.json()["actor"]["user_id"] == studio.me(editor)["user"]["user_id"]
        assert revised.json()["cost"] == task["cost"]
        assert studio.media_requests == []


def test_authority_http_outage_closes_idle_sse_and_rejects_replay_without_business_effects(studio_identity):
    studio = studio_identity
    owner = studio.sign_in()
    task = studio.create(owner)
    headers = studio.headers(owner, "outage-command-0001")
    cookies = {cookie["name"]: cookie["value"] for cookie in owner.cookies(ORIGIN)}
    with httpx.Client(trust_env=False, cookies=cookies, timeout=35) as client:
        with client.stream("GET", f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events") as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            for line in lines:
                if not line:
                    break
            before = studio.accepted_production_counts()
            with authority_outage(studio):
                started = time.monotonic()
                for suffix in ("/api/studio/config", "/api/studio/tasks", f"/api/studio/tasks/{task['task_id']}",
                               f"/api/studio/tasks/{task['task_id']}/result", f"/api/studio/tasks/{task['task_id']}/events"):
                    denied = client.get(ORIGIN + suffix, headers={"Range": "bytes=0-31", "If-None-Match": '"prior"'})
                    assert denied.status_code == 503
                    assert denied.json()["error"]["code"] == "identity_unavailable"
                    assert "etag" not in denied.headers and task["task_id"] not in denied.text
                replay = client.post(ORIGIN + "/api/studio/tasks", headers={**headers, "Idempotency-Key": "studio-auth28-create"},
                    json={"brief": "A two second lighthouse animation", "profile_id": "local", "duration_seconds": 2})
                assert replay.status_code == 503
                assert studio.accepted_production_counts() == before
                assert_no_remaining_sse_frames(lines)
                assert time.monotonic() - started < 30
            assert client.get(ORIGIN + "/api/studio/tasks/" + task["task_id"]).status_code == 200


def test_real_expired_session_cannot_read_or_write_studio_resources(monkeypatch):
    with studio_browser(monkeypatch, absolute_seconds=5) as studio:
        owner = studio.sign_in()
        task = studio.create(owner)
        headers = studio.headers(owner, "expired-write-0001")
        cookies = {cookie["name"]: cookie["value"] for cookie in owner.cookies(ORIGIN)}
        before = studio.accepted_production_counts()
        time.sleep(6)
        with httpx.Client(trust_env=False, cookies=cookies, timeout=10) as client:
            for suffix in ("/api/studio/config", "/api/studio/tasks", f"/api/studio/tasks/{task['task_id']}",
                           f"/api/studio/tasks/{task['task_id']}/events", f"/api/studio/tasks/{task['task_id']}/result"):
                denied = client.get(ORIGIN + suffix, headers={"Range": "bytes=0-31", "If-None-Match": '"prior"'})
                assert denied.status_code == 401
                assert "etag" not in denied.headers
            for suffix in ("", f"/{task['task_id']}/cancel", f"/{task['task_id']}/resume", f"/{task['task_id']}/approvals/unknown/decision"):
                denied = client.post(ORIGIN + "/api/studio/tasks" + suffix, headers=headers, json={"expected_version": 99})
                assert denied.status_code == 401
        assert studio.accepted_production_counts() == before


def test_sse_replay_and_foreign_last_event_id_remain_in_current_workspace(studio_identity):
    studio = studio_identity
    owner, foreign = studio.sign_in(), studio.sign_in("u2")
    own_task, foreign_task = studio.create(owner), studio.create(foreign)

    def first_frame(context, task):
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies(ORIGIN)}
        with httpx.Client(trust_env=False, cookies=cookies, timeout=10) as client:
            with client.stream("GET", f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events") as response:
                assert response.status_code == 200
                frame = []
                for line in response.iter_lines():
                    if not line:
                        break
                    frame.append(line)
                event = json.loads(next(line[6:] for line in frame if line.startswith("data: ")))
                assert event["task_id"] == task["task_id"]
                assert all(word not in json.dumps(event) for word in ('"issuer"', '"sub"', '"session_ref"', '"sdk_storage"'))
                return event

    own_event, foreign_event = first_frame(owner, own_task), first_frame(foreign, foreign_task)
    path = f"{ORIGIN}/api/studio/tasks/{own_task['task_id']}/events"
    invalid = owner.request.get(path, headers={"Last-Event-ID": str(foreign_event["event_id"])})
    assert invalid.status == 409
    assert invalid.json()["error"]["code"] == "cursor_expired"
    assert foreign_task["task_id"] not in invalid.text()
    foreign_denied = owner.request.get(f"{ORIGIN}/api/studio/tasks/{foreign_task['task_id']}/events",
        headers={"Last-Event-ID": "private-invalid-cursor"})
    assert foreign_denied.status == 404
    cancelled = owner.request.post(f"{ORIGIN}/api/studio/tasks/{own_task['task_id']}/cancel",
        headers=studio.headers(owner, "replay-cancel-0001"), data={"expected_version": own_task["version"]})
    assert cancelled.status == 202
    cookies = {cookie["name"]: cookie["value"] for cookie in owner.cookies(ORIGIN)}
    with httpx.Client(trust_env=False, cookies=cookies, timeout=10) as client:
        with client.stream("GET", path, headers={"Last-Event-ID": str(own_event["event_id"])}) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("data: "):
                    replayed = json.loads(line[6:])
                    assert replayed["task_id"] == own_task["task_id"]
                    assert replayed["event_id"] > own_event["event_id"]
                    assert replayed["data"]["state"] == "cancel_requested"
                    break


def test_idle_sse_ends_when_its_real_pg_resource_read_is_blocked(studio_identity):
    import psycopg
    from psycopg import sql
    studio = studio_identity
    owner = studio.sign_in()
    task = studio.create(owner)
    cookies = {cookie["name"]: cookie["value"] for cookie in owner.cookies(ORIGIN)}
    with httpx.Client(trust_env=False, cookies=cookies, timeout=31) as client:
        with client.stream("GET", f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events") as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            for line in lines:
                if not line:
                    break
            with psycopg.connect(os.environ["STUDIO_TEST_DATABASE_URL"]) as fault:
                fault.execute(sql.SQL("LOCK TABLE {}.tasks IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(studio.repository.schema)))
                started = time.monotonic()
                assert_no_remaining_sse_frames(lines)
                assert time.monotonic() - started < 30


def test_sse_transport_backpressure_cannot_extend_the_logout_window(monkeypatch):
    with studio_browser(monkeypatch, backpressure=True) as studio:
        owner = studio.sign_in()
        task = studio.create(owner)
        headers = studio.headers(owner, "logout-buffer-0001")
        cookies = {cookie["name"]: cookie["value"] for cookie in owner.cookies(ORIGIN)}
        with httpx.Client(trust_env=False, cookies=cookies, timeout=31) as client:
            with client.stream("GET", f"{ORIGIN}/api/studio/tasks/{task['task_id']}/events") as response:
                assert response.status_code == 200
                assert studio.app.state.transport_ready.wait(timeout=5)
                headers = studio.headers(owner, "logout-buffer-0001")
                assert owner.request.post(ORIGIN + "/api/auth/logout", headers=headers, data={}).status == 200
                started = time.monotonic()
                try:
                    assert list(response.iter_bytes()) == []
                except httpx.RemoteProtocolError:
                    pass
                except httpx.ReadTimeout:
                    time.sleep(15)  # Let the injected transport finish before fixture cleanup reports the failure.
                    pytest.fail("The buffered Studio event exceeded the 30 second logout window")
                assert time.monotonic() - started < 30


def test_real_pi_and_media_children_do_not_receive_identity_server_secrets(monkeypatch):
    from production.pi_rpc import PiRPC
    from tests.fixtures.auth28_media import EnvironmentProbeImage
    private = {"OPENMONTAGE_AUTH_SESSION_KEY": "auth28-private-session-sentinel",
               "OPENMONTAGE_AUTH_APP_SECRET": "auth28-private-app-sentinel",
               "OPENMONTAGE_AUTH_M2M_SECRET": "auth28-private-management-sentinel",
               "OPENMONTAGE_AUTH_ASSERTION": "auth28-private-assertion-sentinel"}
    for name, value in private.items():
        monkeypatch.setenv(name, value)
    pi_processes, diagnostics = [], []

    class ObservedPi(PiRPC):
        def __init__(self, command, **options):
            super().__init__([*command, "--extension", str(probe)], **options)

        async def close(self):
            await super().close()
            diagnostics.append(self.diagnostics)
            for line in self.diagnostics.splitlines():
                if line.startswith("auth28-environment:"):
                    pi_processes.append(json.loads(line.removeprefix("auth28-environment:")))

    with production_studio(monkeypatch, media_http=True) as studio:
        probe = studio.worker.runtime_root / "environment-probe.ts"
        probe.write_text('export default () => { process.stderr.write("auth28-environment:" + JSON.stringify(Object.keys(process.env)) + "\\n"); };\n')
        studio.worker.runner_factory = ObservedPi
        studio.worker.environment.update(private)
        observation = studio.worker.registry.fixture_manager.list()
        configuration = studio.worker.runtime_root / "private-env"
        configuration.mkdir(mode=0o700)
        (configuration / ".env").write_text("OPENMONTAGE_AUTH_DOTENV_SECRET=auth28-private-dotenv-sentinel\n")
        image = studio.worker.registry.get("newapi_image")
        monkeypatch.setenv("NEW_API_KEY", "auth28-media-provider-credential")
        studio.worker.registry.register(EnvironmentProbeImage(image.address, image.submissions, observation, configuration))
        owner = studio.sign_in()
        task = complete_production(studio, owner)
        assert task["result"]["verified"] is True
        assert pi_processes and observation
        protected = {*private, "OPENMONTAGE_AUTH_DOTENV_SECRET", "STUDIO_TEST_DATABASE_URL", "STUDIO_TEST_SESSION_KEY",
                     "AUTH_TEST_FIXTURE", "OPENMONTAGE_AUTH_CONFIG_FILE"}
        assert all("PI_CODING_AGENT_DIR" in process for process in pi_processes)
        assert all(not protected.intersection(process) for process in pi_processes)
        assert all(not protected.intersection(environment) for environment in observation)
        assert all("NEW_API_KEY" in environment for environment in observation)
        content = json.dumps(studio.model_requests) + "".join(diagnostics)
        assert not any(value in content for value in private.values())
        assert not any(value in path.read_text(errors="replace") for value in private.values()
                       for path in studio.worker.runtime_root.rglob("*") if path.is_file() and configuration not in path.parents)


def test_unassigned_history_and_signed_out_studio_requests_have_no_business_effects(studio_identity):
    from production.contracts import TaskCreate
    from production.pi_config import snapshot_for
    studio = studio_identity
    owner = studio.sign_in()
    owned = studio.create(owner)
    legacy_request = TaskCreate(brief="Unassigned historical production", profile_id="local")
    historical = studio.repository.create_task(legacy_request,
        snapshot_for(studio.app.state.studio_config.profiles["local"], legacy_request), "auth28-historical-key")
    before = studio.accepted_production_counts()
    assert [task["task_id"] for task in owner.request.get(ORIGIN + "/api/studio/tasks").json()] == [owned["task_id"]]
    for suffix in ("", "/events", "/result?download=1"):
        denied = owner.request.get(ORIGIN + "/api/studio/tasks/" + historical.task_id + suffix,
                                   headers={"Range": "bytes=0-31", "If-None-Match": '"prior"'})
        assert denied.status == 404 and "etag" not in denied.headers
    assert owner.request.get(ORIGIN + "/api/studio/tasks?after=" + historical.task_id).status == 404
    anonymous = studio.browser.new_context()
    headers = studio.headers(owner, "signed-out-no-write")
    for suffix in ("/api/studio/config", "/api/studio/tasks", f"/api/studio/tasks/{owned['task_id']}",
                   f"/api/studio/tasks/{owned['task_id']}/events", f"/api/studio/tasks/{owned['task_id']}/result?download=1"):
        assert anonymous.request.get(ORIGIN + suffix, headers={"Range": "bytes=0-31"}).status == 401
    for context, target, expected in ((owner, historical.task_id, 404), (anonymous, owned["task_id"], 401)):
        for suffix in ("cancel", "resume", "approvals/unknown/decision"):
            assert context.request.post(f"{ORIGIN}/api/studio/tasks/{target}/{suffix}", headers=headers,
                                        data={"expected_version": 99}).status == expected
    assert anonymous.request.post(ORIGIN + "/api/studio/tasks", headers=headers, data={}).status == 401
    assert studio.accepted_production_counts() == before


def test_lost_create_response_replays_only_after_returning_to_its_original_workspace(studio_identity):
    studio = studio_identity
    owner = studio.sign_in()
    original = studio.me(owner)
    body = {"brief": "A two second lighthouse animation", "profile_id": "local", "duration_seconds": 2}
    key = "auth28-lost-response"
    accepted_headers = studio.headers(owner, key)
    headers = {**accepted_headers, "Host": "127.0.0.1:4762", "Content-Type": "application/json",
               "Cookie": "; ".join(cookie["name"] + "=" + cookie["value"] for cookie in owner.cookies(ORIGIN))}
    payload = json.dumps(body).encode()
    headers["Content-Length"] = str(len(payload))
    request = "POST /api/studio/tasks HTTP/1.1\r\n" + "\r\n".join(name + ": " + value for name, value in headers.items()) + "\r\n\r\n"
    with socket.create_connection(("127.0.0.1", 4762), timeout=10) as connection:
        connection.sendall(request.encode() + payload)
        assert connection.recv(1) == b"H"
    accepted = owner.request.get(ORIGIN + "/api/studio/tasks").json()
    assert len(accepted) == 1
    before = studio.accepted_production_counts()
    studio.switch(owner, "A")
    stale = owner.request.post(ORIGIN + "/api/studio/tasks", headers=accepted_headers, data=body)
    assert stale.status == 409 and stale.json()["error"]["code"] == "auth_context_conflict"
    assert owner.request.get(ORIGIN + "/api/studio/tasks").json() == []
    restored = owner.request.post(ORIGIN + "/api/auth/workspace", headers=studio.headers(owner, "return-personal"),
        data={"kind": "personal", "owner_id": original["user"]["user_id"]})
    assert restored.status == 200
    replay = owner.request.post(ORIGIN + "/api/studio/tasks", headers=studio.headers(owner, key), data=body)
    assert replay.status == 202 and replay.json()["task_id"] == accepted[0]["task_id"]
    assert replay.json()["ownership"] == accepted[0]["ownership"]
    assert studio.accepted_production_counts() == before
