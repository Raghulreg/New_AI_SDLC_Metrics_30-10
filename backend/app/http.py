from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import httpx

from .errors import SourceError, Status, status_for_http

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}


class RateLimiter:
    """Sliding-window limiter: at most `limit` calls per `period` seconds."""

    def __init__(self, limit: int, period: float = 60.0):
        self.limit = limit
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._calls[0]) + 0.01)


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.reason_phrase or "Request failed"
    if isinstance(body, dict):
        for key in ("message", "error_description", "error"):
            val = body.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                message = val.get("message")
                if isinstance(message, str) and message:
                    return message
    return resp.reason_phrase or "Request failed"


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 120.0)
        except ValueError:
            pass
    return float(2**attempt)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    source: str,
    params: dict[str, Any] | None = None,
    json: Any = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = 3,
    limiter: RateLimiter | None = None,
    backoff_base: float = 1.0,
) -> tuple[Any, httpx.Headers]:
    """Perform a read request and return (json_body, headers).

    Only used for reads (GET, or POST queries that do not mutate state).
    Raises SourceError with a status that distinguishes auth, permission,
    unsupported and generic failures.
    """
    last_error: SourceError | None = None
    for attempt in range(max_attempts):
        if limiter:
            await limiter.acquire()
        try:
            resp = await client.request(method, url, params=params, json=json, headers=headers)
        except httpx.TimeoutException:
            last_error = SourceError(Status.ERROR, "Request timed out", source)
        except httpx.TransportError as exc:
            last_error = SourceError(Status.ERROR, f"Network error: {type(exc).__name__}", source)
        else:
            if resp.status_code in RETRYABLE:
                last_error = SourceError(
                    Status.ERROR,
                    "Rate limit exceeded" if resp.status_code == 429 else _error_message(resp),
                    source,
                    resp.status_code,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(_retry_after(resp, attempt) * backoff_base)
                continue
            if resp.status_code >= 400:
                raise SourceError(status_for_http(resp.status_code), _error_message(resp), source, resp.status_code)
            content_type = resp.headers.get("content-type", "")
            # Azure DevOps answers unauthenticated requests with 203 + an HTML sign-in page.
            if resp.status_code == 203 or "text/html" in content_type:
                raise SourceError(
                    Status.AUTH_ERROR, "Received a sign-in page instead of data; credentials were not accepted",
                    source, resp.status_code,
                )
            if resp.status_code == 204 or not resp.content:
                return None, resp.headers
            try:
                return resp.json(), resp.headers
            except ValueError:
                raise SourceError(Status.ERROR, "Response was not valid JSON", source, resp.status_code)
        if attempt < max_attempts - 1:
            await asyncio.sleep((2**attempt) * backoff_base)
    assert last_error is not None
    log.warning("Request to %s failed after %d attempts: %s", source, max_attempts, last_error)
    raise last_error
