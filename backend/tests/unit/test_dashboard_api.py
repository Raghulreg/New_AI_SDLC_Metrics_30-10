"""End-to-end API behaviour with mocked sources: failures must never surface as zero."""
import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.unit.test_http_clients import ORG, FakeCredential


def make_client(**kw) -> TestClient:
    s = Settings(ado_org_url=ORG, ado_default_project="p", **kw)
    return TestClient(create_app(s, http=httpx.AsyncClient(), credential=FakeCredential()))


def metrics(resp) -> dict:
    assert resp.status_code == 200, resp.text
    return {m["id"]: m for m in resp.json()["metrics"]}


@respx.mock
def test_ado_401_is_auth_error_not_zero():
    respx.route(host="dev.azure.com").mock(return_value=httpx.Response(401, json={"message": "unauthorized"}))
    with make_client() as client:
        m = metrics(client.get("/api/dashboard", params={"from": "2026-10-01", "to": "2026-10-20"}))
    for mid in ("pr_cycle_time", "time_to_first_review", "defect_count"):
        assert m[mid]["status"] == "auth_error", mid
        assert m[mid]["value"] is None
    assert m["ai_generated_pct"]["status"] == "auth_error" and m["ai_generated_pct"]["value"] is None
    assert m["cursor_token_usage"]["status"] == "not_configured"
    assert m["cursor_token_usage"]["value"] is None
    assert m["test_coverage"]["status"] == "not_configured"
    assert m["sprint_velocity"]["status"] == "not_configured"  # no team selected
    assert set(m) == {"pr_cycle_time", "time_to_first_review", "sprint_velocity", "test_coverage", "defect_count",
                      "cursor_token_usage", "ai_generated_pct"}


@respx.mock
def test_403_is_permission_denied():
    respx.route(host="dev.azure.com").mock(return_value=httpx.Response(403, json={"message": "denied"}))
    with make_client() as client:
        m = metrics(client.get("/api/dashboard", params={"metric": "defect_count"}))
    assert m["defect_count"]["status"] == "permission_denied" and m["defect_count"]["value"] is None


def test_removed_metrics_are_rejected():
    with make_client() as client:
        for mid in ("pr_completion_count", "build_success_rate", "deployment_frequency", "sprint_completion_rate",
                    "open_defects", "defect_resolution_time", "defect_severity_distribution", "ai_assisted_prs"):
            assert client.get("/api/dashboard", params={"metric": mid}).status_code == 400, mid


@respx.mock
def test_empty_prs_is_valid_empty_with_undefined_cycle_time():
    respx.get(f"{ORG}/p/_apis/git/pullrequests").mock(return_value=httpx.Response(200, json={"value": []}))
    with make_client() as client:
        m = metrics(client.get("/api/dashboard", params={"metric": ["pr_cycle_time", "ai_generated_pct"]}))
    b = m["pr_cycle_time"]
    assert b["status"] == "empty" and b["value"] is None and b["fetchedAt"]
    assert m["ai_generated_pct"]["status"] == "empty" and m["ai_generated_pct"]["value"] is None


@respx.mock
def test_refresh_refetches_and_cache_serves_otherwise():
    route = respx.get(f"{ORG}/p/_apis/git/pullrequests").mock(return_value=httpx.Response(200, json={"value": []}))
    with make_client() as client:
        client.get("/api/dashboard", params={"metric": "pr_cycle_time"})
        client.get("/api/dashboard", params={"metric": "pr_cycle_time"})
        assert route.call_count == 1
        client.get("/api/dashboard", params={"metric": "pr_cycle_time", "refresh": "true"})
        assert route.call_count == 2


@respx.mock
def test_failed_refresh_does_not_return_cached_value():
    route = respx.get(f"{ORG}/p/_apis/git/pullrequests")
    route.side_effect = [
        httpx.Response(200, json={"value": [{"pullRequestId": 1, "status": "completed",
                                             "creationDate": "2026-10-10T00:00:00Z",
                                             "closedDate": "2026-10-10T06:00:00Z"}]}),
        httpx.Response(401, json={"message": "expired"}),
    ]
    params = {"metric": "pr_cycle_time", "from": "2026-10-01", "to": "2026-10-20"}
    with make_client() as client:
        first = metrics(client.get("/api/dashboard", params=params))["pr_cycle_time"]
        assert first["value"] == 6.0
        second = metrics(client.get("/api/dashboard", params={**params, "refresh": "true"}))["pr_cycle_time"]
    assert second["status"] == "auth_error" and second["value"] is None
    assert second["lastSuccessfulFetch"] == first["fetchedAt"]


@respx.mock
def test_coverage_attribution_unavailable_for_team():
    with make_client() as client:
        m = metrics(client.get("/api/dashboard", params={"team": "t", "metric": "test_coverage"}))
    assert m["test_coverage"]["status"] == "attribution_unavailable"


@respx.mock
def test_cursor_enterprise_restriction_without_llm_is_attribution_unavailable():
    respx.get(f"{ORG}/p/_apis/git/pullrequests").mock(return_value=httpx.Response(200, json={"value": [
        {"pullRequestId": 1, "status": "completed", "creationDate": "2026-10-10T00:00:00Z",
         "closedDate": "2026-10-10T06:00:00Z", "repository": {"id": "r1", "name": "r"}}]}))
    respx.get(f"{ORG}/p/_apis/git/repositories/r1/pullRequests/1/commits").mock(
        return_value=httpx.Response(200, json={"value": [{"commitId": "c" * 40}]}))
    respx.get("https://api.cursor.com/analytics/ai-code/commits").mock(
        return_value=httpx.Response(403, json={"message": "Enterprise access required"}))
    with make_client(cursor_api_key="k") as client:
        m = metrics(client.get("/api/dashboard", params={
            "metric": "ai_generated_pct", "from": "2026-10-01", "to": "2026-10-20"}))
    ai = m["ai_generated_pct"]
    assert ai["status"] == "attribution_unavailable" and ai["value"] is None
    assert "Enterprise access required" in ai["message"]
    assert ai["summary"]["cursorAttribution"]["status"] == "attribution_unavailable"
    assert ai["summary"]["commits"]["unmeasured"] == 1


@respx.mock
def test_cursor_identity_unmapped_is_attribution_unavailable():
    respx.get(f"{ORG}/_apis/projects/p/teams/t/members").mock(return_value=httpx.Response(200, json={"value": [
        {"identity": {"id": "u1", "displayName": "Dev", "uniqueName": "DOMAIN\\dev"}}]}))
    respx.get("https://api.cursor.com/teams/members").mock(return_value=httpx.Response(200, json={
        "teamMembers": [{"email": "someone@x.com"}]}))
    with make_client(cursor_api_key="k") as client:
        m = metrics(client.get("/api/dashboard", params={"team": "t", "person": "u1", "metric": "cursor_token_usage"}))
    assert m["cursor_token_usage"]["status"] == "attribution_unavailable"
    assert m["cursor_token_usage"]["value"] is None


def _cursor_event(email: str, tokens: int, conversation_id: str | None = None) -> dict:
    return {
        "timestamp": "1760000000000", "userEmail": email, "model": "m",
        "conversationId": conversation_id or email,
        "tokenUsage": {"inputTokens": tokens, "outputTokens": 0, "cacheWriteTokens": 0, "cacheReadTokens": 0},
    }


@respx.mock
def test_cursor_usage_team_and_individual_use_normalized_exact_email_without_double_counting():
    members = [
        {"identity": {"id": "u1", "displayName": "Dev", "uniqueName": " Dev@Example.COM "}},
        {"identity": {"id": "u2", "displayName": "Rev", "uniqueName": "rev@example.com"}},
    ]
    respx.get(f"{ORG}/_apis/projects/p/teams/t/members").mock(return_value=httpx.Response(200, json={"value": members}))
    respx.get("https://api.cursor.com/teams/members").mock(return_value=httpx.Response(200, json={
        "teamMembers": [{"email": "dev@example.com"}, {"email": "rev@example.com"}, {"email": "outside@example.com"}]}))
    usage = respx.post("https://api.cursor.com/teams/filtered-usage-events").mock(return_value=httpx.Response(200, json={
        "usageEvents": [_cursor_event("DEV@example.com", 5, "same"), _cursor_event("dev@example.com", 5, "same"),
                        _cursor_event("REV@example.com", 7), _cursor_event("outside@example.com", 11)],
        "pagination": {"hasNextPage": False}}))
    with make_client(cursor_api_key="k") as client:
        project = metrics(client.get("/api/dashboard", params={"metric": "cursor_token_usage"}))["cursor_token_usage"]
        team = metrics(client.get("/api/dashboard", params={"team": "t", "metric": "cursor_token_usage"}))["cursor_token_usage"]
        person = metrics(client.get("/api/dashboard", params={
            "team": "t", "person": "u1", "metric": "cursor_token_usage"}))["cursor_token_usage"]
    assert project["value"] == 23  # project behavior remains the complete Cursor team total
    assert team["value"] == 12  # duplicate Dev event is counted once; external member is excluded
    assert person["value"] == 5
    assert team["summary"]["costUsd"] is None  # these events omit chargedCents, so cost is not zero
    assert team["summary"]["eventsWithoutCost"] == 2
    assert project["summary"]["eventsWithoutCost"] == 3
    assert team["summary"]["exactEmailMatches"] == 2 and team["summary"]["configuredMappings"] == 0
    assert usage.call_count == 1  # same date-range source cache, local filtering only


@respx.mock
def test_cursor_usage_explicit_mapping_handles_different_ado_identity():
    respx.get(f"{ORG}/_apis/projects/p/teams/t/members").mock(return_value=httpx.Response(200, json={"value": [
        {"identity": {"id": "ado-42", "displayName": "Dev", "uniqueName": "DOMAIN\\dev"}}]}))
    respx.get("https://api.cursor.com/teams/members").mock(return_value=httpx.Response(200, json={
        "teamMembers": [{"email": "cursor.dev@example.com"}]}))
    respx.post("https://api.cursor.com/teams/filtered-usage-events").mock(return_value=httpx.Response(200, json={
        "usageEvents": [{**_cursor_event("CURSOR.DEV@example.com", 9), "chargedCents": 125},
                        {**_cursor_event("other@example.com", 40), "chargedCents": 900}],
        "pagination": {"hasNextPage": False}}))
    with make_client(cursor_api_key="k", cursor_identity_mappings={"ado-42": "cursor.dev@example.com"}) as client:
        m = metrics(client.get("/api/dashboard", params={
            "team": "t", "person": "ado-42", "metric": "cursor_token_usage"}))["cursor_token_usage"]
    assert m["status"] == "ok" and m["value"] == 9
    assert m["summary"]["costUsd"] == 1.25  # only the mapped person's chargedCents; 900 is not included
    assert m["summary"]["exactEmailMatches"] == 0 and m["summary"]["configuredMappings"] == 1


async def test_default_azure_credential_constructs_with_installed_dependencies():
    from app.main import _credential

    cred = _credential(Settings(ado_org_url=ORG))
    assert type(cred).__name__ == "DefaultAzureCredential"
    await cred.close()


def test_person_requires_team():
    with make_client() as client:
        assert client.get("/api/dashboard", params={"person": "x"}).status_code == 400


def test_application_code_contains_no_mock_or_demo_data_path():
    app_dir = Path(__file__).resolve().parents[2] / "app"
    pattern = re.compile(r"\b(mock|demo|fake|sample[_ ]?data|seed|fixture)\b", re.IGNORECASE)
    offenders = [str(p) for p in app_dir.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
