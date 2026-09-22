"""Reproducible, isolated validation of the Drawbridge MVP simulation path."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from drawbridge import MINIMUM_PYTHON

ROOT = Path(__file__).resolve().parents[1]


def run_step(name: str, argv: list[str], output: Path, *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(argv, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    (output / f"{name}.log").write_text(
        f"$ {' '.join(argv)}\nexit_code={result.returncode}\n\nSTDOUT\n{result.stdout}\nSTDERR\n{result.stderr}",
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(f"{name} failed (exit {result.returncode}); see {name}.log")
    return result.stdout


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def check_runtime_metadata() -> dict[str, str]:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires_python = metadata["project"]["requires-python"]
    ruff_target = metadata["tool"]["ruff"]["target-version"]
    mypy_version = metadata["tool"]["mypy"]["python_version"]
    baseline = f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"
    require(requires_python == f">={baseline}", "project.requires-python must match the runtime baseline")
    require(ruff_target == f"py{baseline.replace('.', '')}", "Ruff target-version must match the runtime baseline")
    require(mypy_version == baseline, "mypy python_version must match the runtime baseline")
    return {
        "requires_python": requires_python,
        "ruff_target": ruff_target,
        "mypy_python_version": mypy_version,
    }


def make_fixture(root: Path, output: Path) -> tuple[Path, Path, str]:
    project = root / "projects" / "verification-fixture"
    project.mkdir(parents=True)
    (project / "compose.yaml").write_text(
        "services:\n  app:\n    image: example.invalid/drawbridge/fixture:simulation-only\n",
        encoding="utf-8",
    )
    run_step("git_init", ["git", "init", "--initial-branch=main", str(project)], output)
    run_step(
        "git_remote",
        ["git", "-C", str(project), "remote", "add", "origin", "https://example.invalid/verification.git"],
        output,
    )
    run_step("git_add", ["git", "-C", str(project), "add", "."], output)
    run_step(
        "git_commit",
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Drawbridge Harness",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        output,
    )
    sha = run_step("git_sha", ["git", "-C", str(project), "rev-parse", "HEAD"], output).strip()

    config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    config.update(
        state_dir=str(root / "state"),
        log_dir=str(root / "log"),
        allowed_project_roots=[str(root / "projects")],
        managed_release_root=str(root / "releases"),
        managed_template_root=str(root / "templates"),
        managed_data_root=str(root / "data"),
    )
    config["auth"]["token"] = "isolated-harness-token"
    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path, project, sha


def check_smoke(result: dict[str, Any], root: Path, sha: str) -> dict[str, Any]:
    for stage in ("registered", "planned", "applied", "status"):
        require(result.get(stage, {}).get("status") == "ok", f"simulation {stage} did not return ok")
    plan = result["planned"]["data"]
    job = result["status"]["data"]
    require(plan["commit_sha"] == sha, "plan commit differs from fixture HEAD")
    require(job["status"] == "succeeded", "simulation job did not succeed")
    require(job["source_sha"] == sha, "release source differs from fixture HEAD")
    require(job["mode"] == "simulation", "job used a non-simulation deployment mode")
    require(job["health"] == {"status": "passed", "validation_level": "simulation"}, "simulation health failed")
    require(job["services"] == ["app"], "unexpected deployed service set")
    require(job.get("release_id"), "simulation job has no release ID")
    release = root / "releases" / "verification" / "staging" / job["release_id"]
    for filename in ("release.json", "compose.yaml"):
        require((release / filename).is_file(), f"missing release artifact: {filename}")
    with sqlite3.connect(root / "state" / "state.db") as db:
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("plans", "jobs", "releases", "events")
        }
    require(all(count >= 1 for count in counts.values()), "missing persisted plan/job/release/event evidence")
    return {
        "commit_sha": sha,
        "plan_id": plan["plan_id"],
        "job_id": job["job_id"],
        "release_id": job["release_id"],
        "job_status": job["status"],
        "database_counts": counts,
        "artifacts": ["release.json", "compose.yaml"],
    }


def verify(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "repository": str(ROOT),
        "git_head": run_step("repository_head", ["git", "rev-parse", "HEAD"], output).strip(),
        "result": "failed",
        "checks": {},
    }
    try:
        report["checks"]["runtime_metadata"] = check_runtime_metadata()
        for name, argv in (
            ("ruff", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"]),
            ("format", [sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "scripts"]),
            ("mypy", [sys.executable, "-m", "mypy", "src/drawbridge"]),
            ("pytest", [sys.executable, "-m", "pytest", "-q"]),
            ("compileall", [sys.executable, "-m", "compileall", "-q", "src"]),
        ):
            run_step(name, argv, output)
            report["checks"][name] = "passed"

        with tempfile.TemporaryDirectory(prefix="drawbridge-verify-") as temp:
            root = Path(temp)
            config, project, sha = make_fixture(root, output)
            selfcheck = json.loads(
                run_step(
                    "self_check",
                    [
                        sys.executable,
                        "-c",
                        "from drawbridge.cli import main; main()",
                        "self-check",
                        "--config",
                        str(config),
                        "--role",
                        "all",
                    ],
                    output,
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                )
            )
            require(selfcheck["ok"], "self-check has a blocking failure")
            report["checks"]["self_check"] = selfcheck

            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            smoke = json.loads(
                run_step(
                    "simulation",
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "smoke_simulation.py"),
                        "--config",
                        str(config),
                        "--project",
                        str(project),
                    ],
                    output,
                    env=env,
                )
            )
            report["checks"]["simulation"] = check_smoke(smoke, root, sha)
        report["result"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new evidence directory (must not exist)")
    args = parser.parse_args()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or ROOT / "var" / "verification" / timestamp).resolve()
    report = verify(output)
    print(f"{report['result']}: {output / 'report.json'}")
    if report["result"] != "passed":
        print(report["error"], file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
