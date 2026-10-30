# SDLC Metrics (fresh local build)

Live SDLC dashboard over **real Azure DevOps** and **real Cursor API** data.
There is no mock, demo or fallback data path: missing credentials, permissions
or source support are shown as explicit states, never as zeros.

* Architecture, auth, permissions: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
* Metric definitions, assumptions, AI attribution rules: [`docs/METRICS.md`](docs/METRICS.md)

## Run locally

```powershell
# 1. Configuration — edit the root .env (never commit it; no database is needed)
#    Azure DevOps auth, pick one:
#      a) AZURE_DEVOPS_PAT=<read-only PAT>   (local development only), or
#      b) leave it empty and run `az login`  (DefaultAzureCredential)
#    Cursor: CURSOR_API_KEY=<team Admin API key>  (separate from the PAT)

# 2. Backend (http://127.0.0.1:8000)
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. Frontend (http://localhost:5173, proxies /api to the backend)
cd ..\frontend
npm install
npm run dev
```

## Tests

```powershell
cd backend;  .\.venv\Scripts\python -m pytest            # unit tests (fixtures / mocked HTTP only)
cd frontend; npm test                                     # UI formatting/status tests
# Live integration (opt-in, real configured org + az login):
cd backend;  $env:LIVE_INTEGRATION="1"; .\.venv\Scripts\python -m pytest tests/integration -m live -s
```

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /api/health` | Which sources are configured (no secret values) |
| `GET /api/projects`, `/api/projects/{p}/teams`, `/teams/{t}/members`, `/teams/{t}/iterations`, `/repositories` | Real ADO lookups for filters |
| `GET /api/dashboard?project&team&person&from&to&iteration&repo&metric&refresh` | The 7 metrics (PR Cycle Time, Time to First Review, Velocity, Test Coverage, Defects, Cursor Token Usage, AI Generated %); `refresh=true` re-reads sources |
