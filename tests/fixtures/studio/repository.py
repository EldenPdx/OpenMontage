"""Isolated real PostgreSQL schemas shared by Studio behavior tests."""

from contextlib import contextmanager
import os
from pathlib import Path
from uuid import uuid4

import pytest


@contextmanager
def test_repository():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from production.repository import PostgresRepository

    dsn = os.environ.get("STUDIO_TEST_DATABASE_URL") or os.environ.get("STUDIO_DATABASE_URL")
    local = Path(__file__).resolve().parents[3] / ".runtime/studio/database-local.env"
    if not dsn and local.is_file():
        from dotenv import dotenv_values
        dsn = dotenv_values(local).get("STUDIO_DATABASE_URL")
    if not dsn:
        if os.environ.get("STUDIO_REQUIRE_INTEGRATION"):
            pytest.fail("Studio verification requires a real PostgreSQL database")
        pytest.skip("Set STUDIO_TEST_DATABASE_URL for real PostgreSQL verification")
    schema = "studio_test_" + uuid4().hex
    repository = PostgresRepository(dsn, schema=schema)
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
