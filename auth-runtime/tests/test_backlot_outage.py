"""Root exclusively pauses the real identity/database services for these HTTP cases."""

import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
import pytest

from tests.fixtures.backlot29 import ORIGIN, backlot_environment, backlot_browser, project_for, sign_in
from tests.fixtures.identity.environment import private_json
from test_backlot import actual_cookies, next_frame


@pytest.mark.parametrize("service", ["logto", "postgres"])
@pytest.mark.parametrize("surface", ["project", "library", "media"])
def test_real_service_outage_terminates_protected_stream(backlot_environment, service, surface):
    control_path = os.environ.get("BACKLOT29_OUTAGE_CONTROL")
    if not control_path:
        pytest.fail("Root must own the real-service outage window and private control file")
    control_path = Path(control_path)
    control = {"scenario": uuid4().hex, "service": service, "surface": surface, "phase": "preparing"}
    private_json(control_path, control)
    fixture, repository = backlot_environment
    with backlot_browser(repository) as (browser, app):
        context = browser.new_context()
        sign_in(context, fixture)
        project = project_for(context, app, repository, title="Outage " + service + surface)
        if surface == "media":
            media = project / "long.bin"
            with media.open("wb") as stream:
                stream.truncate(128 * 1024 * 1024)
            path = f"/media/{project.name}/long.bin"
        else:
            path = f"/api/project/{project.name}/events" if surface == "project" else "/api/library/events"
        time.sleep(0.8)
        try:
            with httpx.Client(trust_env=False, cookies=actual_cookies(context), timeout=31) as client:
                with client.stream("GET", ORIGIN + path) as response:
                    assert response.status_code == 200
                    chunks = response.iter_raw() if surface == "media" else response.iter_lines()
                    if surface == "media":
                        received = len(next(chunks))
                    else:
                        assert next_frame(chunks)["type"] == "hello"
                    control["phase"] = "ready"
                    private_json(control_path, control)
                    deadline = time.monotonic() + 60
                    while json.loads(control_path.read_text()).get("phase") != "paused":
                        assert time.monotonic() < deadline, "Root did not acknowledge the exclusive outage window"
                        time.sleep(0.05)
                    started = time.monotonic()
                    if surface == "media":
                        time.sleep(25)
                        try:
                            received += sum(len(chunk) for chunk in chunks)
                        except httpx.RemoteProtocolError:
                            pass
                        assert received < 128 * 1024 * 1024
                    else:
                        with pytest.raises((StopIteration, httpx.RemoteProtocolError)):
                            next_frame(chunks)
                    assert time.monotonic() - started <= 30
                    control["observed_close_seconds"] = round(time.monotonic() - started, 3)
                    denied = context.request.get(ORIGIN + path, max_redirects=0, timeout=20000,
                                                 headers={"Range": "bytes=0-15", "If-None-Match": "*"})
                    assert denied.status == 503
                    assert not {"etag", "last-modified", "content-range"}.intersection(denied.headers)
                    assert context.request.get(ORIGIN + "/api/health").status == 200
                    control["passed"] = True
        finally:
            control["phase"] = "finished"
            private_json(control_path, control)
            deadline = time.monotonic() + 60
            while json.loads(control_path.read_text()).get("phase") != "resumed":
                assert time.monotonic() < deadline, "Root must restore the service before isolated schema cleanup"
                time.sleep(0.05)
