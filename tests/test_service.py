from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drawbridge.config import Settings
from drawbridge.service import DrawbridgeService
from drawbridge.storage import Database


def make_project(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    project = root / "demo"
    project.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "drawbridge@example.invalid"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Drawbridge Test"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/drawbridge.git"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    (project / "compose.yaml").write_text(
        'services:\n  app:\n    image: alpine:3.20\n    command: ["sleep", "30"]\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()
    return project, sha


@pytest.mark.asyncio
async def test_register_plan_apply_simulation_is_idempotent(tmp_path: Path) -> None:
    project, sha = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        managed_template_root=str(tmp_path / "templates"),
        managed_data_root=str(tmp_path / "data"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)

    registered = await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-001",
    )
    assert registered["status"] == "ok"
    assert registered["data"]["services"] == ["app"]

    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    assert planned["status"] == "ok"
    assert planned["data"]["commit_sha"] == sha
    (project / "compose.yaml").write_text("not valid compose\n", encoding="utf-8")

    applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="deploy-001")
    duplicate = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="deploy-002")
    assert applied["data"]["job_id"] == duplicate["data"]["job_id"]

    await service.run_one_job()
    status = await service.release_status(applied["data"]["job_id"])
    assert status["data"]["status"] == "succeeded"
    assert status["data"]["release_id"]
    replayed = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="deploy-003")
    assert replayed["data"]["job_id"] == applied["data"]["job_id"]
    assert replayed["data"]["created"] is False

    current = await database.get_current_release("demo", "staging")
    assert current is not None
    assert current["source_sha"] == sha
    assert "services:" in Path(current["compose_file"]).read_text(encoding="utf-8")
    await database.close()


@pytest.mark.asyncio
async def test_workspace_patch_requires_revision_and_preserves_base_anchor(tmp_path: Path) -> None:
    project, sha = make_project(tmp_path / "projects")
    (project / "config.yaml").write_text("port: 8080\n", encoding="utf-8")
    (project / "runtime.yaml").write_text("workers: 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "config.yaml", "runtime.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "config"], cwd=project, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        managed_template_root=str(tmp_path / "templates"),
        managed_data_root=str(tmp_path / "data"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
        apps={
            "demo": {
                "git": {
                    "repo_path": str(project),
                    "origin": "https://example.invalid/drawbridge.git",
                    "allowed_ref_patterns": [r"^refs/heads/main$"],
                },
                "environments": {
                    "staging": {
                        "project_name": "drawbridge-demo-staging",
                        "editable_files": [
                            {"alias": "app_config", "path": "config.yaml", "display": "yaml"},
                            {"alias": "runtime", "path": "runtime.yaml", "display": "yaml"},
                        ],
                    }
                },
            }
        },
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)

    registered = await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-001",
    )
    revision = registered["data"]["current_revision"]
    git_status = await service.git_status(app="demo")
    assert git_status["status"] == "ok"
    git_log = await service.git_log(app="demo", git_ref="refs/heads/main", count=5)
    assert git_log["status"] == "ok"
    assert git_log["data"]["commit_sha"] == sha
    config_read = await service.config_read(app="demo", file_alias="app_config")
    assert config_read["status"] == "ok"
    assert "port: 8080" in config_read["data"]["content"]
    config_valid = await service.config_validate(app="demo", file_alias="app_config")
    assert config_valid["status"] == "ok"
    patched = await service.workspace_patch(
        app="demo",
        environment="staging",
        file_alias="app_config",
        patch={"port": 9090},
        expected_revision=revision,
        base_commit_sha=sha,
        idempotency_key="patch-0001",
    )
    assert patched["status"] == "ok"
    assert patched["data"]["base_commit_sha"] == sha
    assert "port: 9090" in (project / "config.yaml").read_text(encoding="utf-8")
    first_revision = await database.get_revision(patched["data"]["revision_id"])
    assert first_revision is not None
    assert set(first_revision["files"]) == {"app_config", "runtime"}
    assert first_revision["files"]["runtime"]["pre_digest"] == first_revision["files"]["runtime"]["post_digest"]

    chained = await service.workspace_patch(
        app="demo",
        environment="staging",
        file_alias="app_config",
        patch={"port": 9091},
        expected_revision=patched["data"]["revision_id"],
        idempotency_key="patch-0001b",
    )
    assert chained["status"] == "ok"
    assert chained["data"]["diff"]["before_sha256"] != chained["data"]["diff"]["after_sha256"]
    second_revision = await database.get_revision(chained["data"]["revision_id"])
    assert second_revision is not None
    assert set(second_revision["files"]) == {"app_config", "runtime"}

    stale = await service.workspace_patch(
        app="demo",
        environment="staging",
        file_alias="app_config",
        patch={"port": 8081},
        expected_revision=revision,
        idempotency_key="patch-0002",
    )
    assert stale["error"]["code"] == "PATCH_BASE_MISMATCH"
    await database.close()
