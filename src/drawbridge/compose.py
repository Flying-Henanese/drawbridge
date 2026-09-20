from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ComposeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComposeSpec:
    path: Path
    raw: dict[str, Any]
    services: tuple[str, ...]
    image_services: dict[str, str]
    build_services: tuple[str, ...]


def parse_compose(path: Path, project_dir: Path) -> ComposeSpec:
    if not path.is_absolute():
        path = project_dir / path
    path = path.resolve(strict=True)
    project_dir = project_dir.resolve(strict=True)
    try:
        path.relative_to(project_dir)
    except ValueError as exc:
        raise ComposeError("compose_file must be inside project_dir") from exc
    if path.is_symlink() or not path.is_file():
        raise ComposeError("compose_file must be a regular non-symlink file")
    if path.stat().st_size > 256 * 1024:
        raise ComposeError("compose_file is too large")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ComposeError(f"invalid compose YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict) or not raw["services"]:
        raise ComposeError("compose file must contain a non-empty services mapping")
    if any(key in raw for key in ("include", "extends")):
        raise ComposeError("compose include/extends is not supported")

    allowed_root = {"version", "name", "services", "networks", "volumes", "configs", "secrets"}
    unknown_root = set(raw) - allowed_root
    if unknown_root:
        raise ComposeError(f"unsupported compose top-level keys: {sorted(unknown_root)}")

    services: list[str] = []
    image_services: dict[str, str] = {}
    build_services: list[str] = []
    for name, service in raw["services"].items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
            raise ComposeError(f"invalid service name: {name!r}")
        if not isinstance(service, dict):
            raise ComposeError(f"service {name} must be a mapping")
        if "extends" in service or "privileged" in service and service["privileged"]:
            raise ComposeError(f"service {name} uses an unsupported privilege feature")
        for key in ("pid", "ipc", "network_mode", "devices", "cap_add", "security_opt"):
            if key in service:
                raise ComposeError(f"service {name} uses unsupported key {key}")
        if "environment" in service and isinstance(service["environment"], dict):
            for key, value in service["environment"].items():
                if isinstance(value, str) and "${" in value:
                    raise ComposeError(f"dynamic interpolation is not allowed: {name}.environment.{key}")
        for value in _walk_strings(service):
            if "${" in value:
                raise ComposeError(f"dynamic interpolation is not allowed in service {name}")
        if "volumes" in service:
            _validate_volumes(name, service["volumes"], project_dir)
        if "env_file" in service:
            _validate_env_files(name, service["env_file"], project_dir)
        image = service.get("image")
        build = service.get("build")
        if image is None and build is None:
            raise ComposeError(f"service {name} must declare image or build")
        if image is not None and not isinstance(image, str):
            raise ComposeError(f"service {name}.image must be a string")
        if build is not None:
            build_services.append(name)
        if image is not None:
            image_services[name] = image
        services.append(name)
    return ComposeSpec(
        path=path,
        raw=raw,
        services=tuple(services),
        image_services=image_services,
        build_services=tuple(build_services),
    )


def write_trusted_compose(spec: ComposeSpec, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(spec.raw, sort_keys=False), encoding="utf-8")
    destination.chmod(0o640)
    return destination


def docker_discover(*, max_items: int = 1000) -> list[dict[str, Any]]:
    docker = shutil.which("docker")
    if docker is None:
        raise ComposeError("docker executable is not available")
    listing = subprocess.run(
        [
            docker,
            "ps",
            "--all",
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            "{{.ID}}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=15,
        check=False,
    )
    if listing.returncode != 0:
        raise ComposeError(listing.stderr.strip()[:1024] or "docker discovery failed")
    ids = [value for value in listing.stdout.splitlines() if value][:max_items]
    candidates: list[dict[str, Any]] = []
    for container_id in ids:
        inspected = subprocess.run(
            [
                docker,
                "inspect",
                "--format",
                "{{.Id}}\t{{.Image}}\t{{json .Config.Labels}}\t{{json .Mounts}}",
                container_id,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
        if inspected.returncode != 0:
            continue
        parts = inspected.stdout.strip().split("\t", 3)
        if len(parts) != 4:
            continue
        try:
            labels = json.loads(parts[2]) or {}
            mounts = json.loads(parts[3]) or []
        except json.JSONDecodeError:
            continue
        candidates.append(
            {
                "container_id": parts[0][:128],
                "image_id": parts[1][:256],
                "project": str(labels.get("com.docker.compose.project", ""))[:128],
                "service": str(labels.get("com.docker.compose.service", ""))[:128],
                "working_dir": str(labels.get("com.docker.compose.project.working_dir", ""))[:256],
                "config_files": str(labels.get("com.docker.compose.config-hash", ""))[:256],
                "mounts": _safe_mounts(mounts),
            }
        )
    return candidates


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                yield key
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _validate_volumes(service_name: str, volumes: Any, project_dir: Path) -> None:
    if not isinstance(volumes, list):
        raise ComposeError(f"service {service_name}.volumes must be a list")
    for volume in volumes:
        if isinstance(volume, str):
            source = volume.split(":", 1)[0]
            if source.startswith("/"):
                try:
                    Path(source).resolve().relative_to(project_dir)
                except ValueError as exc:
                    raise ComposeError(f"host volume is outside the project: {service_name}") from exc
        elif isinstance(volume, dict):
            source_value = volume.get("source")
            if isinstance(source_value, str) and source_value.startswith("/"):
                raise ComposeError(f"absolute volume is not allowed in {service_name}")
        else:
            raise ComposeError(f"invalid volume in {service_name}")


def _validate_env_files(service_name: str, env_files: Any, project_dir: Path) -> None:
    values = [env_files] if isinstance(env_files, str) else env_files
    if not isinstance(values, list):
        raise ComposeError(f"service {service_name}.env_file must be a string or list")
    for value in values:
        if not isinstance(value, str) or value.startswith("/") or ".." in Path(value).parts:
            raise ComposeError(f"invalid env_file in {service_name}")
        path = (project_dir / value).resolve()
        try:
            path.relative_to(project_dir)
        except ValueError as exc:
            raise ComposeError(f"env_file escapes project in {service_name}") from exc
        if path.is_symlink() or not path.is_file():
            raise ComposeError(f"env_file must exist and be a regular file in {service_name}")


def _safe_mounts(mounts: Any) -> list[dict[str, str | bool]]:
    if not isinstance(mounts, list):
        return []
    result: list[dict[str, str | bool]] = []
    for mount in mounts[:32]:
        if not isinstance(mount, dict):
            continue
        result.append(
            {
                "type": str(mount.get("Type", ""))[:32],
                "destination": str(mount.get("Destination", ""))[:256],
                "read_only": mount.get("RW") is False,
            }
        )
    return result


def summarize_compose(spec: ComposeSpec) -> dict[str, Any]:
    return {
        "compose_file": str(spec.path),
        "services": list(spec.services),
        "image_services": dict(spec.image_services),
        "build_services": list(spec.build_services),
    }
