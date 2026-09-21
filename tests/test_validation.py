from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from drawbridge.compose import ComposeError, parse_compose
from drawbridge.config import DataMountConfig
from drawbridge.models import (
    HttpRequestInput,
    IdempotencyInput,
    ReleasePlanInput,
    validate_project_path,
    validate_ref,
    validate_subdir,
)


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
