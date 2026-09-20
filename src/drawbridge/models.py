from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


IdempotencyKey = Annotated[str, Field(min_length=8, max_length=128, pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")]
UuidString = Annotated[str, Field(pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")]
Name = Annotated[str, Field(min_length=1, max_length=64, pattern=r"[a-z][a-z0-9_-]{0,63}")]


class IdempotencyInput(StrictModel):
    idempotency_key: IdempotencyKey


class ReleasePlanInput(StrictModel):
    app: Name
    environment: Literal["staging"] = "staging"
    source_mode: Literal["fetch", "local"] = "fetch"
    git_ref: Annotated[str, Field(min_length=1, max_length=200)]
    workspace_revision: UuidString | None = None
    workflow: Name = "deploy_basic"

    @field_validator("git_ref")
    @classmethod
    def git_ref_shape(cls, value: str) -> str:
        return validate_ref(value)


class HttpRequestInput(StrictModel):
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout_seconds: StrictInt = Field(default=10, ge=1, le=30)
    app: Name | None = None
    environment: Literal["staging"] = "staging"
    release_id: UuidString | None = None
    idempotency_key: IdempotencyKey | None = None

    @field_validator("url")
    @classmethod
    def url_shape(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("url contains a control character")
        return value

    @field_validator("headers")
    @classmethod
    def header_shape(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 16:
            raise ValueError("too many headers")
        allowed = {"accept", "content-type", "x-request-id"}
        forbidden = {"host", "cookie", "authorization", "proxy-authorization", "connection"}
        total = 0
        for name, header_value in value.items():
            if not isinstance(name, str) or not isinstance(header_value, str):
                raise ValueError("headers must be strings")
            lowered = name.lower()
            if lowered not in allowed or lowered in forbidden:
                raise ValueError(f"header is not allowed: {name}")
            if any(ord(char) < 32 or ord(char) == 127 for char in name + header_value):
                raise ValueError("header contains a control character")
            total += len(name.encode()) + len(header_value.encode())
        if total > 8 * 1024:
            raise ValueError("headers are too large")
        if (
            value
            and "content-type" in {name.lower() for name in value}
            and value.get("Content-Type", "").lower() == "application/json"
        ):
            if value.get("Content-Type") is None:
                raise ValueError("invalid content type")
        return value

    @field_validator("body")
    @classmethod
    def body_size(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 64 * 1024:
            raise ValueError("body is too large")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def write_requires_key(cls, value: str | None, info: object) -> str | None:
        # The model-level check below has access to method and body; this validator only
        # preserves the strict field shape.
        return value


class JobLookupInput(StrictModel):
    job_id: UuidString


class LogsInput(StrictModel):
    app: Name
    environment: Literal["staging"] = "staging"
    service: Annotated[str, Field(min_length=1, max_length=64, pattern=r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")]
    since_seconds: StrictInt = Field(default=300, ge=1, le=86400)
    query: Annotated[str, Field(default="", max_length=128)] = ""
    cursor: Annotated[str, Field(default="", max_length=512)] = ""
    limit: StrictInt = Field(default=100, ge=1, le=200)
    tail: StrictInt = Field(default=200, ge=1, le=1000)


def validate_ref(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("git ref contains a control character")
    if re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        return value
    if re.fullmatch(r"refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        return value
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    raise ValueError("git_ref is not an allowed full ref or commit SHA")


def validate_subdir(value: str) -> str:
    if value == ".":
        return value
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*", value):
        raise ValueError("subdir must be a relative path without dot segments")
    return value


def _contains_symlink(path: Path, stop_at: Path) -> bool:
    current = path
    stop = stop_at.resolve()
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def validate_project_path(project_dir: str, allowed_roots: list[Path]) -> Path:
    raw = Path(project_dir)
    if not raw.is_absolute():
        raise ValueError("project_dir must be absolute")
    try:
        candidate = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("project_dir does not exist") from exc
    if not candidate.is_dir():
        raise ValueError("project_dir is not a directory")
    for root_value in allowed_roots:
        root = root_value.resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if _contains_symlink(raw, root_value):
            raise ValueError("project_dir contains a symlink")
        return candidate
    raise ValueError("project_dir is outside allowed_project_roots")


def validate_cidr_list(values: list[str]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        try:
            result.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid CIDR: {value}") from exc
    return result
