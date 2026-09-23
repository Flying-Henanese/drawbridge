"""Copy administrator-maintained Compose inputs into a release snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import yaml

from .config import OperatorComposeConfig


class OperatorFileError(ValueError):
    pass


def _relative_file(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path == Path(".") or "${" in value:
        raise OperatorFileError("operator env_file must be a static relative path")
    return path


def _no_symlink(path: Path, *, require_read_only: bool) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise OperatorFileError("operator file path contains a symlink")
        if require_read_only and current.exists():
            info = current.stat()
            if info.st_uid == os.geteuid() or info.st_mode & 0o022:
                raise OperatorFileError("operator files must be administrator-owned and read-only to Drawbridge")
        if current == current.parent:
            return
        current = current.parent


def _read_regular(path: Path, *, max_bytes: int, require_read_only: bool) -> bytes:
    _no_symlink(path, require_read_only=require_read_only)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise OperatorFileError("operator file is missing or unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise OperatorFileError("operator file is not a regular file or exceeds its size limit")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OperatorFileError("operator file exceeds its size limit")
        return data
    finally:
        os.close(fd)


def _env_files(raw: Any) -> set[Path]:
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
        raise OperatorFileError("operator Compose must contain services")
    files: set[Path] = set()
    for service in raw["services"].values():
        if not isinstance(service, dict) or "env_file" not in service:
            continue
        declared = service["env_file"]
        entries = [declared] if isinstance(declared, (str, dict)) else declared
        if not isinstance(entries, list):
            raise OperatorFileError("operator env_file has an unsupported form")
        for entry in entries:
            if isinstance(entry, str):
                value = entry
            elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
                value = entry["path"]
            else:
                raise OperatorFileError("operator env_file has an unsupported form")
            files.add(_relative_file(value))
    return files


def materialize_operator_files(
    config: OperatorComposeConfig,
    destination: Path,
    *,
    forbidden_roots: list[Path],
    require_read_only: bool = True,
) -> tuple[str, bool, frozenset[str]]:
    """Return the digest, .env presence, and copied relative paths."""
    root = Path(config.directory)
    _no_symlink(root, require_read_only=require_read_only)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise OperatorFileError("operator directory is missing") from exc
    if not root.is_dir():
        raise OperatorFileError("operator directory must be a directory")
    for forbidden in forbidden_roots:
        try:
            root.relative_to(forbidden.resolve())
        except ValueError:
            continue
        raise OperatorFileError("operator directory must be outside managed project and state roots")

    compose_relative = _relative_file(config.file)
    compose_bytes = _read_regular(root / compose_relative, max_bytes=256 * 1024, require_read_only=require_read_only)
    try:
        raw = yaml.safe_load(compose_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OperatorFileError("operator Compose is invalid") from exc
    dependencies = _env_files(raw)
    has_dotenv = (root / ".env").exists()
    if has_dotenv:
        dependencies.add(Path(".env"))
    dependencies.add(compose_relative)
    if len(dependencies) > 32:
        raise OperatorFileError("operator Compose references too many environment files")

    fingerprints: list[dict[str, str]] = []
    total_bytes = 0
    for relative in sorted(dependencies):
        content = (
            compose_bytes
            if relative == compose_relative
            else _read_regular(root / relative, max_bytes=1024 * 1024, require_read_only=require_read_only)
        )
        total_bytes += len(content)
        if total_bytes > 4 * 1024 * 1024:
            raise OperatorFileError("operator files exceed the snapshot size limit")
        target = destination / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OperatorFileError("operator file collides with a repository path") from exc
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise OperatorFileError("operator file collides with a repository path")
        target.write_bytes(content)
        target.chmod(0o600)
        fingerprints.append({"path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest()})
    encoded = json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), has_dotenv, frozenset(relative.as_posix() for relative in dependencies)
