from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx

from ..config import Settings
from ..errors import SourceError, Status
from ..http import RateLimiter, request_json

PAGE_SIZE = 1000

KeyResolver = Callable[[], Awaitable[str]]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class CursorClient:
    """Read-only client for the Cursor Admin API and AI Code Tracking API."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient, key_resolver: KeyResolver | None = None):
        self.base = settings.cursor_api_base_url
        self.http = http
        self._settings = settings
        self._key_resolver = key_resolver
        self._key: str | None = None
        self._key_lock = asyncio.Lock()
        self._ai_commits_blocked: tuple[float, SourceError] | None = None
        # Documented limits: 20 req/min default, 60 req/min for filtered-usage-events.
        self._limiters = {
            "members": RateLimiter(20),
            "usage": RateLimiter(60),
            "aiCommits": RateLimiter(20),
        }

    async def _headers(self) -> dict[str, str]:
        async with self._key_lock:
            if self._key is None:
                if self._settings.cursor_api_key:
                    self._key = self._settings.cursor_api_key
                elif self._key_resolver is not None:
                    self._key = await self._key_resolver()
                else:
                    raise SourceError(
                        Status.NOT_CONFIGURED,
                        "Cursor API key not configured (CURSOR_API_KEY or Key Vault secret)",
                        "cursor",
                    )
        token = base64.b64encode(f"{self._key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def _call(self, method: str, path: str, limiter: str, source: str, **kw: Any) -> Any:
        data, _ = await request_json(
            self.http, method, f"{self.base}{path}", source=source, headers=await self._headers(),
            limiter=self._limiters[limiter], **kw,
        )
        return data

    async def team_members(self) -> list[dict]:
        data = await self._call("GET", "/teams/members", "members", "cursor.teamMembers")
        return (data or {}).get("teamMembers", [])

    async def usage_events(self, start: datetime, end: datetime, email: str | None = None) -> list[dict]:
        events: list[dict] = []
        page = 1
        while True:
            body: dict[str, Any] = {
                "startDate": int(start.timestamp() * 1000),
                "endDate": int(end.timestamp() * 1000),
                "page": page,
                "pageSize": PAGE_SIZE,
            }
            if email:
                body["email"] = email
            data = await self._call("POST", "/teams/filtered-usage-events", "usage", "cursor.usageEvents", json=body)
            if not isinstance(data, dict) or "usageEvents" not in data:
                raise SourceError(Status.ERROR, "Unexpected response shape from filtered-usage-events", "cursor.usageEvents")
            events.extend(data.get("usageEvents") or [])
            if not (data.get("pagination") or {}).get("hasNextPage"):
                return events
            page += 1

    async def ai_commits(self, start: datetime, end: datetime, user: str | None = None) -> list[dict]:
        """AI Code Tracking commits. A team-access rejection is remembered for the cache TTL so the
        same refusal is not re-requested on every dashboard load or refresh."""
        blocked = self._ai_commits_blocked
        if blocked and time.monotonic() < blocked[0]:
            raise blocked[1]
        try:
            return await self._ai_commits(start, end, user)
        except SourceError as e:
            if e.http_status in (401, 403) and "enterprise" in e.message.lower():
                limited = SourceError(
                    Status.ATTRIBUTION_UNAVAILABLE,
                    f"Cursor AI Code Attribution: access unavailable. {e.message} "
                    "The API key was not reported as invalid. Token usage is unaffected. Not retried for "
                    f"{self._settings.cache_ttl_minutes} min",
                    "cursor.aiCommits", e.http_status)
                self._ai_commits_blocked = (time.monotonic() + self._settings.cache_ttl_minutes * 60, limited)
                raise limited from e
            raise

    async def _ai_commits(self, start: datetime, end: datetime, user: str | None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "startDate": _iso(start), "endDate": _iso(end), "page": page, "pageSize": PAGE_SIZE,
            }
            if user:
                params["user"] = user
            data = await self._call("GET", "/analytics/ai-code/commits", "aiCommits", "cursor.aiCommits", params=params)
            if not isinstance(data, dict) or "items" not in data:
                raise SourceError(Status.ERROR, "Unexpected response shape from ai-code/commits", "cursor.aiCommits")
            batch = data.get("items") or []
            items.extend(batch)
            total = data.get("totalCount")
            if not batch or (isinstance(total, int) and page * PAGE_SIZE >= total) or len(batch) < PAGE_SIZE:
                return items
            page += 1
