from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TerminationReason(str, Enum):
    EXITED = "exited"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    START_FAILED = "start_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ExecutionSpec:
    program: str
    argv: list[str]
    cwd: Path
    timeout_seconds: float
    output_limit_bytes: int
    label: str
    env: dict[str, str] = field(default_factory=dict)
    accepted_exit_codes: frozenset[int] = frozenset({0})


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    termination_reason: TerminationReason
    duration_ms: int
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    total_output_bytes: int
    truncated: bool

    @property
    def accepted(self) -> bool:
        return self.exit_code is not None and self.termination_reason is TerminationReason.EXITED


class SafeExecutor:
    """Execute one server-created argv vector without a shell.

    The executor deliberately accepts an already validated ``ExecutionSpec``. It does not
    parse strings, inherit the caller's environment, or accept stdin from the caller.
    """

    def __init__(self, *, grace_seconds: float = 0.5) -> None:
        self.grace_seconds = grace_seconds

    async def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        if not os.path.isabs(spec.program):
            raise ValueError("execution program must be an absolute path")
        if any(not isinstance(arg, str) for arg in spec.argv):
            raise TypeError("argv must contain strings")
        if spec.timeout_seconds <= 0 or spec.output_limit_bytes <= 0:
            raise ValueError("execution budget must be positive")

        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONUNBUFFERED": "1",
        }
        env.update(spec.env)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                spec.program,
                *spec.argv,
                cwd=str(spec.cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            return ExecutionResult(
                exit_code=None,
                termination_reason=TerminationReason.START_FAILED,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr=str(exc),
                stdout_bytes=0,
                stderr_bytes=len(str(exc).encode()),
                total_output_bytes=len(str(exc).encode()),
                truncated=False,
            )

        output_lock = asyncio.Lock()
        termination_lock = asyncio.Lock()
        state: dict[str, Any] = {
            "stdout": bytearray(),
            "stderr": bytearray(),
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "total": 0,
            "truncated": False,
            "reason": TerminationReason.EXITED,
            "terminated": False,
        }

        async def terminate(reason: TerminationReason) -> None:
            async with termination_lock:
                if state["terminated"]:
                    return
                state["terminated"] = True
                state["reason"] = reason
                await self._terminate_process_group(process)

        async def read_stream(name: str, stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                async with output_lock:
                    state[f"{name}_bytes"] += len(chunk)
                    state["total"] += len(chunk)
                    remaining = max(spec.output_limit_bytes - len(state[name]), 0)
                    if remaining:
                        state[name].extend(chunk[:remaining])
                    over_limit = state["total"] > spec.output_limit_bytes
                    if over_limit:
                        state["truncated"] = True
                if over_limit:
                    await terminate(TerminationReason.OUTPUT_LIMIT)

        readers = [
            asyncio.create_task(read_stream("stdout", process.stdout)),
            asyncio.create_task(read_stream("stderr", process.stderr)),
        ]
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=spec.timeout_seconds)
            except asyncio.TimeoutError:
                await terminate(TerminationReason.TIMEOUT)
                await process.wait()
            except asyncio.CancelledError:
                await terminate(TerminationReason.CANCELLED)
                await process.wait()
                raise
            await asyncio.gather(*readers)
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        reason = state["reason"]
        if reason is TerminationReason.EXITED and process.returncode is not None:
            reason = TerminationReason.EXITED
        return ExecutionResult(
            exit_code=process.returncode,
            termination_reason=reason,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=bytes(state["stdout"]).decode("utf-8", errors="replace"),
            stderr=bytes(state["stderr"]).decode("utf-8", errors="replace"),
            stdout_bytes=state["stdout_bytes"],
            stderr_bytes=state["stderr_bytes"],
            total_output_bytes=state["total"],
            truncated=bool(state["truncated"]),
        )

    async def _terminate_process_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()
