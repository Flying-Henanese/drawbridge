from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from drawbridge.compose import ComposeError, parse_compose
from drawbridge.config import DataMountConfig, OperatorComposeConfig, Settings
from drawbridge.models import (
    HttpRequestInput,
    IdempotencyInput,
    ReleasePlanInput,
    validate_project_path,
    validate_ref,
    validate_subdir,
)
from drawbridge.operator_files import OperatorFileError, materialize_operator_files


def test_release_plan_rejects_extra_fields_and_string_numbers() -> None:
    with pytest.raises(ValidationError):
        ReleasePlanInput.model_validate(
            {
                "app": "demo",
                "environment": "staging",
                "source_mode": "local",
                "git_ref": "refs/heads/main",
                "count": "20",
            }
        )

    with pytest.raises(ValidationError):
        ReleasePlanInput.model_validate(
            {
                "app": "demo",
                "environment": "staging",
                "source_mode": "local",
                "git_ref": "refs/heads/main",
                "unexpected": True,
            }
        )


def test_request_fields_reject_shell_syntax_and_invalid_method() -> None:
    with pytest.raises(ValidationError):
        IdempotencyInput(idempotency_key="bad;touch /tmp/pwned")

    with pytest.raises(ValidationError):
        HttpRequestInput(url="http://127.0.0.1:8080/health", method="TRACE")

    valid = HttpRequestInput(url="http://127.0.0.1:8080/health", method="GET")
    assert valid.method == "GET"


def test_operator_compose_requires_a_fixed_entry_and_external_directory(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "compose.yaml").write_text("services:\n  app:\n    image: alpine:3.20\n", encoding="utf-8")
    config = OperatorComposeConfig(directory=str(project), file="compose.yaml")
    with pytest.raises(OperatorFileError, match="outside managed project"):
        materialize_operator_files(
            config, tmp_path / "snapshot", forbidden_roots=[tmp_path / "projects"], require_read_only=False
        )
    with pytest.raises(OperatorFileError, match="administrator-owned"):
        materialize_operator_files(config, tmp_path / "snapshot", forbidden_roots=[])

    with pytest.raises(ValidationError, match="file must be a file name"):
        OperatorComposeConfig(directory=str(tmp_path), file="subdir/compose.yaml")


def test_operator_compose_rejects_escaping_env_file(tmp_path: Path) -> None:
    operator_dir = tmp_path / "operator"
    operator_dir.mkdir()
    (operator_dir / "compose.yaml").write_text(
        "services:\n  app:\n    image: alpine:3.20\n    env_file: ../outside.env\n", encoding="utf-8"
    )
    config = OperatorComposeConfig(directory=str(operator_dir), file="compose.yaml")
    with pytest.raises(OperatorFileError, match="static relative path"):
        materialize_operator_files(config, tmp_path / "snapshot", forbidden_roots=[], require_read_only=False)


def test_ref_and_subdir_validation_are_shape_checks() -> None:
    assert validate_ref("refs/heads/agent/feature-1") == "refs/heads/agent/feature-1"
    assert validate_ref("a" * 40) == "a" * 40

    with pytest.raises(ValueError):
        validate_ref("refs/heads/main; echo unsafe")
    with pytest.raises(ValueError):
        validate_subdir("../outside")
    with pytest.raises(ValueError):
        validate_subdir("config/../secret")


def test_project_path_requires_an_existing_directory_inside_allowed_root(tmp_path) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)

    assert validate_project_path(str(project), [root]) == project.resolve()

    with pytest.raises(ValueError):
        validate_project_path(str(root.parent), [root])


def _write_compose(project: Path, volumes: list[object]) -> Path:
    compose = project / "compose.yaml"
    compose.write_text(
        f"services:\n  app:\n    image: example.invalid/drawbridge/test:latest\n    volumes: {volumes!r}\n",
        encoding="utf-8",
    )
    return compose


@pytest.mark.parametrize(
    "volume",
    [
        "../../../../etc:/etc/app/config",
        "./../etc:/etc/app/config",
        {"type": "bind", "source": "../../../../etc", "target": "/etc/app/config"},
    ],
)
def test_compose_rejects_volume_parent_traversal(tmp_path: Path, volume: object) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ComposeError, match="parent traversal"):
        parse_compose(_write_compose(project, [volume]), project)


def test_compose_rejects_absolute_and_symlinked_host_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config").write_text("secret", encoding="utf-8")
    link = project / "linked"
    link.symlink_to(outside, target_is_directory=True)

    for volume in (
        f"{outside}:/etc/app/config",
        "./linked/config:/etc/app/config",
        {"type": "bind", "source": "./linked/config", "target": "/etc/app/config"},
    ):
        with pytest.raises(ComposeError, match="(absolute host volume|symlink)"):
            parse_compose(_write_compose(project, [volume]), project)


def test_compose_rejects_unreviewed_long_volume_options(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source"
    source.mkdir()

    with pytest.raises(ComposeError, match="unsupported volume key bind"):
        parse_compose(
            _write_compose(
                project,
                [
                    {
                        "type": "bind",
                        "source": "./source",
                        "target": "/data",
                        "bind": {"propagation": "rshared"},
                    }
                ],
            ),
            project,
        )


def test_compose_rejects_a_symlinked_compose_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    real_compose = _write_compose(project, [])
    linked_compose = project / "compose-link.yaml"
    linked_compose.symlink_to(real_compose)

    with pytest.raises(ComposeError, match="symlink"):
        parse_compose(linked_compose, project)


def test_compose_allows_named_volumes_and_safe_relative_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.ini").write_text("safe", encoding="utf-8")

    spec = parse_compose(
        _write_compose(
            project,
            [
                "named-volume:/var/lib/app",
                "./config.ini:/etc/app/config.ini:ro",
                {"type": "bind", "source": "./config.ini", "target": "/etc/app/other.ini", "read_only": True},
            ],
        ),
        project,
    )

    assert spec.services == ("app",)


def test_compose_allows_only_an_exact_registered_data_mount(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data_mount = tmp_path / "managed-data"
    data_mount.mkdir()
    allowed = {
        "host_path": str(data_mount),
        "container_path": "/var/lib/app/data",
        "read_only": True,
    }

    spec = parse_compose(
        _write_compose(
            project,
            [
                f"{data_mount}:/var/lib/app/data:ro",
            ],
        ),
        project,
        allowed_data_mounts=[allowed],
    )
    assert spec.services == ("app",)

    for volume in (
        f"{data_mount}:/var/lib/app/data:rw",
        {"type": "bind", "source": str(data_mount), "target": "/var/lib/app/data", "read_only": False},
        f"{data_mount}:/var/lib/app/other:ro",
    ):
        with pytest.raises(ComposeError, match="registered data mount"):
            parse_compose(_write_compose(project, [volume]), project, allowed_data_mounts=[allowed])


def test_data_mount_read_only_is_strict() -> None:
    mount = DataMountConfig(
        name="uploads",
        host_path="/srv/drawbridge/data/uploads",
        container_path="/var/lib/app/uploads",
        read_only=True,
    )
    assert mount.read_only is True

    with pytest.raises(ValidationError):
        DataMountConfig(
            name="uploads",
            host_path="/srv/drawbridge/data/uploads",
            container_path="/var/lib/app/uploads",
            read_only="true",
        )


@pytest.mark.parametrize(
    ("service_field", "expected_field"),
    [
        ("volumes_from: [other]", "volumes_from"),
        ("userns_mode: host", "userns_mode"),
        ("uts: host", "uts"),
        ("network_mode: host", "network_mode"),
        ("devices: [/dev/npu0:/dev/npu0]", "devices"),
        ("cap_add: [SYS_ADMIN]", "cap_add"),
    ],
)
def test_compose_rejects_unreviewed_service_capabilities(
    tmp_path: Path, service_field: str, expected_field: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        f"services:\n  app:\n    image: alpine:3.20\n    {service_field}\n",
        encoding="utf-8",
    )

    with pytest.raises(ComposeError, match=expected_field):
        parse_compose(compose, project)


def test_compose_rejects_top_level_volume_host_options_but_allows_managed_named_volume(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    volumes: [cache:/var/lib/app]\n"
        "volumes:\n"
        "  cache:\n"
        "    driver_opts:\n"
        "      type: none\n"
        "      device: /etc\n"
        "      o: bind\n",
        encoding="utf-8",
    )

    with pytest.raises(ComposeError, match="driver_opts"):
        parse_compose(compose, project)

    compose.write_text(
        "services:\n  app:\n    image: alpine:3.20\n    volumes: [cache:/var/lib/app]\nvolumes:\n  cache: {}\n",
        encoding="utf-8",
    )
    assert parse_compose(compose, project).services == ("app",)


@pytest.mark.parametrize(
    ("resource", "declaration", "expected"),
    [
        ("configs", "app-config: {file: ./config.yaml}", "configs"),
        ("secrets", "app-secret: {file: ./secret.txt}", "secrets"),
        ("networks", "host-net: {external: true}", "external"),
    ],
)
def test_compose_rejects_unsupported_top_level_resources(
    tmp_path: Path, resource: str, declaration: str, expected: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        f"services:\n  app:\n    image: alpine:3.20\n{resource}:\n  {declaration}\n",
        encoding="utf-8",
    )

    with pytest.raises(ComposeError, match=expected):
        parse_compose(compose, project)


def test_trusted_compose_allows_admin_approved_capabilities_and_prebuilt_images(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: ${APP_IMAGE:-alpine:3.20}\n"
        "    build:\n"
        "      context: .\n"
        "      args: {APP_MODE: production}\n"
        "    network_mode: host\n"
        "    ipc: host\n"
        "    cap_add: [SYS_ADMIN]\n"
        "    devices: [/dev/example:/dev/example]\n"
        "    security_opt: [seccomp:unconfined]\n"
        "volumes:\n"
        "  shared: {external: true}\n"
        "networks:\n"
        "  existing: {external: true}\n",
        encoding="utf-8",
    )

    with pytest.raises(ComposeError, match="unsupported key"):
        parse_compose(compose, project)

    approved_digest = hashlib.sha256(compose.read_bytes()).hexdigest()
    spec = parse_compose(
        compose,
        project,
        runtime_profile={"approved_compose_digests": [approved_digest], "prefer_prebuilt_images": True},
    )

    assert spec.services == ("app",)
    assert spec.build_services == ()
    assert spec.image_services == {"app": "${APP_IMAGE:-alpine:3.20}"}
    assert spec.raw["services"]["app"]["network_mode"] == "host"
    assert spec.raw["services"]["app"]["build"]["args"] == {"APP_MODE": "production"}


def test_trusted_compose_does_not_allow_include_or_build_only_prebuilt_services(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        "include: [other.yaml]\nservices:\n  app:\n    image: alpine:3.20\n",
        encoding="utf-8",
    )
    approved_digest = hashlib.sha256(compose.read_bytes()).hexdigest()
    profile = {"approved_compose_digests": [approved_digest], "prefer_prebuilt_images": True}

    with pytest.raises(ComposeError, match="include/extends"):
        parse_compose(compose, project, runtime_profile=profile)

    for key in ("include", "extends"):
        compose.write_text(
            f"services:\n  app:\n    image: alpine:3.20\n    {key}:\n      file: other.yaml\n      service: base\n",
            encoding="utf-8",
        )
        profile["approved_compose_digests"] = [hashlib.sha256(compose.read_bytes()).hexdigest()]
        with pytest.raises(ComposeError, match="include/extends"):
            parse_compose(compose, project, runtime_profile=profile)

    compose.write_text("services:\n  app:\n    build: .\n", encoding="utf-8")
    profile["approved_compose_digests"] = [hashlib.sha256(compose.read_bytes()).hexdigest()]
    with pytest.raises(ComposeError, match="prebuilt image"):
        parse_compose(compose, project, runtime_profile=profile)


def test_trusted_compose_requires_an_exact_approved_digest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        "services:\n  app:\n    image: alpine:3.20\n    network_mode: host\n",
        encoding="utf-8",
    )
    original_digest = hashlib.sha256(compose.read_bytes()).hexdigest()
    profile = {"approved_compose_digests": [original_digest]}

    assert parse_compose(compose, project, runtime_profile=profile).services == ("app",)

    compose.write_text(
        "services:\n  app:\n    image: alpine:3.20\n    network_mode: host\n    cap_add: [SYS_ADMIN]\n",
        encoding="utf-8",
    )
    with pytest.raises(ComposeError, match="unsupported key"):
        parse_compose(compose, project, runtime_profile=profile)

    with pytest.raises(ComposeError, match="digest is not approved"):
        parse_compose(
            compose,
            project,
            runtime_profile={
                "approved_compose_digests": [original_digest],
                "prefer_prebuilt_images": True,
            },
        )


def test_compose_allows_only_exact_ascend_runtime_profile(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    driver = tmp_path / "driver"
    driver.mkdir()
    install_info = tmp_path / "ascend_install.info"
    install_info.write_text("version=1\n", encoding="utf-8")
    npu_log = tmp_path / "npu-log"
    npu_log.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        "services:\n"
        "  api:\n"
        "    image: alpine:3.20\n"
        "    security_opt: [no-new-privileges:true]\n"
        "    ports: [8888:8888]\n"
        "  npu:\n"
        "    image: ascend.invalid/server:latest\n"
        "    privileged: true\n"
        "    volumes:\n"
        f"      - {driver}:/usr/local/Ascend/driver:ro\n"
        f"      - {install_info}:/etc/ascend_install.info:ro\n"
        f"      - {npu_log}:/var/log/npu\n",
        encoding="utf-8",
    )
    runtime_profile = {
        "privileged_services": ["npu"],
        "ports": {"api": ["8888:8888"]},
        "host_mounts": [
            {
                "service": "npu",
                "host_path": str(driver),
                "container_path": "/usr/local/Ascend/driver",
                "read_only": True,
            },
            {
                "service": "npu",
                "host_path": str(install_info),
                "container_path": "/etc/ascend_install.info",
                "read_only": True,
            },
            {
                "service": "npu",
                "host_path": str(npu_log),
                "container_path": "/var/log/npu",
                "read_only": False,
            },
        ],
    }

    spec = parse_compose(compose, project, runtime_profile=runtime_profile)
    assert spec.services == ("api", "npu")

    runtime_profile["ports"] = {"api": ["127.0.0.1:8888:8888"]}
    with pytest.raises(ComposeError, match="published ports"):
        parse_compose(compose, project, runtime_profile=runtime_profile)

    runtime_profile["ports"] = {"api": ["8888:8888"]}
    runtime_profile["host_mounts"][2]["read_only"] = True
    with pytest.raises(ComposeError, match="registered host mount"):
        parse_compose(compose, project, runtime_profile=runtime_profile)


def test_compose_allows_only_exact_registered_device_reservations(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "compose.yaml"
    compose.write_text(
        "services:\n"
        "  gpu:\n"
        "    image: nvidia.invalid/server:latest\n"
        "    deploy:\n"
        "      resources:\n"
        "        reservations:\n"
        "          devices:\n"
        "            - driver: nvidia\n"
        "              device_ids: ['4']\n"
        "              capabilities: [gpu]\n",
        encoding="utf-8",
    )
    profile = {"device_reservations": {"gpu": [{"driver": "nvidia", "device_ids": ["4"], "capabilities": ["gpu"]}]}}

    assert parse_compose(compose, project, runtime_profile=profile).services == ("gpu",)
    profile["device_reservations"]["gpu"][0]["device_ids"] = ["5"]
    with pytest.raises(ComposeError, match="device reservations"):
        parse_compose(compose, project, runtime_profile=profile)


def test_runtime_profile_configuration_is_strict_and_must_exist() -> None:
    settings = Settings(
        auth={"mode": "token", "token": "local-test-token"},
        runtime_profiles={
            "ascend": {
                "privileged_services": ["npu"],
                "host_mounts": [
                    {
                        "service": "npu",
                        "host_path": "/etc/ascend_install.info",
                        "container_path": "/etc/ascend_install.info",
                        "read_only": True,
                    },
                    {
                        "service": "npu",
                        "host_path": "/var/log/npu",
                        "container_path": "/var/log/npu",
                        "read_only": False,
                    },
                ],
            }
        },
        apps={
            "demo": {
                "git": {"repo_path": "/srv/demo", "origin": "https://example.invalid/demo.git"},
                "environments": {"staging": {"project_name": "demo", "runtime_profile": "ascend"}},
            }
        },
    )
    assert settings.apps["demo"].environments["staging"].runtime_profile == "ascend"

    trusted = Settings(
        auth={"mode": "token", "token": "local-test-token"},
        runtime_profiles={"legacy": {"approved_compose_digests": ["a" * 64], "prefer_prebuilt_images": True}},
    )
    assert trusted.runtime_profiles["legacy"].approved_compose_digests == ["a" * 64]
    assert trusted.runtime_profiles["legacy"].prefer_prebuilt_images is True

    with pytest.raises(ValidationError, match="prefer_prebuilt_images"):
        Settings(
            auth={"mode": "token", "token": "local-test-token"},
            runtime_profiles={"invalid": {"prefer_prebuilt_images": True}},
        )

    with pytest.raises(ValidationError, match="runtime_profile"):
        Settings(
            auth={"mode": "token", "token": "local-test-token"},
            apps={
                "demo": {
                    "git": {"repo_path": "/srv/demo", "origin": "https://example.invalid/demo.git"},
                    "environments": {"staging": {"project_name": "demo", "runtime_profile": "missing"}},
                }
            },
        )
