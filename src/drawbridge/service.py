from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from .build import build_images, cleanup_image_tags, validate_build_declarations
from .compose import (
    ComposeError,
    ComposeSpec,
    docker_discover,
    parse_compose,
    write_trusted_compose,
)
from .config import AppConfig, BuildProfile, EnvironmentConfig, Settings
from .errors import DrawbridgeError, error_response, ok
from .gitops import GitError, GitRepository
from .models import (
    HttpRequestInput,
    LogsInput,
    ReleasePlanInput,
    validate_project_path,
    validate_subdir,
)
from .process import ExecutionSpec, SafeExecutor
from .storage import Database, IdempotencyConflict, JobRecord, QueueFull, create_request_hash

_PLAN_SCHEMA_VERSION = 1
_PLAN_SNAPSHOT_FIELDS = (
    "workspace_revision_digest",
    "service_set",
    "compose_digest",
    "build_declaration_digest",
)


class DrawbridgeService:
    def __init__(self, settings: Settings, database: Database, *, base_dir: Path) -> None:
        self.settings = settings
        self.database = database
        self.base_dir = base_dir.resolve()
        self.executor = SafeExecutor()
        self._read_limit = asyncio.Semaphore(settings.concurrency.max_read_requests)

    async def catalog(self) -> dict[str, Any]:
        return ok(
            {
                "operations": {
                    "ops_catalog": {"access": "read"},
                    "ops_status": {"access": "read"},
                    "ops_logs": {"access": "read"},
                    "ops_app_discover": {"access": "read"},
                    "ops_app_register": {"access": "workspace_write"},
                    "ops_git_status": {"access": "read"},
                    "ops_git_log": {"access": "read"},
                    "ops_process_list": {"access": "read"},
                    "ops_config_read": {"access": "read"},
                    "ops_config_validate": {"access": "read"},
                    "ops_release_plan": {"access": "read"},
                    "ops_release_apply": {"access": "runtime_write"},
                    "ops_release_status": {"access": "read"},
                    "ops_release_rollback": {"access": "runtime_write"},
                    "ops_service_restart": {"access": "runtime_write"},
                    "ops_workspace_patch": {"access": "workspace_write"},
                    "ops_http_request": {"access": "read or verification_write"},
                    "ops_operation_run": {"access": "read or runtime_write"},
                    "ops_workflow_run": {"access": "runtime_write"},
                },
                "workflows": {
                    "deploy_basic": {
                        "requires_plan": True,
                        "steps": ["snapshot", "build", "import", "deploy", "health", "finalize"],
                    }
                },
                "constraints": {
                    "max_running_jobs": self.settings.concurrency.max_running_jobs,
                    "max_queued_jobs": self.settings.concurrency.max_queued_jobs,
                    "simulation_enabled": self.settings.allow_simulation,
                },
                "getting_started": [
                    "Register a Git-backed Compose project with ops_app_register if it is not registered.",
                    "Create a plan with ops_release_plan using a full Git ref or commit SHA.",
                    "Queue that plan with ops_release_apply(plan_id, idempotency_key).",
                    "Poll ops_release_status(job_id) until succeeded or failed, then inspect status, logs, "
                    "and HTTP evidence.",
                ],
                "response_contract": (
                    "Normal operation responses have status, data, error, and request_id. status=ok can mean a "
                    "job was queued; its final outcome is in ops_release_status. Request validation can also "
                    "produce an MCP tool error."
                ),
                "supported_environments": ["staging"],
            },
            request_id=str(uuid.uuid4()),
        )

    async def app_register(
        self,
        *,
        app: str,
        environment: str,
        project_dir: str,
        compose_file: str,
        idempotency_key: str,
        profile: str = "default",
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            self._validate_name(app)
            if environment != "staging":
                raise DrawbridgeError("INVALID_PARAMETER", "only staging is supported by the MVP")
            if profile not in self.settings.build_profiles:
                raise DrawbridgeError("INVALID_PARAMETER", "profile is not registered")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key):
                raise DrawbridgeError("INVALID_PARAMETER", "invalid idempotency_key")
            existing = await self.database.get_binding(app, environment)
            project = validate_project_path(project_dir, self.settings.resolved_allowed_roots(self.base_dir))
            relative_compose = validate_subdir(compose_file)
            configured_app = self.settings.apps.get(app)
            configured_env = configured_app.environments.get(environment) if configured_app else None
            allowed_data_mounts = [item.model_dump() for item in configured_env.data_mounts] if configured_env else []
            runtime_profile = self._runtime_profile_payload(configured_env)
            compose = parse_compose(
                project / relative_compose,
                project,
                allowed_data_mounts=allowed_data_mounts,
                runtime_profile=runtime_profile,
            )
            origin = GitRepository.detect_origin(project)
            git = GitRepository(project, origin, self._default_ref_patterns())
            git.verify_repository()
            head_sha = git.head_commit()
            binding = self._new_binding(
                app=app,
                environment=environment,
                project=project,
                relative_compose=relative_compose,
                compose=compose,
                origin=origin,
                head_sha=head_sha,
                profile=profile,
            )
            if binding["deployment_mode"] == "docker":
                validate_build_declarations(compose, self.settings.build_profiles[profile])
            if binding["editable_files"]:
                revision_id = str(uuid.uuid4())
                revision_files: dict[str, Any] = {}
                for editable in binding["editable_files"]:
                    editable["path"] = validate_subdir(editable["path"])
                    editable_path = project / editable["path"]
                    self._safe_regular_file(editable_path, project)
                    content = git.read_file_at_commit(head_sha, editable["path"])
                    if self._sha256(editable_path.read_bytes()) != self._sha256(content):
                        raise DrawbridgeError("PATCH_BASE_MISMATCH", "editable file is dirty at registration")
                    revision_files[editable["alias"]] = {
                        "path": editable["path"],
                        "pre_digest": self._sha256(content),
                        "post_digest": self._sha256(content),
                        "content_b64": base64.b64encode(content).decode("ascii"),
                    }
                await self.database.save_revision(
                    revision_id=revision_id,
                    app=app,
                    environment=environment,
                    base_commit_sha=head_sha,
                    payload={"files": revision_files, "base_commit_sha": head_sha, "initial": True},
                )
                binding["current_revision"] = revision_id
            if existing is not None:
                if self._binding_identity(existing) != self._binding_identity(binding):
                    raise DrawbridgeError("APP_ALREADY_REGISTERED", "app/environment is already registered")
                if self._runtime_policy_identity(existing) == self._runtime_policy_identity(binding):
                    return ok(self._binding_summary(existing), request_id=request_id)
                version = await self.database.save_binding(app, environment, binding, status=binding["status"])
                binding["version"] = version
                await self.database.append_event(
                    "app_runtime_profile_updated",
                    self._public_summary(binding),
                    app=app,
                    environment=environment,
                )
                return ok(self._binding_summary(binding), request_id=request_id)
            version = await self.database.save_binding(app, environment, binding, status=binding["status"])
            binding["version"] = version
            await self.database.append_event(
                "app_registered", self._public_summary(binding), app=app, environment=environment
            )
            return ok(self._binding_summary(binding), request_id=request_id)
        except (ValueError, ComposeError, GitError) as exc:
            return error_response(DrawbridgeError("INVALID_PARAMETER", str(exc)), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def app_discover(self, *, limit: int = 100, cursor: str = "") -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            if not 1 <= limit <= 200:
                raise DrawbridgeError("INVALID_PARAMETER", "limit must be between 1 and 200")
            candidates = docker_discover(max_items=1000)
            offset = self._decode_offset(cursor)
            page = candidates[offset : offset + limit]
            next_cursor = str(offset + limit) if offset + limit < len(candidates) else None
            return ok(
                {
                    "candidates": page,
                    "next_cursor": next_cursor,
                    "truncated": next_cursor is not None,
                },
                request_id=request_id,
            )
        except (ComposeError, ValueError) as exc:
            return error_response(DrawbridgeError("UNSUPPORTED", str(exc), retryable=True), request_id=request_id)

    async def git_status(self, *, app: str, environment: str = "staging") -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            binding = await self._require_binding(app, environment)
            git_status = self._git_for_binding(binding).status()
            return ok(
                {"app": app, "environment": environment, "status": git_status, "observed_at": time.time()},
                request_id=request_id,
            )
        except (DrawbridgeError, GitError) as exc:
            error = exc if isinstance(exc, DrawbridgeError) else DrawbridgeError("INVALID_PARAMETER", str(exc))
            return error_response(error, request_id=request_id)

    async def git_log(self, *, app: str, environment: str = "staging", git_ref: str, count: int = 20) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            if isinstance(count, bool) or not 1 <= count <= 100:
                raise DrawbridgeError("INVALID_PARAMETER", "count must be between 1 and 100")
            binding = await self._require_binding(app, environment)
            repository = self._git_for_binding(binding)
            commit_sha = repository.resolve_commit(git_ref)
            return ok(
                {
                    "app": app,
                    "environment": environment,
                    "commit_sha": commit_sha,
                    "entries": repository.log(commit_sha, count),
                    "observed_at": time.time(),
                },
                request_id=request_id,
            )
        except (ValueError, GitError) as exc:
            return error_response(DrawbridgeError("INVALID_PARAMETER", str(exc)), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def process_list(self) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        ps = shutil.which("ps")
        if ps is None:
            return error_response(
                DrawbridgeError("RUNTIME_UNAVAILABLE", "ps executable is not available"), request_id=request_id
            )
        result = await self.executor.execute(
            ExecutionSpec(
                program=str(Path(ps).resolve()),
                argv=["-eo", "pid,ppid,user,comm,pcpu,pmem", "--sort=-pcpu"],
                cwd=Path("/"),
                timeout_seconds=10,
                # ps emits one row per process and can exceed the normal diagnostic
                # budget on a busy host; the handler still returns only 100 rows.
                output_limit_bytes=128 * 1024,
                label="process-list",
            )
        )
        if result.exit_code != 0:
            return error_response(DrawbridgeError("VERIFY_FAILED", result.stderr[:1024]), request_id=request_id)
        rows = []
        for line in result.stdout.splitlines()[1:101]:
            fields = line.split()
            if len(fields) != 6:
                continue
            rows.append(
                {
                    "pid": fields[0],
                    "ppid": fields[1],
                    "user": fields[2],
                    "comm": fields[3],
                    "cpu_percent": fields[4],
                    "memory_percent": fields[5],
                }
            )
        return ok({"processes": rows, "truncated": result.truncated, "observed_at": time.time()}, request_id=request_id)

    async def config_read(
        self, *, app: str, environment: str = "staging", file_alias: str, release_id: str | None = None
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            path, editable, root = await self._diagnostic_file(app, environment, file_alias, release_id)
            self._safe_regular_file(path, root)
            if path.stat().st_size > editable.get("max_bytes", 65536):
                raise DrawbridgeError("OUTPUT_LIMIT", "configuration file is too large")
            content = path.read_text(encoding="utf-8")
            return ok(
                {
                    "app": app,
                    "environment": environment,
                    "file_alias": file_alias,
                    "release_id": release_id,
                    "sha256": self._sha256(content.encode("utf-8")),
                    "content": content,
                    "truncated": False,
                },
                request_id=request_id,
            )
        except (OSError, UnicodeDecodeError, ValueError, GitError, ComposeError) as exc:
            return error_response(DrawbridgeError("INVALID_PARAMETER", str(exc)), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def config_validate(
        self, *, app: str, environment: str = "staging", file_alias: str, release_id: str | None = None
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            path, editable, root = await self._diagnostic_file(app, environment, file_alias, release_id)
            self._safe_regular_file(path, root)
            document = self._load_document(path, editable)
            self._validate_sensitive_document(document)
            return ok(
                {
                    "app": app,
                    "environment": environment,
                    "file_alias": file_alias,
                    "release_id": release_id,
                    "valid": True,
                    "format": editable.get("display", "yaml"),
                    "sha256": self._sha256(path.read_bytes()),
                },
                request_id=request_id,
            )
        except (OSError, UnicodeDecodeError, ValueError, GitError, ComposeError) as exc:
            return error_response(DrawbridgeError("INVALID_PARAMETER", str(exc)), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def release_plan(
        self,
        *,
        app: str,
        environment: str,
        source_mode: str,
        git_ref: str,
        workspace_revision: str | None = None,
        workflow: str = "deploy_basic",
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            request = ReleasePlanInput.model_validate(
                {
                    "app": app,
                    "environment": environment,
                    "source_mode": source_mode,
                    "git_ref": git_ref,
                    "workspace_revision": workspace_revision,
                    "workflow": workflow,
                }
            )
            binding = await self._require_binding(request.app, request.environment)
            if binding["status"] != "deployable":
                raise DrawbridgeError("APP_NOT_DEPLOYABLE", "application binding is not deployable")
            git = self._git_for_binding(binding)
            if request.source_mode == "fetch":
                commit_sha = git.fetch_and_resolve(request.git_ref)
            else:
                commit_sha = git.resolve_commit(request.git_ref)
            current = await self.database.get_current_release(request.app, request.environment)
            profile = self.settings.build_profiles.get(binding["profile"])
            if profile is None:
                raise DrawbridgeError("APP_NOT_DEPLOYABLE", "registered build profile is unavailable")
            plan_root = self.settings.resolved_state_dir(self.base_dir) / "plan-snapshots"
            plan_root.mkdir(parents=True, exist_ok=True)
            temporary_root = Path(tempfile.mkdtemp(prefix="snapshot-", dir=plan_root))
            try:
                snapshot = await self._prepare_snapshot(
                    commit_sha=commit_sha,
                    binding=binding,
                    revision_id=request.workspace_revision,
                    destination=temporary_root / "source",
                    archive_path=temporary_root / "source.tar",
                    profile=profile,
                    current=current,
                )
                fingerprint = self._snapshot_fingerprint(snapshot["compose"], snapshot["revision"], profile)
            finally:
                shutil.rmtree(temporary_root, ignore_errors=True)
            plan_id = str(uuid.uuid4())
            expires_at = time.time() + 15 * 60
            payload = {
                "plan_schema_version": _PLAN_SCHEMA_VERSION,
                "commit_sha": commit_sha,
                "git_ref": request.git_ref,
                "source_mode": request.source_mode,
                "workspace_revision": request.workspace_revision,
                **fingerprint,
                "binding_version": binding["version"],
                "baseline_release_id": current["release_id"] if current else None,
                "workflow": request.workflow,
                "compose_file": binding["compose_file"],
                "project_name": binding["project_name"],
                "configuration_digest": self._configuration_digest(binding),
                "build_profile_digest": self._digest(profile.model_dump()),
            }
            await self.database.save_plan(
                plan_id=plan_id,
                app=request.app,
                environment=request.environment,
                payload=payload,
                expires_at=expires_at,
            )
            await self.database.append_event(
                "release_planned",
                payload,
                request_id=request_id,
                app=request.app,
                environment=request.environment,
                plan_id=plan_id,
            )
            return ok(
                {
                    "plan_id": plan_id,
                    "commit_sha": commit_sha,
                    "workspace_revision": request.workspace_revision,
                    "baseline_release_id": current["release_id"] if current else None,
                    "services": fingerprint["service_set"],
                    "expires_at": expires_at,
                    "workflow": request.workflow,
                    "compose_digest": fingerprint["compose_digest"],
                    "build_declaration_digest": fingerprint["build_declaration_digest"],
                },
                request_id=request_id,
            )
        except (ValueError, GitError) as exc:
            return error_response(DrawbridgeError("INVALID_PARAMETER", str(exc)), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def release_apply(self, *, plan_id: str, idempotency_key: str) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            self._validate_uuid(plan_id, "plan_id")
            self._validate_idempotency_key(idempotency_key)
            plan = await self.database.get_plan(plan_id)
            if plan is None:
                raise DrawbridgeError("STALE_PLAN", "plan does not exist")
            self._validate_plan_schema(plan)
            existing_job = await self.database.get_job_by_plan(plan_id)
            if existing_job is not None:
                return ok(
                    {"job_id": existing_job.job_id, "created": False, "status": existing_job.status},
                    request_id=request_id,
                )
            if plan["expires_at"] <= time.time():
                await self.database.mark_plan(plan_id, "expired")
                raise DrawbridgeError("STALE_PLAN", "plan has expired", retryable=True)
            binding = await self._require_binding(plan["app"], plan["environment"])
            if binding["version"] != plan["binding_version"]:
                raise DrawbridgeError("STALE_PLAN", "binding changed after plan creation", retryable=True)
            if self._configuration_digest(binding) != plan.get("configuration_digest"):
                raise DrawbridgeError("STALE_PLAN", "configuration_digest changed after plan creation", retryable=True)
            profile = self.settings.build_profiles.get(binding["profile"])
            if profile is None or self._digest(profile.model_dump()) != plan.get("build_profile_digest"):
                raise DrawbridgeError("STALE_PLAN", "build_profile_digest changed after plan creation", retryable=True)
            current = await self.database.get_current_release(plan["app"], plan["environment"])
            current_release_id = current["release_id"] if current else None
            if current_release_id != plan.get("baseline_release_id"):
                raise DrawbridgeError("STALE_PLAN", "a newer release is already active", retryable=True)
            now = time.time()
            if current and self.settings.concurrency.min_deploy_interval_seconds:
                elapsed = now - float(current["created_at"])
                minimum = self.settings.concurrency.min_deploy_interval_seconds
                if elapsed < minimum:
                    raise DrawbridgeError(
                        "RATE_LIMITED",
                        "deployment cooling interval has not elapsed",
                        retryable=True,
                        retry_after_seconds=max(1, int(minimum - elapsed)),
                    )
            request_hash = create_request_hash({"action": "deploy", "plan_id": plan_id})
            try:
                queued = await self.database.enqueue_job(
                    action="deploy",
                    app=plan["app"],
                    environment=plan["environment"],
                    payload=plan,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    plan_id=plan_id,
                    max_queued_jobs=self.settings.concurrency.max_queued_jobs,
                    max_queued_jobs_per_target=self.settings.concurrency.max_queued_jobs_per_target,
                    queue_timeout_seconds=self.settings.concurrency.queue_timeout_seconds,
                )
            except QueueFull as exc:
                raise DrawbridgeError("BUSY", str(exc), retryable=True, retry_after_seconds=5) from exc
            except IdempotencyConflict as exc:
                raise DrawbridgeError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
            await self.database.mark_plan(plan_id, "queued")
            return ok(
                {"job_id": queued.job_id, "created": queued.created, "status": "queued"},
                request_id=request_id,
            )
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def release_status(self, job_id: str) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            self._validate_uuid(job_id, "job_id")
            job = await self.database.get_job(job_id)
            if job is None:
                raise DrawbridgeError("INVALID_PARAMETER", "job does not exist")
            payload = {
                "job_id": job.job_id,
                "action": job.action,
                "status": job.status,
                "app": job.app,
                "environment": job.environment,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
            }
            if job.result:
                payload.update(job.result)
            return ok(payload, request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def release_rollback(
        self, *, app: str, environment: str, release_id: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            self._validate_name(app)
            self._validate_uuid(release_id, "release_id")
            self._validate_idempotency_key(idempotency_key)
            if not reason or any(ord(char) < 32 for char in reason) or len(reason) > 256:
                raise DrawbridgeError("INVALID_PARAMETER", "invalid reason")
            target = await self.database.get_release(release_id)
            if (
                target is None
                or target["app"] != app
                or target["environment"] != environment
                or target["status"] != "succeeded"
            ):
                raise DrawbridgeError("ROLLBACK_PRECHECK_FAILED", "target is not a successful historical release")
            current = await self.database.get_current_release(app, environment)
            if current is None:
                raise DrawbridgeError("ROLLBACK_PRECHECK_FAILED", "there is no current release to restore from")
            binding = await self._require_binding(app, environment)
            if sorted(target.get("services", [])) != sorted(binding["services"]):
                raise DrawbridgeError("UNSUPPORTED_SERVICE_CHANGE", "target release has a different service set")
            payload = {
                "target_release_id": release_id,
                "reason": reason,
                "baseline_release_id": current["release_id"],
            }
            try:
                queued = await self.database.enqueue_job(
                    action="rollback",
                    app=app,
                    environment=environment,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    request_hash=create_request_hash(
                        {"action": "rollback", "app": app, "environment": environment, **payload}
                    ),
                    max_queued_jobs=self.settings.concurrency.max_queued_jobs,
                    max_queued_jobs_per_target=self.settings.concurrency.max_queued_jobs_per_target,
                    queue_timeout_seconds=self.settings.concurrency.queue_timeout_seconds,
                )
            except QueueFull as exc:
                raise DrawbridgeError("BUSY", str(exc), retryable=True, retry_after_seconds=5) from exc
            except IdempotencyConflict as exc:
                raise DrawbridgeError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
            return ok(
                {"job_id": queued.job_id, "created": queued.created, "status": "queued"},
                request_id=request_id,
            )
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def service_restart(
        self, *, app: str, environment: str, service: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            binding = await self._require_binding(app, environment)
            if service not in binding.get("restartable_services", binding["services"]):
                raise DrawbridgeError("INVALID_PARAMETER", "service is not restartable")
            self._validate_idempotency_key(idempotency_key)
            payload = {"service": service, "reason": reason}
            queued = await self.database.enqueue_job(
                action="restart",
                app=app,
                environment=environment,
                payload=payload,
                idempotency_key=idempotency_key,
                request_hash=create_request_hash(
                    {"action": "restart", "app": app, "environment": environment, **payload}
                ),
                max_queued_jobs=self.settings.concurrency.max_queued_jobs,
                max_queued_jobs_per_target=self.settings.concurrency.max_queued_jobs_per_target,
                queue_timeout_seconds=self.settings.concurrency.queue_timeout_seconds,
            )
            return ok(
                {"job_id": queued.job_id, "created": queued.created, "status": "queued"},
                request_id=request_id,
            )
        except IdempotencyConflict as exc:
            return error_response(DrawbridgeError("IDEMPOTENCY_CONFLICT", str(exc)), request_id=request_id)
        except QueueFull as exc:
            return error_response(DrawbridgeError("BUSY", str(exc), retryable=True), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def workspace_patch(
        self,
        *,
        app: str,
        environment: str,
        file_alias: str,
        patch: dict[str, Any] | str,
        expected_revision: str,
        idempotency_key: str,
        base_commit_sha: str | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            binding = await self._require_binding(app, environment)
            self._validate_idempotency_key(idempotency_key)
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", file_alias):
                raise DrawbridgeError("INVALID_PARAMETER", "invalid file_alias")
            editable = next(
                (item for item in binding.get("editable_files", []) if item["alias"] == file_alias),
                None,
            )
            if editable is None:
                raise DrawbridgeError("FORBIDDEN_OPERATION", "file alias is not editable")
            current_revision_id = binding.get("current_revision")
            if expected_revision != current_revision_id:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "expected_revision is stale")
            repository = self._git_for_binding(binding)
            if base_commit_sha is None:
                current_revision = (
                    await self.database.get_revision(current_revision_id) if current_revision_id else None
                )
                if current_revision is None:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "a base commit is required for the first revision")
                base_commit_sha = current_revision["base_commit_sha"]
            else:
                base_commit_sha = repository.resolve_commit(base_commit_sha)
            previous = await self.database.get_revision(current_revision_id) if current_revision_id else None
            if current_revision_id and previous is None:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "current workspace revision is missing")
            if base_commit_sha is None:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "a base commit is required")
            previous_data: dict[str, Any] = previous or {}

            # Validate the complete overlay before touching any source file. A revision
            # is always a full snapshot of every registered editable file, not just the
            # file named by this patch.
            file_state: dict[str, dict[str, Any]] = {}
            same_chain = bool(previous and previous.get("base_commit_sha") == base_commit_sha)
            for candidate in binding.get("editable_files", []):
                candidate_alias = candidate["alias"]
                candidate_path = validate_subdir(candidate["path"])
                candidate_source = Path(binding["source_workspace"]) / candidate_path
                self._safe_regular_file(candidate_source, Path(binding["source_workspace"]))
                candidate_base = repository.read_file_at_commit(base_commit_sha, candidate_path)
                candidate_current = candidate_source.read_bytes()
                expected_digest = self._sha256(candidate_base)
                if same_chain:
                    previous_file = previous_data.get("files", {}).get(candidate_alias)
                    if not isinstance(previous_file, dict) or not previous_file.get("post_digest"):
                        raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision overlay is incomplete")
                    expected_digest = previous_file["post_digest"]
                if self._sha256(candidate_current) != expected_digest:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "editable file changed outside Drawbridge")
                file_state[candidate_alias] = {
                    "editable": candidate,
                    "path": candidate_path,
                    "source": candidate_source,
                    "base_bytes": candidate_base,
                    "current_bytes": candidate_current,
                }

            target_state = file_state[file_alias]
            source_path = target_state["source"]
            document = self._load_document(source_path, editable)
            patch_document = self._parse_patch(patch)
            updated = self._merge_document(document, patch_document)
            encoded = self._dump_document(updated, editable)
            if len(encoded) > editable.get("max_bytes", 65536):
                raise DrawbridgeError("INVALID_PARAMETER", "patched file is too large")
            self._validate_sensitive_document(updated)
            temporary = source_path.with_name(f".{source_path.name}.drawbridge-{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(encoded)
            os.chmod(temporary, stat.S_IMODE(source_path.stat().st_mode))
            os.replace(temporary, source_path)
            revision_id = str(uuid.uuid4())
            files: dict[str, Any] = {}
            for alias, state in file_state.items():
                content = encoded if alias == file_alias else state["current_bytes"]
                files[alias] = {
                    "path": state["path"],
                    "pre_digest": self._sha256(state["base_bytes"]),
                    "post_digest": self._sha256(content),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                }
            await self.database.save_revision(
                revision_id=revision_id,
                app=app,
                environment=environment,
                base_commit_sha=base_commit_sha,
                payload={"files": files, "base_commit_sha": base_commit_sha},
            )
            binding["current_revision"] = revision_id
            await self.database.save_binding(app, environment, binding, status=binding["status"])
            diff = {
                "file_alias": file_alias,
                "changed": True,
                "before_sha256": self._sha256(target_state["current_bytes"]),
                "after_sha256": self._sha256(encoded),
            }
            return ok(
                {"revision_id": revision_id, "base_commit_sha": base_commit_sha, "diff": diff},
                request_id=request_id,
            )
        except (ValueError, GitError, ComposeError) as exc:
            return error_response(DrawbridgeError("PATCH_BASE_MISMATCH", str(exc)), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def http_request(self, request: HttpRequestInput) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            from .httpverify import HttpVerifier

            verifier = HttpVerifier(self.settings.http_verify)
            if request.method in {"GET", "HEAD"}:
                async with self._read_limit:
                    data = await verifier.request(request)
                return ok(data, request_id=request_id)
            if not request.idempotency_key:
                raise DrawbridgeError("INVALID_PARAMETER", "write HTTP requests require idempotency_key")
            request_payload = request.model_dump(mode="json", exclude={"idempotency_key"})
            queued = await self.database.enqueue_job(
                action="http_request",
                app=request.app,
                environment=request.environment if request.app else None,
                payload=request.model_dump(mode="json"),
                idempotency_key=request.idempotency_key,
                request_hash=create_request_hash(
                    {
                        "action": "http_request",
                        "app": request.app,
                        "environment": request.environment,
                        "request": request_payload,
                    }
                ),
                max_queued_jobs=self.settings.concurrency.max_queued_jobs,
                max_queued_jobs_per_target=self.settings.concurrency.max_queued_jobs_per_target,
                queue_timeout_seconds=self.settings.concurrency.queue_timeout_seconds,
            )
            return ok(
                {"job_id": queued.job_id, "created": queued.created, "status": "queued"},
                request_id=request_id,
            )
        except IdempotencyConflict as exc:
            return error_response(DrawbridgeError("IDEMPOTENCY_CONFLICT", str(exc)), request_id=request_id)
        except QueueFull as exc:
            return error_response(DrawbridgeError("BUSY", str(exc), retryable=True), request_id=request_id)
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)
        except Exception as exc:
            return error_response(
                DrawbridgeError("TARGET_NOT_ALLOWED", str(exc), retryable=False),
                request_id=request_id,
            )

    async def status(self, *, app: str | None = None, environment: str = "staging") -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            host = self._host_metrics()
            applications: list[dict[str, Any]] = []
            names = [app] if app else list(self.settings.apps)
            if app:
                names = [app]
                binding = await self.database.get_binding(app, environment)
                if binding is not None:
                    applications.append(await self._status_for_binding(binding, host))
            else:
                for name in names:
                    binding = await self.database.get_binding(name, environment)
                    if binding is not None:
                        applications.append(await self._status_for_binding(binding, host))
            return ok(
                {"host": host, "applications": applications, "observed_at": time.time()},
                request_id=request_id,
            )
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def logs(self, request: LogsInput) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        try:
            binding = await self._require_binding(request.app, request.environment)
            current = await self.database.get_current_release(request.app, request.environment)
            if current is None:
                return ok(
                    {"lines": [], "next_cursor": None, "truncated": False, "release_id": None},
                    request_id=request_id,
                )
            if request.service not in binding["services"]:
                raise DrawbridgeError("INVALID_PARAMETER", "service is not registered")
            docker = shutil.which("docker")
            if docker is None or current.get("mode") == "simulation":
                return ok(
                    {
                        "lines": [],
                        "next_cursor": None,
                        "truncated": False,
                        "release_id": current["release_id"],
                    },
                    request_id=request_id,
                )
            result = await self.executor.execute(
                ExecutionSpec(
                    program=str(Path(docker).resolve()),
                    argv=[
                        "compose",
                        "--ansi",
                        "never",
                        "--project-name",
                        binding["project_name"],
                        "logs",
                        "--no-color",
                        "--timestamps",
                        "--tail",
                        str(request.tail),
                        request.service,
                    ],
                    cwd=Path(current["release_dir"]),
                    timeout_seconds=15,
                    output_limit_bytes=1024 * 1024,
                    label="compose-logs",
                )
            )
            if result.exit_code != 0:
                raise DrawbridgeError("VERIFY_FAILED", result.stderr[:1024])
            lines = [self._redact(line) for line in result.stdout.splitlines()]
            if request.query:
                lines = [line for line in lines if request.query in line]
            return ok(
                {
                    "lines": lines[-request.limit :],
                    "next_cursor": None,
                    "truncated": result.truncated,
                    "release_id": current["release_id"],
                },
                request_id=request_id,
            )
        except DrawbridgeError as exc:
            return error_response(exc, request_id=request_id)

    async def run_one_job(self) -> JobRecord | None:
        job = await self.database.claim_next_job(
            owner=f"runner-{os.getpid()}",
            max_running_jobs=self.settings.concurrency.max_running_jobs,
        )
        if job is None:
            return None
        try:
            if job.action == "deploy":
                result = await self._execute_deploy(job)
            elif job.action == "rollback":
                result = await self._execute_rollback(job)
            elif job.action == "restart":
                result = await self._execute_restart(job)
            elif job.action == "http_request":
                result = await self._execute_http_job(job)
            else:
                raise DrawbridgeError("UNKNOWN_OPERATION", f"unsupported queued action: {job.action}")
            await self.database.finish_job(job.job_id, status="succeeded", result=result)
        except DrawbridgeError as exc:
            await self.database.finish_job(
                job.job_id,
                status="failed",
                result={"error_code": exc.code, "message": exc.message, "retryable": exc.retryable},
            )
        except Exception as exc:
            await self.database.finish_job(
                job.job_id,
                status="failed",
                result={"error_code": "INTERNAL_ERROR", "message": str(exc)[:1024]},
            )
        return await self.database.get_job(job.job_id)

    async def _execute_deploy(self, job: JobRecord) -> dict[str, Any]:
        plan = job.payload
        self._validate_plan_schema(plan)
        binding = await self._require_binding(plan["app"], plan["environment"])
        if binding["version"] != plan["binding_version"]:
            raise DrawbridgeError("STALE_PLAN", "binding changed before dispatch", retryable=True)
        if self._configuration_digest(binding) != plan.get("configuration_digest"):
            raise DrawbridgeError("STALE_PLAN", "configuration_digest changed before dispatch", retryable=True)
        profile = self.settings.build_profiles.get(binding["profile"])
        if profile is None or self._digest(profile.model_dump()) != plan.get("build_profile_digest"):
            raise DrawbridgeError("STALE_PLAN", "build_profile_digest changed before dispatch", retryable=True)
        current = await self.database.get_current_release(plan["app"], plan["environment"])
        current_release_id = current["release_id"] if current else None
        if current_release_id != plan.get("baseline_release_id"):
            raise DrawbridgeError("STALE_PLAN", "current release changed before dispatch", retryable=True)
        release_id = str(uuid.uuid4())
        release_root = Path(binding["release_root"])
        release_dir = release_root / release_id
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot = await self._prepare_snapshot(
                commit_sha=plan["commit_sha"],
                binding=binding,
                revision_id=plan.get("workspace_revision"),
                destination=release_dir,
                archive_path=release_dir.parent / f".{release_id}.tar",
                profile=profile,
                current=current,
            )
            fingerprint = self._snapshot_fingerprint(snapshot["compose"], snapshot["revision"], profile)
            self._compare_snapshot_fingerprint(plan, fingerprint)
            if binding["deployment_mode"] == "simulation":
                if not self.settings.allow_simulation:
                    raise DrawbridgeError("APP_NOT_DEPLOYABLE", "simulation mode is disabled")
                (release_dir / "release.json").write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
                release_payload = {
                    "source_sha": plan["commit_sha"],
                    "services": plan["service_set"],
                    "mode": "simulation",
                    "release_dir": str(release_dir),
                    "project_name": binding["project_name"],
                    "workspace_revision": plan.get("workspace_revision"),
                    "compose_file": str(snapshot["compose_file"]),
                    "health": {"status": "passed", "validation_level": "simulation"},
                }
            else:
                release_payload = await self._deploy_docker(plan, binding, release_id, release_dir, snapshot["compose"])
        except Exception as exc:
            runtime_marker = release_dir / f".drawbridge-runtime-started-{release_id}"
            if release_dir.exists() and not runtime_marker.exists():
                shutil.rmtree(release_dir)
            if runtime_marker.exists() and isinstance(exc, DrawbridgeError):
                raise DrawbridgeError(
                    exc.code,
                    f"{exc.message}; release artifacts retained at {release_dir}",
                    retryable=exc.retryable,
                ) from exc
            raise
        await self.database.save_release(
            release_id=release_id,
            app=plan["app"],
            environment=plan["environment"],
            payload=release_payload,
        )
        await self.database.append_event(
            "release_succeeded",
            release_payload,
            job_id=job.job_id,
            app=plan["app"],
            environment=plan["environment"],
            release_id=release_id,
        )
        return {"release_id": release_id, "status": "succeeded", **release_payload}

    async def _prepare_snapshot(
        self,
        *,
        commit_sha: str,
        binding: dict[str, Any],
        revision_id: str | None,
        destination: Path,
        archive_path: Path,
        profile: BuildProfile,
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        repository = self._git_for_binding(binding)
        try:
            repository.archive(commit_sha, archive_path)
            repository.extract_archive(archive_path, destination)
        finally:
            archive_path.unlink(missing_ok=True)
        revision = None
        if revision_id:
            revision = await self.database.get_revision(revision_id)
            if revision is None:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision disappeared before snapshot")
            if revision.get("app") != binding["app"] or revision.get("environment") != binding["environment"]:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision does not belong to the target")
            if revision.get("base_commit_sha") != commit_sha:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision is based on a different commit")
            files = revision.get("files")
            if not isinstance(files, dict):
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision overlay is invalid")
            for file_info in files.values():
                if not isinstance(file_info, dict):
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision file is invalid")
                path_value = file_info.get("path")
                pre_digest = file_info.get("pre_digest")
                post_digest = file_info.get("post_digest")
                if (
                    not isinstance(path_value, str)
                    or not isinstance(pre_digest, str)
                    or not isinstance(post_digest, str)
                ):
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision digest fields are invalid")
                try:
                    relative_path = Path(validate_subdir(path_value))
                except ValueError as exc:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "revision path is invalid") from exc
                target = destination / relative_path
                try:
                    target.relative_to(destination)
                except ValueError as exc:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "revision path escapes release snapshot") from exc
                self._safe_regular_file(target)
                if self._sha256(target.read_bytes()) != pre_digest:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "release archive does not match revision base")
                try:
                    content = base64.b64decode(file_info["content_b64"], validate=True)
                except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "revision overlay content is invalid") from exc
                temporary = target.with_name(f".{target.name}.drawbridge-{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(content)
                os.replace(temporary, target)
                if self._sha256(target.read_bytes()) != post_digest:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "revision overlay digest mismatch")
        release_root = destination.resolve()
        compose_path = (destination / binding["compose_file"]).resolve()
        try:
            compose_path.relative_to(release_root)
        except ValueError as exc:
            raise DrawbridgeError("PATCH_BASE_MISMATCH", "compose path escapes release snapshot") from exc
        if compose_path.is_symlink() or not compose_path.is_file():
            raise DrawbridgeError("PATCH_BASE_MISMATCH", "compose file is missing from release snapshot")
        try:
            compose = parse_compose(
                compose_path,
                destination,
                allowed_data_mounts=binding.get("data_mounts", []),
                runtime_profile=binding.get("runtime_profile"),
            )
        except ComposeError as exc:
            raise DrawbridgeError("INVALID_PARAMETER", f"compose validation failed: {exc}") from exc
        self._validate_service_topology(
            snapshot_services=sorted(compose.services),
            binding=binding,
            current=current,
        )
        validate_build_declarations(compose, profile)
        return {"compose": compose, "compose_file": compose_path, "revision": revision}

    async def _deploy_docker(
        self, plan: dict[str, Any], binding: dict[str, Any], release_id: str, release_dir: Path, compose: ComposeSpec
    ) -> dict[str, Any]:
        built_images: dict[str, dict[str, Any]] = {}
        if compose.build_services:
            profile = self.settings.build_profiles.get(binding["profile"])
            if profile is None or plan.get("build_profile_digest") != self._digest(profile.model_dump()):
                raise DrawbridgeError("STALE_PLAN", "build profile changed after plan creation")
            compose, built_images = await build_images(compose, profile, release_dir, release_id, self.executor)
        runtime_marker = release_dir / f".drawbridge-runtime-started-{release_id}"
        try:
            runtime_compose = release_dir / f".drawbridge-runtime-{release_id}.yaml"
            if runtime_compose.exists() or runtime_marker.exists():
                raise DrawbridgeError("DEPLOY_FAILED", "runtime artifact path already exists in source snapshot")
            compose_path = write_trusted_compose(compose, runtime_compose)
            empty_env = Path(self.settings.resolved_state_dir(self.base_dir)) / "empty.env"
            empty_env.parent.mkdir(parents=True, exist_ok=True)
            empty_env.touch(exist_ok=True)
            docker = shutil.which("docker")
            if docker is None:
                raise DrawbridgeError("RUNTIME_UNAVAILABLE", "docker executable is not available")
            runtime_marker.write_text("compose up started\n", encoding="utf-8")
            result = await self.executor.execute(
                ExecutionSpec(
                    program=str(Path(docker).resolve()),
                    argv=[
                        "compose",
                        "--ansi",
                        "never",
                        "--project-name",
                        binding["project_name"],
                        "--project-directory",
                        str(release_dir),
                        "--env-file",
                        str(empty_env),
                        "-f",
                        str(compose_path),
                        "up",
                        "--detach",
                        "--no-build",
                        "--pull",
                        "never",
                        "--wait",
                        "--wait-timeout",
                        "90",
                    ],
                    cwd=release_dir,
                    timeout_seconds=120,
                    output_limit_bytes=20 * 1024 * 1024,
                    label="compose-deploy",
                )
            )
            if result.exit_code != 0:
                raise DrawbridgeError("DEPLOY_FAILED", self._redact(result.stderr or result.stdout)[-1024:])
            health = await self._health_check(binding, release_dir, compose_path)
            return {
                "source_sha": plan["commit_sha"],
                "services": list(compose.services),
                "mode": "docker",
                "release_dir": str(release_dir),
                "compose_file": str(compose_path),
                "project_name": binding["project_name"],
                "image_services": compose.image_services,
                "built_images": built_images,
                "health": health,
            }
        except Exception:
            if not runtime_marker.exists() and built_images:
                await cleanup_image_tags([value["tag"] for value in built_images.values()], self.executor, release_dir)
            raise

    async def _execute_rollback(self, job: JobRecord) -> dict[str, Any]:
        target = await self.database.get_release(job.payload["target_release_id"])
        current = await self.database.get_current_release(job.app or "", job.environment or "staging")
        if target is None or current is None:
            raise DrawbridgeError("ROLLBACK_PRECHECK_FAILED", "rollback release evidence is missing")
        release_id = str(uuid.uuid4())
        if target.get("mode") == "simulation":
            payload = {
                **target,
                "release_id": None,
                "mode": "simulation",
                "health": {"status": "passed", "validation_level": "simulation"},
            }
        else:
            raise DrawbridgeError("ROLLBACK_PRECHECK_FAILED", "docker rollback requires a retained runtime template")
        payload.pop("created_at", None)
        await self.database.save_release(
            release_id=release_id,
            app=job.app or "",
            environment=job.environment or "staging",
            payload=payload,
            replaces_release_id=current["release_id"],
            restored_from_release_id=target["release_id"],
        )
        return {
            "release_id": release_id,
            "status": "succeeded",
            "restored_from_release_id": target["release_id"],
            "replaces_release_id": current["release_id"],
        }

    async def _execute_restart(self, job: JobRecord) -> dict[str, Any]:
        binding = await self._require_binding(job.app or "", job.environment or "staging")
        current = await self.database.get_current_release(job.app or "", job.environment or "staging")
        if current is None:
            raise DrawbridgeError("NO_BASELINE", "cannot restart an application without a release")
        if current.get("mode") == "simulation":
            return {
                "status": "succeeded",
                "service": job.payload["service"],
                "validation": "simulation",
            }
        docker = shutil.which("docker")
        if docker is None:
            raise DrawbridgeError("RUNTIME_UNAVAILABLE", "docker executable is not available")
        result = await self.executor.execute(
            ExecutionSpec(
                program=str(Path(docker).resolve()),
                argv=[
                    "compose",
                    "--ansi",
                    "never",
                    "--project-name",
                    binding["project_name"],
                    "-f",
                    current["compose_file"],
                    "restart",
                    "--timeout",
                    "10",
                    job.payload["service"],
                ],
                cwd=Path(current["release_dir"]),
                timeout_seconds=120,
                output_limit_bytes=1024 * 1024,
                label="compose-restart",
            )
        )
        if result.exit_code != 0:
            raise DrawbridgeError("VERIFY_FAILED", self._redact(result.stderr or result.stdout)[-1024:])
        return {"status": "succeeded", "service": job.payload["service"]}

    async def _execute_http_job(self, job: JobRecord) -> dict[str, Any]:
        request = HttpRequestInput.model_validate(job.payload)
        from .httpverify import HttpVerifier

        data = await HttpVerifier(self.settings.http_verify).request(request)
        return {"status": "succeeded", "http": data}

    async def _health_check(self, binding: dict[str, Any], release_dir: Path, compose_path: Path) -> dict[str, Any]:
        checks = binding.get("health_checks", [])
        if not checks:
            return {"status": "passed", "validation_level": "runtime_only"}
        from .httpverify import HttpVerifier

        for check in checks:
            if check.get("type") != "http" or not check.get("url"):
                continue
            request = HttpRequestInput(
                url=check["url"],
                method="GET",
                timeout_seconds=min(3, check.get("timeout_seconds", 3)),
            )
            data = await HttpVerifier(self.settings.http_verify, trusted_health_check=True).request(request)
            if data["status_code"] != check.get("expected_status", 200):
                raise DrawbridgeError("VERIFY_FAILED", "health check returned an unexpected status")
        return {"status": "passed", "validation_level": "http"}

    async def _status_for_binding(self, binding: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
        current = await self.database.get_current_release(binding["app"], binding["environment"])
        return {
            "app": binding["app"],
            "environment": binding["environment"],
            "services": binding["services"],
            "project_name": binding["project_name"],
            "current_release": current["release_id"] if current else None,
            "health": current.get("health") if current else {"status": "unknown"},
        }

    async def _require_binding(self, app: str, environment: str) -> dict[str, Any]:
        self._validate_name(app)
        if environment != "staging":
            raise DrawbridgeError("INVALID_PARAMETER", "only staging is supported")
        binding = await self.database.get_binding(app, environment)
        if binding is not None:
            return binding
        configured = self.settings.apps.get(app)
        if configured is None or environment not in configured.environments:
            raise DrawbridgeError("INVALID_PARAMETER", "application is not registered")
        return self._binding_from_config(app, environment, configured, configured.environments[environment])

    async def _diagnostic_file(
        self, app: str, environment: str, file_alias: str, release_id: str | None
    ) -> tuple[Path, dict[str, Any], Path]:
        binding = await self._require_binding(app, environment)
        editable = next(
            (item for item in binding.get("editable_files", []) if item.get("alias") == file_alias),
            None,
        )
        if editable is None:
            raise DrawbridgeError("FORBIDDEN_OPERATION", "file alias is not registered")
        relative_path = validate_subdir(editable["path"])
        if release_id is not None:
            self._validate_uuid(release_id, "release_id")
            release = await self.database.get_release(release_id)
            if (
                release is None
                or release.get("app") != app
                or release.get("environment") != environment
                or release.get("status") != "succeeded"
            ):
                raise DrawbridgeError("INVALID_PARAMETER", "release does not belong to the target application")
            root = Path(release["release_dir"]).resolve(strict=True)
        else:
            current = await self.database.get_current_release(app, environment)
            root = (
                Path(current["release_dir"]).resolve(strict=True)
                if current and current.get("release_dir")
                else Path(binding["source_workspace"]).resolve(strict=True)
            )
        path = root / relative_path
        return path, editable, root

    def _binding_from_config(
        self, app: str, environment: str, app_config: AppConfig, env: EnvironmentConfig
    ) -> dict[str, Any]:
        source = Path(app_config.git.repo_path)
        if not source.is_absolute():
            source = (self.base_dir / source).resolve()
        compose_file = validate_subdir(env.compose_file or "compose.yaml")
        release = self.settings.resolved_release_root(self.base_dir) / app / environment
        return {
            "app": app,
            "environment": environment,
            "version": 1,
            "status": "deployable",
            "source_workspace": str(source),
            "compose_file": compose_file,
            "compose_file_abs": str((source / compose_file).resolve()),
            "origin": app_config.git.origin,
            "allowed_ref_patterns": app_config.git.allowed_ref_patterns,
            "services": env.services,
            "restartable_services": env.restartable_services or env.services,
            "editable_files": [item.model_dump() for item in env.editable_files],
            "health_checks": [item.model_dump() for item in env.health_checks],
            "project_name": env.project_name,
            "release_root": str(Path(env.release_root) if env.release_root else release),
            "deployment_mode": env.deployment_mode,
            "current_revision": None,
            "data_mounts": [item.model_dump() for item in env.data_mounts],
            "runtime_profile_name": env.runtime_profile,
            "runtime_profile": self._runtime_profile_payload(env),
        }

    def _new_binding(
        self,
        *,
        app: str,
        environment: str,
        project: Path,
        relative_compose: str,
        compose: ComposeSpec,
        origin: str,
        head_sha: str,
        profile: str,
    ) -> dict[str, Any]:
        project_name = f"drawbridge-{app}-{environment}"
        release_root = self.settings.resolved_release_root(self.base_dir) / app / environment
        data_root = self.settings.resolve_path(self.settings.managed_data_root, self.base_dir) / app / environment
        configured_app = self.settings.apps.get(app)
        configured_env = configured_app.environments.get(environment) if configured_app else None
        editable_files = (
            [item.model_dump(by_alias=True) for item in configured_env.editable_files] if configured_env else []
        )
        health_checks = [item.model_dump() for item in configured_env.health_checks] if configured_env else []
        data_mounts = [item.model_dump() for item in configured_env.data_mounts] if configured_env else []
        runtime_profile = self._runtime_profile_payload(configured_env)
        return {
            "app": app,
            "environment": environment,
            "version": 1,
            "status": "deployable",
            "source_workspace": str(project),
            "compose_file": relative_compose,
            "compose_file_abs": str(compose.path),
            "origin": origin,
            "allowed_ref_patterns": configured_app.git.allowed_ref_patterns
            if configured_app
            else self._default_ref_patterns(),
            "services": list(compose.services),
            "restartable_services": configured_env.restartable_services
            if configured_env and configured_env.restartable_services
            else list(compose.services),
            "editable_files": editable_files,
            "health_checks": health_checks,
            "project_name": project_name,
            "release_root": str(release_root),
            "data_root": str(data_root),
            "data_mounts": data_mounts,
            "runtime_profile_name": configured_env.runtime_profile if configured_env else None,
            "runtime_profile": runtime_profile,
            "deployment_mode": configured_env.deployment_mode
            if configured_env
            else ("simulation" if self.settings.allow_simulation else "docker"),
            "current_revision": None,
            "profile": profile,
            "registered_head_sha": head_sha,
        }

    def _git_for_binding(self, binding: dict[str, Any]) -> GitRepository:
        return GitRepository(Path(binding["source_workspace"]), binding["origin"], binding["allowed_ref_patterns"])

    def _snapshot_fingerprint(
        self,
        compose: ComposeSpec,
        revision: dict[str, Any] | None,
        profile: BuildProfile,
    ) -> dict[str, Any]:
        build_declarations: dict[str, Any] = {}
        for service in sorted(compose.build_services):
            target = profile.targets.get(service) if profile.targets else None
            if target is None:
                target = profile
            build_declarations[service] = {
                "declaration": compose.raw["services"][service]["build"],
                "matched_target": {
                    "context": target.context,
                    "dockerfile": target.dockerfile,
                },
            }
        return {
            "workspace_revision_digest": self._workspace_revision_digest(revision),
            "service_set": sorted(compose.services),
            "compose_digest": self._digest(compose.raw),
            "build_declaration_digest": self._digest(build_declarations),
        }

    def _workspace_revision_digest(self, revision: dict[str, Any] | None) -> str | None:
        if revision is None:
            return None
        files = revision.get("files")
        if not isinstance(files, dict):
            raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision overlay is invalid")
        normalized_files: list[dict[str, str]] = []
        for file_info in files.values():
            if not isinstance(file_info, dict):
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision file is invalid")
            path = file_info.get("path")
            pre_digest = file_info.get("pre_digest")
            post_digest = file_info.get("post_digest")
            if not isinstance(path, str) or not isinstance(pre_digest, str) or not isinstance(post_digest, str):
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "workspace revision digest fields are invalid")
            normalized_files.append(
                {
                    "path": path,
                    "pre_digest": pre_digest,
                    "post_digest": post_digest,
                }
            )
        normalized_files.sort(key=lambda item: item["path"])
        return self._digest(
            {
                "base_commit_sha": revision.get("base_commit_sha"),
                "files": normalized_files,
            }
        )

    def _configuration_digest(self, binding: dict[str, Any]) -> str:
        fields = (
            "source_workspace",
            "compose_file",
            "origin",
            "allowed_ref_patterns",
            "services",
            "deployment_mode",
            "project_name",
            "data_mounts",
            "runtime_profile_name",
            "runtime_profile",
            "health_checks",
            "release_root",
            "data_root",
            "profile",
        )
        return self._digest({field: binding.get(field) for field in fields})

    @staticmethod
    def _validate_service_topology(
        *,
        snapshot_services: list[str],
        binding: dict[str, Any],
        current: dict[str, Any] | None,
    ) -> None:
        registered_services = sorted(binding.get("services", []))
        if snapshot_services != registered_services:
            raise DrawbridgeError(
                "UNSUPPORTED_SERVICE_CHANGE",
                "snapshot service topology differs from the registered service set",
            )
        if current is not None and sorted(current.get("services", [])) != snapshot_services:
            raise DrawbridgeError(
                "UNSUPPORTED_SERVICE_CHANGE",
                "snapshot service topology differs from the current release",
            )

    @staticmethod
    def _validate_plan_schema(plan: dict[str, Any]) -> None:
        if type(plan.get("plan_schema_version")) is not int or plan["plan_schema_version"] != _PLAN_SCHEMA_VERSION:
            raise DrawbridgeError("STALE_PLAN", "plan_schema_version is missing or unsupported", retryable=True)
        required = (
            "commit_sha",
            "workspace_revision",
            "workspace_revision_digest",
            "service_set",
            "compose_digest",
            "build_declaration_digest",
            "binding_version",
            "configuration_digest",
            "build_profile_digest",
        )
        missing = [field for field in required if field not in plan]
        if missing:
            raise DrawbridgeError("STALE_PLAN", f"plan is missing {missing[0]}", retryable=True)
        if not isinstance(plan["commit_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["commit_sha"]):
            raise DrawbridgeError("STALE_PLAN", "plan commit_sha is invalid", retryable=True)
        revision_id = plan["workspace_revision"]
        revision_digest = plan["workspace_revision_digest"]
        if revision_id is None:
            if revision_digest is not None:
                raise DrawbridgeError("STALE_PLAN", "workspace_revision_digest is invalid", retryable=True)
        elif (
            not isinstance(revision_id, str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                revision_id,
            )
            or not DrawbridgeService._is_sha256(revision_digest)
        ):
            raise DrawbridgeError("STALE_PLAN", "workspace revision fields are invalid", retryable=True)
        service_set = plan["service_set"]
        if (
            not isinstance(service_set, list)
            or not service_set
            or any(not isinstance(service, str) for service in service_set)
            or service_set != sorted(set(service_set))
        ):
            raise DrawbridgeError("STALE_PLAN", "plan service_set is invalid", retryable=True)
        for field in ("compose_digest", "build_declaration_digest", "configuration_digest", "build_profile_digest"):
            if not DrawbridgeService._is_sha256(plan[field]):
                raise DrawbridgeError("STALE_PLAN", f"plan {field} is invalid", retryable=True)
        if type(plan["binding_version"]) is not int or plan["binding_version"] < 1:
            raise DrawbridgeError("STALE_PLAN", "plan binding_version is invalid", retryable=True)

    @staticmethod
    def _compare_snapshot_fingerprint(plan: dict[str, Any], fingerprint: dict[str, Any]) -> None:
        for field in _PLAN_SNAPSHOT_FIELDS:
            if plan.get(field) != fingerprint.get(field):
                raise DrawbridgeError("STALE_PLAN", f"{field} changed after plan creation", retryable=True)

    def _default_ref_patterns(self) -> list[str]:
        return [
            r"^refs/heads/main$",
            r"^refs/heads/agent/[A-Za-z0-9._/-]+$",
            r"^refs/tags/v[0-9][A-Za-z0-9._-]*$",
        ]

    def _binding_identity(self, binding: dict[str, Any]) -> tuple[Any, ...]:
        return (
            binding.get("source_workspace"),
            binding.get("compose_file"),
            binding.get("origin"),
            tuple(binding.get("services", [])),
        )

    def _runtime_policy_identity(self, binding: dict[str, Any]) -> tuple[Any, ...]:
        return (
            binding.get("runtime_profile_name"),
            self._digest(binding.get("runtime_profile")),
        )

    def _runtime_profile_payload(self, environment: EnvironmentConfig | None) -> dict[str, Any] | None:
        if environment is None or environment.runtime_profile is None:
            return None
        return self.settings.runtime_profiles[environment.runtime_profile].model_dump()

    @staticmethod
    def _binding_summary(binding: dict[str, Any]) -> dict[str, Any]:
        return {
            key: binding.get(key)
            for key in (
                "app",
                "environment",
                "version",
                "status",
                "project_name",
                "source_workspace",
                "compose_file",
                "origin",
                "services",
                "restartable_services",
                "current_revision",
            )
        }

    @staticmethod
    def _public_summary(binding: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in DrawbridgeService._binding_summary(binding).items()
            if key != "source_workspace"
        }

    @staticmethod
    def _validate_name(value: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
            raise DrawbridgeError("INVALID_PARAMETER", "invalid name")

    @staticmethod
    def _validate_uuid(value: str, field: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
            raise DrawbridgeError("INVALID_PARAMETER", f"invalid {field}")

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value):
            raise DrawbridgeError("INVALID_PARAMETER", "invalid idempotency_key")

    @staticmethod
    def _validate_sensitive_document(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if re.search(r"(secret|password|token|private.?key|credential)", str(key), re.IGNORECASE):
                    raise DrawbridgeError("INVALID_PARAMETER", f"sensitive field is not editable: {key}")
                DrawbridgeService._validate_sensitive_document(child)
        elif isinstance(value, list):
            for child in value:
                DrawbridgeService._validate_sensitive_document(child)

    @staticmethod
    def _parse_patch(patch: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(patch, str):
            if len(patch.encode()) > 64 * 1024:
                raise DrawbridgeError("INVALID_PARAMETER", "patch is too large")
            parsed = yaml.safe_load(patch)
        else:
            parsed = patch
        if not isinstance(parsed, dict):
            raise DrawbridgeError("INVALID_PARAMETER", "patch must be a mapping")
        return parsed

    @staticmethod
    def _load_document(path: Path, editable: dict[str, Any]) -> Any:
        text = path.read_text(encoding="utf-8")
        if editable.get("display", "yaml") == "text":
            return {"content": text}
        try:
            return yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise DrawbridgeError("INVALID_PARAMETER", f"editable configuration is invalid: {exc}") from exc

    @staticmethod
    def _dump_document(value: Any, editable: dict[str, Any]) -> bytes:
        if editable.get("display", "yaml") == "text":
            content = value.get("content") if isinstance(value, dict) else None
            if not isinstance(content, str):
                raise DrawbridgeError("INVALID_PARAMETER", "text patch must contain content")
            return content.encode("utf-8")
        return yaml.safe_dump(value, sort_keys=False).encode("utf-8")

    @staticmethod
    def _merge_document(current: Any, patch: dict[str, Any]) -> Any:
        if not isinstance(current, dict):
            raise DrawbridgeError("INVALID_PARAMETER", "editable config must be a mapping")
        result = dict(current)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = DrawbridgeService._merge_document(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _safe_regular_file(path: Path, root: Path | None = None) -> None:
        if root is not None:
            root_path = root.absolute()
            current = path.absolute()
            try:
                path.resolve(strict=True).relative_to(root_path.resolve(strict=True))
            except (FileNotFoundError, ValueError) as exc:
                raise DrawbridgeError("PATCH_BASE_MISMATCH", "editable path escapes its workspace") from exc
            while current != root_path:
                if current.is_symlink():
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "editable path contains a symlink")
                if current.parent == current:
                    raise DrawbridgeError("PATCH_BASE_MISMATCH", "editable path escapes its workspace")
                current = current.parent
        if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise DrawbridgeError("PATCH_BASE_MISMATCH", "editable path is not a regular file")

    @staticmethod
    def _sha256(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _is_sha256(value: Any) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    @staticmethod
    def _digest(value: Any) -> str:
        try:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DrawbridgeError("INVALID_PARAMETER", "validated configuration cannot be normalized") from exc
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _redact(value: str) -> str:
        return re.sub(r"(?i)(token|password|secret|authorization)=\S+", r"\1=[REDACTED]", value)

    @staticmethod
    def _decode_offset(cursor: str) -> int:
        if not cursor:
            return 0
        if not cursor.isdigit() or int(cursor) < 0:
            raise ValueError("invalid cursor")
        return int(cursor)

    def _host_metrics(self) -> dict[str, Any]:
        cpu = self._read_cpu_usage()
        memory_total = 0
        memory_available = 0
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition(":")
                if key == "MemTotal":
                    memory_total = int(raw.strip().split()[0]) * 1024
                elif key == "MemAvailable":
                    memory_available = int(raw.strip().split()[0]) * 1024
        except (OSError, ValueError):
            pass
        disk = []
        for path in [
            self.settings.resolved_state_dir(self.base_dir),
            self.settings.resolved_release_root(self.base_dir),
        ]:
            try:
                usage = os.statvfs(path)
                disk.append(
                    {
                        "path_label": "drawbridge",
                        "total_bytes": usage.f_blocks * usage.f_frsize,
                        "available_bytes": usage.f_bavail * usage.f_frsize,
                    }
                )
            except OSError:
                continue
        return {
            "cpu_percent": cpu,
            "memory_total_bytes": memory_total,
            "memory_available_bytes": memory_available,
            "disk": disk,
        }

    @staticmethod
    def _read_cpu_usage() -> float | None:
        try:
            first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            first_values = [int(value) for value in first]
            time.sleep(0.01)
            second = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            second_values = [int(value) for value in second]
            total_delta = sum(second_values) - sum(first_values)
            idle_delta = second_values[3] - first_values[3]
            return round((1 - idle_delta / total_delta) * 100, 2) if total_delta else 0.0
        except (OSError, IndexError, ValueError, ZeroDivisionError):
            return None
