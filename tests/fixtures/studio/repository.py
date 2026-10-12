"""Isolated real PostgreSQL schemas shared by Studio behavior tests."""

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest


@contextmanager
def test_repository():
    dsn = os.environ.get("STUDIO_TEST_DATABASE_URL")
    if os.environ.get("STUDIO_REQUIRE_INTEGRATION") and not dsn:
        pytest.fail("Required integration needs an explicit isolated STUDIO_TEST_DATABASE_URL")
    try:
        import psycopg
    except ImportError:
        if os.environ.get("STUDIO_REQUIRE_INTEGRATION"):
            pytest.fail("Required integration needs psycopg")
        pytest.skip("Install psycopg for PostgreSQL integration")
    from psycopg import sql
    from production.repository import PostgresRepository

    if not dsn:
        if os.environ.get("STUDIO_REQUIRE_INTEGRATION"):
            pytest.fail("Studio verification requires a real PostgreSQL database")
        pytest.skip("Set STUDIO_TEST_DATABASE_URL for real PostgreSQL verification")
    schema = "studio_test_" + uuid4().hex
    with TemporaryDirectory(prefix="studio-test-projects-") as projects:
        repository = PostgresRepository(dsn, schema=schema, projects_dir=projects)
        repository.migrate()
        try:
            yield repository
        finally:
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def require_pi():
    executable = Path(__file__).resolve().parents[3] / ".runtime/pi/source/packages/coding-agent/dist/bundle/cli.js"
    if not executable.is_file():
        if os.environ.get("STUDIO_REQUIRE_INTEGRATION"):
            pytest.fail("Install the pinned official Pi with make studio-pi")
        pytest.skip("Install the pinned official Pi with make studio-pi")
    return executable
