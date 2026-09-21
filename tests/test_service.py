from __future__ import annotations

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
    assert planned["status"] == "ok"

    seen: list[ExecutionSpec] = []

    async def execute(spec: ExecutionSpec) -> ExecutionResult:
        seen.append(spec)
        raise AssertionError("unsafe snapshot must fail before external execution")

    service.executor.execute = execute
    applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="apply-snapshot-001")
    assert applied["status"] == "ok"
    await service.run_one_job()
    status = await service.release_status(applied["data"]["job_id"])

    assert status["status"] == "ok"
    assert status["data"]["status"] == "failed"
    assert status["data"]["error_code"] == "INVALID_PARAMETER"
    assert seen == []
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
    assert planned["status"] == "ok"

    seen: list[ExecutionSpec] = []

    async def execute(spec: ExecutionSpec) -> ExecutionResult:
        seen.append(spec)
        raise AssertionError("unsafe snapshot must fail before external execution")

    service.executor.execute = execute
    applied = await service.release_apply(plan_id=planned["data"]["plan_id"], idempotency_key="apply-capability-001")
    assert applied["status"] == "ok"
    await service.run_one_job()
    status = await service.release_status(applied["data"]["job_id"])

    assert status["data"]["status"] == "failed"
    assert status["data"]["error_code"] == "INVALID_PARAMETER"
    assert "volumes_from" in status["data"]["message"]
    assert seen == []
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
