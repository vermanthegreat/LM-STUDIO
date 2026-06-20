"""Structured application errors for HTTP and logging."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AppError(Exception):
    error_code: str
    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


class ValidationError(AppError):
    pass


def parse_command_id(command_id: str) -> UUID:
    try:
        return UUID(str(command_id).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError(
            error_code="invalid_command_id",
            message=f"Invalid command_id: {command_id!r}",
            status_code=422,
        ) from exc
