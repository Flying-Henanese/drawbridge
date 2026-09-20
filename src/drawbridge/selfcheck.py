from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Settings


def run_self_check(settings: Settings, *, base_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, *, blocking: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "blocking": blocking, "detail": detail})

    python_ok = sys.version_info >= (3, 12)
    add("python", python_ok, ".".join(str(value) for value in sys.version_info[:3]), blocking=True)
    git = shutil.which("git")
    add("git", git is not None, git or "not found", blocking=True)
    docker = shutil.which("docker")
    add("docker", docker is not None, docker or "not found", blocking=False)
    if docker:
        compose_result = subprocess.run(
            [docker, "compose", "version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
        compose = (compose_result.stdout or compose_result.stderr).strip()
        add("docker_compose", bool(compose), compose or "compose plugin unavailable", blocking=False)
    token = settings.token_value(base_dir)
    add(
        "gateway_auth",
        settings.auth.mode == "none" or bool(token),
        "configured" if token or settings.auth.mode == "none" else "missing bearer token",
        blocking=True,
    )
    state_dir = settings.resolved_state_dir(base_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("state_directory", True, str(state_dir), blocking=True)
    except OSError as exc:
        add("state_directory", False, str(exc), blocking=True)
    try:
        connection = sqlite3.connect(state_dir / "selfcheck.db")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.close()
        add("sqlite_wal", journal_mode.lower() == "wal", str(journal_mode), blocking=True)
    except sqlite3.Error as exc:
        add("sqlite_wal", False, str(exc), blocking=True)
    if settings.http_verify.allowed_cidrs:
        add("http_egress_policy", True, f"{len(settings.http_verify.allowed_cidrs)} CIDR(s)")
    else:
        add("http_egress_policy", False, "no outbound CIDR is configured", blocking=False)
    buildkit_profiles = [profile for profile in settings.build_profiles.values() if profile.mode == "buildkit"]
    buildctl = shutil.which("buildctl")
    add(
        "rootless_buildkit",
        not buildkit_profiles or buildctl is not None,
        buildctl or "not configured",
        blocking=bool(buildkit_profiles),
    )
    for name, profile in settings.build_profiles.items():
        if profile.mode != "buildkit":
            continue
        address = profile.buildkit_socket or ""
        socket = Path(address.removeprefix("unix://")) if address.startswith("unix:///") else None
        ready = socket is not None and socket.is_socket() and not socket.is_symlink()
        add(
            f"buildkit_socket_{name}",
            ready,
            str(socket) if socket is not None else "local Unix socket is not configured",
            blocking=True,
        )
    return {
        "ok": all(check["ok"] for check in checks if check["blocking"]),
        "checks": checks,
        "runtime_baseline": "Python 3.12+",
    }
