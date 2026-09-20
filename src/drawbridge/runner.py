from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_settings
from .service import DrawbridgeService
from .storage import Database


async def run(config_path: Path, *, once: bool = False) -> None:
    settings = load_settings(config_path)
    base_dir = config_path.parent.resolve()
    database = Database(settings.resolved_state_dir(base_dir) / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=base_dir)
    if once:
        await service.run_one_job()
        return
    while True:
        await service.run_one_job()
        await asyncio.sleep(settings.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Drawbridge mutation queue")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.config, once=args.once))
