"""One entry point: inherit host packages privately, install locked extras, run lifecycle."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "auth-runtime/.state"


def execute(arguments, environment):
    try:
        result = subprocess.run(arguments, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise SystemExit("Private identity bootstrap timed out; dependency installation is bounded") from None
    descriptor = os.open(STATE / "bootstrap.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit("Private identity bootstrap failed; inspect auth-runtime/.state/bootstrap.log")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true",
                        help="Allow cold dependency and image downloads; default is offline without a process proxy")
    options, command = parser.parse_known_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    private_python = STATE / "venv/bin/python"
    environment = {**os.environ, "UV_CACHE_DIR": str(STATE / "uv-cache"), "PYTHONDONTWRITEBYTECODE": "1"}
    uv = shutil.which("uv")
    if not private_python.exists():
        if not uv:
            raise SystemExit("Install uv to prepare the separate auth venv")
        execute([uv, "venv", "--python", sys.executable, "--system-site-packages", str(STATE / "venv")], environment)
        site = execute([str(private_python), "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], environment).strip()
        Path(site, "host-packages.pth").write_text(sysconfig.get_path("purelib") + "\n")
    requirements = ROOT / "requirements-auth.txt"
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = STATE / "dependencies.sha256"
    if not marker.exists() or marker.read_text().strip() != fingerprint:
        if not uv:
            raise SystemExit("Install uv to install the locked private auth dependencies")
        arguments = [uv, "pip", "install", "--python", str(private_python), "-r", str(requirements)]
        if not (options.online or environment.get("HTTPS_PROXY") or environment.get("https_proxy")):
            arguments.append("--offline")
        execute(arguments, environment)
        marker.write_text(fingerprint + "\n")
    # Lifecycle emits only safe summaries; detailed command output stays in private files.
    command = command or ["up"]
    if options.online:
        command.append("--online")
    result = subprocess.run([str(private_python), "-B", str(ROOT / "auth-runtime/manage.py"), *command], cwd=ROOT, env=environment)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
