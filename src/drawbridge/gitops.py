from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import regex

from .models import validate_ref


class GitError(RuntimeError):
    pass


class GitRepository:
    def __init__(self, repo_path: Path, expected_origin: str, allowed_ref_patterns: list[str]) -> None:
        self.repo_path = repo_path
        self.expected_origin = expected_origin
        self.allowed_ref_patterns = allowed_ref_patterns
        git = shutil.which("git")
        if git is None:
            raise GitError("git executable is not available")
        self.git = Path(git).resolve()

    @classmethod
    def detect_origin(cls, repo_path: Path) -> str:
        git = shutil.which("git")
        if git is None:
            raise GitError("git executable is not available")
        try:
            result = subprocess.run(
                [git, "config", "--local", "--get-all", "remote.origin.url"],
                cwd=repo_path,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"unable to read git origin: {exc}") from exc
        origins = result.stdout.splitlines()
        if result.returncode != 0 or len(origins) != 1 or not origins[0]:
            raise GitError("repository must have exactly one origin")
        return origins[0]

    def head_commit(self) -> str:
        self.verify_repository()
        return self._resolve("HEAD")

    def verify_repository(self) -> None:
        if not self.repo_path.is_absolute() or not self.repo_path.is_dir():
            raise GitError("repository path is not an existing directory")
        git_dir = self.repo_path / ".git"
        if not git_dir.exists() or git_dir.is_symlink():
            raise GitError("repository must have a local .git directory")
        origin = self._run(["config", "--get-all", "remote.origin.url"], timeout=5).stdout.splitlines()
        if origin != [self.expected_origin]:
            raise GitError("repository origin does not match the registered origin")

        keys_output = self._run(["config", "--local", "--name-only", "--null", "--list"], timeout=5).stdout
        keys = [key for key in keys_output.split("\x00") if key]
        forbidden_prefixes = (
            "include",
            "includeif",
            "url.",
            "core.sshcommand",
            "http.proxy",
            "https.proxy",
            "remote.origin.pushurl",
            "credential.",
            "filter.",
            "diff.",
            "submodule.",
        )
        for key in keys:
            lowered = key.lower()
            if lowered.startswith(forbidden_prefixes):
                raise GitError(f"unsafe local git configuration: {key}")

    def resolve_commit(self, git_ref: str) -> str:
        try:
            validate_ref(git_ref)
        except ValueError as exc:
            raise GitError(str(exc)) from exc
        if not self._allowed_ref(git_ref):
            raise GitError("git ref is not allowed for this application")
        self.verify_repository()
        if re.fullmatch(r"[0-9a-f]{40}", git_ref):
            sha = self._resolve(git_ref)
            for tip in self._allowed_tips():
                result = self._run(["merge-base", "--is-ancestor", sha, tip], timeout=5, check=False)
                if result.returncode == 0:
                    return sha
            raise GitError("commit is not reachable from an allowed current ref")

        self._run(["check-ref-format", git_ref], timeout=5)
        mapped_ref = self._mapped_ref(git_ref)
        return self._resolve(mapped_ref)

    def fetch_and_resolve(self, git_ref: str) -> str:
        try:
            validate_ref(git_ref)
        except ValueError as exc:
            raise GitError(str(exc)) from exc
        if not self._allowed_ref(git_ref):
            raise GitError("git ref is not allowed for this application")
        self.verify_repository()
        if re.fullmatch(r"[0-9a-f]{40}", git_ref):
            raise GitError("fetch mode requires a full allowed branch or tag ref")
        self._run(["check-ref-format", git_ref], timeout=5)
        if git_ref.startswith("refs/heads/"):
            branch = git_ref.removeprefix("refs/heads/")
            refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
            mapped = f"refs/remotes/origin/{branch}"
        else:
            tag = git_ref.removeprefix("refs/tags/")
            refspec = f"refs/tags/{tag}:refs/tags/{tag}"
            mapped = f"refs/tags/{tag}"
        self._run(
            [
                "fetch",
                "--no-auto-maintenance",
                "--no-write-commit-graph",
                "--no-tags",
                "--no-recurse-submodules",
                "origin",
                refspec,
            ],
            timeout=120,
        )
        return self._resolve(mapped)

    def status(self) -> str:
        self.verify_repository()
        return self._run(["status", "--porcelain=v1", "--untracked-files=no"], timeout=10).stdout

    def log(self, sha: str, count: int) -> list[dict[str, str | int]]:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitError("log requires a resolved commit SHA")
        if not 1 <= count <= 100:
            raise GitError("count must be between 1 and 100")
        result = self._run(
            [
                "log",
                "--no-show-signature",
                "--format=%H%x09%ct%x09%s",
                f"--max-count={count}",
                sha,
                "--",
            ],
            timeout=10,
        )
        rows: list[dict[str, str | int]] = []
        for line in result.stdout.splitlines():
            commit, timestamp, subject = line.split("\t", 2)
            rows.append({"commit_sha": commit, "timestamp": int(timestamp), "subject": subject})
        return rows

    def archive(self, sha: str, archive_path: Path) -> Path:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitError("archive requires a resolved commit SHA")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["archive", "--format=tar", f"--output={archive_path}", sha],
            timeout=30,
        )
        return archive_path

    def extract_archive(self, archive_path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        total_bytes = 0
        members = 0
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive.getmembers():
                members += 1
                if members > 100_000:
                    raise GitError("archive contains too many members")
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise GitError("archive path escapes the destination")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise GitError("archive contains an unsupported link or special file")
                if member.isfile() and member.size > 100 * 1024 * 1024:
                    raise GitError("archive member is too large")
                total_bytes += member.size
                if total_bytes > 1024 * 1024 * 1024:
                    raise GitError("archive expands beyond the release budget")
            for member in archive.getmembers():
                archive.extract(member, destination)

    def read_file_at_commit(self, sha: str, relative_path: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_./-]*", relative_path) or ".." in Path(relative_path).parts:
            raise GitError("invalid repository relative path")
        result = self._run(["show", "--no-ext-diff", f"{sha}:{relative_path}"], timeout=10)
        return result.stdout.encode("utf-8")

    def _allowed_ref(self, value: str) -> bool:
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return True
        for pattern in self.allowed_ref_patterns:
            try:
                if regex.fullmatch(pattern, value, timeout=0.1):
                    return True
            except TimeoutError as exc:
                raise GitError("configured ref pattern timed out") from exc
        return False

    def _allowed_tips(self) -> list[str]:
        result = self._run(["for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"], timeout=10)
        tips: list[str] = []
        for ref in result.stdout.splitlines():
            if not self._allowed_ref(ref):
                continue
            try:
                tips.append(self._resolve(ref))
            except GitError:
                continue
        return list(dict.fromkeys(tips))

    def _resolve(self, ref: str) -> str:
        result = self._run(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], timeout=5)
        sha = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitError("git did not return a complete SHA-1 commit")
        return sha

    @staticmethod
    def _mapped_ref(git_ref: str) -> str:
        if git_ref.startswith("refs/heads/"):
            return git_ref
        if git_ref.startswith("refs/tags/"):
            return git_ref
        return git_ref

    def _run(self, args: list[str], *, timeout: float, check: bool = True) -> subprocess.CompletedProcess[str]:
        prefix = [
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.ssh.allow=always",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "credential.helper=",
            "-c",
            "gc.auto=0",
        ]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        try:
            result = subprocess.run(
                [str(self.git), *prefix, *args],
                cwd=self.repo_path,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git command failed to start or timed out: {exc}") from exc
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "git command failed"
            raise GitError(message[:1024])
        return result
