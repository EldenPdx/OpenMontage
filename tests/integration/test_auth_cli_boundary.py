"""Actual CLI transport retains the physical peer before trusted proxy checks."""

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time

import httpx

from tests.fixtures.studio.repository import test_repository as isolated_repository


def test_cli_trusted_proxy_is_checked_against_the_actual_loopback_peer(tmp_path):
    with isolated_repository() as repository:
        private = tmp_path / "auth.json"
        private.write_text(json.dumps({"mode": "authenticated", "origin": "https://studio.example",
            "endpoint": "https://identity.example", "issuer": "https://identity.example/oidc", "app_id": "cli-test",
            "app_secret": "unused-test-value", "redirect_uri": "https://studio.example/api/auth/callback",
            "post_logout_redirect_uri": "https://studio.example/en", "management_endpoint": "https://identity.example",
            "management_resource": "https://default.logto.app/api", "management_app_id": "cli-test", "management_app_secret": "unused-test-value",
            "database_url": os.environ["STUDIO_TEST_DATABASE_URL"], "database_schema": repository.schema,
            "active_key_id": "test", "encryption_keys": {"test": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()},
            "trusted_proxy_ips": ["127.0.0.1"]}))
        private.chmod(0o600)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        environment = {**os.environ, "OPENMONTAGE_AUTH_CONFIG_FILE": str(private), "OPENMONTAGE_AUTH_MODE": "authenticated",
                       "OPENMONTAGE_PROJECTS_DIR": str(repository.projects_dir)}
        process = subprocess.Popen([sys.executable, "-m", "backlot", "serve", "--port", str(port)], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            origin = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=3) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        ready = client.get(origin + "/api/health", headers={"Host": "studio.example"})
                        if ready.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert process.poll() is None and time.monotonic() < deadline, "Backlot CLI did not start"
                    time.sleep(0.05)
                response = client.get(origin + "/api/health", headers={"Host": "studio.example", "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "studio.example", "X-Forwarded-For": "192.0.2.35"})
                assert response.status_code == 200
                assert response.json() == {"ok": True, "app": "backlot"}
                rejected = client.get(origin + "/api/health", headers={"Host": "studio.example", "X-Forwarded-Proto": "http",
                    "X-Forwarded-Host": "foreign.example", "X-Forwarded-For": "192.0.2.35"})
                assert rejected.status_code == 403
        finally:
            process.terminate()
            process.wait(timeout=10)
