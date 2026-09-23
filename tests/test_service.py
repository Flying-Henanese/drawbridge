from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from drawbridge.config import Settings
from drawbridge.errors import DrawbridgeError
from drawbridge.process import ExecutionResult, ExecutionSpec, TerminationReason
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


def mutate_plan(database: Database, plan_id: str, field: str, value: object) -> None:
    with sqlite3.connect(database.path) as connection:
        row = connection.execute("SELECT payload FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload[field] = value
        connection.execute(
            "UPDATE plans SET payload = ? WHERE plan_id = ?",
            (json.dumps(payload, sort_keys=True), plan_id),
        )


def mutate_job(database: Database, job_id: str, field: str, value: object) -> None:
    with sqlite3.connect(database.path) as connection:
        row = connection.execute("SELECT payload FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload[field] = value
        connection.execute(
            "UPDATE jobs SET payload = ? WHERE job_id = ?",
            (json.dumps(payload, sort_keys=True), job_id),
        )


def plan_count(database: Database) -> int:
    with sqlite3.connect(database.path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM plans").fetchone()
        assert row is not None
        return int(row[0])


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
    (project / "README.md").write_text("branch advanced\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "advance branch"], cwd=project, check=True, capture_output=True)
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
async def test_release_plan_rejects_service_topology_change_before_saving_plan(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
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
        idempotency_key="register-topology-001",
    )
    assert registered["status"] == "ok"
    (project / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n  extra:\n    image: alpine:3.20\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add service"], cwd=project, check=True, capture_output=True)

    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )

    assert planned["status"] == "error"
    assert planned["error"]["code"] == "UNSUPPORTED_SERVICE_CHANGE"
    assert plan_count(database) == 0
    assert not any((tmp_path / "state" / "plan-snapshots").iterdir())
    await database.close()


@pytest.mark.asyncio
async def test_operator_compose_and_env_changes_need_only_a_new_plan(tmp_path: Path) -> None:
    project, sha = make_project(tmp_path / "projects")
    operator_dir = tmp_path / "operator" / "demo"
    operator_dir.mkdir(parents=True)
    (operator_dir / "compose.yaml").write_text(
        "services:\n  app:\n    image: ${APP_IMAGE}\n    env_file: .env\n    command: [sleep, '30']\n",
        encoding="utf-8",
    )
    (operator_dir / ".env").write_text("APP_IMAGE=alpine:3.20\nAPP_MODE=first\n", encoding="utf-8")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        concurrency={"min_deploy_interval_seconds": 0},
        allow_simulation=True,
        apps={
            "demo": {
                "git": {"repo_path": str(project), "origin": "https://example.invalid/drawbridge.git"},
                "environments": {
                    "staging": {
                        "project_name": "demo",
                        "deployment_mode": "simulation",
                        "operator_compose": {"directory": str(operator_dir), "file": "compose.yaml"},
                    }
                },
            }
        },
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-operator-001",
        )
        assert registered["status"] == "ok", registered
        first = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert first["status"] == "ok"
        stored_plan = await database.get_plan(first["data"]["plan_id"])
        assert stored_plan is not None
        assert "APP_MODE=first" not in json.dumps(stored_plan)
        (operator_dir / ".env").write_text("APP_IMAGE=alpine:3.20\nAPP_MODE=second\n", encoding="utf-8")
        queued = await service.release_apply(plan_id=first["data"]["plan_id"], idempotency_key="apply-operator-001")
        await service.run_one_job()
        stale = await service.release_status(queued["data"]["job_id"])
        assert stale["data"]["error_code"] == "STALE_PLAN"
        assert await database.get_current_release("demo", "staging") is None

        (operator_dir / "compose.yaml").write_text(
            "services:\n  app:\n    image: ${APP_IMAGE}\n    env_file: .env\n    command: [sleep, '60']\n",
            encoding="utf-8",
        )
        planned = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert planned["status"] == "ok"
        applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="apply-operator-002")
        await service.run_one_job()
        status = await service.release_status(applied["data"]["job_id"])
        assert status["data"]["status"] == "succeeded"
        release = await database.get_current_release("demo", "staging")
        assert release is not None
        assert release["source_sha"] == sha
        assert Path(release["release_dir"], ".env").read_text(encoding="utf-8").endswith("APP_MODE=second\n")
        assert "'60'" in Path(release["compose_file"]).read_text(encoding="utf-8")

        (operator_dir / "docker-compose.yaml").write_text(
            (operator_dir / "compose.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        settings.apps["demo"].environments["staging"].operator_compose.file = "docker-compose.yaml"
        refreshed = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="docker-compose.yaml",
            idempotency_key="register-operator-002",
        )
        assert refreshed["status"] == "ok", refreshed
        assert refreshed["data"]["version"] > registered["data"]["version"]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_operator_docker_release_pins_resolved_local_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = make_project(tmp_path / "projects")
    operator_dir = tmp_path / "operator" / "demo"
    operator_dir.mkdir(parents=True)
    (operator_dir / "compose.yaml").write_text(
        "services:\n  app:\n    image: ${APP_IMAGE}\n    env_file: .env\n    build: .\n",
        encoding="utf-8",
    )
    (operator_dir / ".env").write_text("APP_IMAGE=alpine:3.20\n", encoding="utf-8")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        apps={
            "demo": {
                "git": {"repo_path": str(project), "origin": "https://example.invalid/drawbridge.git"},
                "environments": {
                    "staging": {
                        "project_name": "demo",
                        "operator_compose": {"directory": str(operator_dir), "file": "compose.yaml"},
                    }
                },
            }
        },
        build_profiles={"default": {"mode": "prebuilt"}},
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        from drawbridge.operator_files import materialize_operator_files

        def materialize_for_test(
            config: object, destination: Path, **kwargs: object
        ) -> tuple[str, bool, frozenset[str]]:
            kwargs["require_read_only"] = False
            return materialize_operator_files(config, destination, **kwargs)

        monkeypatch.setattr("drawbridge.service.materialize_operator_files", materialize_for_test)
        monkeypatch.setattr("drawbridge.service.shutil.which", lambda name: f"/usr/bin/{name}")
        seen: list[ExecutionSpec] = []
        image_version = ["a"]

        async def execute(spec: ExecutionSpec) -> ExecutionResult:
            seen.append(spec)
            if spec.label == "operator-compose-resolve-images":
                output = '{"services":{"app":{"image":"alpine:3.20"}}}'
            elif spec.label == "operator-image-inspect":
                output = "sha256:" + image_version[0] * 64 + "\n"
            else:
                output = ""
            return ExecutionResult(0, TerminationReason.EXITED, 1, output, "", len(output), 0, len(output), False)

        monkeypatch.setattr(service.executor, "execute", execute)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-operator-docker-001",
        )
        assert registered["status"] == "ok", registered
        planned = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert planned["status"] == "ok", planned
        assert seen == []
        image_version[0] = "b"
        applied = await service.release_apply(
            plan_id=planned["data"]["plan_id"], idempotency_key="apply-operator-docker-001"
        )
        await service.run_one_job()
        status = await service.release_status(applied["data"]["job_id"])
        assert status["data"]["status"] == "succeeded", status
        assert [spec.label for spec in seen].count("operator-image-inspect") == 1
        assert all("--no-build" in spec.argv for spec in seen if spec.label == "compose-deploy")
        current = await database.get_current_release("demo", "staging")
        assert current is not None
        runtime_compose = yaml.safe_load(Path(current["compose_file"]).read_text(encoding="utf-8"))
        assert runtime_compose["services"]["app"]["image"] == "sha256:" + "b" * 64
        assert any(
            str(Path(current["release_dir"]) / ".env") in spec.argv for spec in seen if spec.label == "compose-deploy"
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_operator_env_cannot_overlap_mcp_editable_file(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    operator_dir = tmp_path / "operator"
    operator_dir.mkdir()
    (operator_dir / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n    env_file: runtime.env\n", encoding="utf-8"
    )
    (operator_dir / "runtime.env").write_text("PASSWORD=private\n", encoding="utf-8")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
        apps={
            "demo": {
                "git": {"repo_path": str(project), "origin": "https://example.invalid/drawbridge.git"},
                "environments": {
                    "staging": {
                        "project_name": "demo",
                        "deployment_mode": "simulation",
                        "operator_compose": {"directory": str(operator_dir)},
                        "editable_files": [{"alias": "secrets", "path": "runtime.env", "display": "text"}],
                    }
                },
            }
        },
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-operator-overlap-001",
        )
        assert registered["error"]["code"] == "FORBIDDEN_OPERATION", registered
        assert await database.get_binding("demo", "staging") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_release_apply_rejects_old_plan_schema_without_queueing(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-old-plan-001",
    )
    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    plan_id = planned["data"]["plan_id"]
    mutate_plan(database, plan_id, "plan_schema_version", 0)

    applied = await service.release_apply(plan_id=plan_id, idempotency_key="apply-old-plan-001")

    assert applied["status"] == "error"
    assert applied["error"]["code"] == "STALE_PLAN"
    assert applied["error"]["retryable"] is True
    assert await database.get_job_by_plan(plan_id) is None
    await database.close()


@pytest.mark.asyncio
async def test_runner_rechecks_plan_schema_before_creating_snapshot(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-runner-schema-001",
    )
    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    applied = await service.release_apply(
        plan_id=planned["data"]["plan_id"],
        idempotency_key="apply-runner-schema-001",
    )
    mutate_job(database, applied["data"]["job_id"], "plan_schema_version", 0)

    await service.run_one_job()
    status = await service.release_status(applied["data"]["job_id"])

    assert status["data"]["status"] == "failed"
    assert status["data"]["error_code"] == "STALE_PLAN"
    assert await database.get_current_release("demo", "staging") is None
    assert not (tmp_path / "releases" / "demo" / "staging").exists()
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service_set", ["other"]),
        ("compose_digest", "0" * 64),
        ("build_declaration_digest", "1" * 64),
    ],
)
async def test_runner_rejects_tampered_snapshot_fingerprint_before_side_effects(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key=f"register-fingerprint-{field}",
    )
    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    plan_id = planned["data"]["plan_id"]
    mutate_plan(database, plan_id, field, value)
    applied = await service.release_apply(plan_id=plan_id, idempotency_key=f"apply-fingerprint-{field}")
    assert applied["status"] == "ok"

    await service.run_one_job()
    status = await service.release_status(applied["data"]["job_id"])

    assert status["data"]["status"] == "failed"
    assert status["data"]["error_code"] == "STALE_PLAN"
    assert field in status["data"]["message"]
    assert await database.get_current_release("demo", "staging") is None
    release_root = tmp_path / "releases" / "demo" / "staging"
    assert not release_root.exists() or not list(release_root.iterdir())
    await database.close()


@pytest.mark.asyncio
async def test_release_apply_compares_configuration_and_build_profile_without_build_services(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-config-digest-001",
    )
    configuration_plan = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    binding = await database.get_binding("demo", "staging")
    assert binding is not None
    with sqlite3.connect(database.path) as connection:
        binding["project_name"] = "changed-without-version"
        connection.execute(
            "UPDATE app_bindings SET payload = ? WHERE app = 'demo' AND environment = 'staging'",
            (json.dumps(binding, sort_keys=True),),
        )
    configuration_apply = await service.release_apply(
        plan_id=configuration_plan["data"]["plan_id"],
        idempotency_key="apply-config-digest-001",
    )
    assert configuration_apply["status"] == "error"
    assert configuration_apply["error"]["code"] == "STALE_PLAN"
    assert "configuration_digest" in configuration_apply["error"]["message"]

    binding["project_name"] = "drawbridge-demo-staging"
    await database.save_binding("demo", "staging", binding, status="deployable")
    profile_plan = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    profile_apply = await service.release_apply(
        plan_id=profile_plan["data"]["plan_id"],
        idempotency_key="apply-profile-digest-001",
    )
    assert profile_apply["status"] == "ok"
    settings.build_profiles["default"].platform = "linux/arm64"
    await service.run_one_job()
    profile_status = await service.release_status(profile_apply["data"]["job_id"])
    assert profile_status["data"]["status"] == "failed"
    assert profile_status["data"]["error_code"] == "STALE_PLAN"
    assert "build_profile_digest" in profile_status["data"]["message"]
    assert await database.get_current_release("demo", "staging") is None
    await database.close()


@pytest.mark.asyncio
async def test_compose_digest_uses_normalized_structure(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-normalized-digest-001",
    )
    first = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    (project / "compose.yaml").write_text(
        "# keys intentionally reordered\nservices:\n  app:\n    command: [sleep, '30']\n    image: alpine:3.20\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "format compose"], cwd=project, check=True, capture_output=True)
    reordered = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    assert reordered["data"]["compose_digest"] == first["data"]["compose_digest"]

    (project / "compose.yaml").write_text(
        "services:\n  app:\n    command: [sleep, '31']\n    image: alpine:3.20\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "change compose"], cwd=project, check=True, capture_output=True)
    changed = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    assert changed["data"]["compose_digest"] != first["data"]["compose_digest"]
    await database.close()


@pytest.mark.asyncio
async def test_release_plan_validates_new_build_declaration_before_saving(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        allow_simulation=True,
        build_profiles={"default": {"mode": "prebuilt"}},
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-build-plan-001",
    )
    (project / "compose.yaml").write_text(
        "services:\n  app:\n    build: .\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add build"], cwd=project, check=True, capture_output=True)

    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )

    assert planned["status"] == "error"
    assert planned["error"]["code"] == "BUILD_UNAVAILABLE"
    assert plan_count(database) == 0
    await database.close()


@pytest.mark.asyncio
async def test_workspace_revision_is_applied_before_plan_and_execution_fingerprints(tmp_path: Path) -> None:
    project, sha = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
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
                        "deployment_mode": "simulation",
                        "editable_files": [
                            {"alias": "compose", "path": "compose.yaml", "display": "yaml"},
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
        idempotency_key="register-revision-plan-001",
    )
    initial_revision = registered["data"]["current_revision"]
    patched = await service.workspace_patch(
        app="demo",
        environment="staging",
        file_alias="compose",
        patch={"services": {"app": {"command": ["sleep", "45"]}}},
        expected_revision=initial_revision,
        base_commit_sha=sha,
        idempotency_key="patch-revision-plan-001",
    )
    revision_id = patched["data"]["revision_id"]
    planned = await service.release_plan(
        app="demo",
        environment="staging",
        source_mode="local",
        git_ref="refs/heads/main",
        workspace_revision=revision_id,
    )
    assert planned["status"] == "ok"
    stored_plan = await database.get_plan(planned["data"]["plan_id"])
    assert stored_plan is not None
    assert stored_plan["workspace_revision"] == revision_id
    assert stored_plan["workspace_revision_digest"]

    tampered = await service.release_plan(
        app="demo",
        environment="staging",
        source_mode="local",
        git_ref="refs/heads/main",
        workspace_revision=revision_id,
    )
    mutate_plan(database, tampered["data"]["plan_id"], "workspace_revision_digest", "2" * 64)
    tampered_apply = await service.release_apply(
        plan_id=tampered["data"]["plan_id"],
        idempotency_key="apply-tampered-revision-001",
    )
    await service.run_one_job()
    tampered_status = await service.release_status(tampered_apply["data"]["job_id"])
    assert tampered_status["data"]["status"] == "failed"
    assert tampered_status["data"]["error_code"] == "STALE_PLAN"
    assert "workspace_revision_digest" in tampered_status["data"]["message"]
    assert await database.get_current_release("demo", "staging") is None

    applied = await service.release_apply(
        plan_id=planned["data"]["plan_id"],
        idempotency_key="apply-revision-plan-001",
    )
    await service.run_one_job()
    status = await service.release_status(applied["data"]["job_id"])
    assert status["data"]["status"] == "succeeded"
    current = await database.get_current_release("demo", "staging")
    assert current is not None
    deployed = yaml.safe_load(Path(current["compose_file"]).read_text(encoding="utf-8"))
    assert deployed["services"]["app"]["command"] == ["sleep", "45"]
    await database.close()


@pytest.mark.asyncio
async def test_register_rejects_unsafe_volume_without_creating_binding(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    (project / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n    volumes:\n      - ../../../../etc:/etc/app/config\n",
        encoding="utf-8",
    )
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
        idempotency_key="register-unsafe-001",
    )

    assert registered["status"] == "error"
    assert registered["error"]["code"] == "INVALID_PARAMETER"
    assert await database.get_binding("demo", "staging") is None
    await database.close()


@pytest.mark.asyncio
async def test_register_uses_admin_runtime_profile_for_privileged_service(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    install_info = tmp_path / "ascend_install.info"
    install_info.write_text("version=1\n", encoding="utf-8")
    (project / "compose.yaml").write_text(
        "services:\n"
        "  npu:\n"
        "    image: ascend.invalid/server:latest\n"
        "    privileged: true\n"
        "    ports: [8118:8118]\n"
        "    volumes:\n"
        f"      - {install_info}:/etc/ascend_install.info:ro\n",
        encoding="utf-8",
    )
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        managed_template_root=str(tmp_path / "templates"),
        managed_data_root=str(tmp_path / "data"),
        auth={"mode": "token", "token": "local-test-token"},
        runtime_profiles={
            "ascend": {
                "privileged_services": ["npu"],
                "ports": {"npu": ["8118:8118"]},
                "host_mounts": [
                    {
                        "service": "npu",
                        "host_path": str(install_info),
                        "container_path": "/etc/ascend_install.info",
                        "read_only": True,
                    }
                ],
            }
        },
        apps={
            "demo": {
                "git": {
                    "repo_path": str(project),
                    "origin": "https://example.invalid/drawbridge.git",
                },
                "environments": {
                    "staging": {
                        "project_name": "drawbridge-demo-staging",
                        "deployment_mode": "simulation",
                        "runtime_profile": "ascend",
                    }
                },
            }
        },
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
        idempotency_key="register-ascend-001",
    )

    assert registered["status"] == "ok"
    binding = await database.get_binding("demo", "staging")
    assert binding is not None
    assert binding["runtime_profile_name"] == "ascend"
    assert binding["runtime_profile"]["privileged_services"] == ["npu"]
    await database.close()


@pytest.mark.asyncio
async def test_reregister_refreshes_admin_runtime_profile_for_same_project(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        managed_template_root=str(tmp_path / "templates"),
        managed_data_root=str(tmp_path / "data"),
        auth={"mode": "token", "token": "local-test-token"},
        runtime_profiles={"ascend": {"privileged_services": ["app"]}},
        apps={
            "demo": {
                "git": {
                    "repo_path": str(project),
                    "origin": "https://example.invalid/drawbridge.git",
                },
                "environments": {
                    "staging": {
                        "project_name": "drawbridge-demo-staging",
                        "deployment_mode": "simulation",
                    }
                },
            }
        },
        allow_simulation=True,
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    service = DrawbridgeService(settings, database, base_dir=tmp_path)
    first = await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-profile-refresh-001",
    )
    assert first["status"] == "ok"
    assert first["data"]["version"] == 1

    settings.apps["demo"].environments["staging"].runtime_profile = "ascend"
    refreshed = await service.app_register(
        app="demo",
        environment="staging",
        project_dir=str(project),
        compose_file="compose.yaml",
        idempotency_key="register-profile-refresh-002",
    )

    assert refreshed["status"] == "ok"
    assert refreshed["data"]["version"] == 2
    binding = await database.get_binding("demo", "staging")
    assert binding is not None
    assert binding["runtime_profile_name"] == "ascend"
    await database.close()


@pytest.mark.asyncio
async def test_reregister_refreshes_deployment_mode_after_config_change(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    config = {
        "state_dir": str(tmp_path / "state"),
        "allowed_project_roots": [str(tmp_path / "projects")],
        "managed_release_root": str(tmp_path / "releases"),
        "auth": {"mode": "token", "token": "local-test-token"},
        "allow_simulation": True,
        "apps": {
            "demo": {
                "git": {"repo_path": str(project), "origin": "https://example.invalid/drawbridge.git"},
                "environments": {
                    "staging": {"project_name": "drawbridge-demo-staging", "deployment_mode": "simulation"}
                },
            }
        },
    }
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        simulation = DrawbridgeService(Settings.model_validate(config), database, base_dir=tmp_path)
        first = await simulation.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-mode-refresh-001",
        )
        assert first["status"] == "ok"
        assert first["data"]["version"] == 1
        original = await database.get_binding("demo", "staging")
        assert original is not None
        assert original["deployment_mode"] == "simulation"
        old_plan = await simulation.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert old_plan["status"] == "ok"

        config["apps"]["demo"]["environments"]["staging"]["deployment_mode"] = "docker"
        docker = DrawbridgeService(Settings.model_validate(config), database, base_dir=tmp_path)
        refreshed = await docker.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-mode-refresh-002",
        )
        assert refreshed["status"] == "ok"
        assert refreshed["data"]["version"] == 2
        binding = await database.get_binding("demo", "staging")
        assert binding is not None
        assert binding["deployment_mode"] == "docker"
        stale = await docker.release_apply(
            plan_id=old_plan["data"]["plan_id"], idempotency_key="apply-old-mode-plan-001"
        )
        assert stale["status"] == "error"
        assert stale["error"]["code"] == "STALE_PLAN"
        new_plan = await docker.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert new_plan["status"] == "ok"

        unchanged = await docker.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-mode-refresh-003",
        )
        assert unchanged["status"] == "ok"
        assert unchanged["data"]["version"] == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_release_snapshot_revalidates_volumes_before_executor(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
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
        idempotency_key="register-snapshot-001",
    )
    assert registered["status"] == "ok"

    (project / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n    volumes:\n      - ../../../../etc:/etc/app/config\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add unsafe volume"], cwd=project, check=True, capture_output=True)
    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    assert planned["status"] == "error"
    assert planned["error"]["code"] == "INVALID_PARAMETER"
    assert "parent traversal" in planned["error"]["message"]
    await database.close()


@pytest.mark.asyncio
async def test_release_snapshot_revalidates_host_capabilities_before_executor(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
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
        idempotency_key="register-capability-001",
    )
    assert registered["status"] == "ok"

    (project / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n    volumes_from: [other]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add host capability"], cwd=project, check=True, capture_output=True)
    planned = await service.release_plan(
        app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
    )
    assert planned["status"] == "error"
    assert planned["error"]["code"] == "INVALID_PARAMETER"
    assert "volumes_from" in planned["error"]["message"]
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "build_failed", "profile_changed", "prepare_failed", "deploy_failed"])
async def test_release_builds_each_registered_service_from_frozen_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    project, _ = make_project(tmp_path / "projects")
    (project / "api").mkdir()
    (project / "worker").mkdir()
    (project / "api" / "Dockerfile").write_text(
        "# syntax=docker/dockerfile:1.7\nFROM scratch\nLABEL version=committed\n", encoding="utf-8"
    )
    (project / "worker" / "Dockerfile").write_text("FROM scratch\nLABEL role=worker\n", encoding="utf-8")
    (project / "compose.yaml").write_text(
        "services:\n"
        "  api:\n"
        "    build: {context: ./api, dockerfile: Dockerfile}\n"
        "  worker:\n"
        "    build: {context: ./worker, dockerfile: Dockerfile}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "build services"], cwd=project, check=True, capture_output=True)
    socket_path = tmp_path / "buildkit.sock"
    socket_path.touch()
    real_is_socket = Path.is_socket
    monkeypatch.setattr(Path, "is_socket", lambda path: path == socket_path or real_is_socket(path))
    database: Database | None = None
    try:
        settings = Settings(
            state_dir=str(tmp_path / "state"),
            allowed_project_roots=[str(tmp_path / "projects")],
            managed_release_root=str(tmp_path / "releases"),
            managed_template_root=str(tmp_path / "templates"),
            managed_data_root=str(tmp_path / "data"),
            auth={"mode": "token", "token": "local-test-token"},
            build_profiles={
                "default": {
                    "mode": "buildkit",
                    "buildkit_socket": f"unix://{socket_path}",
                    "targets": {
                        "api": {"context": "api"},
                        "worker": {"context": "worker"},
                    },
                }
            },
        )
        database = Database(tmp_path / "state" / "state.db")
        await database.initialize()
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        seen: list[ExecutionSpec] = []
        imported = False

        async def execute(spec: ExecutionSpec) -> ExecutionResult:
            nonlocal imported
            seen.append(spec)
            if spec.label == "image-build":
                output = next(item for item in spec.argv if item.startswith("type=docker,name="))
                archive = Path(output.split(",dest=", 1)[1])
                archive.write_bytes(b"docker archive fixture")
                assert str(spec.cwd).startswith(str(tmp_path / "releases"))
                dockerfile_arg = next(item for item in spec.argv if item.startswith("dockerfile="))
                dockerfile_name = next(item for item in spec.argv if item.startswith("filename=")).removeprefix(
                    "filename="
                )
                dockerfile = Path(dockerfile_arg.removeprefix("dockerfile=")) / dockerfile_name
                assert "dirty" not in dockerfile.read_text(encoding="utf-8")
                assert "syntax=" not in dockerfile.read_text(encoding="utf-8")
                if outcome == "build_failed" and archive.name == "image-1.tar":
                    return ExecutionResult(1, TerminationReason.EXITED, 1, "", "build failed", 0, 12, 12, False)
                return ExecutionResult(0, TerminationReason.EXITED, 1, "", "", 0, 0, 0, False)
            if spec.label == "image-inspect-before":
                return ExecutionResult(1, TerminationReason.EXITED, 1, "", "No such image", 0, 13, 13, False)
            if spec.label == "image-import":
                imported = True
            if spec.label == "image-identify":
                assert imported
                image_id = "sha256:" + ("a" if spec.argv[-1].endswith("-0") else "b") * 64
                return ExecutionResult(0, TerminationReason.EXITED, 1, image_id + "\n", "", 72, 0, 72, False)
            if spec.label == "compose-deploy" and outcome == "deploy_failed":
                return ExecutionResult(1, TerminationReason.EXITED, 1, "", "compose failed", 0, 14, 14, False)
            return ExecutionResult(0, TerminationReason.EXITED, 1, "", "", 0, 0, 0, False)

        monkeypatch.setattr("drawbridge.service.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(service.executor, "execute", execute)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-build-001",
        )
        assert registered["status"] == "ok"
        planned = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert planned["status"] == "ok"
        (project / "api" / "Dockerfile").write_text("FROM scratch\nLABEL version=dirty\n", encoding="utf-8")
        applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="apply-build-001")
        if outcome == "profile_changed":
            settings.build_profiles["default"].platform = "linux/arm64"
        if outcome == "prepare_failed":

            def fail_runtime_compose(*args: object) -> None:
                raise DrawbridgeError("DEPLOY_FAILED", "runtime compose preparation failed")

            monkeypatch.setattr("drawbridge.service.write_trusted_compose", fail_runtime_compose)
        await service.run_one_job()
        status = await service.release_status(applied["data"]["job_id"])
        if outcome == "profile_changed":
            assert status["data"]["status"] == "failed"
            assert status["data"]["error_code"] == "STALE_PLAN"
            assert not any(item.label == "image-build" for item in seen)
            return
        if outcome == "build_failed":
            assert status["data"]["status"] == "failed"
            assert status["data"]["error_code"] == "BUILD_FAILED"
            assert await database.get_current_release("demo", "staging") is None
            assert not any(item.label == "compose-deploy" for item in seen)
            assert any(item.label == "image-cleanup" for item in seen)
            return
        if outcome == "prepare_failed":
            assert status["data"]["status"] == "failed"
            assert status["data"]["error_code"] == "DEPLOY_FAILED"
            assert len([item for item in seen if item.label == "image-cleanup"]) == 2
            assert not any(item.label == "compose-deploy" for item in seen)
            release_root = tmp_path / "releases" / "demo" / "staging"
            assert not list(release_root.iterdir())
            return
        if outcome == "deploy_failed":
            assert status["data"]["status"] == "failed"
            assert status["data"]["error_code"] == "DEPLOY_FAILED"
            assert "release artifacts retained at" in status["data"]["message"]
            assert not any(item.label == "image-cleanup" for item in seen)
            release_dirs = list((tmp_path / "releases" / "demo" / "staging").iterdir())
            assert len(release_dirs) == 1
            assert list(release_dirs[0].glob(".drawbridge-images-*/*.tar"))
            return
        assert status["data"]["status"] == "succeeded"
        current = await database.get_current_release("demo", "staging")
        assert current is not None
        runtime_compose = yaml.safe_load(Path(current["compose_file"]).read_text(encoding="utf-8"))
        assert all("build" not in service for service in runtime_compose["services"].values())
        assert all(service["image"].startswith("sha256:") for service in runtime_compose["services"].values())
        assert len([item for item in seen if item.label == "image-build"]) == 2
        assert len([item for item in seen if item.label == "image-import"]) == 2
        assert any(item.label == "compose-deploy" for item in seen)
    finally:
        if database is not None:
            await database.close()


@pytest.mark.asyncio
async def test_registration_rejects_unregistered_build_options(tmp_path: Path) -> None:
    project, _ = make_project(tmp_path / "projects")
    (project / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (project / "compose.yaml").write_text(
        "services:\n  app:\n    build:\n      context: .\n      args: {SECRET: value}\n",
        encoding="utf-8",
    )
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        build_profiles={"default": {"mode": "buildkit", "buildkit_socket": "unix:///run/buildkit/test.sock"}},
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-build-args-001",
        )
        assert registered["status"] == "error"
        assert "unsupported build options" in registered["error"]["message"]
        assert await database.get_binding("demo", "staging") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_release_with_prebuilt_image_skips_buildkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = make_project(tmp_path / "projects")
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        build_profiles={"default": {"mode": "prebuilt"}},
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        seen: list[str] = []

        async def execute(spec: ExecutionSpec) -> ExecutionResult:
            seen.append(spec.label)
            return ExecutionResult(0, TerminationReason.EXITED, 1, "", "", 0, 0, 0, False)

        monkeypatch.setattr("drawbridge.service.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(service.executor, "execute", execute)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-prebuilt-001",
        )
        assert registered["status"] == "ok"
        planned = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="apply-prebuilt-001")
        await service.run_one_job()
        status = await service.release_status(applied["data"]["job_id"])
        assert status["data"]["status"] == "succeeded"
        assert seen == ["compose-deploy"]
        current = await database.get_current_release("demo", "staging")
        assert current is not None
        assert current["image_services"] == {"app": "alpine:3.20"}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_trusted_compose_prefers_existing_image_over_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = make_project(tmp_path / "projects")
    (project / "compose.yaml").write_text(
        "services:\n"
        "  app:\n"
        "    image: ${APP_IMAGE:-alpine:3.20}\n"
        "    build:\n"
        "      context: .\n"
        "      args: {APP_MODE: production}\n"
        "    network_mode: host\n"
        "    cap_add: [SYS_ADMIN]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "trusted compose"], cwd=project, check=True, capture_output=True)
    approved_digest = hashlib.sha256((project / "compose.yaml").read_bytes()).hexdigest()
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        allowed_project_roots=[str(tmp_path / "projects")],
        managed_release_root=str(tmp_path / "releases"),
        auth={"mode": "token", "token": "local-test-token"},
        concurrency={"min_deploy_interval_seconds": 0},
        runtime_profiles={
            "trusted": {
                "approved_compose_digests": [approved_digest],
                "prefer_prebuilt_images": True,
            }
        },
        apps={
            "demo": {
                "git": {
                    "repo_path": str(project),
                    "origin": "https://example.invalid/drawbridge.git",
                },
                "environments": {
                    "staging": {
                        "project_name": "demo",
                        "runtime_profile": "trusted",
                    }
                },
            }
        },
        build_profiles={"default": {"mode": "prebuilt"}},
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.initialize()
    try:
        service = DrawbridgeService(settings, database, base_dir=tmp_path)
        seen: list[str] = []

        async def execute(spec: ExecutionSpec) -> ExecutionResult:
            seen.append(spec.label)
            return ExecutionResult(0, TerminationReason.EXITED, 1, "", "", 0, 0, 0, False)

        monkeypatch.setattr("drawbridge.service.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(service.executor, "execute", execute)
        registered = await service.app_register(
            app="demo",
            environment="staging",
            project_dir=str(project),
            compose_file="compose.yaml",
            idempotency_key="register-trusted-001",
        )
        assert registered["status"] == "ok"
        planned = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="apply-trusted-001")
        await service.run_one_job()
        status = await service.release_status(applied["data"]["job_id"])

        assert status["data"]["status"] == "succeeded"
        assert seen == ["compose-deploy"]
        current = await database.get_current_release("demo", "staging")
        assert current is not None
        assert current["built_images"] == {}
        runtime_compose = yaml.safe_load(Path(current["compose_file"]).read_text(encoding="utf-8"))
        assert runtime_compose["services"]["app"]["build"]["args"] == {"APP_MODE": "production"}

        (project / "compose.yaml").write_text(
            "services:\n  app:\n    image: alpine:3.20\n    build: .\n    command: [sleep, '60']\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "compose.yaml"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "change trusted compose"], cwd=project, check=True, capture_output=True)
        seen.clear()
        changed_plan = await service.release_plan(
            app="demo", environment="staging", source_mode="local", git_ref="refs/heads/main"
        )
        assert changed_plan["status"] == "error"
        assert changed_plan["error"]["code"] == "INVALID_PARAMETER"
        assert "digest is not approved" in changed_plan["error"]["message"]
        assert seen == []
    finally:
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
