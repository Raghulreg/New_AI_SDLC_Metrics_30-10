"""Mocked-HTTP tests for error mapping and pagination. Test-only responses."""
import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.ado.client import AdoClient
from app.config import Settings
from app.cursor.client import CursorClient
from app.errors import SourceError, Status
from app.metrics import cursor_usage
from app.http import request_json

ORG = "https://dev.azure.com/testorg"


class FakeToken:
    token = "test-token"
    expires_on = int(time.time()) + 3600


class FakeCredential:
    async def get_token(self, *scopes):
        return FakeToken()


class FailingCredential:
    async def get_token(self, *scopes):
        raise RuntimeError("no credential")


def settings(**kw) -> Settings:
    return Settings(ado_org_url=ORG, cursor_api_key="test-key", **kw)


@pytest.mark.parametrize("code,status", [(401, Status.AUTH_ERROR), (403, Status.PERMISSION_DENIED),
                                         (404, Status.UNSUPPORTED), (400, Status.ERROR)])
@respx.mock
async def test_status_mapping(code, status):
    respx.get("https://x.test/a").mock(return_value=httpx.Response(code, json={"message": "nope"}))
    async with httpx.AsyncClient() as c:
        with pytest.raises(SourceError) as ei:
            await request_json(c, "GET", "https://x.test/a", source="t")
    assert ei.value.status == status
    assert ei.value.http_status == code


@respx.mock
async def test_ado_sign_in_page_is_auth_error():
    respx.get("https://x.test/a").mock(return_value=httpx.Response(203, html="<html>sign in</html>"))
    async with httpx.AsyncClient() as c:
        with pytest.raises(SourceError) as ei:
            await request_json(c, "GET", "https://x.test/a", source="t")
    assert ei.value.status == Status.AUTH_ERROR


@respx.mock
async def test_retries_429_then_succeeds():
    route = respx.get("https://x.test/a").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={"ok": 1})])
    async with httpx.AsyncClient() as c:
        data, _ = await request_json(c, "GET", "https://x.test/a", source="t", backoff_base=0)
    assert data == {"ok": 1} and route.call_count == 2


@respx.mock
async def test_timeout_exhausts_retries_as_error():
    respx.get("https://x.test/a").mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as c:
        with pytest.raises(SourceError) as ei:
            await request_json(c, "GET", "https://x.test/a", source="t", backoff_base=0)
    assert ei.value.status == Status.ERROR


async def test_ado_credential_failure_is_auth_error():
    async with httpx.AsyncClient() as c:
        client = AdoClient(settings(), FailingCredential(), c)
        with pytest.raises(SourceError) as ei:
            await client.repositories("p")
    assert ei.value.status == Status.AUTH_ERROR
    assert "test-token" not in str(ei.value)


async def test_ado_not_configured():
    async with httpx.AsyncClient() as c:
        client = AdoClient(Settings(), FakeCredential(), c)
        with pytest.raises(SourceError) as ei:
            await client.repositories("p")
    assert ei.value.status == Status.NOT_CONFIGURED


@respx.mock
async def test_ado_pr_pagination_top_skip():
    def page(request):
        skip = int(request.url.params["$skip"])
        n = 500 if skip == 0 else 3
        return httpx.Response(200, json={"value": [{"pullRequestId": skip + i} for i in range(n)]})

    route = respx.get(f"{ORG}/p/_apis/git/pullrequests").mock(side_effect=page)
    async with httpx.AsyncClient() as c:
        client = AdoClient(settings(), FakeCredential(), c)
        items = await client.completed_pull_requests("p", datetime(2026, 10, 1, tzinfo=timezone.utc),
                                                     datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert len(items) == 503 and route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"
    assert route.calls[0].request.url.params["searchCriteria.queryTimeRangeType"] == "closed"


@respx.mock
async def test_ado_builds_continuation_token():
    route = respx.get(f"{ORG}/p/_apis/build/builds").mock(side_effect=[
        httpx.Response(200, json={"value": [{"id": 1}]}, headers={"x-ms-continuationtoken": "tok"}),
        httpx.Response(200, json={"value": [{"id": 2}]}),
    ])
    async with httpx.AsyncClient() as c:
        client = AdoClient(settings(), FakeCredential(), c)
        items = await client.completed_builds("p", datetime(2026, 10, 1, tzinfo=timezone.utc),
                                              datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert [b["id"] for b in items] == [1, 2]
    assert route.calls[1].request.url.params["continuationToken"] == "tok"


@respx.mock
async def test_cursor_usage_pagination_and_basic_auth():
    route = respx.post("https://api.cursor.com/teams/filtered-usage-events").mock(side_effect=[
        httpx.Response(200, json={"usageEvents": [{"timestamp": "1"}], "pagination": {"hasNextPage": True}}),
        httpx.Response(200, json={"usageEvents": [{"timestamp": "2"}], "pagination": {"hasNextPage": False}}),
    ])
    async with httpx.AsyncClient() as c:
        client = CursorClient(settings(), c)
        events = await client.usage_events(datetime(2026, 10, 1, tzinfo=timezone.utc),
                                           datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert len(events) == 2
    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_cursor_usage_pages_sum_charged_cents_once():
    repeated = {"timestamp": "1", "userEmail": "a@x.com", "model": "m", "conversationId": "c",
                "chargedCents": 100, "tokenUsage": {"inputTokens": 2, "outputTokens": 0, "cacheWriteTokens": 0, "cacheReadTokens": 0}}
    second = {"timestamp": "2", "userEmail": "a@x.com", "model": "m", "conversationId": "d",
              "chargedCents": 50, "tokenUsage": {"inputTokens": 1, "outputTokens": 0, "cacheWriteTokens": 0, "cacheReadTokens": 0}}
    respx.post("https://api.cursor.com/teams/filtered-usage-events").mock(side_effect=[
        httpx.Response(200, json={"usageEvents": [repeated], "pagination": {"hasNextPage": True}}),
        httpx.Response(200, json={"usageEvents": [repeated, second], "pagination": {"hasNextPage": False}}),
    ])
    async with httpx.AsyncClient() as c:
        client = CursorClient(settings(), c)
        events = await client.usage_events(datetime(2026, 10, 1, tzinfo=timezone.utc),
                                           datetime(2026, 10, 2, tzinfo=timezone.utc))
    calc = cursor_usage.token_usage(events)
    assert calc.summary["events"] == 2
    assert calc.summary["chargedCents"] == 150
    assert calc.summary["costUsd"] == 1.5


@respx.mock
async def test_cursor_enterprise_only_is_attribution_unavailable_and_not_retried():
    route = respx.get("https://api.cursor.com/analytics/ai-code/commits").mock(
        return_value=httpx.Response(401, json={"error": "You must be a member of an enterprise team to access this resource"}))
    start, end = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc)
    async with httpx.AsyncClient() as c:
        client = CursorClient(settings(), c)
        for _ in range(3):
            with pytest.raises(SourceError) as ei:
                await client.ai_commits(start, end)
            assert ei.value.status == Status.ATTRIBUTION_UNAVAILABLE
            assert "Cursor AI Code Attribution: access unavailable" in ei.value.message
            assert "not reported as invalid" in ei.value.message and ei.value.http_status == 401
    assert route.call_count == 1


@respx.mock
async def test_cursor_invalid_key_on_ai_commits_stays_auth_error_and_is_retried():
    route = respx.get("https://api.cursor.com/analytics/ai-code/commits").mock(
        return_value=httpx.Response(401, json={"error": "Invalid Team API Key"}))
    start, end = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc)
    async with httpx.AsyncClient() as c:
        client = CursorClient(settings(), c)
        for _ in range(2):
            with pytest.raises(SourceError) as ei:
                await client.ai_commits(start, end)
            assert ei.value.status == Status.AUTH_ERROR
    assert route.call_count == 2


async def test_cursor_without_key_is_not_configured():
    async with httpx.AsyncClient() as c:
        client = CursorClient(Settings(), c)
        with pytest.raises(SourceError) as ei:
            await client.team_members()
    assert ei.value.status == Status.NOT_CONFIGURED
