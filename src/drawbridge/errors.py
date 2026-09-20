from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DrawbridgeError(Exception):
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: int | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def ok(data: Any, *, request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": "ok",
        "data": data,
        "error": None,
    }


def error_response(exc: DrawbridgeError, *, request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": "error",
        "data": None,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "retry_after_seconds": exc.retry_after_seconds,
            **(exc.details or {}),
        },
    }
