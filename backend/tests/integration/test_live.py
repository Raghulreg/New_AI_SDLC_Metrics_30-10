"""Live integration tests against the REAL configured Azure DevOps org and Cursor team.

Opt-in only:  LIVE_INTEGRATION=1 pytest tests/integration -m live
Requires `az login` (or managed identity) and the variables in ../../.env.
Outputs metric statuses and sample sizes only; never values of secrets.
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LIVE_INTEGRATION") != "1", reason="LIVE_INTEGRATION=1 not set"),
]

FAILURE = {"error", "auth_error", "permission_denied"}


@pytest.fixture(scope="module")
def client():
    settings = load_settings()
    if not settings.ado_configured or not settings.ado_default_project:
        pytest.skip("AZURE_DEVOPS_ORG_URL / AZURE_DEVOPS_DEFAULT_PROJECT not configured")
    with TestClient(create_app(settings)) as c:
        yield c


def test_live_projects_and_teams(client):
    projects = client.get("/api/projects")
    assert projects.status_code == 200, projects.text
    assert projects.json(), "Organization returned no projects"
    # The project comes from the .env file, which is not exported into the process environment.
    project = load_settings().ado_default_project
    teams = client.get(f"/api/projects/{project}/teams")
    assert teams.status_code == 200, teams.text


def test_live_project_dashboard_has_no_source_failures(client):
    resp = client.get("/api/dashboard", params={"refresh": "true"})
    assert resp.status_code == 200, resp.text
    report = {m["id"]: (m["status"], m["sampleSize"], m["message"]) for m in resp.json()["metrics"]}
    for mid, (status, n, msg) in report.items():
        print(f"{mid:32s} {status:24s} n={n} {msg or ''}")
    ado_failures = {k: v for k, v in report.items() if v[0] in FAILURE and k != "cursor_token_usage"
                    and k != "ai_generated_pct"}
    assert not ado_failures, ado_failures


def test_live_cursor_usage(client):
    settings = load_settings()
    if not settings.cursor_configured:
        pytest.skip("Cursor API key not configured")
    resp = client.get("/api/dashboard", params={"metric": "cursor_token_usage", "refresh": "true"})
    m = resp.json()["metrics"][0]
    print(m["status"], m["sampleSize"], m["message"])
    assert m["status"] not in FAILURE, m["message"]
