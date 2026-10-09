#!/usr/bin/env python3
"""Probe the installed official Pi through its public CLI and RPC interface."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".runtime/pi/source/packages/coding-agent/dist/bundle/cli.js"


def main():
    if not CLI.is_file():
        raise SystemExit("Pi is not installed. Run make studio-pi.")
    with tempfile.TemporaryDirectory(prefix="openmontage-pi-check-") as directory:
        base = Path(directory)
        env = {
            "PATH": os.environ["PATH"], "HOME": str(base / "home"),
            "PI_CODING_AGENT_DIR": str(base / "agent"), "PI_OFFLINE": "1",
        }
        version = subprocess.check_output(
            ["node", str(CLI), "--version"], env=env, text=True, timeout=30,
        ).strip()
        assert version == "1.1.0", f"Expected official Pi 1.1.0, got {version}"
        requests = [
            {"id": "state", "type": "get_state"},
            {"id": "commands", "type": "get_commands"},
        ]
        result = subprocess.run(
            ["node", str(CLI), "--mode", "rpc", "--offline", "--no-tools",
             "--no-extensions", "--no-mcp", "--no-skills", "--no-themes",
             "--no-context-files", "--no-prompt-templates", "--no-approve",
             "--session-dir", str(base / "sessions")],
            input="".join(json.dumps(item) + "\n" for item in requests),
            env=env, cwd=base, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, "Pi did not shut down cleanly after stdin EOF"
        responses = {
            item.get("id"): item for line in result.stdout.split("\n")
            if line and (item := json.loads(line)).get("type") == "response"
        }
        for request in requests:
            assert responses.get(request["id"], {}).get("success"), request["type"]
        session = responses["state"]["data"].get("sessionFile")
        if session:
            assert Path(session).is_relative_to(base / "sessions"), "Session escaped managed directory"
        print(json.dumps({"pi": version, "rpc": "ready", "shutdown": "clean"}))


if __name__ == "__main__":
    main()
