"""Required public Backlot acceptance with real Logto, PostgreSQL and Chromium."""

import json
import subprocess
import time

import httpx
import pytest

from tests.fixtures.backlot29 import ORIGIN, backlot_environment, backlot_browser, project_for, sign_in, write_headers, select_organization


def next_frame(lines):
    for line in lines:
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise StopIteration


def actual_cookies(context):
    return {item["name"]: item["value"] for item in context.cookies() if item["domain"] == "127.0.0.1"}


def test_signed_out_board_redirects_to_safe_login_without_project_data(backlot_environment):
    _, repository = backlot_environment
    with backlot_browser(repository) as (browser, _):
        context = browser.new_context()
        for prefix, locale in (("", "zh"), ("/zh", "zh"), ("/ja", "ja"), ("/en", "en")):
            response = context.request.get(ORIGIN + prefix + "/p/private-project", max_redirects=0)
            assert response.status == 302
            assert response.headers["location"] == f"/api/auth/login?return_to=%2F{locale}%2Fp%2Fprivate-project"
            assert response.headers["cache-control"] == "private, no-store"
        for path in ("/api/projects", "/api/project/private-project/state", "/api/project/private-project/events",
                     "/api/library/events", "/media/private-project/output.mp4", "/thumb/private-project/output.png"):
            response = context.request.get(ORIGIN + path, max_redirects=0)
            assert response.status == 401
            assert "location" not in response.headers


def test_thumbnail_raw_active_documents_remain_sandboxed(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        sign_in(context, fixture)
        project = project_for(context, app, repository, title="Active documents")
        for extension in ("html", "htm", "svg", "xhtml"):
            (project / ("active." + extension)).write_text('<script>parent.location="https://foreign.invalid"</script>')
            for route in ("thumb", "media"):
                response = context.request.get(ORIGIN + f"/{route}/{project.name}/active.{extension}")
                assert response.status == 200
                assert response.headers["content-security-policy"].startswith("sandbox;")
                assert response.headers["x-content-type-options"] == "nosniff"


def test_personal_library_contains_only_owned_projects(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        first, second = browser.new_context(), browser.new_context()
        sign_in(first, fixture, "u1")
        sign_in(second, fixture, "u2")
        own = project_for(first, app, repository, title="Personal first")
        foreign = project_for(second, app, repository, title="Personal second")
        legacy = repository.projects_dir / "unassigned"
        legacy.mkdir()
        (legacy / "project.json").write_text('{"title":"Unassigned private project"}')
        response = first.request.get(ORIGIN + "/api/projects")
        assert response.status == 200
        assert [item["project_id"] for item in response.json()] == [own.name]
        assert foreign.name not in response.text() and "Unassigned" not in response.text()
        assert response.headers["cache-control"] == "private, no-store"


def test_project_direct_paths_authorize_the_selected_owner_before_files(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        first, second = browser.new_context(), browser.new_context()
        sign_in(first, fixture, "u1")
        sign_in(second, fixture, "u2")
        own = project_for(first, app, repository, title="Owned board")
        foreign = project_for(second, app, repository, title="Foreign board")
        for project in (own, foreign):
            (project / "clip.mp4").write_bytes(b"0123456789")
        response = first.request.get(ORIGIN + f"/api/project/{own.name}/state")
        assert response.status == 200 and response.json()["title"] == "Owned board"
        for project_id in (foreign.name, "unassigned", "absent"):
            for path in (f"/api/project/{project_id}/state", f"/api/project/{project_id}/events",
                         f"/media/{project_id}/clip.mp4", f"/thumb/{project_id}/clip.mp4",
                         f"/p/{project_id}", *(f"/{locale}/p/{project_id}" for locale in ("zh", "ja", "en"))):
                denied = first.request.get(ORIGIN + path, max_redirects=0, headers={"Range": "bytes=500-600", "If-None-Match": '*', "Last-Event-ID": own.name})
                assert denied.status == 404, path
                assert not {"etag", "last-modified", "content-range"}.intersection(denied.headers), path
                assert "Foreign board" not in denied.text(), path
        ranged = first.request.get(ORIGIN + f"/media/{own.name}/clip.mp4", headers={"Range": "bytes=2-5"})
        assert ranged.status == 206 and ranged.body() == b"2345"
        for locale in ("zh", "ja", "en"):
            page = first.request.get(ORIGIN + f"/{locale}/p/{own.name}")
            assert page.status == 200 and "Owned board" not in page.text()
        legacy = first.request.get(ORIGIN + f"/p/{own.name}?static=1&tag=a%2Fb", max_redirects=0)
        assert legacy.status == 307
        assert legacy.headers["location"] == f"/zh/p/{own.name}?static=1&tag=a%2Fb"


def test_owner_cannot_download_auth_configuration_or_control_files(backlot_environment):
    from production.ownership_guard import MARKER, WITNESS_DIRECTORY, WITNESS_PREFIX
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        sign_in(context, fixture)
        project = project_for(context, app, repository, title="Private controls")
        private_paths = (MARKER, WITNESS_DIRECTORY + "/private-record", WITNESS_PREFIX + "a" * 64 + ".json",
                         ".studio-owner.json", ".studio-writer.lock", ".env", "config.yaml", "AUTH.JSON",
                         "agents/run/models.json", "sessions/run/session.jsonl", "auth-runtime/private.txt", ".provider-private.json")
        for name in private_paths:
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name != MARKER:
                target.write_text("private-control-sentinel")
            for route in ("media", "thumb"):
                response = context.request.get(ORIGIN + f"/{route}/{project.name}/{name}", headers={"Range": "bytes=0-2"})
                assert response.status == 404, name
                assert "private-control-sentinel" not in response.text()
                assert "etag" not in response.headers
        (project / "alias.txt").symlink_to(project / "AUTH.JSON")
        assert context.request.get(ORIGIN + f"/media/{project.name}/alias.txt").status == 404


def test_library_stream_filters_foreign_filesystem_notifications(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        first, second = browser.new_context(), browser.new_context()
        sign_in(first, fixture, "u1")
        sign_in(second, fixture, "u2")
        own = project_for(first, app, repository, title="Library first")
        foreign = project_for(second, app, repository, title="Library second")
        with httpx.Client(trust_env=False, cookies=actual_cookies(first), timeout=20) as client:
            with client.stream("GET", ORIGIN + "/api/library/events", headers={"Last-Event-ID": foreign.name}) as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                assert next_frame(lines) == {"type": "hello"}
                time.sleep(0.7)
                (foreign / "changed.txt").write_text("foreign-notification-sentinel")
                time.sleep(0.7)
                (own / "changed.txt").write_text("owned-notification-sentinel")
                frame = next_frame(lines)
                assert frame == {"type": "change", "project_id": own.name}


@pytest.mark.parametrize("surface", ["project", "library"])
def test_idle_stream_logout_closes_within_thirty_seconds(backlot_environment, surface):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        me = sign_in(context, fixture)
        project = project_for(context, app, repository, title="Logout " + surface)
        time.sleep(0.8)
        path = f"/api/project/{project.name}/events" if surface == "project" else "/api/library/events"
        with httpx.Client(trust_env=False, cookies=actual_cookies(context), timeout=31) as client:
            with client.stream("GET", ORIGIN + path) as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                assert next_frame(lines)["type"] == "hello"
                assert context.request.post(ORIGIN + "/api/auth/logout", headers=write_headers(me), data={}).status == 200
                started = time.monotonic()
                with pytest.raises((StopIteration, httpx.RemoteProtocolError)):
                    next_frame(lines)
                assert time.monotonic() - started <= 30
        assert context.request.get(ORIGIN + path, max_redirects=0).status == 401


def test_backpressured_media_stops_after_logout(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        me = sign_in(context, fixture)
        project = project_for(context, app, repository, title="Long download")
        media = project / "long.bin"
        size = 128 * 1024 * 1024
        with media.open("wb") as stream:
            stream.truncate(size)
        with httpx.Client(trust_env=False, cookies=actual_cookies(context), timeout=30) as client:
            with client.stream("GET", ORIGIN + f"/media/{project.name}/long.bin") as response:
                assert response.status_code == 200
                chunks = response.iter_raw()
                received = len(next(chunks))
                assert context.request.post(ORIGIN + "/api/auth/logout", headers=write_headers(me), data={}).status == 200
                started = time.monotonic()
                time.sleep(17)
                try:
                    received += sum(len(chunk) for chunk in chunks)
                except httpx.RemoteProtocolError:
                    pass
                assert received < size
                assert time.monotonic() - started <= 30


def test_state_does_not_follow_another_projects_marker(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        first, second = browser.new_context(), browser.new_context()
        sign_in(first, fixture, "u1")
        sign_in(second, fixture, "u2")
        own = project_for(first, app, repository, title="Safe projection")
        foreign = project_for(second, app, repository, title="foreign-marker-sentinel")
        (own / "project.json").unlink()
        (own / "project.json").symlink_to(foreign / "project.json")
        response = first.request.get(ORIGIN + f"/api/project/{own.name}/state")
        assert response.status == 200
        assert "foreign-marker-sentinel" not in response.text()


def test_state_scopes_checkpoints_artifacts_events_and_media_links(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        first, second = browser.new_context(), browser.new_context()
        sign_in(first, fixture, "u1")
        sign_in(second, fixture, "u2")
        own = project_for(first, app, repository, title="Scoped state paths")
        foreign = project_for(second, app, repository, title="Foreign state paths")
        for project in (own, foreign):
            (project / "artifacts").mkdir()
            (project / "renders").mkdir()
            (project / "snapshots").mkdir()
        (foreign / "checkpoint_script.json").write_text(json.dumps({"status": "failed", "error": "foreign-checkpoint-sentinel"}))
        (foreign / "artifacts/research_brief.json").write_text('{"topic":"foreign-artifact-sentinel"}')
        (foreign / "events.jsonl").write_text('{"event":"foreign-event-sentinel"}\n')
        (foreign / "renders/private.mp4").write_bytes(b"foreign-render-sentinel" * 100)
        (foreign / "snapshots/private.png").write_bytes(b"foreign-snapshot-sentinel")
        for relative in ("checkpoint_script.json", "artifacts/research_brief.json", "events.jsonl", "renders/private.mp4", "snapshots/private.png"):
            (own / relative).symlink_to(foreign / relative)
        (own / "artifacts/scene_plan.json").write_text('{"scenes":[{"id":"own-scene"}]}')
        (own / "local-script.json").write_text('{"sections":[{"text":"legitimate-inside-link-sentinel"}]}')
        (own / "artifacts/script.json").symlink_to(own / "local-script.json")
        response = first.request.get(ORIGIN + f"/api/project/{own.name}/state")
        assert response.status == 200
        assert "foreign-" not in response.text()
        assert "legitimate-inside-link-sentinel" in response.text()
        assert response.json()["media"] == {"renders": [], "snapshots": [], "music": []}


def test_replaced_project_directory_cannot_alias_a_foreign_project(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        first, second = browser.new_context(), browser.new_context()
        sign_in(first, fixture, "u1")
        sign_in(second, fixture, "u2")
        own = project_for(first, app, repository, title="Registered own project")
        foreign = project_for(second, app, repository, title="foreign-directory-sentinel")
        (foreign / "note.txt").write_text("foreign-bytes-sentinel")
        own.rename(own.with_name("_retained-own-project"))
        own.symlink_to(foreign, target_is_directory=True)
        for path in (f"/api/project/{own.name}/state", f"/media/{own.name}/note.txt", f"/thumb/{own.name}/note.txt"):
            response = first.request.get(ORIGIN + path)
            assert response.status == 404
            assert "foreign-" not in response.text()


def test_multi_organization_members_read_only_the_selected_workspace(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        cross, admin_b, viewer = browser.new_context(), browser.new_context(), browser.new_context()
        cross_me = sign_in(cross, fixture, "cross")
        personal = project_for(cross, app, repository, title="Cross personal")
        cross_me = select_organization(cross, fixture, "A", cross_me)
        assert cross_me["role"] == "editor"
        project_a = project_for(cross, app, repository, title="Organization A project")
        b_me = select_organization(admin_b, fixture, "B", sign_in(admin_b, fixture, "u2"))
        assert b_me["role"] == "admin"
        project_b = project_for(admin_b, app, repository, title="Organization B project")
        viewer_me = select_organization(viewer, fixture, "A", sign_in(viewer, fixture, "viewer"))
        assert viewer_me["role"] == "viewer"
        for context in (cross, viewer):
            response = context.request.get(ORIGIN + "/api/projects")
            assert [item["project_id"] for item in response.json()] == [project_a.name]
            assert context.request.get(ORIGIN + f"/api/project/{project_a.name}/state").status == 200
            for project in (project_b, personal):
                assert context.request.get(ORIGIN + f"/api/project/{project.name}/state").status == 404
        cross_me = select_organization(cross, fixture, "B", cross_me)
        assert cross_me["role"] == "viewer"
        response = cross.request.get(ORIGIN + "/api/projects")
        assert [item["project_id"] for item in response.json()] == [project_b.name]
        assert cross.request.get(ORIGIN + f"/api/project/{project_a.name}/state").status == 404


def test_authorized_browser_plays_and_downloads_real_video(backlot_environment):
    from io import BytesIO
    from PIL import Image
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        sign_in(context, fixture)
        project = project_for(context, app, repository, title="Actual playback")
        video = project / "clip.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
                        "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)], check=True, capture_output=True, timeout=30)
        url = ORIGIN + f"/media/{project.name}/clip.mp4"
        page = context.new_page()
        page.goto(ORIGIN + "/en")
        page.evaluate("""async url => {
          const video = document.createElement('video');
          video.id = 'verified-playback'; video.muted = true; video.src = url;
          document.body.appendChild(video); await video.play();
        }""", url)
        page.wait_for_function("document.querySelector('#verified-playback').currentTime > 0.3")
        assert page.locator("#verified-playback").evaluate("video => video.videoWidth") == 160
        response = context.request.get(url + "?download=1")
        assert response.status == 200 and response.body() == video.read_bytes()
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "private, no-store"
        multipart = context.request.get(url, headers={"Range": "bytes=0-15,32-47"})
        assert multipart.status == 206
        assert multipart.headers["content-type"].startswith("multipart/byteranges")
        invalid = context.request.get(url, headers={"Range": "bytes=999999999-"})
        assert invalid.status == 416
        thumbnail = context.request.get(ORIGIN + f"/thumb/{project.name}/clip.mp4?w=320")
        assert thumbnail.status == 200
        with Image.open(BytesIO(thumbnail.body())) as image:
            assert image.format == "JPEG" and image.width == 320


def test_no_read_permission_returns_owned403_and_foreign404(backlot_environment):
    fixture, repository = backlot_environment
    assert fixture._data["namespace"] == "auth29", "Revocation cases require their private dataset"
    roles = fixture.organization_roles("viewer", "A")
    with backlot_browser(repository) as (browser, app):
        owner, viewer, foreign_owner = browser.new_context(), browser.new_context(), browser.new_context()
        select_organization(owner, fixture, "A", sign_in(owner, fixture, "u1"))
        select_organization(foreign_owner, fixture, "B", sign_in(foreign_owner, fixture, "u2"))
        project = project_for(owner, app, repository, title="Read permission")
        foreign = project_for(foreign_owner, app, repository, title="Foreign permission")
        select_organization(viewer, fixture, "A", sign_in(viewer, fixture, "viewer"))
        try:
            fixture.set_roles("viewer", "A", [])
            for project_id, status in ((project.name, 403), (foreign.name, 404), ("absent", 404)):
                for path in (f"/api/project/{project_id}/state", f"/api/project/{project_id}/events",
                             f"/media/{project_id}/absent.mp4", f"/thumb/{project_id}/absent.mp4",
                             f"/p/{project_id}", *(f"/{locale}/p/{project_id}" for locale in ("zh", "ja", "en"))):
                    response = viewer.request.get(ORIGIN + path, max_redirects=0)
                    assert response.status == status, path
            for path in ("/api/projects", "/api/library/events"):
                assert viewer.request.get(ORIGIN + path).status == 403
        finally:
            fixture.set_roles("viewer", "A", roles)


@pytest.mark.parametrize("revocation", ["read", "membership", "suspension"])
@pytest.mark.parametrize("surface", ["project", "library", "media"])
def test_current_authority_revocation_ends_each_stream(backlot_environment, revocation, surface):
    fixture, repository = backlot_environment
    assert fixture._data["namespace"] == "auth29", "Revocation cases require their private dataset"
    roles = fixture.organization_roles("viewer", "A")
    with backlot_browser(repository) as (browser, app):
        owner, viewer = browser.new_context(), browser.new_context()
        select_organization(owner, fixture, "A", sign_in(owner, fixture, "u1"))
        project = project_for(owner, app, repository, title="Revoke " + revocation + surface)
        select_organization(viewer, fixture, "A", sign_in(viewer, fixture, "viewer"))
        if surface == "media":
            with (project / "long.bin").open("wb") as stream:
                stream.truncate(128 * 1024 * 1024)
            path = f"/media/{project.name}/long.bin"
        else:
            path = f"/api/project/{project.name}/events" if surface == "project" else "/api/library/events"
        time.sleep(0.8)
        try:
            with httpx.Client(trust_env=False, cookies=actual_cookies(viewer), timeout=31) as client:
                with client.stream("GET", ORIGIN + path) as response:
                    assert response.status_code == 200
                    chunks = response.iter_raw() if surface == "media" else response.iter_lines()
                    if surface == "media":
                        received = len(next(chunks))
                    else:
                        assert next_frame(chunks)["type"] == "hello"
                    if revocation == "read":
                        fixture.set_roles("viewer", "A", [])
                    elif revocation == "membership":
                        fixture.remove_member("viewer", "A")
                    else:
                        fixture.suspend("viewer")
                    started = time.monotonic()
                    if surface == "media":
                        time.sleep(17)
                        try:
                            received += sum(len(chunk) for chunk in chunks)
                        except httpx.RemoteProtocolError:
                            pass
                        assert received < 128 * 1024 * 1024
                    else:
                        with pytest.raises((StopIteration, httpx.RemoteProtocolError)):
                            next_frame(chunks)
                    assert time.monotonic() - started <= 30
                    reconnect = viewer.request.get(ORIGIN + path, max_redirects=0)
                    assert reconnect.status in {401, 403, 404}
        finally:
            if revocation == "suspension":
                fixture.suspend("viewer", False)
            elif revocation == "membership":
                fixture.add_member("viewer", "A", roles)
            else:
                fixture.set_roles("viewer", "A", roles)


def test_event_transport_backpressure_cannot_extend_logout_window(backlot_environment):
    fixture, repository = backlot_environment
    control = {"armed": False, "blocked": False}
    with backlot_browser(repository, delayed_event_transport=control) as (browser, app):
        context = browser.new_context()
        me = sign_in(context, fixture)
        project = project_for(context, app, repository, title="Backpressured events")
        time.sleep(0.8)
        with httpx.Client(trust_env=False, cookies=actual_cookies(context), timeout=31) as client:
            with client.stream("GET", ORIGIN + f"/api/project/{project.name}/events") as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                assert next_frame(lines)["type"] == "hello"
                control["armed"] = True
                (project / "changed.txt").write_text("actual-event-before-logout")
                deadline = time.monotonic() + 5
                while not control["blocked"]:
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                assert context.request.post(ORIGIN + "/api/auth/logout", headers=write_headers(me), data={}).status == 200
                started = time.monotonic()
                with pytest.raises((StopIteration, httpx.RemoteProtocolError)):
                    next_frame(lines)
                assert time.monotonic() - started <= 30


def test_direct_static_shells_and_head_cannot_expose_business_data(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        owner, other = browser.new_context(), browser.new_context()
        sign_in(owner, fixture, "u1")
        sign_in(other, fixture, "u2")
        project = project_for(owner, app, repository, title="static-business-sentinel")
        (project / "note.txt").write_text("owned-note-sentinel")
        anonymous = browser.new_context()
        for context in (anonymous, owner, other):
            for path in ("/ui/index.html", "/ui/studio.html", "/ui/board.html", "/zh", "/ja", "/en"):
                response = context.request.get(ORIGIN + path)
                assert response.status == 200
                assert "static-business-sentinel" not in response.text() and project.name not in response.text()
            assert context.request.head(ORIGIN + "/ui/board.html").status == 200
            assert context.request.get(ORIGIN + "/openapi.json").status == 404
        etag = owner.request.get(ORIGIN + f"/media/{project.name}/note.txt").headers["etag"]
        for method in (other.request.get, other.request.head):
            response = method(ORIGIN + f"/media/{project.name}/note.txt", headers={"If-None-Match": etag, "If-Range": etag, "Range": "bytes=0-2"})
            assert response.status == 404
            assert not {"etag", "last-modified", "content-range", "accept-ranges"}.intersection(response.headers)
        assert owner.request.head(ORIGIN + f"/media/{project.name}/note.txt").status == 405


@pytest.mark.parametrize("reference", ["absolute", "relative"])
def test_board_pipeline_metadata_cannot_read_another_projects_manifest(backlot_environment, reference):
    import os
    from pathlib import Path
    import yaml

    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        own_context, foreign_context = browser.new_context(), browser.new_context()
        sign_in(own_context, fixture, "u1")
        sign_in(foreign_context, fixture, "u2")
        own = project_for(own_context, app, repository, title="Named pipeline only")
        foreign = project_for(foreign_context, app, repository, title="Private pipeline")
        manifest = yaml.safe_load((Path(__file__).resolve().parents[2] / "pipeline_defs/framework-smoke.yaml").read_text())
        manifest["stages"][0]["name"] = "foreign-stage-sentinel"
        (foreign / "private-pipeline.yaml").write_text(yaml.safe_dump(manifest))
        marker = json.loads((own / "project.json").read_text())
        marker["pipeline_type"] = (str(foreign / "private-pipeline") if reference == "absolute" else
                                   os.path.relpath(foreign / "private-pipeline", Path(__file__).resolve().parents[2] / "pipeline_defs"))
        (own / "project.json").write_text(json.dumps(marker))
        response = own_context.request.get(ORIGIN + f"/api/project/{own.name}/state")
        assert response.status == 200
        assert "foreign-stage-sentinel" not in response.text()


def test_cached_summaries_and_thumbnails_are_authorized_again_for_each_user(backlot_environment):
    from io import BytesIO
    from PIL import Image

    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        owner, other = browser.new_context(), browser.new_context()
        own_me = sign_in(owner, fixture, "u1")
        other_me = sign_in(other, fixture, "u2")
        own = project_for(owner, app, repository, title="Cached own summary")
        foreign = project_for(other, app, repository, title="Cached foreign summary")
        Image.new("RGB", (800, 600), (210, 20, 30)).save(own / "poster.png")
        thumbnail_url = ORIGIN + f"/thumb/{own.name}/poster.png?w=320"
        thumbnail = owner.request.get(thumbnail_url)
        assert thumbnail.status == 200
        with Image.open(BytesIO(thumbnail.body())) as image:
            assert image.size == (320, 240)
        for context, project, me in ((owner, own, own_me), (other, foreign, other_me), (owner, own, own_me)):
            response = context.request.get(ORIGIN + "/api/projects")
            assert [item["project_id"] for item in response.json()] == [project.name]
            assert response.headers["x-auth-context"] == me["context_id"]
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["vary"] == "Cookie"
        conditional = {"If-None-Match": thumbnail.headers["etag"], "If-Range": thumbnail.headers["etag"], "Range": "bytes=0-15"}
        denied = other.request.get(thumbnail_url, headers=conditional)
        assert denied.status == 404
        assert not {"etag", "last-modified", "content-range", "accept-ranges"}.intersection(denied.headers)
        assert thumbnail.body() not in denied.body()
        assert other.request.head(thumbnail_url, headers=conditional).status == 404
        conditional_owner = owner.request.get(thumbnail_url, headers={"If-None-Match": thumbnail.headers["etag"]})
        assert conditional_owner.status == 200 and conditional_owner.body() == thumbnail.body()
        assert owner.request.head(thumbnail_url).status == 405
        public = owner.request.get(ORIGIN + "/ui/board.html")
        public_cached = other.request.get(ORIGIN + "/ui/board.html", headers={"If-None-Match": public.headers["etag"]})
        assert public_cached.status == 304 and public_cached.body() == b""
        assert public_cached.headers["cache-control"] == "no-cache"


def test_explicit_historical_import_exposes_only_the_mapped_owner(backlot_environment, tmp_path):
    from datetime import datetime, timezone
    import os
    from production.identity_migrate import AuthorityEvidence, OwnershipImporter

    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        owner, other = browser.new_context(), browser.new_context()
        sign_in(owner, fixture, "u1")
        sign_in(other, fixture, "u2")
        legacy = repository.projects_dir / "legacy-cli-project"
        unassigned = repository.projects_dir / "unassigned-cli-project"
        for project in (legacy, unassigned):
            project.mkdir()
            (project / "project.json").write_text(json.dumps({"project_id": project.name, "title": project.name}))
            (project / "source.txt").write_bytes(b"preserved-cli-source-sentinel\n")
            for context in (owner, other):
                for route in (f"/api/project/{project.name}/state", f"/media/{project.name}/source.txt", f"/thumb/{project.name}/source.txt"):
                    assert context.request.get(ORIGIN + route).status == 404
        assert owner.request.get(ORIGIN + "/api/projects").json() == []
        cookie = next(item["value"] for item in owner.cookies() if item["name"] == "openmontage_session")
        identity = app.state.auth_runtime.store.get_session(cookie).metadata.identity
        remote_user = fixture.api.request("GET", "/api/users/" + identity.sub)
        assert remote_user["id"] == identity.sub
        importer = OwnershipImporter(repository, projects_dir=repository.projects_dir, runtime_root=tmp_path / "runtime")
        plan = importer.plan([{"project_id": legacy.name, "creator_user_id": identity.user_id,
                              "workspace": {"kind": "personal", "owner_id": identity.user_id}}])
        assert plan.conflicts == []
        original = {name: (legacy / name).read_bytes() for name in ("project.json", "source.txt")}
        backup = importer.backup(plan, tmp_path / "private-backup", postgres_container=os.environ.get("STUDIO_TEST_POSTGRES_CONTAINER"))
        authority = AuthorityEvidence(mapping_sha256=plan.mapping_sha256, checked_at=datetime.now(timezone.utc),
                                      identities=[identity], organization_ids=[])
        assert importer.apply(plan, backup, authority=authority).changed == 1
        assert importer.apply(plan, backup, authority=authority).changed == 0
        assert {name: (legacy / name).read_bytes() for name in original} == original
        assert [item["project_id"] for item in owner.request.get(ORIGIN + "/api/projects").json()] == [legacy.name]
        assert owner.request.get(ORIGIN + f"/api/project/{legacy.name}/state").json()["title"] == legacy.name
        assert owner.request.get(ORIGIN + f"/media/{legacy.name}/source.txt").body() == original["source.txt"]
        assert owner.request.get(ORIGIN + f"/thumb/{legacy.name}/source.txt").body() == original["source.txt"]
        assert other.request.get(ORIGIN + "/api/projects").json() == []
        for context, project in ((other, legacy), (owner, unassigned), (other, unassigned)):
            assert context.request.get(ORIGIN + f"/api/project/{project.name}/state").status == 404
            assert context.request.get(ORIGIN + f"/media/{project.name}/source.txt").status == 404


@pytest.mark.parametrize("surface", ["project", "library"])
def test_authorized_events_reconnect_without_replaying_a_foreign_cursor(backlot_environment, surface):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        owner, other = browser.new_context(), browser.new_context()
        sign_in(owner, fixture, "u1")
        sign_in(other, fixture, "u2")
        own = project_for(owner, app, repository, title="Reconnect " + surface)
        foreign = project_for(other, app, repository, title="Foreign cursor " + surface)
        path = f"/api/project/{own.name}/events" if surface == "project" else "/api/library/events"
        time.sleep(0.8)
        with httpx.Client(trust_env=False, cookies=actual_cookies(owner), timeout=20) as client:
            for cursor in (foreign.name, own.name):
                with client.stream("GET", ORIGIN + path, headers={"Last-Event-ID": cursor}) as response:
                    assert response.status_code == 200
                    assert response.headers["cache-control"] == "private, no-store"
                    assert response.headers["vary"] == "Cookie"
                    frames = response.iter_lines()
                    hello = next_frame(frames)
                    assert hello == ({"type": "hello", "project_id": own.name} if surface == "project" else {"type": "hello"})
                    (foreign / "private-change.txt").write_text("never-replayed-sentinel")
                    time.sleep(0.7)
                    (own / "reconnect-change.txt").write_text(cursor)
                    assert next_frame(frames) == {"type": "change", "project_id": own.name}


def test_browser_html_and_svg_previews_cannot_execute_scripts(backlot_environment):
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        sign_in(context, fixture)
        own = project_for(context, app, repository, title="Sandboxed previews")
        script = '<script>parent.postMessage("preview-script-sentinel", "*")</script>'
        (own / "preview.html").write_text("<!doctype html>" + script)
        (own / "preview.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg">' + script + '<rect width="20" height="20"/></svg>')
        page = context.new_page()
        page.goto(ORIGIN + "/en")
        page.evaluate("window.previewMessages = []; window.addEventListener('message', event => window.previewMessages.push(event.data))")
        for route in ("media", "thumb"):
            for extension in ("html", "svg"):
                page.evaluate("""url => new Promise((resolve, reject) => {
                    const frame = document.createElement('iframe');
                    const timer = setTimeout(() => reject(new Error('Preview did not load')), 5000);
                    frame.onload = () => { clearTimeout(timer); resolve(); };
                    frame.src = url; document.body.appendChild(frame);
                })""", ORIGIN + f"/{route}/{own.name}/preview.{extension}")
        assert page.evaluate("window.previewMessages") == []
        assert page.url == ORIGIN + "/en"
