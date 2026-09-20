from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from drawbridge.config import load_settings
from drawbridge.service import DrawbridgeService
from drawbridge.storage import Database


async def run(config_path: Path, project_dir: Path) -> dict[str, object]:
    config_path = config_path.resolve()
    project_dir = project_dir.resolve()
    settings = load_settings(config_path)
    base_dir = config_path.parent.resolve()
    database = Database(settings.resolved_state_dir(base_dir) / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=base_dir)
    registered = await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project_dir),
        compose_file="compose.yaml",
        idempotency_key="register-smoke-001",
    )
    planned = await service.release_plan(
        app="demo",
        environment="staging",
        source_mode="local",
        git_ref="refs/heads/main",
    )
    if planned["status"] != "ok":
        await database.close()
        return {"registered": registered, "planned": planned}
    applied = await service.release_apply(
        plan_id=str(planned["data"]["plan_id"]),
        idempotency_key=f"deploy-smoke-{uuid.uuid4().hex[:12]}",
    )
    if applied.get("status") != "ok":
        await database.close()
        return {"registered": registered, "planned": planned, "applied": applied, "status": {"status": "not_run"}}
    job_id = applied.get("data", {}).get("job_id") if isinstance(applied.get("data"), dict) else None
    if isinstance(job_id, str):
        await service.run_one_job()
        status = await service.release_status(job_id)
    else:
        status = {"status": "error", "error": {"code": "NO_JOB"}}
    await database.close()
    return {"registered": registered, "planned": planned, "applied": applied, "status": status}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.config, args.project))
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("status", {}).get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
