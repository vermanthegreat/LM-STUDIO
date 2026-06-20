"""Structured application errors for HTTP and logging."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppError(Exception):
    error_code: str
    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


class ValidationError(AppError):
    pass
