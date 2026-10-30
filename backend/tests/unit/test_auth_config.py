"""Auth and configuration tests. All credential strings here are test placeholders."""
import base64
import json
import os
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.ado.client import AdoClient
from app.config import Settings, load_settings, read_env_files
from app.cursor.client import CursorClient
from app.errors import SourceError, Status
from app.main import _credential, create_app
from tests.unit.test_http_clients import ORG, FailingCredential

TEST_PAT = "test-pat-value-not-real"
TEST_CURSOR_KEY = "crsr_test_key_not_real"
TEST_LLM_KEY = "llm_test_key_not_real"
ENV_KEYS = ("AZURE_DEVOPS_ORG_URL", "AZURE_DEVOPS_DEFAULT_PROJECT", "AZURE_DEVOPS_PAT", "CURSOR_API_KEY", "APP_ENV")


@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def write_env(tmp_path: Path, **values) -> Path:
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    return path


def test_env_file_is_loaded_without_leaking_into_process_env(tmp_path, clean_env):
    env = write_env(tmp_path, AZURE_DEVOPS_ORG_URL=ORG + "/", AZURE_DEVOPS_DEFAULT_PROJECT="p",
                    AZURE_DEVOPS_PAT=TEST_PAT, CURSOR_API_KEY=TEST_CURSOR_KEY)
    s = load_settings(env)
    assert s.ado_org_url == ORG and s.ado_default_project == "p"
    assert s.ado_pat == TEST_PAT and s.cursor_api_key == TEST_CURSOR_KEY
    assert s.ado_auth_mode == "pat"
    assert "AZURE_DEVOPS_PAT" not in os.environ and "CURSOR_API_KEY" not in os.environ
    assert TEST_PAT not in repr(s) and TEST_CURSOR_KEY not in repr(s)


def test_cursor_identity_mappings_accept_normalized_ado_id_or_unique_name(tmp_path, clean_env):
    env = write_env(tmp_path, CURSOR_IDENTITY_MAPPINGS="  ADO-ID = Cursor.User@Example.COM , ado@x.com=alias@x.com")
    assert load_settings(env).cursor_identity_mappings == {
        "ado-id": "cursor.user@example.com",
        "ado@x.com": "alias@x.com",
    }


def test_cursor_identity_mappings_reject_invalid_entries(tmp_path, clean_env):
    env = write_env(tmp_path, CURSOR_IDENTITY_MAPPINGS="ado-id=not-an-email")
    with pytest.raises(ValueError, match="CURSOR_IDENTITY_MAPPINGS"):
        load_settings(env)


def test_later_env_file_wins_but_blank_values_do_not_erase(tmp_path):
    root = tmp_path / "root.env"
    root.write_text("AZURE_DEVOPS_ORG_URL=https://dev.azure.com/root\nAZURE_DEVOPS_PAT=fromroot\n", encoding="utf-8")
    backend = tmp_path / "backend.env"
    backend.write_text("AZURE_DEVOPS_ORG_URL=https://dev.azure.com/backend\nAZURE_DEVOPS_PAT=\n", encoding="utf-8")
    missing = tmp_path / "missing.env"
    values = read_env_files([root, backend, missing])
    assert values["AZURE_DEVOPS_ORG_URL"] == "https://dev.azure.com/backend"
    assert values["AZURE_DEVOPS_PAT"] == "fromroot"


def test_real_environment_overrides_env_file(tmp_path, clean_env, monkeypatch):
    env = write_env(tmp_path, AZURE_DEVOPS_DEFAULT_PROJECT="from-file")
    monkeypatch.setenv("AZURE_DEVOPS_DEFAULT_PROJECT", "from-env")
    assert load_settings(env).ado_default_project == "from-env"


def test_missing_env_file_is_allowed(tmp_path, clean_env):
    s = load_settings(tmp_path / "does-not-exist.env")
    assert not s.ado_configured and not s.cursor_configured


def test_pat_ignored_in_production():
    s = Settings(ado_org_url=ORG, ado_pat=TEST_PAT, app_env="Production")
    assert s.ado_auth_mode == "default_azure_credential"


@respx.mock
async def test_pat_basic_auth_header_without_azure_credential():
    route = respx.get(f"{ORG}/p/_apis/git/repositories").mock(return_value=httpx.Response(200, json={"value": []}))
    async with httpx.AsyncClient() as c:
        await AdoClient(Settings(ado_org_url=ORG, ado_pat=TEST_PAT), None, c).repositories("p")
    expected = "Basic " + base64.b64encode(f":{TEST_PAT}".encode()).decode()
    assert route.calls[0].request.headers["Authorization"] == expected


async def test_production_with_pat_still_uses_default_azure_credential():
    async with httpx.AsyncClient() as c:
        client = AdoClient(Settings(ado_org_url=ORG, ado_pat=TEST_PAT, app_env="Production"), FailingCredential(), c)
        with pytest.raises(SourceError) as ei:
            await client.repositories("p")
    assert ei.value.status == Status.AUTH_ERROR and "DefaultAzureCredential" in ei.value.message


@pytest.mark.parametrize("response", [httpx.Response(401, json={"message": "Unauthorized"}),
                                      httpx.Response(203, html="<html>Sign in</html>")])
@respx.mock
async def test_rejected_pat_is_auth_error_without_echoing_secret(response):
    respx.get(f"{ORG}/p/_apis/git/repositories").mock(return_value=response)
    async with httpx.AsyncClient() as c:
        with pytest.raises(SourceError) as ei:
            await AdoClient(Settings(ado_org_url=ORG, ado_pat=TEST_PAT), None, c).repositories("p")
    assert ei.value.status == Status.AUTH_ERROR
    assert "AZURE_DEVOPS_PAT was rejected" in ei.value.message
    assert TEST_PAT not in str(ei.value)


def test_no_azure_credential_created_in_pat_mode():
    assert _credential(Settings(ado_org_url=ORG, ado_pat=TEST_PAT)) is None


async def test_cursor_key_resolved_from_key_vault_when_env_key_absent():
    calls = []

    async def resolver():
        calls.append(1)
        return TEST_CURSOR_KEY

    async with httpx.AsyncClient() as c:
        headers = await CursorClient(Settings(), c, resolver)._headers()
    assert calls == [1]
    assert headers["Authorization"] == "Basic " + base64.b64encode(f"{TEST_CURSOR_KEY}:".encode()).decode()


@respx.mock
def test_health_and_dashboard_never_expose_secrets_and_need_no_database():
    respx.get(f"{ORG}/p/_apis/git/pullrequests").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get("https://api.cursor.com/analytics/ai-code/commits").mock(
        return_value=httpx.Response(200, json={"items": [], "totalCount": 0}))
    s = Settings(ado_org_url=ORG, ado_default_project="p", ado_pat=TEST_PAT, cursor_api_key=TEST_CURSOR_KEY,
                 llm_code_analysis_enabled=True, llm_provider="openai", llm_model="m", llm_api_key=TEST_LLM_KEY)
    with TestClient(create_app(s, http=httpx.AsyncClient())) as client:
        health = client.get("/api/health").json()
        dash = client.get("/api/dashboard", params={"metric": ["pr_cycle_time", "ai_generated_pct"]})
    assert health["adoAuthMode"] == "pat" and health["cursorKeySource"] == "env"
    assert health["llmConfigured"] is True and health["llmKeySource"] == "env"
    text = json.dumps(health) + dash.text
    assert TEST_PAT not in text and TEST_CURSOR_KEY not in text and TEST_LLM_KEY not in text
    assert TEST_LLM_KEY not in repr(s)
    assert [m["status"] for m in dash.json()["metrics"]] == ["empty", "empty"]


def test_llm_requires_explicit_opt_in_and_complete_settings():
    assert not Settings(llm_provider="openai", llm_model="m", llm_api_key=TEST_LLM_KEY).llm_configured
    assert not Settings(llm_code_analysis_enabled=True, llm_provider="azure_openai", llm_model="m",
                        llm_api_key=TEST_LLM_KEY).llm_configured  # Azure OpenAI needs an endpoint
    assert Settings(llm_code_analysis_enabled=True, llm_provider="openai", llm_model="m",
                    llm_api_key=TEST_LLM_KEY).llm_configured


def test_backend_has_no_database_dependency():
    root = Path(__file__).resolve().parents[2]
    reqs = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    for driver in ("sqlalchemy", "pyodbc", "psycopg", "asyncpg", "pymssql", "prisma", "alembic", "sqlite"):
        assert driver not in reqs
    assert not any(f.startswith("db_") or "database" in f for f in Settings.__dataclass_fields__)
