from pathlib import Path

from drawbridge.config import Settings
from drawbridge.selfcheck import run_self_check


def test_gateway_self_check_skips_runner_buildkit_requirements(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "auth": {"mode": "none"},
            "build_profiles": {
                "default": {
                    "mode": "buildkit",
                    "buildkit_socket": "unix:///missing/buildkitd.sock",
                }
            },
        }
    )
    monkeypatch.setattr(
        "drawbridge.selfcheck.shutil.which",
        lambda program: "/usr/bin/git" if program == "git" else None,
    )

    gateway = run_self_check(settings, base_dir=tmp_path, role="gateway")
    runner = run_self_check(settings, base_dir=tmp_path, role="runner")

    gateway_checks = {check["name"] for check in gateway["checks"]}
    runner_checks = {check["name"] for check in runner["checks"]}
    assert gateway["role"] == "gateway"
    assert gateway["ok"] is True
    assert "rootless_buildkit" not in gateway_checks
    assert "buildkit_socket_default" not in gateway_checks
    assert runner["role"] == "runner"
    assert runner["ok"] is False
    assert "rootless_buildkit" in runner_checks
    assert "buildkit_socket_default" in runner_checks
