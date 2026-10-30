"""Orchestration test over mocked ADO + Cursor + LLM responses (test-only payloads)."""
import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.unit.test_http_clients import ORG, FakeCredential

DEV = {"id": "u1", "displayName": "Dev", "uniqueName": "dev@x.com"}
REV = {"id": "u2", "displayName": "Rev", "uniqueName": "rev@x.com"}
OUTSIDER = {"id": "u9", "displayName": "Out", "uniqueName": "out@x.com"}
SHA_AI = "a" * 40
SHA_PLAIN = "b" * 40
SHA_PARENT = "f" * 40
LLM_URL = "https://llm.test/openai/deployments/gpt-test/chat/completions"
TEST_LLM_KEY = "llm-test-key-not-real"


def pr(pid, author, labels=()):
    return {"pullRequestId": pid, "status": "completed", "creationDate": "2026-10-05T00:00:00Z",
            "closedDate": "2026-10-06T00:00:00Z", "createdBy": author, "title": f"PR{pid}",
            "repository": {"id": "repo1", "name": "app"}, "labels": [{"name": n, "active": True} for n in labels]}


STATES_STORY = [{"name": "New", "category": "Proposed"}, {"name": "Closed", "category": "Completed"}]
STATES_BUG = [{"name": "New", "category": "Proposed"}, {"name": "Active", "category": "InProgress"},
              {"name": "Closed", "category": "Completed"}, {"name": "Removed", "category": "Removed"}]


def wi(i, wit, state, **fields):
    return {"id": i, "fields": {"System.WorkItemType": wit, "System.State": state, "System.Title": f"W{i}",
                                "System.IterationPath": "P\\S1", "System.CreatedDate": "2026-10-02T00:00:00Z",
                                **fields}}


def setup_routes(time_frame="past", finish="2026-10-14T00:00:00Z", cursor_status=200):
    respx.get(f"{ORG}/_apis/projects/p/teams/t/members").mock(return_value=httpx.Response(
        200, json={"value": [{"identity": DEV}, {"identity": REV}]}))
    respx.get(f"{ORG}/p/t/_apis/work/teamsettings/teamfieldvalues").mock(return_value=httpx.Response(
        200, json={"field": {"referenceName": "System.AreaPath"}, "values": [{"value": "P\\Team", "includeChildren": True}]}))
    respx.get(f"{ORG}/p/t/_apis/work/teamsettings/iterations").mock(return_value=httpx.Response(200, json={"value": [
        {"id": "it1", "name": "S1", "path": "P\\S1",
         "attributes": {"startDate": "2026-10-01T00:00:00Z", "finishDate": finish, "timeFrame": time_frame}}]}))
    respx.get(f"{ORG}/p/_apis/git/pullrequests").mock(return_value=httpx.Response(200, json={"value": [
        pr(1, DEV), pr(2, REV, labels=["AI-Assisted"]), pr(3, OUTSIDER)]}))
    respx.get(url__regex=rf"{ORG}/p/_apis/git/repositories/repo1/pullRequests/\d+/threads").mock(
        return_value=httpx.Response(200, json={"value": [
            {"comments": [{"author": REV, "publishedDate": "2026-10-05T03:00:00Z", "commentType": "text"}]}]}))

    def commits(request):
        pid = int(request.url.path.split("/")[-2])
        return httpx.Response(200, json={"value": [{"commitId": SHA_AI if pid == 1 else SHA_PLAIN}]})

    respx.get(url__regex=rf"{ORG}/p/_apis/git/repositories/repo1/pullRequests/\d+/commits").mock(side_effect=commits)
    if cursor_status == 200:
        respx.get("https://api.cursor.com/analytics/ai-code/commits").mock(return_value=httpx.Response(200, json={
            "items": [{"commitHash": SHA_AI, "totalLinesAdded": 10, "tabLinesAdded": 3, "composerLinesAdded": 7,
                       "nonAiLinesAdded": 0, "createdAt": "2026-10-05T00:00:00Z"}], "totalCount": 1}))
    else:
        respx.get("https://api.cursor.com/analytics/ai-code/commits").mock(
            return_value=httpx.Response(cursor_status, json={"error": "Invalid Team API Key"}))
    respx.get(f"{ORG}/p/_apis/wit/workitemtypes/User%20Story/states").mock(
        return_value=httpx.Response(200, json={"value": STATES_STORY}))
    respx.get(f"{ORG}/p/_apis/wit/workitemtypes/Bug/states").mock(
        return_value=httpx.Response(200, json={"value": STATES_BUG}))

    wiql_queries: list[str] = []

    def wiql(request):
        q = json.loads(request.content)["query"]
        wiql_queries.append(q)
        if "User Story" in q:
            return httpx.Response(200, json={"workItems": [{"id": 10}, {"id": 11}]})
        return httpx.Response(200, json={"workItems": [{"id": 20}, {"id": 21}]})

    respx.post(f"{ORG}/p/_apis/wit/wiql").mock(side_effect=wiql)

    def batch(request):
        body = json.loads(request.content)
        if body["ids"][0] == 10:
            return httpx.Response(200, json={"value": [
                wi(10, "User Story", "Closed", **{"Microsoft.VSTS.Scheduling.StoryPoints": 5, "System.AssignedTo": DEV}),
                wi(11, "User Story", "New", **{"Microsoft.VSTS.Scheduling.StoryPoints": 3}),
            ]})
        return httpx.Response(200, json={"value": [
            wi(20, "Bug", "Active", **{"Microsoft.VSTS.Common.Severity": "2 - High", "System.AssignedTo": DEV}),
            wi(21, "Bug", "New"),
        ]})

    respx.post(f"{ORG}/p/_apis/wit/workitemsbatch").mock(side_effect=batch)
    return wiql_queries


def setup_diff_routes():
    """One edited source file (3 added lines) and one lock file (2 added lines, excluded) per commit."""
    base = f"{ORG}/p/_apis/git/repositories/repo1"
    respx.get(url__regex=rf"{base}/commits/[0-9a-f]{{40}}/changes").mock(return_value=httpx.Response(200, json={
        "changes": [{"item": {"path": "/src/app.py", "gitObjectType": "blob"}, "changeType": "edit"},
                    {"item": {"path": "/package-lock.json", "gitObjectType": "blob"}, "changeType": "add"}]}))

    def commit(request):
        sha = request.url.path.rsplit("/", 1)[-1]
        message = "Generate service layer" if sha == SHA_AI else "Fix typo by hand"
        return httpx.Response(200, json={"commitId": sha, "parents": [SHA_PARENT], "comment": message})

    respx.get(url__regex=rf"{base}/commits/[0-9a-f]{{40}}").mock(side_effect=commit)

    def items(request):
        path = request.url.params["path"]
        version = request.url.params["versionDescriptor.version"]
        if path == "/package-lock.json":
            return httpx.Response(200, json={"content": "x\ny\n"})
        content = "a\nb\n" if version == SHA_PARENT else "a\nb\nc\nd\ne\n"
        return httpx.Response(200, json={"content": content})

    respx.get(f"{base}/items").mock(side_effect=items)


def llm_settings(**kw) -> Settings:
    return Settings(ado_org_url=ORG, ado_default_project="p", cursor_api_key="k", llm_code_analysis_enabled=True,
                    llm_provider="azure_openai", llm_endpoint="https://llm.test", llm_model="gpt-test",
                    llm_api_key=TEST_LLM_KEY, **kw)


@respx.mock
def test_team_dashboard_flow_for_retained_metrics():
    wiql_queries = setup_routes()
    s = Settings(ado_org_url=ORG, ado_default_project="p", cursor_api_key="k")
    with TestClient(create_app(s, http=httpx.AsyncClient(), credential=FakeCredential())) as client:
        resp = client.get("/api/dashboard", params={
            "team": "t", "from": "2026-10-01", "to": "2026-10-20",
            "metric": ["pr_cycle_time", "time_to_first_review", "ai_generated_pct", "sprint_velocity",
                       "defect_count"]})
        assert resp.status_code == 200, resp.text
        m = {x["id"]: x for x in resp.json()["metrics"]}

        assert m["pr_cycle_time"]["sampleSize"] == 2  # outsider's PR excluded at team scope
        assert m["pr_cycle_time"]["value"] == 24.0
        assert m["time_to_first_review"]["summary"]["noReviewCount"] == 1  # Rev's own comment on PR2 is not a review
        assert m["time_to_first_review"]["value"] == 3.0

        ai = m["ai_generated_pct"]
        assert ai["status"] == "ok" and ai["value"] == 100.0
        assert ai["summary"]["evidenceBasis"] == "cursor_verified"
        assert ai["summary"]["lines"]["verifiedAi"] == 10
        assert ai["summary"]["commits"] == {"counted": 2, "cursorTracked": 1, "llmAnalysed": 0, "unmeasured": 1,
                                            "mergeExcluded": 0, "duplicatesAcrossPrs": 0}
        assert ai["summary"]["llmAnalysis"]["status"] == "not_configured"
        assert ai["summary"]["prsWithAiLabelSelfReported"] == 1

        assert m["sprint_velocity"]["value"] == 5
        assert m["sprint_velocity"]["summary"]["sprintProgress"] == "completed"
        assert m["defect_count"]["value"] == 2

        assert all("[System.AreaPath] UNDER 'P\\Team'" in q for q in wiql_queries)

        person = client.get("/api/dashboard", params={
            "team": "t", "person": "u1", "from": "2026-10-01", "to": "2026-10-20",
            "metric": ["pr_cycle_time", "sprint_velocity", "test_coverage"]})
        pm = {x["id"]: x for x in person.json()["metrics"]}
        assert pm["pr_cycle_time"]["sampleSize"] == 1
        assert pm["sprint_velocity"]["value"] == 5
        assert pm["test_coverage"]["status"] == "attribution_unavailable"


@respx.mock
def test_dashboard_returns_exactly_the_seven_retained_metrics():
    setup_routes()
    s = Settings(ado_org_url=ORG, ado_default_project="p")
    with TestClient(create_app(s, http=httpx.AsyncClient(), credential=FakeCredential())) as client:
        body = client.get("/api/dashboard", params={"team": "t", "from": "2026-10-01", "to": "2026-10-20"}).json()
        health = client.get("/api/health").json()
    ids = [m["id"] for m in body["metrics"]]
    assert ids == ["pr_cycle_time", "time_to_first_review", "sprint_velocity", "test_coverage", "defect_count",
                   "cursor_token_usage", "ai_generated_pct"]
    assert [m["id"] for m in health["metrics"]] == ids
    assert all(m["sources"] for m in body["metrics"])


@respx.mock
def test_cursor_401_with_llm_estimates_untracked_commits_and_labels_them_estimates():
    setup_routes(cursor_status=401)
    setup_diff_routes()
    calls = []

    def llm(request):
        calls.append(request)
        prompt = json.loads(request.content)["messages"][1]["content"]
        label = "ai_generated" if "Generate service layer" in prompt else "manual"
        content = json.dumps({"hunks": [{"id": "f0h1", "label": label, "confidence": 0.9,
                                         "rationale": "Mocked test-only evidence."}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    respx.post(LLM_URL).mock(side_effect=llm)
    with TestClient(create_app(llm_settings(), http=httpx.AsyncClient(), credential=FakeCredential())) as client:
        params = {"team": "t", "from": "2026-10-01", "to": "2026-10-20", "metric": "ai_generated_pct"}
        resp = client.get("/api/dashboard", params=params)
        again = client.get("/api/dashboard", params={**params, "refresh": "true"})
    assert resp.status_code == 200, resp.text
    ai = resp.json()["metrics"][0]
    s = ai["summary"]
    assert ai["status"] == "ok" and ai["value"] == 50.0
    assert s["evidenceBasis"] == "llm_estimate"
    assert s["verifiedAiPct"] == 0.0 and s["estimatedAiGeneratedPct"] == 50.0 and s["manualPct"] == 50.0
    assert s["lines"]["estimatedAiGenerated"] == 3 and s["lines"]["estimatedManual"] == 3
    assert s["lines"]["excluded"] == 4  # lock file lines are never classified
    assert s["cursorAttribution"]["status"] == "auth_error"
    assert "LLM estimates, not verified" in ai["message"] and "Cursor attribution unavailable" in ai["message"]
    assert calls[0].headers["api-key"] == TEST_LLM_KEY
    assert TEST_LLM_KEY not in resp.text
    assert len(calls) == 2  # refresh re-reads sources but reuses per-commit estimates
    assert again.json()["metrics"][0]["value"] == 50.0
    assert again.json()["metrics"][0]["summary"]["llmAnalysis"]["cacheHits"] == 2


@respx.mock
def test_cursor_and_llm_both_unauthorised_is_auth_error_not_zero():
    setup_routes(cursor_status=401)
    setup_diff_routes()
    respx.post(LLM_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    with TestClient(create_app(llm_settings(), http=httpx.AsyncClient(), credential=FakeCredential())) as client:
        resp = client.get("/api/dashboard", params={
            "team": "t", "from": "2026-10-01", "to": "2026-10-20", "metric": "ai_generated_pct"})
    ai = resp.json()["metrics"][0]
    assert ai["status"] == "auth_error" and ai["value"] is None
    assert ai["summary"]["llmAnalysis"]["status"] == "auth_error"
    assert ai["summary"]["lines"]["estimatedAiGenerated"] == 0
    assert TEST_LLM_KEY not in resp.text


@respx.mock
def test_cursor_tracked_commit_is_never_sent_to_the_llm():
    setup_routes()
    setup_diff_routes()
    route = respx.post(LLM_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps({"hunks": [{"id": "f0h1", "label": "manual", "confidence": 0.95,
                                          "rationale": "Mocked test-only evidence."}]})}}]}))
    with TestClient(create_app(llm_settings(), http=httpx.AsyncClient(), credential=FakeCredential())) as client:
        resp = client.get("/api/dashboard", params={
            "team": "t", "from": "2026-10-01", "to": "2026-10-20", "metric": "ai_generated_pct"})
    ai = resp.json()["metrics"][0]
    assert route.call_count == 1
    assert SHA_AI[:12] not in route.calls[0].request.content.decode()
    s = ai["summary"]
    # 10 Cursor-verified AI lines + 3 LLM-estimated manual lines = 13 classified lines
    assert s["classifiedLines"] == 13 and ai["value"] == round(10 / 13 * 100, 2)
    assert s["evidenceBasis"] == "cursor_verified+llm_estimate"


@respx.mock
def test_current_sprint_is_labelled_in_progress_with_the_same_formula():
    setup_routes(time_frame="current", finish="2026-11-04T00:00:00Z")
    s = Settings(ado_org_url=ORG, ado_default_project="p", cursor_api_key="k")
    with TestClient(create_app(s, http=httpx.AsyncClient(), credential=FakeCredential())) as client:
        resp = client.get("/api/dashboard", params={
            "team": "t", "from": "2026-10-01", "to": "2026-10-20", "metric": "sprint_velocity"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["iteration"]["timeFrame"] == "current"
        v = body["metrics"][0]
        assert v["value"] == 5
        assert v["summary"]["sprintProgress"] == "in_progress" and v["summary"]["interim"] is True
        assert "Sprint is in progress" in v["message"]


def test_sprint_end_is_clamped_for_an_active_sprint():
    from datetime import datetime, timedelta, timezone

    from app.dashboard import Dashboard
    from app.store import utcnow

    dash = Dashboard(Settings(ado_org_url=ORG), None, None, None)
    _, past_end = dash._sprint_bounds({"name": "Sprint 2", "attributes": {
        "startDate": "2026-09-10T00:00:00Z", "finishDate": "2026-09-23T00:00:00Z", "timeFrame": "past"}})
    assert past_end == datetime(2026, 9, 24, tzinfo=timezone.utc) - timedelta(milliseconds=1)
    _, active_end = dash._sprint_bounds({"name": "Sprint 5", "attributes": {
        "startDate": "2026-10-22T00:00:00Z", "finishDate": "2026-11-04T00:00:00Z", "timeFrame": "current"}})
    assert active_end < utcnow()
