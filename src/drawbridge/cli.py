from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import load_settings, write_default_config
from .selfcheck import run_self_check
from .storage import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="drawbridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-config")
    init.add_argument("path", type=Path)

    check = subparsers.add_parser("self-check")
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--role", choices=("gateway", "runner", "all"), default="all")

    db = subparsers.add_parser("init-db")
    db.add_argument("--config", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "init-config":
        write_default_config(args.path)
        print(args.path)
        return
    settings = load_settings(args.config)
    base_dir = args.config.parent.resolve()
    if args.command == "self-check":
        print(json.dumps(run_self_check(settings, base_dir=base_dir, role=args.role), indent=2, sort_keys=True))
        return
    if args.command == "init-db":
        database = Database(settings.resolved_state_dir(base_dir) / "state.db")
        asyncio.run(database.initialize())
        print(settings.resolved_state_dir(base_dir) / "state.db")
