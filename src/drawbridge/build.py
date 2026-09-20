from __future__ import annotations

import copy
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from .compose import ComposeSpec
from .config import BuildProfile, BuildTarget
from .errors import DrawbridgeError
from .process import ExecutionResult, ExecutionSpec, SafeExecutor, TerminationReason


def _relative_path(value: str, *, allow_current: bool = False) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or (path == Path(".") and not allow_current):
        raise DrawbridgeError("INVALID_PARAMETER", "build path must be relative to the release snapshot")
    return path


def _snapshot_path(root: Path, relative: str, *, directory: bool) -> Path:
    path = root / _relative_path(relative, allow_current=directory)
    current = path
    while current != root:
        if current.is_symlink():
            raise DrawbridgeError("BUILD_FAILED", "build path contains a symlink")
        current = current.parent
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise DrawbridgeError("BUILD_FAILED", "build path is missing or escapes the release snapshot") from exc
    if (directory and not resolved.is_dir()) or (not directory and not resolved.is_file()):
        raise DrawbridgeError("BUILD_FAILED", "build path has the wrong file type")
    return resolved


def _target(profile: BuildProfile, service: str) -> BuildTarget:
    if profile.targets:
        try:
            return profile.targets[service]
        except KeyError as exc:
            raise DrawbridgeError("INVALID_PARAMETER", f"no build target is registered for service {service}") from exc
    return BuildTarget(context=profile.context, dockerfile=profile.dockerfile)


def validate_build_declarations(compose: ComposeSpec, profile: BuildProfile) -> None:
    if not compose.build_services:
        return
    if profile.mode != "buildkit":
        raise DrawbridgeError("BUILD_UNAVAILABLE", "build services require a buildkit profile")
    if not profile.buildkit_socket or not profile.buildkit_socket.startswith("unix:///"):
        raise DrawbridgeError("BUILD_UNAVAILABLE", "buildkit profile requires a local Unix socket")
    for name in compose.build_services:
        target = _target(profile, name)
        expected_context = _relative_path(target.context, allow_current=True)
        expected_dockerfile = _relative_path(target.dockerfile)
        declared = compose.raw["services"][name]["build"]
        if isinstance(declared, str):
            context = declared
            dockerfile = "Dockerfile"
        elif isinstance(declared, dict) and set(declared) <= {"context", "dockerfile"}:
            context = declared.get("context", ".")
            dockerfile = declared.get("dockerfile", "Dockerfile")
        else:
            raise DrawbridgeError("INVALID_PARAMETER", f"service {name} has unsupported build options")
        if not isinstance(context, str) or not isinstance(dockerfile, str):
            raise DrawbridgeError("INVALID_PARAMETER", f"service {name} has invalid build paths")
        if (
            _relative_path(context, allow_current=True) != expected_context
            or _relative_path(dockerfile) != expected_dockerfile
        ):
            raise DrawbridgeError(
                "INVALID_PARAMETER", f"service {name} build paths do not match its registered profile"
            )


def _require_success(result: ExecutionResult, operation: str) -> None:
    if result.termination_reason is not TerminationReason.EXITED or result.exit_code != 0:
        raise DrawbridgeError(
            "BUILD_FAILED", f"{operation} failed ({result.termination_reason.value}, exit {result.exit_code})"
        )


async def build_images(
    compose: ComposeSpec,
    profile: BuildProfile,
    release_dir: Path,
    release_id: str,
    executor: SafeExecutor,
) -> tuple[ComposeSpec, dict[str, dict[str, Any]]]:
    validate_build_declarations(compose, profile)
    if not compose.build_services:
        return compose, {}
    assert profile.buildkit_socket is not None
    socket = Path(profile.buildkit_socket.removeprefix("unix://"))
    if not socket.is_socket() or socket.is_symlink():
        raise DrawbridgeError("BUILD_UNAVAILABLE", "rootless BuildKit socket is unavailable")
    buildctl = shutil.which("buildctl")
    docker = shutil.which("docker")
    if buildctl is None or docker is None:
        raise DrawbridgeError("BUILD_UNAVAILABLE", "buildctl and docker are required for image builds")
    runtime_raw = copy.deepcopy(compose.raw)
    built: dict[str, dict[str, Any]] = {}
    archive_dir = release_dir.parent / f".{release_id}-builds"
    try:
        archive_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise DrawbridgeError("BUILD_FAILED", "cannot create image artifact directory") from exc
    loaded_tags: list[str] = []
    completed = False
    try:
        for index, name in enumerate(compose.build_services):
            target = _target(profile, name)
            context = _snapshot_path(release_dir, target.context, directory=True)
            dockerfile = _snapshot_path(context, target.dockerfile, directory=False)
            if dockerfile.stat().st_size > 1024 * 1024:
                raise DrawbridgeError("BUILD_FAILED", "Dockerfile is too large")
            if re.search(rb"(?im)^\s*#\s*syntax\s*=", dockerfile.read_bytes()):
                raise DrawbridgeError("BUILD_FAILED", "custom Dockerfile frontends are not allowed")
            tag = f"drawbridge.local/build:{release_id}-{index}"
            archive = archive_dir / f"image-{index}.tar"
            before = await executor.execute(
                ExecutionSpec(
                    program=str(Path(docker).resolve()),
                    argv=["image", "inspect", "--format", "{{.Id}}", tag],
                    cwd=release_dir,
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                    label="image-inspect-before",
                )
            )
            if (
                before.termination_reason is not TerminationReason.EXITED
                or before.exit_code != 1
                or not re.search(r"No such (image|object)", before.stderr, re.IGNORECASE)
            ):
                raise DrawbridgeError(
                    "BUILD_FAILED", "unique build image tag is already present or Docker is unavailable"
                )
            result = await executor.execute(
                ExecutionSpec(
                    program=str(Path(buildctl).resolve()),
                    argv=[
                        "--addr",
                        profile.buildkit_socket,
                        "build",
                        "--frontend",
                        "dockerfile.v0",
                        "--local",
                        f"context={context}",
                        "--local",
                        f"dockerfile={dockerfile.parent}",
                        "--opt",
                        f"filename={dockerfile.name}",
                        "--opt",
                        f"platform={profile.platform}",
                        "--output",
                        f"type=docker,name={tag},dest={archive}",
                    ],
                    cwd=release_dir,
                    timeout_seconds=profile.timeout_seconds,
                    output_limit_bytes=20 * 1024 * 1024,
                    label="image-build",
                )
            )
            _require_success(result, "BuildKit build")
            if archive.is_symlink() or not archive.is_file() or archive.stat().st_size == 0:
                raise DrawbridgeError("BUILD_FAILED", "BuildKit did not produce a Docker image archive")
            image_hash = hashlib.sha256()
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    image_hash.update(chunk)
            imported = await executor.execute(
                ExecutionSpec(
                    program=str(Path(docker).resolve()),
                    argv=["image", "load", "--input", str(archive)],
                    cwd=release_dir,
                    timeout_seconds=120,
                    output_limit_bytes=1024 * 1024,
                    label="image-import",
                )
            )
            loaded_tags.append(tag)
            _require_success(imported, "Docker image import")
            verified_hash = hashlib.sha256()
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    verified_hash.update(chunk)
            if verified_hash.digest() != image_hash.digest():
                raise DrawbridgeError("BUILD_FAILED", "Docker image archive changed during import")
            identified = await executor.execute(
                ExecutionSpec(
                    program=str(Path(docker).resolve()),
                    argv=["image", "inspect", "--format", "{{.Id}}", tag],
                    cwd=release_dir,
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                    label="image-identify",
                )
            )
            _require_success(identified, "Docker image identification")
            image_id = identified.stdout.strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise DrawbridgeError("BUILD_FAILED", "Docker returned an invalid image ID")
            service = runtime_raw["services"][name]
            service.pop("build")
            service["image"] = image_id
            built[name] = {
                "image_id": image_id,
                "tag": tag,
                "archive": str(archive),
                "archive_sha256": image_hash.hexdigest(),
                "archive_bytes": archive.stat().st_size,
            }
        retained_dir = release_dir / f".drawbridge-images-{release_id}"
        if retained_dir.exists():
            raise DrawbridgeError("BUILD_FAILED", "image artifact path already exists in source snapshot")
        archive_dir.rename(retained_dir)
        for value in built.values():
            value["archive"] = str(retained_dir / Path(value["archive"]).name)
        completed = True
    finally:
        if not completed:
            for tag in loaded_tags:
                try:
                    await executor.execute(
                        ExecutionSpec(
                            program=str(Path(docker).resolve()),
                            argv=["image", "rm", tag],
                            cwd=release_dir,
                            timeout_seconds=30,
                            output_limit_bytes=4096,
                            label="image-cleanup",
                        )
                    )
                except Exception:
                    pass
            if archive_dir.exists():
                shutil.rmtree(archive_dir, ignore_errors=True)
    runtime = ComposeSpec(
        path=compose.path,
        raw=runtime_raw,
        services=compose.services,
        image_services={**compose.image_services, **{name: value["image_id"] for name, value in built.items()}},
        build_services=(),
    )
    return runtime, built
