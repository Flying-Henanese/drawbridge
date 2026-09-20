from __future__ import annotations

import pytest
from pydantic import ValidationError

from drawbridge.models import (
    HttpRequestInput,
    IdempotencyInput,
    ReleasePlanInput,
    validate_project_path,
    validate_ref,
    validate_subdir,
)


def test_release_plan_rejects_extra_fields_and_string_numbers() -> None:
    with pytest.raises(ValidationError):
        ReleasePlanInput.model_validate(
            {
                "app": "demo",
                "environment": "staging",
                "source_mode": "local",
                "git_ref": "refs/heads/main",
                "count": "20",
            }
        )

    with pytest.raises(ValidationError):
        ReleasePlanInput.model_validate(
            {
                "app": "demo",
                "environment": "staging",
                "source_mode": "local",
                "git_ref": "refs/heads/main",
                "unexpected": True,
            }
        )


def test_request_fields_reject_shell_syntax_and_invalid_method() -> None:
    with pytest.raises(ValidationError):
        IdempotencyInput(idempotency_key="bad;touch /tmp/pwned")

    with pytest.raises(ValidationError):
        HttpRequestInput(url="http://127.0.0.1:8080/health", method="TRACE")

    valid = HttpRequestInput(url="http://127.0.0.1:8080/health", method="GET")
    assert valid.method == "GET"


def test_ref_and_subdir_validation_are_shape_checks() -> None:
    assert validate_ref("refs/heads/agent/feature-1") == "refs/heads/agent/feature-1"
    assert validate_ref("a" * 40) == "a" * 40

    with pytest.raises(ValueError):
        validate_ref("refs/heads/main; echo unsafe")
    with pytest.raises(ValueError):
        validate_subdir("../outside")
    with pytest.raises(ValueError):
        validate_subdir("config/../secret")


def test_project_path_requires_an_existing_directory_inside_allowed_root(tmp_path) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)

    assert validate_project_path(str(project), [root]) == project.resolve()

    with pytest.raises(ValueError):
        validate_project_path(str(root.parent), [root])
