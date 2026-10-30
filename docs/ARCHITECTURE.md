# SDLC Metrics — Architecture and Implementation Plan

This is a fresh application. No source code, binaries, ZIPs or databases were
copied from the cloud PC project. The cloud project was used as a requirements
reference only.

## 1. Stack

| Layer    | Choice                                   | Reason |
|----------|------------------------------------------|--------|
| Backend  | Python 3.12, FastAPI, httpx (async)      | `azure-identity` gives first-class `DefaultAzureCredential` (Azure CLI locally, Managed Identity in Azure). |
| Frontend | React 18 + TypeScript + Vite             | Static bundle; talks only to the backend. Holds no secrets. |
| Storage  | None (in-memory TTL cache only)          | No database is required for live metrics. No DB variables are needed. |
| Secrets  | `.env` locally, Azure Key Vault in cloud | Cursor API key may be resolved from Key Vault by secret name. |

No approved stack document existed in the workspace; this choice is documented
here so it can be reviewed.

## 2. Real-data flow

```
Azure DevOps REST 7.1 ──► app/ado/client.py ──► app/sources.py (validation, error mapping)
                                                     │
Cursor Admin API      ──► app/cursor/client.py ──────┤
Cursor AI Code Tracking API ─────────────────────────┤
                                                     ▼
                                      app/metrics/*.py (pure calculation functions)
                                                     ▼
                                      app/dashboard.py (scope: project → team → person)
                                                     ▼
                                      FastAPI /api/*  ──►  React dashboard
```

* Every external call originates in the backend. The frontend calls only `/api/*`.
* Calculation functions in `app/metrics/` are pure: they take validated source
  records and return a `MetricResult`. They never fetch, and never invent data.
* There is **no mock / demo / fixture code path in the running application**.
  Fixtures live only under `backend/tests/`.

## 3. Result envelope and status model

Every metric is returned as a `MetricResult` (see `app/models.py`):

| `status`                    | Meaning | `value` |
|-----------------------------|---------|---------|
| `ok`                        | Successful source call with eligible records | number |
| `empty`                     | Successful source call, zero eligible records | `0` for counts, `null` for rates/averages (undefined) |
| `error`                     | Source call failed (network, 5xx, timeout, rate limit exhausted, bad payload) | `null` |
| `auth_error`                | 401, credential unavailable, ADO sign-in redirect (203/HTML) | `null` |
| `permission_denied`         | 403 | `null` |
| `unsupported`               | Endpoint/field/plan not available (404, Enterprise-only, field absent) | `null` |
| `not_configured`            | Required configuration (org URL, API key, coverage pipeline, LLM settings) missing | `null` |
| `attribution_unavailable`   | Metric cannot be reliably attributed to the selected person/team | `null` |

Staleness (condition 7) is computed from `fetchedAt` against
`FRESHNESS_WINDOW_MINUTES` and shown as a **Stale** badge in the UI; the backend
also sets `stale: true` on results whose fetch time is outside the window.

A failed refresh **never** returns a previously cached value or a zero. It
returns the failure status with `lastSuccessfulFetch` so the user can see when
real data was last obtained.

## 4. Resilience

* Timeouts: `HTTP_TIMEOUT_SECONDS` (default 30s) per request.
* Retries: only idempotent reads and documented read-only POST queries
  (WIQL, work-item batch, Cursor usage queries); max 3 attempts with
  exponential backoff on 429/5xx/transport errors, honoring `Retry-After`.
* Rate limits: client-side per-endpoint limiter for Cursor (20 req/min default,
  60 req/min for `/teams/filtered-usage-events`, per Cursor docs).
* Pagination: ADO `$top/$skip` and `x-ms-continuationtoken`; Cursor
  `page/pageSize` (`hasNextPage`, `totalCount`).
* Deduplication: by PR id, build id, work-item id, commit id (once across PRs),
  Cursor commit hash (+ user), Cursor usage event composite key.
* Cancellation: requests are bound to the HTTP request lifetime (async tasks are
  cancelled when the client disconnects).
* Cache: in-memory, keyed by (metric, org, project, team, person, range);
  TTL `CACHE_TTL_MINUTES`. `POST /api/refresh` (or `?refresh=true`) bypasses the
  cache. Cursor usage endpoints are aggregated hourly by Cursor, so automatic
  polling is not performed; refresh is explicit only.

## 5. Authentication

* **Azure DevOps (default / cloud)**: `DefaultAzureCredential` acquiring a token for the Azure
  DevOps resource `499b84ac-1321-427f-aa17-267ca6975798/.default`.
  Locally this resolves to `az login`; in Azure it resolves to Managed Identity.
* **Azure DevOps (local development option)**: if `AZURE_DEVOPS_PAT` is set and
  `APP_ENV` is not `Production`, requests use Basic auth with the PAT. With
  `APP_ENV=Production` a PAT is ignored (warning logged) and Managed Identity is used.
  The PAT is read from `.env` without being copied into the process environment,
  is excluded from `repr`, logs, API responses and the frontend bundle.
  Minimum read-only PAT scopes: **Code (Read)**, **Work Items (Read)**,
  **Build (Read)**, **Project and Team (Read)**, **Test Management (Read)**.
  Code (Read) also covers the commit, change and file-content reads used for
  LLM code analysis.
* **Cursor**: Basic auth with the team Admin API key (`admin:*` scope) as
  username. Read from `CURSOR_API_KEY` locally, or from Key Vault secret
  `CURSOR_API_KEY_SECRET_NAME` in `AZURE_KEY_VAULT_URL` via `DefaultAzureCredential`.
  The key identifies the Cursor team, so no team id variable is needed.
* **LLM (optional)**: off unless `LLM_CODE_ANALYSIS_ENABLED=true`. Azure OpenAI
  uses the `api-key` header; OpenAI uses a Bearer token; Gemini uses the official
  `generateContent` API with an `x-goog-api-key` header. The key comes from
  `LLM_API_KEY` locally or Key Vault secret `LLM_API_KEY_SECRET_NAME`. It is
  excluded from `repr`, logs, API responses and the frontend bundle; `/api/health`
  reports only whether it is configured and its source (`env` / `key_vault`).
* **Data egress**: when LLM analysis is enabled, added diff lines of commits that
  Cursor did not track (with credential-like strings redacted, lock/minified files
  excluded) are sent to the configured endpoint. Use an Azure OpenAI deployment
  inside your tenant if source code must not leave it.

## 6. Required permissions (Azure DevOps)

| Area | Permission for the signed-in identity / managed identity |
|------|---------------------------------------------------------|
| Projects, teams, team members, iterations, team area paths | Project Valid Users (View project-level information) |
| Repositories, PRs, PR threads, PR commits, PR labels | Git repositories: **Read** |
| Work items, WIQL, revisions/updates, work item type states | Area path: **View work items in this node**; Boards access level Basic or Stakeholder |
| Commits, commit changes, file content (LLM code analysis) | Git repositories: **Read** |
| Builds (coverage source) | Build pipelines: **View builds** |
| Code coverage | **View builds** + test results readable (Test Plans: View test runs) |

Cursor: Admin API key with `admin:*` scope. AI Code Tracking API requires the
Cursor Enterprise plan (alpha); without it the API returns 403 and the app shows
`unsupported` / `permission_denied` for attribution — never a zero.

## 7. Scope hierarchy

Project → Team → Individual → Metric details.

* **Project**: all records in the configured project (optionally a repo filter).
* **Team**: PRs authored by team members; work items/bugs under the team's area
  paths (`teamfieldvalues`); team iterations for sprint metrics.
* **Individual**: see `docs/METRICS.md` §Individual attribution. Roles are never
  mixed (PR author ≠ reviewer ≠ work-item assignee). Test coverage is reported as
  `attribution_unavailable` at team/individual scope because the build requester
  of CI/merge builds is not a reliable owner of the result.

## 8. Implementation phases

1. Config, error model, resilient HTTP, ADO and Cursor clients (real-source first).
2. Pure metric calculators for the 7 retained metrics + unit tests with fixtures.
3. Dashboard service (scopes, caching, freshness) + FastAPI routes.
4. AI attribution (Cursor AI Code Tracking ↔ ADO PR commits; optional LLM
   estimate on ADO commit diffs for untracked commits).
5. React dashboard (filters, cards, status badges, details, refresh).
6. Tests: unit (fixtures/mocked HTTP), live integration (opt-in, separate).
