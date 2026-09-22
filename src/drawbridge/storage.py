from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiosqlite


class StorageError(RuntimeError):
    pass


class IdempotencyConflict(StorageError):
    pass


class QueueFull(StorageError):
    pass


def create_request_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    action: str
    app: str | None
    environment: str | None
    payload: dict[str, Any]
    idempotency_key: str
    request_hash: str
    status: str
    created_at: float
    queued_until: float
    started_at: float | None
    finished_at: float | None
    owner: str | None
    result: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> JobRecord:
        return cls(
            job_id=row["job_id"],
            action=row["action"],
            app=row["app"],
            environment=row["environment"],
            payload=json.loads(row["payload"]),
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            status=row["status"],
            created_at=row["created_at"],
            queued_until=row["queued_until"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            owner=row["owner"],
            result=json.loads(row["result"]) if row["result"] else None,
        )


@dataclass(frozen=True)
class EnqueueResult:
    job_id: str
    created: bool


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS app_bindings (
    app TEXT NOT NULL,
    environment TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (app, environment)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    app TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    app TEXT,
    environment TEXT,
    plan_id TEXT UNIQUE,
    payload TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    queued_until REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    owner TEXT,
    heartbeat_at REAL,
    result TEXT,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_target_idx ON jobs(status, app, environment);

CREATE TABLE IF NOT EXISTS steps (
    step_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL,
    finished_at REAL,
    result TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    app TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    replaces_release_id TEXT,
    restored_from_release_id TEXT
);

CREATE INDEX IF NOT EXISTS releases_current_idx ON releases(app, environment, status, created_at);

CREATE TABLE IF NOT EXISTS workspace_revisions (
    revision_id TEXT PRIMARY KEY,
    app TEXT NOT NULL,
    environment TEXT NOT NULL,
    base_commit_sha TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    job_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    request_id TEXT,
    event_type TEXT NOT NULL,
    app TEXT,
    environment TEXT,
    job_id TEXT,
    plan_id TEXT,
    release_id TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = await self._get_connection()
            await connection.executescript(SCHEMA)
            await connection.commit()
            self._initialized = True

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            self._initialized = False

    async def _get_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            self._connection = await aiosqlite.connect(self.path)
            self._connection.row_factory = aiosqlite.Row
            await self._connection.execute("PRAGMA foreign_keys = ON")
            await self._connection.execute("PRAGMA busy_timeout = 5000")
        return self._connection

    @asynccontextmanager
    async def _locked_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            yield await self._get_connection()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            connection = await self._get_connection()
            started = False
            try:
                await connection.execute("BEGIN IMMEDIATE")
                started = True
                yield connection
                await connection.commit()
            except BaseException:
                if started:
                    await connection.rollback()
                raise

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection, query: str, parameters: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(query, parameters)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def enqueue_job(
        self,
        *,
        action: str,
        app: str | None,
        environment: str | None,
        payload: dict[str, Any],
        idempotency_key: str,
        request_hash: str,
        max_queued_jobs: int,
        max_queued_jobs_per_target: int,
        queue_timeout_seconds: int = 600,
        plan_id: str | None = None,
    ) -> EnqueueResult:
        async with self._transaction() as connection:
            existing = await self._fetchone(
                connection,
                "SELECT job_id, request_hash FROM idempotency_keys WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency key is bound to a different request")
                return EnqueueResult(job_id=existing["job_id"], created=False)

            if plan_id is not None:
                existing_plan_job = await self._fetchone(
                    connection, "SELECT job_id FROM jobs WHERE plan_id = ?", (plan_id,)
                )
                if existing_plan_job is not None:
                    await connection.execute(
                        "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
                        (
                            idempotency_key,
                            action,
                            request_hash,
                            existing_plan_job["job_id"],
                            time.time(),
                        ),
                    )
                    return EnqueueResult(job_id=existing_plan_job["job_id"], created=False)

            queued = await self._fetchone(connection, "SELECT COUNT(*) AS count FROM jobs WHERE status = 'queued'")
            assert queued is not None
            if queued["count"] >= max_queued_jobs:
                raise QueueFull("global mutation queue is full")
            if app is not None:
                target = await self._fetchone(
                    connection,
                    "SELECT COUNT(*) AS count FROM jobs WHERE status = 'queued' AND app = ? AND environment = ?",
                    (app, environment),
                )
                assert target is not None
                if target["count"] >= max_queued_jobs_per_target:
                    raise QueueFull("target mutation queue is full")

            now = time.time()
            job_id = str(uuid.uuid4())
            await connection.execute(
                """INSERT INTO jobs(
                    job_id, action, app, environment, plan_id, payload, idempotency_key,
                    request_hash, status, created_at, queued_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    job_id,
                    action,
                    app,
                    environment,
                    plan_id,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    idempotency_key,
                    request_hash,
                    now,
                    now + max(queue_timeout_seconds, 0),
                ),
            )
            await connection.execute(
                "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, action, request_hash, job_id, now),
            )
            return EnqueueResult(job_id=job_id, created=True)

    async def claim_next_job(self, *, owner: str, max_running_jobs: int = 1) -> JobRecord | None:
        async with self._transaction() as connection:
            now = time.time()
            running = await self._fetchone(
                connection,
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'",
            )
            assert running is not None
            if running["count"] >= max_running_jobs:
                return None
            while True:
                row = await self._fetchone(
                    connection,
                    "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1",
                )
                if row is None:
                    return None
                if row["queued_until"] <= now:
                    await connection.execute(
                        "UPDATE jobs SET status = 'queue_expired', finished_at = ?, result = ? WHERE job_id = ?",
                        (
                            now,
                            json.dumps({"error_code": "QUEUE_TIMEOUT", "message": "queue timeout"}),
                            row["job_id"],
                        ),
                    )
                    continue
                cursor = await connection.execute(
                    "UPDATE jobs SET status = 'running', owner = ?, started_at = ?, heartbeat_at = ? "
                    "WHERE job_id = ? AND status = 'queued'",
                    (owner, now, now, row["job_id"]),
                )
                try:
                    claimed_count = cursor.rowcount
                finally:
                    await cursor.close()
                if claimed_count != 1:
                    continue
                claimed = await self._fetchone(connection, "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],))
                assert claimed is not None
                return JobRecord.from_row(claimed)

    async def get_job(self, job_id: str) -> JobRecord | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(connection, "SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return JobRecord.from_row(row) if row else None

    async def get_job_by_plan(self, plan_id: str) -> JobRecord | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(connection, "SELECT * FROM jobs WHERE plan_id = ?", (plan_id,))
        return JobRecord.from_row(row) if row else None

    async def finish_job(self, job_id: str, *, status: str, result: dict[str, Any]) -> None:
        async with self._transaction() as connection:
            now = time.time()
            await connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, heartbeat_at = ?, result = ? WHERE job_id = ?",
                (status, now, now, json.dumps(result, sort_keys=True, ensure_ascii=False), job_id),
            )

    async def complete_job_with_release(
        self,
        *,
        job_id: str,
        result: dict[str, Any],
        release_id: str,
        app: str,
        environment: str,
        release_payload: dict[str, Any],
        release_status: str = "succeeded",
        replaces_release_id: str | None = None,
        restored_from_release_id: str | None = None,
        event_type: str | None = None,
    ) -> None:
        async with self._transaction() as connection:
            now = time.time()
            await connection.execute(
                "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id,
                    app,
                    environment,
                    release_status,
                    json.dumps(release_payload, sort_keys=True),
                    now,
                    replaces_release_id,
                    restored_from_release_id,
                ),
            )
            if event_type is not None:
                await connection.execute(
                    "INSERT INTO events(event_id, created_at, request_id, event_type, app, environment, "
                    "job_id, plan_id, release_id, payload) VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        now,
                        event_type,
                        app,
                        environment,
                        job_id,
                        release_id,
                        json.dumps(release_payload, sort_keys=True),
                    ),
                )
            cursor = await connection.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = ?, heartbeat_at = ?, result = ? "
                "WHERE job_id = ? AND status = 'running'",
                (now, now, json.dumps(result, sort_keys=True, ensure_ascii=False), job_id),
            )
            try:
                completed_count = cursor.rowcount
            finally:
                await cursor.close()
            if completed_count != 1:
                raise StorageError("release completion requires exactly one running job")

    async def heartbeat(self, job_id: str) -> None:
        async with self._transaction() as connection:
            await connection.execute("UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?", (time.time(), job_id))

    async def save_binding(self, app: str, environment: str, payload: dict[str, Any], *, status: str) -> int:
        async with self._transaction() as connection:
            existing = await self._fetchone(
                connection,
                "SELECT version FROM app_bindings WHERE app = ? AND environment = ?",
                (app, environment),
            )
            version = (existing["version"] + 1) if existing else 1
            await connection.execute(
                """INSERT INTO app_bindings(app, environment, version, status, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(app, environment) DO UPDATE SET version=excluded.version,
                       status=excluded.status, payload=excluded.payload, created_at=excluded.created_at""",
                (app, environment, version, status, json.dumps(payload, sort_keys=True), time.time()),
            )
            return version

    async def get_binding(self, app: str, environment: str) -> dict[str, Any] | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(
                connection,
                "SELECT * FROM app_bindings WHERE app = ? AND environment = ?",
                (app, environment),
            )
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["version"] = row["version"]
        payload["status"] = row["status"]
        return cast(dict[str, Any], payload)

    async def save_plan(
        self,
        *,
        plan_id: str,
        app: str,
        environment: str,
        payload: dict[str, Any],
        expires_at: float,
    ) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                "INSERT INTO plans VALUES (?, ?, ?, 'planned', ?, ?, ?)",
                (
                    plan_id,
                    app,
                    environment,
                    json.dumps(payload, sort_keys=True),
                    time.time(),
                    expires_at,
                ),
            )

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(connection, "SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload.update(
            {
                "plan_id": row["plan_id"],
                "app": row["app"],
                "environment": row["environment"],
                "status": row["status"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }
        )
        return cast(dict[str, Any], payload)

    async def mark_plan(self, plan_id: str, status: str) -> None:
        async with self._transaction() as connection:
            await connection.execute("UPDATE plans SET status = ? WHERE plan_id = ?", (status, plan_id))

    async def save_revision(
        self,
        *,
        revision_id: str,
        app: str,
        environment: str,
        base_commit_sha: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                "INSERT INTO workspace_revisions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    app,
                    environment,
                    base_commit_sha,
                    json.dumps(payload, sort_keys=True),
                    time.time(),
                ),
            )

    async def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(
                connection, "SELECT * FROM workspace_revisions WHERE revision_id = ?", (revision_id,)
            )
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload.update(
            {
                "revision_id": row["revision_id"],
                "app": row["app"],
                "environment": row["environment"],
                "base_commit_sha": row["base_commit_sha"],
                "created_at": row["created_at"],
            }
        )
        return cast(dict[str, Any], payload)

    async def save_release(
        self,
        *,
        release_id: str,
        app: str,
        environment: str,
        payload: dict[str, Any],
        status: str = "succeeded",
        replaces_release_id: str | None = None,
        restored_from_release_id: str | None = None,
    ) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id,
                    app,
                    environment,
                    status,
                    json.dumps(payload, sort_keys=True),
                    time.time(),
                    replaces_release_id,
                    restored_from_release_id,
                ),
            )

    async def get_release(self, release_id: str) -> dict[str, Any] | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(connection, "SELECT * FROM releases WHERE release_id = ?", (release_id,))
        return self._release_from_row(row) if row else None

    async def get_current_release(self, app: str, environment: str) -> dict[str, Any] | None:
        async with self._locked_connection() as connection:
            row = await self._fetchone(
                connection,
                "SELECT * FROM releases WHERE app = ? AND environment = ? "
                "AND status = 'succeeded' ORDER BY created_at DESC LIMIT 1",
                (app, environment),
            )
        return self._release_from_row(row) if row else None

    async def list_releases(self, app: str, environment: str) -> list[dict[str, Any]]:
        async with self._locked_connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM releases WHERE app = ? AND environment = ? ORDER BY created_at DESC",
                (app, environment),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._release_from_row(row) for row in rows]

    @staticmethod
    def _release_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        payload.update(
            {
                "release_id": row["release_id"],
                "app": row["app"],
                "environment": row["environment"],
                "status": row["status"],
                "created_at": row["created_at"],
                "replaces_release_id": row["replaces_release_id"],
                "restored_from_release_id": row["restored_from_release_id"],
            }
        )
        return cast(dict[str, Any], payload)

    async def set_control(self, key: str, value: Any) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                "INSERT INTO control_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, sort_keys=True), time.time()),
            )

    async def get_control(self, key: str, default: Any = None) -> Any:
        async with self._locked_connection() as connection:
            row = await self._fetchone(connection, "SELECT value FROM control_state WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def append_event(self, event_type: str, payload: dict[str, Any], **scope: str | None) -> None:
        async with self._transaction() as connection:
            await connection.execute(
                "INSERT INTO events(event_id, created_at, request_id, event_type, app, environment, "
                "job_id, plan_id, release_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    time.time(),
                    scope.get("request_id"),
                    event_type,
                    scope.get("app"),
                    scope.get("environment"),
                    scope.get("job_id"),
                    scope.get("plan_id"),
                    scope.get("release_id"),
                    json.dumps(payload, sort_keys=True),
                ),
            )
