from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .ado.client import AdoClient
from .config import Settings, load_settings
from .cursor.client import CursorClient
from .dashboard import SPECS, Dashboard, Scope
from .errors import SourceError, Status
from .llm.client import LlmClient
from .store import SourceStore

MAX_RANGE_DAYS = 366


def _credential(settings: Settings):
    needed_for_ado = settings.ado_configured and settings.ado_auth_mode == "default_azure_credential"
    needed_for_key_vault = bool(settings.key_vault_url) and (
        bool(settings.cursor_api_key_secret_name and not settings.cursor_api_key)
        or bool(settings.llm_code_analysis_enabled and settings.llm_api_key_secret_name and not settings.llm_api_key)
    )
    if not (needed_for_ado or needed_for_key_vault):
        return None
    from azure.identity.aio import DefaultAzureCredential

    # Locally resolves to Azure CLI (`az login`); in Azure to Managed Identity.
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _key_vault_resolver(settings: Settings, credential, secret_name: str, label: str):
    if not (settings.key_vault_url and secret_name and credential):
        return None

    async def resolve() -> str:
        from azure.keyvault.secrets.aio import SecretClient

        try:
            async with SecretClient(vault_url=settings.key_vault_url, credential=credential) as client:
                secret = await client.get_secret(secret_name)
        except Exception as exc:
            raise SourceError(Status.AUTH_ERROR, f"Could not read {label} from Key Vault ({type(exc).__name__})",
                              "keyvault") from None
        if not secret.value:
            raise SourceError(Status.NOT_CONFIGURED, f"Key Vault secret for {label} is empty", "keyvault")
        return secret.value

    return resolve


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None, credential=None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level.upper())
    for noisy in ("azure", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if settings.ado_pat and settings.is_production:
        logging.getLogger(__name__).warning(
            "AZURE_DEVOPS_PAT is set but ignored because APP_ENV is Production; using DefaultAzureCredential")

    state: dict = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        client = http or httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=False)
        cred = credential if credential is not None else _credential(settings)
        ado = AdoClient(settings, cred, client)
        cursor = CursorClient(settings, client, _key_vault_resolver(
            settings, cred, settings.cursor_api_key_secret_name, "Cursor API key"))
        llm = LlmClient(settings, client, _key_vault_resolver(
            settings, cred, settings.llm_api_key_secret_name, "LLM API key"))
        state["dashboard"] = Dashboard(settings, ado, cursor, SourceStore(settings.cache_ttl_minutes * 60), llm)
        state["ado"] = ado
        try:
            yield
        finally:
            if http is None:
                await client.aclose()
            if credential is None and cred is not None:
                await cred.close()

    app = FastAPI(title="SDLC Metrics", lifespan=lifespan)

    @app.exception_handler(SourceError)
    async def source_error_handler(_req, exc: SourceError):
        code = {Status.AUTH_ERROR: 401, Status.PERMISSION_DENIED: 403, Status.UNSUPPORTED: 404,
                Status.NOT_CONFIGURED: 503}.get(exc.status, 502)
        return JSONResponse(status_code=code, content={"status": exc.status.value, "message": str(exc)})

    def dash() -> Dashboard:
        return state["dashboard"]

    def ado() -> AdoClient:
        return state["ado"]

    async def cached(key: tuple, fetcher, refresh: bool = False):
        value, _ = await dash().store.get(key, fetcher, force=refresh)
        return value

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "environment": settings.app_env,
            "adoConfigured": settings.ado_configured,
            "adoAuthMode": settings.ado_auth_mode,
            "organization": settings.ado_org_url or None,
            "defaultProject": settings.ado_default_project or None,
            "cursorConfigured": settings.cursor_configured,
            "cursorKeySource": "env" if settings.cursor_api_key else ("keyvault" if settings.cursor_configured else None),
            "coverageConfigured": bool(settings.coverage_build_definition_ids),
            "defectRateDenominator": settings.defect_rate_denominator or None,
            "llmConfigured": settings.llm_configured,
            "llmProvider": settings.llm_provider or None,
            "llmModel": settings.llm_model or None,
            "llmKeySource": ("env" if settings.llm_api_key
                             else "keyvault" if settings.key_vault_url and settings.llm_api_key_secret_name else None),
            "freshnessWindowMinutes": settings.freshness_window_minutes,
            "cacheTtlMinutes": settings.cache_ttl_minutes,
            "metrics": [{"id": s.id, "name": s.name} for s in SPECS.values()],
        }

    @app.get("/api/projects")
    async def projects(refresh: bool = False):
        items = await cached(("projects",), ado().projects, refresh)
        return [{"id": p["id"], "name": p["name"]} for p in sorted(items, key=lambda p: p["name"].lower())]

    @app.get("/api/projects/{project}/teams")
    async def teams(project: str, refresh: bool = False):
        items = await cached(("teams", project), lambda: ado().teams(project), refresh)
        return [{"id": t["id"], "name": t["name"]} for t in sorted(items, key=lambda t: t["name"].lower())]

    @app.get("/api/projects/{project}/teams/{team}/members")
    async def members(project: str, team: str, refresh: bool = False):
        items = await cached(("teamMembers", project, team), lambda: ado().team_members(project, team), refresh)
        people = [m.get("identity", m) for m in items]
        return [{"id": p.get("id"), "displayName": p.get("displayName"), "uniqueName": p.get("uniqueName")}
                for p in sorted(people, key=lambda p: str(p.get("displayName", "")).lower()) if not p.get("isContainer")]

    @app.get("/api/projects/{project}/teams/{team}/iterations")
    async def iterations(project: str, team: str, refresh: bool = False):
        items = await cached(("iterations", project, team), lambda: ado().team_iterations(project, team), refresh)
        return [{"id": i["id"], "name": i["name"], "path": i["path"], **(i.get("attributes") or {})} for i in items]

    @app.get("/api/projects/{project}/repositories")
    async def repositories(project: str, refresh: bool = False):
        items = await cached(("repos", project), lambda: ado().repositories(project), refresh)
        return [{"id": r["id"], "name": r["name"]} for r in sorted(items, key=lambda r: r["name"].lower())
                if not r.get("isDisabled")]

    @app.get("/api/dashboard")
    async def dashboard(
        project: str | None = None,
        team: str | None = None,
        person: str | None = Query(None, description="ADO identity id of a team member"),
        start: date | None = Query(None, alias="from"),
        end: date | None = Query(None, alias="to"),
        iteration: str | None = None,
        repo: list[str] = Query(default_factory=list),
        metric: list[str] = Query(default_factory=list),
        refresh: bool = False,
    ):
        project = project or settings.ado_default_project
        if not project:
            raise HTTPException(400, "project is required (or set AZURE_DEVOPS_DEFAULT_PROJECT)")
        today = datetime.now(timezone.utc).date()
        end = end or today
        start = start or (end - timedelta(days=29))
        if start > end:
            raise HTTPException(400, "'from' must not be after 'to'")
        if (end - start).days > MAX_RANGE_DAYS:
            raise HTTPException(400, f"Range may not exceed {MAX_RANGE_DAYS} days")
        unknown = [m for m in metric if m not in SPECS]
        if unknown:
            raise HTTPException(400, f"Unknown metric(s): {', '.join(unknown)}")
        if person and not team:
            raise HTTPException(400, "person requires team (Project → Team → Individual)")

        person_identity = None
        if person:
            ppl = await members(project, team)
            person_identity = next((p for p in ppl if str(p["id"]).lower() == person.lower()), None)
            if person_identity is None:
                raise HTTPException(404, "Person is not a member of the selected team")

        scope = Scope(
            project=project, team=team, person=person_identity,
            start=datetime.combine(start, time.min, tzinfo=timezone.utc),
            end=datetime.combine(end, time.max, tzinfo=timezone.utc),
            iteration_id=iteration, repo_ids=tuple(repo or settings.ado_repository_ids), refresh=refresh,
        )
        return await dash().dashboard(scope, metric or None)

    return app


app = create_app()
