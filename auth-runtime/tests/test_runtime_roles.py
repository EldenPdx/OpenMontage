"""The Worker can execute accepted production without access to BFF session state."""

from hashlib import sha256
import json
import subprocess

import psycopg
from psycopg import sql
import pytest

from tests.fixtures.auth32_roles import isolated_runtime_roles, runtime_role_production


def test_runtime_principals_cannot_create_schema_or_read_another_principals_private_tables(tmp_path):
    with isolated_runtime_roles(tmp_path) as (configuration, owner, bff, worker):
        for field in ("bff_database_url", "worker_database_url"):
            with psycopg.connect(configuration[field]) as connection:
                flags = connection.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
                assert flags == (False, False, False, False, False)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(sql.SQL("CREATE TABLE {}.unpermitted_runtime_ddl(value integer)").format(sql.Identifier(owner.schema)))
        for table in ("auth_sessions", "login_interactions", "organization_intents", "organization_operation_owners", "identity_invitations"):
            with psycopg.connect(configuration["worker_database_url"]) as connection:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(owner.schema), sql.Identifier(table)))
        with psycopg.connect(configuration["bff_database_url"]) as connection:
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.auth_sessions").format(sql.Identifier(owner.schema))).fetchone() == (0,)


@pytest.mark.parametrize("workspace", ["personal", "organization"])
def test_real_production_runs_with_distinct_bff_worker_roles_and_no_private_session_access(tmp_path, monkeypatch, workspace):
    from tests.fixtures.auth28 import ORIGIN, complete_production

    with runtime_role_production(tmp_path, monkeypatch) as (studio, configuration):
        context = studio.sign_in()
        if workspace == "organization":
            studio.switch(context, "A")
        with psycopg.connect(configuration["worker_database_url"]) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(sql.SQL("SELECT storage FROM {}.auth_sessions").format(sql.Identifier(studio.repository.schema)))
        result = complete_production(studio, context)
        assert result["state"] == "succeeded" and result["result"]["verified"] is True
        assert result["ownership"]["workspace"]["kind"] == workspace
        download = context.request.get(ORIGIN + result["result"]["download_url"])
        assert download.status == 200
        assert sha256(download.body()).hexdigest() == result["result"]["video"]["sha256"]
        video = studio.repository.projects_dir / result["project_id"] / result["result"]["video"]["path"]
        metadata = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)], timeout=10))
        assert float(metadata["format"]["duration"]) == pytest.approx(2, abs=0.1)
        page = context.new_page()
        page.goto(ORIGIN + "/en")
        page.evaluate("url => {let video=document.createElement('video');video.src=url;video.muted=true;document.body.replaceChildren(video);video.play();}", ORIGIN + result["result"]["preview_url"])
        page.wait_for_function("document.querySelector('video').currentTime > 0.3")
        assert page.locator("video").evaluate("video => video.videoWidth") == 1280
        with psycopg.connect(configuration["worker_database_url"]) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(sql.SQL("SELECT storage FROM {}.auth_sessions").format(sql.Identifier(studio.repository.schema)))
