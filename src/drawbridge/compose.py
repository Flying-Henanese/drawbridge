from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
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


@dataclass(frozen=True)
class _AllowedDataMount:
    host_path: Path
    container_path: str
    read_only: bool


def parse_compose(
    path: Path,
    project_dir: Path,
    *,
    allowed_data_mounts: Sequence[Mapping[str, Any]] | None = None,
) -> ComposeSpec:
    if not path.is_absolute():
        path = project_dir / path
    if _contains_symlink(path):
        raise ComposeError("compose_file contains a symlink")
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
    normalized_data_mounts = _normalize_data_mounts(allowed_data_mounts or ())

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
            _validate_volumes(name, service["volumes"], project_dir, normalized_data_mounts)
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


def _validate_volumes(
    service_name: str,
    volumes: Any,
    project_dir: Path,
    allowed_data_mounts: tuple[_AllowedDataMount, ...],
) -> None:
    if not isinstance(volumes, list):
        raise ComposeError(f"service {service_name}.volumes must be a list")
    for volume in volumes:
        if isinstance(volume, str):
            source, target, read_only = _parse_short_volume(volume, service_name)
            _validate_container_target(target, service_name)
            if source is not None:
                _validate_volume_source(
                    service_name=service_name,
                    source=source,
                    target=target,
                    read_only=read_only,
                    project_dir=project_dir,
                    allowed_data_mounts=allowed_data_mounts,
                    explicit_bind=False,
                )
        elif isinstance(volume, dict):
            _validate_long_volume(service_name, volume, project_dir, allowed_data_mounts)
        else:
            raise ComposeError(f"invalid volume in {service_name}")


def _normalize_data_mounts(values: Sequence[Mapping[str, Any]]) -> tuple[_AllowedDataMount, ...]:
    normalized: list[_AllowedDataMount] = []
    for value in values:
        host_value = value.get("host_path")
        container_value = value.get("container_path")
        read_only = value.get("read_only", False)
        if not isinstance(host_value, str) or not isinstance(container_value, str) or type(read_only) is not bool:
            raise ComposeError("registered data mount must contain strict host_path, container_path, and read_only")
        host_path = Path(host_value)
        if not host_path.is_absolute():
            raise ComposeError("registered data mount host_path must be absolute")
        _reject_parent_segments(host_path, "registered data mount host_path")
        _validate_container_target(container_value, "registered data mount")
        normalized.append(
            _AllowedDataMount(
                host_path=_normalize_host_path(host_path, "registered data mount"),
                container_path=container_value,
                read_only=read_only,
            )
        )
    return tuple(normalized)


def _parse_short_volume(value: str, service_name: str) -> tuple[str | None, str, bool]:
    parts = value.split(":")
    if len(parts) == 1:
        return None, parts[0], False
    if len(parts) > 3:
        raise ComposeError(f"invalid volume syntax in {service_name}")
    source = parts[0] or None
    target = parts[1]
    options = [item for item in parts[2].split(",") if item] if len(parts) == 3 else []
    read_only = False
    access_mode: str | None = None
    for option in options:
        if option in {"ro", "rw"}:
            if access_mode is not None:
                raise ComposeError(f"volume has multiple access modes in {service_name}")
            access_mode = option
            read_only = option == "ro"
        elif option == "nocopy":
            continue
        else:
            raise ComposeError(f"unsupported volume option {option!r} in {service_name}")
    return source, target, read_only


def _validate_long_volume(
    service_name: str,
    volume: dict[Any, Any],
    project_dir: Path,
    allowed_data_mounts: tuple[_AllowedDataMount, ...],
) -> None:
    volume_type = volume.get("type")
    if volume_type is not None and not isinstance(volume_type, str):
        raise ComposeError(f"volume type must be a string in {service_name}")
    if volume_type not in {None, "bind", "volume", "tmpfs"}:
        raise ComposeError(f"unsupported volume type {volume_type!r} in {service_name}")
    source = volume.get("source")
    if source is not None and not isinstance(source, str):
        raise ComposeError(f"volume source must be a string in {service_name}")
    target = volume.get("target")
    if not isinstance(target, str):
        raise ComposeError(f"volume target must be a string in {service_name}")
    _validate_container_target(target, service_name)
    read_only = volume.get("read_only", False)
    if type(read_only) is not bool:
        raise ComposeError(f"volume read_only must be a boolean in {service_name}")

    if volume_type == "tmpfs":
        if source not in {None, ""}:
            raise ComposeError(f"tmpfs volume cannot declare a source in {service_name}")
        return
    if volume_type == "volume":
        _validate_named_volume(source or "", service_name)
        return

    explicit_bind = volume_type == "bind"
    if source is None or source == "":
        if explicit_bind:
            raise ComposeError(f"bind volume must declare a source in {service_name}")
        return
    if not explicit_bind and not _looks_like_path(source):
        _validate_named_volume(source, service_name)
        return
    _validate_volume_source(
        service_name=service_name,
        source=source,
        target=target,
        read_only=read_only,
        project_dir=project_dir,
        allowed_data_mounts=allowed_data_mounts,
        explicit_bind=explicit_bind,
    )


def _validate_volume_source(
    *,
    service_name: str,
    source: str,
    target: str,
    read_only: bool,
    project_dir: Path,
    allowed_data_mounts: tuple[_AllowedDataMount, ...],
    explicit_bind: bool,
) -> None:
    source_path = Path(source)
    _reject_parent_segments(source_path, f"volume source in {service_name}")
    if source_path.is_absolute():
        normalized = _normalize_host_path(source_path, f"volume source in {service_name}")
        _require_registered_data_mount(
            service_name=service_name,
            source=normalized,
            target=target,
            read_only=read_only,
            allowed_data_mounts=allowed_data_mounts,
            absolute=True,
        )
        return
    if source.startswith("~"):
        raise ComposeError(f"volume source uses an unsupported home shortcut in {service_name}")
    if not explicit_bind and not _looks_like_path(source):
        _validate_named_volume(source, service_name)
        return

    candidate = project_dir / source_path
    if _contains_symlink(candidate, project_dir):
        raise ComposeError(f"volume source contains a symlink in {service_name}")
    normalized = _normalize_host_path(candidate, f"volume source in {service_name}", check_symlinks=False)
    try:
        normalized.relative_to(project_dir)
    except ValueError as exc:
        raise ComposeError(f"volume source is outside the project in {service_name}") from exc
    _require_registered_data_mount(
        service_name=service_name,
        source=normalized,
        target=target,
        read_only=read_only,
        allowed_data_mounts=allowed_data_mounts,
        absolute=False,
    )


def _require_registered_data_mount(
    *,
    service_name: str,
    source: Path,
    target: str,
    read_only: bool,
    allowed_data_mounts: tuple[_AllowedDataMount, ...],
    absolute: bool,
) -> None:
    same_source = [mount for mount in allowed_data_mounts if mount.host_path == source]
    if not same_source:
        if absolute:
            raise ComposeError(f"absolute host volume is not a registered data mount in {service_name}")
        return
    if any(mount.container_path == target and mount.read_only == read_only for mount in same_source):
        return
    raise ComposeError(f"volume does not exactly match a registered data mount in {service_name}")


def _validate_named_volume(source: str, service_name: str) -> None:
    if not source:
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", source):
        raise ComposeError(f"invalid named volume in {service_name}")


def _looks_like_path(source: str) -> bool:
    return source.startswith("/") or source == "." or source.startswith("./") or source.startswith("..")


def _reject_parent_segments(path: Path, description: str) -> None:
    if ".." in path.parts:
        raise ComposeError(f"{description} contains parent traversal")


def _validate_container_target(target: str, description: str) -> None:
    if not target.startswith("/") or ".." in Path(target).parts:
        raise ComposeError(f"volume target must be an absolute path without parent traversal in {description}")


def _normalize_host_path(path: Path, description: str, *, check_symlinks: bool = True) -> Path:
    if check_symlinks and _contains_symlink(path):
        raise ComposeError(f"{description} contains a symlink")
    try:
        return path.absolute().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ComposeError(f"unable to resolve {description}") from exc


def _contains_symlink(path: Path, stop_at: Path | None = None) -> bool:
    current = path.absolute()
    stop = stop_at.absolute() if stop_at is not None else None
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


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
