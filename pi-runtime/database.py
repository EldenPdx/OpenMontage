#!/usr/bin/env python3
"""Create private local database credentials or check the real PostgreSQL service."""

import argparse
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/studio"


def prepare():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    password_file = STATE / "postgres.password"
    if not password_file.exists():
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    password = password_file.read_text().strip()
    password_file.chmod(0o600)
    if not password or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in password):
        raise SystemExit("Invalid managed PostgreSQL password file")
    for filename, host in [("database.env", "postgres:5432"), ("database-local.env", "127.0.0.1:55432")]:
        path = STATE / filename
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(f"STUDIO_DATABASE_URL=postgresql://openmontage:{password}@{host}/openmontage_studio\n")
        path.chmod(0o600)
    print("Private development database files ready in .runtime/studio/")


def check():
    try:
        import psycopg
    except ImportError:
        raise SystemExit("PostgreSQL driver missing. Run make install-studio.")
    dsn = os.environ.get("STUDIO_DATABASE_URL")
    if not dsn:
        path = STATE / "database-local.env"
        if not path.is_file():
            raise SystemExit("Database configuration missing. Run make studio-db.")
        dsn = path.read_text().strip().split("=", 1)[1]
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
            version = connection.execute("SHOW server_version").fetchone()[0]
    except psycopg.Error:
        raise SystemExit("PostgreSQL is not ready. Check the database service and backend configuration.")
    print(f"PostgreSQL {version}: ready")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "check"])
    (prepare if parser.parse_args().command == "prepare" else check)()
