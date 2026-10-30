from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..config import Settings
from ..errors import SourceError, Status
from ..http import request_json

ADO_RESOURCE_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
WORKITEM_BATCH_SIZE = 200
CREDENTIAL_FAILURE_BACKOFF_SECONDS = 30


class TokenProvider(Protocol):
    async def get_token(self, *scopes: str) -> Any: ...


def iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _seg(value: str) -> str:
    return quote(value, safe="")


class AdoClient:
    def __init__(self, settings: Settings, credential: TokenProvider | None, http: httpx.AsyncClient):
        self.settings = settings
        self.org = settings.ado_org_url
        self.api = settings.ado_api_version
        self.credential = credential
        self.http = http
        self._token: Any = None
        self._token_error: tuple[float, SourceError] | None = None
        self._token_lock = asyncio.Lock()

    async def _auth_header(self) -> dict[str, str]:
        if not self.org:
            raise SourceError(Status.NOT_CONFIGURED, "AZURE_DEVOPS_ORG_URL is not set", "ado")
        if self.settings.ado_auth_mode == "pat":
            # Azure DevOps PAT: Basic auth with an empty username.
            return {"Authorization": "Basic " + base64.b64encode(f":{self.settings.ado_pat}".encode()).decode()}
        if self.credential is None:
            raise SourceError(Status.NOT_CONFIGURED, "No Azure credential available", "ado")
        async with self._token_lock:
            now = datetime.now().timestamp()
            if self._token_error and self._token_error[0] > now:
                raise self._token_error[1]
            if self._token is None or self._token.expires_on - 300 < now:
                try:
                    self._token = await self.credential.get_token(ADO_RESOURCE_SCOPE)
                    self._token_error = None
                except Exception as exc:  # azure.identity raises several credential error types
                    err = SourceError(
                        Status.AUTH_ERROR,
                        f"Could not acquire an Azure DevOps token via DefaultAzureCredential ({type(exc).__name__}). "
                        "Run 'az login' locally or assign a managed identity in Azure.",
                        "ado",
                    )
                    self._token_error = (now + CREDENTIAL_FAILURE_BACKOFF_SECONDS, err)
                    raise err from None
        return {"Authorization": f"Bearer {self._token.token}"}

    async def _request(self, method: str, url: str, source: str, params: dict, json: Any = None):
        try:
            return await request_json(self.http, method, url, source=source, params=params, json=json,
                                      headers=await self._auth_header())
        except SourceError as e:
            if e.status == Status.AUTH_ERROR and e.source != "ado":
                hint = ("AZURE_DEVOPS_PAT was rejected (invalid, expired, or wrong organization)"
                        if self.settings.ado_auth_mode == "pat"
                        else "the DefaultAzureCredential identity was rejected by Azure DevOps")
                e.message = f"{e.message} — {hint}"
            raise

    async def _get(self, url: str, source: str, params: dict | None = None) -> tuple[Any, httpx.Headers]:
        return await self._request("GET", url, source, {"api-version": self.api, **(params or {})})

    async def _post(self, url: str, source: str, body: Any, params: dict | None = None) -> Any:
        data, _ = await self._request("POST", url, source, {"api-version": self.api, **(params or {})}, body)
        return data

    async def _paged_top_skip(self, url: str, source: str, params: dict | None = None, top: int = 500) -> list[dict]:
        items: list[dict] = []
        skip = 0
        while True:
            data, _ = await self._get(url, source, {**(params or {}), "$top": top, "$skip": skip})
            page = (data or {}).get("value", [])
            items.extend(page)
            if len(page) < top:
                return items
            skip += top

    async def _paged_continuation(
        self, url: str, source: str, params: dict | None = None, top: int = 1000, token_param: str = "continuationToken",
    ) -> list[dict]:
        items: list[dict] = []
        token: str | None = None
        while True:
            p = {**(params or {})}
            if top:
                p.setdefault("$top", top)
            if token:
                p[token_param] = token
            data, headers = await self._get(url, source, p)
            page = (data or {}).get("value", [])
            items.extend(page)
            token = headers.get("x-ms-continuationtoken")
            if not token or not page:
                return items

    # ---- Core / teams -------------------------------------------------
    async def projects(self) -> list[dict]:
        return await self._paged_continuation(f"{self.org}/_apis/projects", "ado.projects", {"stateFilter": "wellFormed"}, top=500)

    async def teams(self, project: str) -> list[dict]:
        return await self._paged_top_skip(f"{self.org}/_apis/projects/{_seg(project)}/teams", "ado.teams")

    async def team_members(self, project: str, team: str) -> list[dict]:
        return await self._paged_top_skip(
            f"{self.org}/_apis/projects/{_seg(project)}/teams/{_seg(team)}/members", "ado.teamMembers"
        )

    async def team_iterations(self, project: str, team: str) -> list[dict]:
        data, _ = await self._get(
            f"{self.org}/{_seg(project)}/{_seg(team)}/_apis/work/teamsettings/iterations", "ado.iterations"
        )
        return (data or {}).get("value", [])

    async def team_field_values(self, project: str, team: str) -> dict:
        data, _ = await self._get(
            f"{self.org}/{_seg(project)}/{_seg(team)}/_apis/work/teamsettings/teamfieldvalues", "ado.teamFieldValues"
        )
        return data or {}

    # ---- Repos / PRs ---------------------------------------------------
    async def repositories(self, project: str) -> list[dict]:
        data, _ = await self._get(f"{self.org}/{_seg(project)}/_apis/git/repositories", "ado.repositories")
        return (data or {}).get("value", [])

    async def completed_pull_requests(self, project: str, start: datetime, end: datetime) -> list[dict]:
        prs = await self._paged_top_skip(
            f"{self.org}/{_seg(project)}/_apis/git/pullrequests",
            "ado.pullRequests",
            {
                "searchCriteria.status": "completed",
                "searchCriteria.queryTimeRangeType": "closed",
                "searchCriteria.minTime": iso_utc(start),
                "searchCriteria.maxTime": iso_utc(end),
            },
        )
        return prs

    async def pr_threads(self, project: str, repo_id: str, pr_id: int) -> list[dict]:
        data, _ = await self._get(
            f"{self.org}/{_seg(project)}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/threads", "ado.prThreads"
        )
        return (data or {}).get("value", [])

    async def pr_commits(self, project: str, repo_id: str, pr_id: int) -> list[dict]:
        return await self._paged_continuation(
            f"{self.org}/{_seg(project)}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/commits",
            "ado.prCommits", top=500,
        )

    # ---- Work items ------------------------------------------------------
    async def wiql_ids(self, project: str, query: str) -> list[int]:
        data = await self._post(f"{self.org}/{_seg(project)}/_apis/wit/wiql", "ado.wiql", {"query": query})
        return sorted({int(w["id"]) for w in (data or {}).get("workItems", [])})

    async def work_items(
        self, project: str, ids: list[int], fields: list[str] | None = None, as_of: datetime | None = None
    ) -> list[dict]:
        """Batch-read work items. Omitting `fields` returns every field, which keeps the
        request valid when an optional field (e.g. severity) does not exist in the process."""
        items: dict[int, dict] = {}
        for i in range(0, len(ids), WORKITEM_BATCH_SIZE):
            body: dict[str, Any] = {"ids": ids[i : i + WORKITEM_BATCH_SIZE], "errorPolicy": "omit"}
            if fields:
                body["fields"] = fields
            if as_of:
                body["asOf"] = iso_utc(as_of)
            data = await self._post(f"{self.org}/{_seg(project)}/_apis/wit/workitemsbatch", "ado.workItems", body)
            for wi in (data or {}).get("value", []):
                if wi:
                    items[int(wi["id"])] = wi
        return [items[k] for k in sorted(items)]

    async def work_item_type_states(self, project: str, work_item_type: str) -> list[dict]:
        data, _ = await self._get(
            f"{self.org}/{_seg(project)}/_apis/wit/workitemtypes/{_seg(work_item_type)}/states", "ado.workItemStates"
        )
        return (data or {}).get("value", [])

    # ---- Pipelines -------------------------------------------------------
    async def completed_builds(
        self, project: str, start: datetime, end: datetime, definitions: list[int] | None = None,
        branch: str | None = None, result_filter: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "statusFilter": "completed",
            "minTime": iso_utc(start),
            "maxTime": iso_utc(end),
            "queryOrder": "finishTimeDescending",
        }
        if definitions:
            params["definitions"] = ",".join(str(d) for d in definitions)
        if branch:
            params["branchName"] = branch
        if result_filter:
            params["resultFilter"] = result_filter
        return await self._paged_continuation(f"{self.org}/{_seg(project)}/_apis/build/builds", "ado.builds", params)

    async def code_coverage(self, project: str, build_id: int) -> dict:
        data, _ = await self._request(
            "GET", f"{self.org}/{_seg(project)}/_apis/test/codecoverage", "ado.codeCoverage",
            {"api-version": "7.1-preview.1", "buildId": build_id, "flags": 1},
        )
        return data or {}

    # ---- Commit diffs (AI vs manual code analysis) ---------------------------
    async def commit(self, project: str, repo_id: str, commit_id: str) -> dict:
        data, _ = await self._get(
            f"{self.org}/{_seg(project)}/_apis/git/repositories/{repo_id}/commits/{commit_id}", "ado.commit"
        )
        return data or {}

    async def commit_changes(self, project: str, repo_id: str, commit_id: str, top: int) -> list[dict]:
        data, _ = await self._get(
            f"{self.org}/{_seg(project)}/_apis/git/repositories/{repo_id}/commits/{commit_id}/changes",
            "ado.commitChanges", {"top": top, "skip": 0},
        )
        return (data or {}).get("changes", [])

    async def file_at_commit(self, project: str, repo_id: str, path: str, commit_id: str) -> dict | None:
        """File content at a commit, or None when the path does not exist at that version."""
        try:
            data, _ = await self._get(
                f"{self.org}/{_seg(project)}/_apis/git/repositories/{repo_id}/items", "ado.items",
                {"path": path, "versionDescriptor.version": commit_id, "versionDescriptor.versionType": "commit",
                 "includeContent": "true", "$format": "json"},
            )
        except SourceError as e:
            if e.status == Status.UNSUPPORTED:
                return None
            raise
        return data or {}
