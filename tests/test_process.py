from __future__ import annotations

import sys
from pathlib import Path

import pytest

from drawbridge.process import ExecutionSpec, SafeExecutor, TerminationReason


@pytest.mark.asyncio
async def test_executor_uses_argv_and_does_not_interpret_shell_syntax(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    spec = ExecutionSpec(
        program=sys.executable,
        argv=["-c", "import sys; print(sys.argv[1])", f"$(touch {marker});"],
        cwd=tmp_path,
        timeout_seconds=2,
        output_limit_bytes=4096,
        label="argv-test",
    )

    result = await SafeExecutor().execute(spec)

    assert result.exit_code == 0
    assert "$(touch" in result.stdout
    assert not marker.exists()
    assert result.termination_reason is TerminationReason.EXITED


@pytest.mark.asyncio
async def test_executor_terminates_on_timeout() -> None:
    spec = ExecutionSpec(
        program=sys.executable,
        argv=["-c", "import time; time.sleep(10)"],
        cwd=Path.cwd(),
        timeout_seconds=0.1,
        output_limit_bytes=4096,
        label="timeout-test",
    )

    result = await SafeExecutor().execute(spec)

    assert result.exit_code is not None
    assert result.exit_code != 0
    assert result.termination_reason is TerminationReason.TIMEOUT


@pytest.mark.asyncio
async def test_executor_stops_when_output_budget_is_exceeded() -> None:
    spec = ExecutionSpec(
        program=sys.executable,
        argv=["-c", "print('x' * 10000)"],
        cwd=Path.cwd(),
        timeout_seconds=2,
        output_limit_bytes=128,
        label="output-test",
    )

    result = await SafeExecutor().execute(spec)

    assert result.termination_reason is TerminationReason.OUTPUT_LIMIT
    assert result.truncated is True
    assert result.total_output_bytes >= 128
