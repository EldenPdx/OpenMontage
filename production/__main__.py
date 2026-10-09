"""Backend-only Studio lifecycle commands; deployment remains an operator action."""

import argparse
import asyncio
import os
from pathlib import Path
import signal

from lib.env_loader import load_env
from lib.paths import PROJECTS_DIR
from production.contracts import ContractViolation
from production.pi_config import load_studio_config
from production.recovery import RecoveryService
from production.repository import PostgresRepository
from production.worker import Worker


async def serve(repository, config, runtime_root):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    await Worker(repository, config, runtime_root, PROJECTS_DIR).run_forever(stop)


def main():
    parser = argparse.ArgumentParser(description="OpenMontage browser production worker")
    parser.add_argument("command", choices=("migrate", "worker", "recover"))
    args = parser.parse_args()
    load_env()
    dsn = os.environ.get("STUDIO_DATABASE_URL")
    if not dsn:
        parser.error("Set STUDIO_DATABASE_URL on the backend before starting Studio")
    repository = PostgresRepository(dsn)
    runtime_root = Path(__file__).resolve().parents[1] / ".runtime/studio"
    try:
        if args.command == "migrate":
            print(f"Studio database schema version {repository.migrate()}")
        elif args.command == "recover":
            count = asyncio.run(RecoveryService(repository, runtime_root, PROJECTS_DIR).recover())
            print(f"Confirmed exit for {count} recovered Pi processes")
        else:
            config = load_studio_config()
            if not config.enabled:
                parser.error("Enable Studio in trusted backend configuration before starting the worker")
            asyncio.run(serve(repository, config, runtime_root))
    except ContractViolation as error:
        parser.exit(1, f"Studio {error.code}: {error}\n")


if __name__ == "__main__":
    main()
