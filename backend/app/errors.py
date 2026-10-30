from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    AUTH_ERROR = "auth_error"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    NOT_CONFIGURED = "not_configured"
    ATTRIBUTION_UNAVAILABLE = "attribution_unavailable"


class SourceError(Exception):
    """A failure obtaining real source data. Never converted into a zero value."""

    def __init__(self, status: Status, message: str, source: str, http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.source = source
        self.http_status = http_status

    def __str__(self) -> str:
        code = f" (HTTP {self.http_status})" if self.http_status else ""
        return f"{self.source}: {self.message}{code}"


def status_for_http(code: int) -> Status:
    if code == 401:
        return Status.AUTH_ERROR
    if code == 403:
        return Status.PERMISSION_DENIED
    if code == 404:
        return Status.UNSUPPORTED
    return Status.ERROR
