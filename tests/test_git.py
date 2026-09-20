from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drawbridge.gitops import GitError, GitRepository


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    commands = [
        ["git", "init", "--initial-branch=main"],
        ["git", "config", "user.email", "drawbridge@example.invalid"],
        ["git", "config", "user.name", "Drawbridge Test"],
        ["git", "remote", "add", "origin", "https://example.invalid/drawbridge.git"],
    ]
    for command in commands:
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / "config.yaml").write_text("port: 8080\n", encoding="utf-8")
    subprocess.run(["git", "add", "config.yaml"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def test_local_ref_resolution_requires_an_allowed_ref(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path)
    service = GitRepository(repo, "https://example.invalid/drawbridge.git", [r"^refs/heads/main$"])

    assert service.resolve_commit("refs/heads/main") == sha
    assert service.resolve_commit(sha) == sha

    with pytest.raises(GitError):
        service.resolve_commit("refs/heads/main; echo unsafe")


def test_git_origin_drift_is_rejected_before_network_operations(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    service = GitRepository(repo, "https://example.invalid/drawbridge.git", [r"^refs/heads/main$"])
    service.verify_repository()
    subprocess.run(
        ["git", "config", "remote.origin.url", "https://attacker.invalid/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(GitError, match="origin"):
        service.verify_repository()
