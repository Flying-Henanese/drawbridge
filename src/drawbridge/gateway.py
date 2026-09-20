from __future__ import annotations

import argparse
import ipaddress
import secrets
from pathlib import Path
from typing import Any, Literal, cast

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Settings, load_settings
from .models import HttpRequestInput, LogsInput
from .service import DrawbridgeService
from .storage import Database


class GatewayAccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: Settings, *, base_dir: Path) -> None:
        super().__init__(app)
        self.settings = settings
        self.base_dir = base_dir
        self.networks = [ipaddress.ip_network(value, strict=False) for value in settings.allowed_client_cidrs]

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        client = request.client.host if request.client else ""
        try:
            address = ipaddress.ip_address(client)
        except ValueError:
            return JSONResponse({"error": "client address is invalid"}, status_code=403)
        if not any(address in network for network in self.networks):
            return JSONResponse({"error": "client address is not allowed"}, status_code=403)
        host = request.headers.get("host", "").split(":", 1)[0]
        if self.settings.auth.allowed_hosts and host not in self.settings.auth.allowed_hosts:
            return JSONResponse({"error": "host is not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in self.settings.auth.allowed_origins:
            return JSONResponse({"error": "origin is not allowed"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > self.settings.ingress_max_body_bytes
            ):
                return JSONResponse({"error": "request body is too large"}, status_code=413)
        if self.settings.auth.mode == "token":
            expected = self.settings.token_value(self.base_dir)
            authorization = request.headers.get("authorization", "")
            provided = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            if not expected or not secrets.compare_digest(provided, expected):
                return JSONResponse({"error": "authentication required"}, status_code=401)
        return cast(Response, await call_next(request))


def build_mcp(service: DrawbridgeService, settings: Settings, *, base_dir: Path) -> FastMCP:
    backend = service
    mcp = FastMCP(
        name="drawbridge",
        instructions="Drawbridge exposes guarded deployment and runtime observation operations.",
        stateless_http=True,
        streamable_http_path="/mcp",
        max_request_body_size=settings.ingress_max_body_bytes,
    )

    async def ready() -> None:
        await backend.database.initialize()

    @mcp.tool(name="ops_catalog")
    async def ops_catalog() -> dict[str, Any]:
        await ready()
        return await backend.catalog()

    @mcp.tool(name="ops_status")
    async def ops_status(app: str | None = None, environment: str = "staging") -> dict[str, Any]:
        await ready()
        return await backend.status(app=app, environment=environment)

    @mcp.tool(name="ops_logs")
    async def ops_logs(
        app: str,
        service: str,
        environment: str = "staging",
        since_seconds: int = 300,
        query: str = "",
        cursor: str = "",
        limit: int = 100,
        tail: int = 200,
    ) -> dict[str, Any]:
        await ready()
        request = LogsInput(
            app=app,
            environment=cast(Literal["staging"], environment),
            service=service,
            since_seconds=since_seconds,
            query=query,
            cursor=cursor,
            limit=limit,
            tail=tail,
        )
        return await backend.logs(request)

    @mcp.tool(name="ops_app_discover")
    async def ops_app_discover(limit: int = 100, cursor: str = "") -> dict[str, Any]:
        await ready()
        return await backend.app_discover(limit=limit, cursor=cursor)

    @mcp.tool(name="ops_app_register")
    async def ops_app_register(
        app: str,
        project_dir: str,
        compose_file: str,
        idempotency_key: str,
        environment: str = "staging",
        profile: str = "default",
    ) -> dict[str, Any]:
        await ready()
        return await backend.app_register(
            app=app,
            environment=environment,
            project_dir=project_dir,
            compose_file=compose_file,
            idempotency_key=idempotency_key,
            profile=profile,
        )

    @mcp.tool(name="ops_git_status")
    async def ops_git_status(app: str, environment: str = "staging") -> dict[str, Any]:
        await ready()
        return await backend.git_status(app=app, environment=environment)

    @mcp.tool(name="ops_git_log")
    async def ops_git_log(
        app: str,
        git_ref: str,
        environment: str = "staging",
        count: int = 20,
    ) -> dict[str, Any]:
        await ready()
        return await backend.git_log(app=app, environment=environment, git_ref=git_ref, count=count)

    @mcp.tool(name="ops_process_list")
    async def ops_process_list() -> dict[str, Any]:
        await ready()
        return await backend.process_list()

    @mcp.tool(name="ops_config_read")
    async def ops_config_read(
        app: str,
        file_alias: str,
        environment: str = "staging",
        release_id: str | None = None,
    ) -> dict[str, Any]:
        await ready()
        return await backend.config_read(
            app=app,
            environment=environment,
            file_alias=file_alias,
            release_id=release_id,
        )

    @mcp.tool(name="ops_config_validate")
    async def ops_config_validate(
        app: str,
        file_alias: str,
        environment: str = "staging",
        release_id: str | None = None,
    ) -> dict[str, Any]:
        await ready()
        return await backend.config_validate(
            app=app,
            environment=environment,
            file_alias=file_alias,
            release_id=release_id,
        )

    @mcp.tool(name="ops_release_plan")
    async def ops_release_plan(
        app: str,
        git_ref: str,
        environment: str = "staging",
        source_mode: str = "fetch",
        workspace_revision: str | None = None,
        workflow: str = "deploy_basic",
    ) -> dict[str, Any]:
        await ready()
        return await backend.release_plan(
            app=app,
            environment=environment,
            source_mode=source_mode,
            git_ref=git_ref,
            workspace_revision=workspace_revision,
            workflow=workflow,
        )

    @mcp.tool(name="ops_release_apply")
    async def ops_release_apply(plan_id: str, idempotency_key: str) -> dict[str, Any]:
        await ready()
        return await backend.release_apply(plan_id=plan_id, idempotency_key=idempotency_key)

    @mcp.tool(name="ops_release_status")
    async def ops_release_status(job_id: str) -> dict[str, Any]:
        await ready()
        return await backend.release_status(job_id)

    @mcp.tool(name="ops_release_rollback")
    async def ops_release_rollback(
        app: str,
        release_id: str,
        reason: str,
        idempotency_key: str,
        environment: str = "staging",
    ) -> dict[str, Any]:
        await ready()
        return await backend.release_rollback(
            app=app,
            environment=environment,
            release_id=release_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(name="ops_service_restart")
    async def ops_service_restart(
        app: str,
        service: str,
        reason: str,
        idempotency_key: str,
        environment: str = "staging",
    ) -> dict[str, Any]:
        await ready()
        return await backend.service_restart(
            app=app,
            environment=environment,
            service=service,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(name="ops_workspace_patch")
    async def ops_workspace_patch(
        app: str,
        file_alias: str,
        patch: dict[str, Any] | str,
        expected_revision: str,
        idempotency_key: str,
        environment: str = "staging",
        base_commit_sha: str | None = None,
    ) -> dict[str, Any]:
        await ready()
        return await backend.workspace_patch(
            app=app,
            environment=environment,
            file_alias=file_alias,
            patch=patch,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            base_commit_sha=base_commit_sha,
        )

    @mcp.tool(name="ops_http_request")
    async def ops_http_request(
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout_seconds: int = 10,
        app: str | None = None,
        release_id: str | None = None,
        idempotency_key: str | None = None,
        environment: str = "staging",
    ) -> dict[str, Any]:
        await ready()
        request = HttpRequestInput(
            url=url,
            method=cast(Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], method),
            headers=headers or {},
            body=body,
            timeout_seconds=timeout_seconds,
            app=app,
            release_id=release_id,
            idempotency_key=idempotency_key,
            environment=cast(Literal["staging"], environment),
        )
        return await backend.http_request(request)

    @mcp.tool(name="ops_operation_run")
    async def ops_operation_run(
        operation: str,
        parameters: dict[str, Any] | None = None,
        app: str | None = None,
        environment: str = "staging",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await ready()
        values = parameters or {}
        if operation == "service_restart":
            if not idempotency_key:
                return {
                    "status": "error",
                    "error": {"code": "INVALID_PARAMETER", "message": "idempotency_key required"},
                }
            return await backend.service_restart(
                app=app or "",
                environment=environment,
                service=str(values.get("service", "")),
                reason=str(values.get("reason", "")),
                idempotency_key=idempotency_key,
            )
        if operation == "git_status" and app:
            return await backend.git_status(app=app, environment=environment)
        if operation == "git_log" and app:
            git_ref = values.get("git_ref")
            count = values.get("count", 20)
            if not isinstance(git_ref, str) or not isinstance(count, int) or isinstance(count, bool):
                return {
                    "status": "error",
                    "error": {"code": "INVALID_PARAMETER", "message": "git_ref and count are invalid"},
                }
            return await backend.git_log(app=app, environment=environment, git_ref=git_ref, count=count)
        if operation == "process_list":
            return await backend.process_list()
        if operation == "config_read" and app:
            file_alias = values.get("file_alias")
            release_id = values.get("release_id")
            if not isinstance(file_alias, str) or (release_id is not None and not isinstance(release_id, str)):
                return {
                    "status": "error",
                    "error": {"code": "INVALID_PARAMETER", "message": "file_alias or release_id is invalid"},
                }
            return await backend.config_read(
                app=app, environment=environment, file_alias=file_alias, release_id=release_id
            )
        if operation == "config_validate" and app:
            file_alias = values.get("file_alias")
            release_id = values.get("release_id")
            if not isinstance(file_alias, str) or (release_id is not None and not isinstance(release_id, str)):
                return {
                    "status": "error",
                    "error": {"code": "INVALID_PARAMETER", "message": "file_alias or release_id is invalid"},
                }
            return await backend.config_validate(
                app=app, environment=environment, file_alias=file_alias, release_id=release_id
            )
        return {
            "status": "error",
            "error": {"code": "UNKNOWN_OPERATION", "message": "operation is not public"},
        }

    @mcp.tool(name="ops_workflow_run")
    async def ops_workflow_run(
        workflow: str,
        plan_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        await ready()
        if workflow not in {"deploy_basic", "deploy_verify"}:
            return {
                "status": "error",
                "error": {"code": "UNKNOWN_OPERATION", "message": "workflow is not registered"},
            }
        return await backend.release_apply(plan_id=plan_id, idempotency_key=idempotency_key)

    @mcp.resource("drawbridge://environments")
    async def environments() -> str:
        return "staging"

    @mcp.resource("drawbridge://apps")
    async def apps() -> str:
        await ready()
        bindings = []
        for app in settings.apps:
            bindings.append(app)
        return "\n".join(bindings)

    return mcp


def create_app(config_path: Path) -> Any:
    settings = load_settings(config_path)
    base_dir = config_path.parent.resolve()
    database = Database(settings.resolved_state_dir(base_dir) / "state.db")
    service = DrawbridgeService(settings, database, base_dir=base_dir)
    mcp = build_mcp(service, settings, base_dir=base_dir)
    app = mcp.streamable_http_app()
    app.add_middleware(GatewayAccessMiddleware, settings=settings, base_dir=base_dir)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Drawbridge MCP gateway")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    app = create_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, forwarded_allow_ips="")
