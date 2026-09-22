from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .models import validate_cidr_list


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class AuthConfig(ConfigModel):
    mode: Literal["token", "none"] = "token"
    token: StrictStr | None = None
    token_file: StrictStr | None = None
    allowed_hosts: list[StrictStr] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    allowed_origins: list[StrictStr] = Field(default_factory=list)


class HttpVerifyConfig(ConfigModel):
    allowed_cidrs: list[StrictStr] = Field(default_factory=list)
    allowed_ports: list[StrictInt] = Field(default_factory=lambda: [80, 443, 8080, 18080])
    max_read_concurrency: StrictInt = Field(default=8, ge=1, le=64)
    timeout_seconds: StrictInt = Field(default=10, ge=1, le=30)
    max_request_bytes: StrictInt = Field(default=65536, ge=1, le=1024 * 1024)
    max_response_bytes: StrictInt = Field(default=262144, ge=1, le=1024 * 1024)
    follow_redirects: StrictBool = False

    @field_validator("allowed_cidrs")
    @classmethod
    def validate_cidrs(cls, values: list[str]) -> list[str]:
        validate_cidr_list(values)
        return values


class ConcurrencyConfig(ConfigModel):
    max_read_requests: StrictInt = Field(default=16, ge=1, le=128)
    max_running_jobs: StrictInt = Field(default=1, ge=1, le=1)
    max_queued_jobs: StrictInt = Field(default=50, ge=1, le=1000)
    max_queued_jobs_per_target: StrictInt = Field(default=5, ge=1, le=100)
    queue_timeout_seconds: StrictInt = Field(default=600, ge=1, le=86400)
    min_deploy_interval_seconds: StrictInt = Field(default=60, ge=0, le=86400)


class EditableFileConfig(ConfigModel):
    alias: StrictStr = Field(min_length=1, max_length=64, pattern=r"[a-z][a-z0-9_-]{0,63}")
    path: StrictStr = Field(min_length=1, max_length=200)
    schema_name: StrictStr = Field(default="text", max_length=128, alias="schema")
    max_bytes: StrictInt = Field(default=65536, ge=1, le=1024 * 1024)
    display: Literal["text", "json", "yaml", "toml"] = "yaml"


class HealthCheckConfig(ConfigModel):
    type: Literal["http", "runtime"] = "runtime"
    url: StrictStr | None = None
    expected_status: StrictInt = Field(default=200, ge=100, le=599)
    timeout_seconds: StrictInt = Field(default=3, ge=1, le=30)
    consecutive_successes: StrictInt = Field(default=3, ge=1, le=10)


class DataMountConfig(ConfigModel):
    name: StrictStr = Field(min_length=1, max_length=64, pattern=r"[a-z][a-z0-9_-]{0,63}")
    host_path: StrictStr
    container_path: StrictStr
    persistent: StrictBool = True
    read_only: StrictBool = False


class RuntimeHostMountConfig(ConfigModel):
    service: StrictStr = Field(min_length=1, max_length=64, pattern=r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
    host_path: StrictStr
    container_path: StrictStr
    read_only: StrictBool = False


class RuntimeDeviceReservationConfig(ConfigModel):
    driver: StrictStr = Field(min_length=1, max_length=64)
    device_ids: list[StrictStr] = Field(default_factory=list)
    capabilities: list[StrictStr] = Field(min_length=1)


class RuntimeProfileConfig(ConfigModel):
    privileged_services: list[StrictStr] = Field(default_factory=list)
    host_mounts: list[RuntimeHostMountConfig] = Field(default_factory=list)
    ports: dict[StrictStr, list[StrictStr]] = Field(default_factory=dict)
    device_reservations: dict[StrictStr, list[RuntimeDeviceReservationConfig]] = Field(default_factory=dict)
    approved_compose_digests: list[StrictStr] = Field(default_factory=list)
    prefer_prebuilt_images: StrictBool = False

    @field_validator("approved_compose_digests")
    @classmethod
    def validate_compose_digests(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("approved_compose_digests must contain lowercase SHA-256 values")
        if len(set(values)) != len(values):
            raise ValueError("approved_compose_digests must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_prebuilt_policy(self) -> RuntimeProfileConfig:
        if self.prefer_prebuilt_images and not self.approved_compose_digests:
            raise ValueError("prefer_prebuilt_images requires approved_compose_digests")
        return self


class GitConfig(ConfigModel):
    repo_path: StrictStr
    origin: StrictStr
    allowed_ref_patterns: list[StrictStr] = Field(
        default_factory=lambda: [r"^refs/heads/main$", r"^refs/heads/agent/[A-Za-z0-9._/-]+$"]
    )


class TestSuiteConfig(ConfigModel):
    mode: Literal["container", "source"] = "container"
    image: StrictStr | None = None
    entrypoint: StrictStr | None = None
    argv: list[StrictStr] = Field(default_factory=list)
    timeout_seconds: StrictInt = Field(default=300, ge=1, le=1800)
    network: StrictStr | None = None


class EnvironmentConfig(ConfigModel):
    runtime: Literal["compose"] = "compose"
    project_name: StrictStr
    source_workspace: StrictStr | None = None
    release_root: StrictStr | None = None
    runtime_workspace: StrictStr | None = None
    data_root: StrictStr | None = None
    compose_file: StrictStr | None = None
    services: list[StrictStr] = Field(default_factory=list)
    restartable_services: list[StrictStr] = Field(default_factory=list)
    editable_files: list[EditableFileConfig] = Field(default_factory=list)
    data_mounts: list[DataMountConfig] = Field(default_factory=list)
    health_checks: list[HealthCheckConfig] = Field(default_factory=list)
    test_suites: dict[str, TestSuiteConfig] = Field(default_factory=dict)
    deployment_mode: Literal["docker", "simulation"] = "docker"
    prebuilt_image: StrictStr | None = None
    runtime_profile: StrictStr | None = None


class AppConfig(ConfigModel):
    git: GitConfig
    environments: dict[str, EnvironmentConfig]
    build_profile: StrictStr = "default"


class BuildTarget(ConfigModel):
    context: StrictStr = "."
    dockerfile: StrictStr = "Dockerfile"


class BuildProfile(BuildTarget):
    platform: StrictStr = "linux/amd64"
    timeout_seconds: StrictInt = Field(default=900, ge=1, le=3600)
    buildkit_socket: StrictStr | None = None
    mode: Literal["buildkit", "simulation", "prebuilt"] = "buildkit"
    targets: dict[str, BuildTarget] = Field(default_factory=dict)


class Settings(ConfigModel):
    schema_version: StrictInt = 1
    state_dir: StrictStr = "./var/state"
    log_dir: StrictStr = "./var/log"
    allowed_project_roots: list[StrictStr] = Field(default_factory=lambda: ["/srv/projects"])
    managed_release_root: StrictStr = "./var/releases"
    managed_template_root: StrictStr = "./var/templates"
    managed_data_root: StrictStr = "./var/data"
    allowed_client_cidrs: list[StrictStr] = Field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])
    auth: AuthConfig = Field(default_factory=AuthConfig)
    http_verify: HttpVerifyConfig = Field(default_factory=HttpVerifyConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    runtime_profiles: dict[str, RuntimeProfileConfig] = Field(default_factory=dict)
    apps: dict[str, AppConfig] = Field(default_factory=dict)
    build_profiles: dict[str, BuildProfile] = Field(default_factory=lambda: {"default": BuildProfile()})
    allow_simulation: StrictBool = False
    ingress_max_body_bytes: StrictInt = Field(default=1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    poll_interval_seconds: StrictFloat = Field(default=1.0, gt=0.05, le=30)

    @model_validator(mode="after")
    def validate_runtime_profile_references(self) -> Settings:
        for app_name, app in self.apps.items():
            for environment_name, environment in app.environments.items():
                profile = environment.runtime_profile
                if profile is not None and profile not in self.runtime_profiles:
                    raise ValueError(
                        f"apps.{app_name}.environments.{environment_name}.runtime_profile is not registered"
                    )
        return self

    @field_validator("allowed_client_cidrs")
    @classmethod
    def validate_client_cidrs(cls, values: list[str]) -> list[str]:
        validate_cidr_list(values)
        return values

    def token_value(self, base_dir: Path | None = None) -> str | None:
        if self.auth.mode == "none":
            return None
        if self.auth.token_file:
            token_path = Path(self.auth.token_file)
            if not token_path.is_absolute() and base_dir is not None:
                token_path = base_dir / token_path
            try:
                return token_path.read_text(encoding="utf-8").strip()
            except OSError:
                return None
        return self.auth.token

    def resolve_path(self, value: str, base_dir: Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (base_dir / path).resolve()

    def resolved_state_dir(self, base_dir: Path) -> Path:
        return self.resolve_path(self.state_dir, base_dir)

    def resolved_log_dir(self, base_dir: Path) -> Path:
        return self.resolve_path(self.log_dir, base_dir)

    def resolved_allowed_roots(self, base_dir: Path) -> list[Path]:
        return [self.resolve_path(value, base_dir) for value in self.allowed_project_roots]

    def resolved_release_root(self, base_dir: Path) -> Path:
        return self.resolve_path(self.managed_release_root, base_dir)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_settings(path: Path) -> Settings:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    settings = Settings.model_validate(raw)
    if settings.auth.mode == "token" and not settings.token_value(path.parent):
        raise ValueError("auth.mode=token requires auth.token or auth.token_file")
    return settings


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("DRAWBRIDGE_BOOTSTRAP_TOKEN", "change-me-local-token")
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "state_dir": "./var/state",
                "log_dir": "./var/log",
                "allowed_project_roots": ["./var/projects"],
                "managed_release_root": "./var/releases",
                "managed_template_root": "./var/templates",
                "managed_data_root": "./var/data",
                "allowed_client_cidrs": ["127.0.0.1/32", "::1/128"],
                "auth": {
                    "mode": "token",
                    "token": token,
                    "allowed_hosts": ["127.0.0.1", "localhost"],
                },
                "http_verify": {
                    "allowed_cidrs": ["127.0.0.1/32", "::1/128"],
                    "allowed_ports": [80, 8080],
                },
                "allow_simulation": True,
                "apps": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
