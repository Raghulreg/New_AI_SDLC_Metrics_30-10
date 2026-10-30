from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Hashable

from .ado.client import AdoClient, iso_utc
from .config import Settings
from .cursor.client import CursorClient
from .errors import SourceError, Status
from .llm.client import LlmClient
from .metrics import attribution, boards, code_diff, cursor_usage, defects, llm_attribution, pipelines, prs
from .metrics.common import identity_matches, parse_dt
from .models import Calc, DashboardResponse, MetricResult
from .store import SourceStore, utcnow

log = logging.getLogger(__name__)

DETAIL_CONCURRENCY = 8
LLM_CONCURRENCY = 3
MAX_FILE_BYTES = 400_000
LLM_CACHE_LIMIT = 5000
LLM_STOP_STATUSES = {Status.AUTH_ERROR, Status.PERMISSION_DENIED, Status.NOT_CONFIGURED, Status.UNSUPPORTED}


@dataclass(frozen=True)
class MetricSpec:
    id: str
    name: str
    unit: str
    definition: str
    sources: tuple[str, ...]
    team: bool = True
    person: bool = True


SPECS = {
    s.id: s
    for s in [
        MetricSpec("pr_cycle_time", "PR Cycle Time", "hours (median)",
                   "closedDate − creationDate for completed PRs", ("Azure Repos · Pull Requests",)),
        MetricSpec("time_to_first_review", "Time to First Review", "hours (median)",
                   "First non-author vote or human comment − PR creation (adding reviewers is not a review)",
                   ("Azure Repos · Pull Requests", "Azure Repos · PR Threads")),
        MetricSpec("sprint_velocity", "Velocity", "story points",
                   "Story points of items in the Completed state category at sprint end. "
                   "Items with no story points are listed separately and left out of the total. "
                   "The current sprint is labelled In Progress.",
                   ("Azure Boards · Iterations", "Azure Boards · Work Items (ASOF)")),
        MetricSpec("test_coverage", "Test Coverage", "%",
                   "Covered / total for configured statistic on latest successful coverage build",
                   ("Azure Pipelines · Builds", "Azure Test · Code Coverage"), team=False, person=False),
        MetricSpec("defect_count", "Defects", "bugs",
                   "Bugs created in range (excluding Removed); rate only with a configured denominator",
                   ("Azure Boards · Bugs",)),
        MetricSpec("cursor_token_usage", "Cursor Token Usage", "tokens",
                   "Sum of input, output and cache tokens on Cursor usage events. Actual cost is the sum of "
                   "chargedCents divided by 100 (USD). Events with no chargedCents are excluded from the cost "
                   "and are not counted as zero",
                   ("Cursor Admin API · filtered-usage-events", "Cursor Admin API · team members")),
        MetricSpec("ai_generated_pct", "AI Generated %", "% of classified lines",
                   "Added lines in completed PRs that are AI-generated (Cursor verified + LLM estimate) / classified "
                   "added lines. Each commit is counted once; Cursor-tracked commits are never re-estimated; "
                   "unknown lines are reported separately and excluded.",
                   ("Azure Repos · PR Commits & Diffs", "Cursor AI Code Tracking API · commits",
                    "LLM code analysis (estimate)")),
    ]
}


@dataclass
class Scope:
    project: str
    start: datetime
    end: datetime
    team: str | None = None
    person: dict | None = None
    iteration_id: str | None = None
    repo_ids: tuple[str, ...] = ()
    refresh: bool = False

    @property
    def level(self) -> str:
        return "person" if self.person else "team" if self.team else "project"


@dataclass
class Run:
    scope: Scope
    refreshed: set = field(default_factory=set)
    iteration: dict | None = None


@dataclass
class Tracker:
    fetched: list[datetime] = field(default_factory=list)
    failed_key: Hashable | None = None


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _wiql_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


class Dashboard:
    def __init__(self, settings: Settings, ado: AdoClient, cursor: CursorClient, store: SourceStore,
                 llm: LlmClient | None = None):
        self.s = settings
        self.ado = ado
        self.cursor = cursor
        self.store = store
        self.llm = llm
        self._sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        self._llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)
        # Estimates for an immutable commit are reused for the same model and prompt version.
        self._llm_cache: dict[tuple[str, str, str], dict[str, str]] = {}

    # ---- source access ---------------------------------------------------
    async def src(self, run: Run, tr: Tracker, key: tuple, fetcher: Callable[[], Awaitable[Any]]) -> Any:
        force = run.scope.refresh and key not in run.refreshed
        run.refreshed.add(key)
        try:
            value, fetched_at = await self.store.get(key, fetcher, force=force)
        except SourceError:
            tr.failed_key = key
            raise
        tr.fetched.append(fetched_at)
        return value

    async def _bounded(self, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        async with self._sem:
            return await coro_fn()

    async def team_members(self, run: Run, tr: Tracker) -> list[dict]:
        sc = run.scope
        members = await self.src(run, tr, ("teamMembers", sc.project, sc.team),
                                 lambda: self.ado.team_members(sc.project, sc.team))
        return [m.get("identity", m) for m in members]

    async def _scoped_prs(self, run: Run, tr: Tracker) -> list[dict]:
        sc = run.scope
        raw = await self.src(run, tr, ("prs", sc.project, sc.start, sc.end),
                             lambda: self.ado.completed_pull_requests(sc.project, sc.start, sc.end))
        items = prs.completed_in_range(raw, sc.start, sc.end)
        if sc.repo_ids:
            wanted = {r.lower() for r in sc.repo_ids}
            items = [p for p in items if str((p.get("repository") or {}).get("id", "")).lower() in wanted]
        if sc.person:
            items = [p for p in items if identity_matches(p.get("createdBy"), sc.person)]
        elif sc.team:
            members = await self.team_members(run, tr)
            items = [p for p in items if any(identity_matches(p.get("createdBy"), m) for m in members)]
        return items

    def _check_cap(self, n: int, cap: int, what: str) -> None:
        if n > cap:
            raise SourceError(
                Status.ERROR,
                f"{n} {what} exceed the per-request detail limit ({cap}). Narrow the date range or scope; "
                "results are not sampled.", "dashboard",
            )

    async def _pr_details(self, run: Run, tr: Tracker, items: list[dict], kind: str) -> list[list[dict]]:
        self._check_cap(len(items), self.s.max_pr_detail_fetch, "pull requests")
        project = run.scope.project
        fn = self.ado.pr_threads if kind == "threads" else self.ado.pr_commits

        async def one(pr: dict) -> list[dict]:
            repo_id, pr_id = pr["repository"]["id"], int(pr["pullRequestId"])
            return await self._bounded(lambda: self.src(run, tr, (kind, project, pr_id), lambda: fn(project, repo_id, pr_id)))

        return list(await asyncio.gather(*(one(p) for p in items)))

    async def categories(self, run: Run, tr: Tracker, types: set[str]) -> boards.CategoryMap:
        project = run.scope.project
        states = {}
        for t in sorted(types):
            states[t] = await self.src(run, tr, ("states", project, t.lower()),
                                       lambda t=t: self.ado.work_item_type_states(project, t))
        return boards.build_category_map(states)

    async def area_clause(self, run: Run, tr: Tracker) -> str:
        sc = run.scope
        if not sc.team:
            return ""
        tfv = await self.src(run, tr, ("teamFieldValues", sc.project, sc.team),
                             lambda: self.ado.team_field_values(sc.project, sc.team))
        fld = ((tfv.get("field") or {}).get("referenceName")) or "System.AreaPath"
        parts = [
            f"[{fld}] {'UNDER' if v.get('includeChildren') else '='} {_q(v['value'])}"
            for v in tfv.get("values") or [] if v.get("value")
        ]
        if not parts:
            raise SourceError(Status.UNSUPPORTED, f"Team '{sc.team}' has no area paths configured", "ado.teamFieldValues")
        return " AND (" + " OR ".join(parts) + ")"

    async def _work_items_by_wiql(self, run: Run, tr: Tracker, label: str, query: str,
                                  fields: list[str] | None, as_of: datetime | None = None) -> list[dict]:
        project = run.scope.project

        async def fetch() -> list[dict]:
            ids = await self.ado.wiql_ids(project, query)
            return await self.ado.work_items(project, ids, fields, as_of=as_of) if ids else []

        return await self.src(run, tr, ("wiql", project, label, query, as_of), fetch)

    def _filter_assigned(self, run: Run, items: list[dict]) -> list[dict]:
        if not run.scope.person:
            return items
        return [w for w in items if identity_matches(w.get("fields", {}).get("System.AssignedTo"), run.scope.person)]

    async def bugs_created(self, run: Run, tr: Tracker) -> list[dict]:
        sc = run.scope
        area = await self.area_clause(run, tr)
        query = (
            "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND [System.WorkItemType] = {_q(self.s.defect_work_item_type)} "
            f"AND [System.CreatedDate] >= {_q(_wiql_date(sc.start))} "
            f"AND [System.CreatedDate] < {_q(_wiql_date(sc.end + timedelta(days=1)))}{area}"
        )
        items = await self._work_items_by_wiql(run, tr, "bugsCreated", query, None)
        return self._filter_assigned(run, items)

    async def bug_categories(self, run: Run, tr: Tracker) -> boards.CategoryMap:
        return await self.categories(run, tr, {self.s.defect_work_item_type})

    # ---- metric computations ---------------------------------------------
    async def m_pr_cycle_time(self, run: Run, tr: Tracker) -> Calc:
        return prs.pr_cycle_time(await self._scoped_prs(run, tr))

    async def m_time_to_first_review(self, run: Run, tr: Tracker) -> Calc:
        items = await self._scoped_prs(run, tr)
        threads = await self._pr_details(run, tr, items, "threads")
        return prs.time_to_first_review(list(zip(items, threads)))

    async def resolve_iteration(self, run: Run, tr: Tracker) -> dict:
        sc = run.scope
        if not sc.team:
            raise SourceError(Status.NOT_CONFIGURED, "Select a team: sprints/iterations are team-scoped", "dashboard")
        if run.iteration is not None:
            return run.iteration
        iterations = await self.src(run, tr, ("iterations", sc.project, sc.team),
                                    lambda: self.ado.team_iterations(sc.project, sc.team))
        chosen = None
        if sc.iteration_id:
            chosen = next((i for i in iterations if i.get("id") == sc.iteration_id), None)
            if chosen is None:
                raise SourceError(Status.UNSUPPORTED, "Selected iteration is not assigned to this team", "ado.iterations")
        else:
            chosen = next((i for i in iterations if (i.get("attributes") or {}).get("timeFrame") == "current"), None)
            if chosen is None:
                past = [i for i in iterations if (i.get("attributes") or {}).get("timeFrame") == "past"]
                chosen = past[-1] if past else None
        if chosen is None:
            raise SourceError(Status.EMPTY, "Team has no current or past iterations", "ado.iterations")
        run.iteration = chosen
        return chosen

    def _sprint_bounds(self, it: dict) -> tuple[datetime, datetime]:
        attrs = it.get("attributes") or {}
        start, finish = parse_dt(attrs.get("startDate")), parse_dt(attrs.get("finishDate"))
        if start is None or finish is None:
            raise SourceError(Status.UNSUPPORTED, f"Iteration '{it.get('name')}' has no start/finish dates", "ado.iterations")
        end = min(finish + timedelta(days=1) - timedelta(milliseconds=1), utcnow() - timedelta(minutes=1))
        return start, end

    def _sprint_query(self, path: str, area: str, as_of: datetime) -> str:
        types = ", ".join(_q(t) for t in self.s.sprint_work_item_types)
        return (
            "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND [System.WorkItemType] IN ({types}) AND [System.IterationPath] UNDER {_q(path)}{area} "
            f"ASOF {_q(iso_utc(as_of))}"
        )

    @property
    def _sprint_fields(self) -> list[str]:
        return ["System.Id", "System.Title", "System.WorkItemType", "System.State", "System.AssignedTo",
                "System.IterationPath", self.s.story_points_field]

    async def _sprint_categories(self, run: Run, tr: Tracker, items: list[dict]) -> boards.CategoryMap:
        types = {w["fields"].get("System.WorkItemType") for w in items if w.get("fields", {}).get("System.WorkItemType")}
        return await self.categories(run, tr, types)

    async def m_sprint_velocity(self, run: Run, tr: Tracker) -> Calc:
        it = await self.resolve_iteration(run, tr)
        _, end = self._sprint_bounds(it)
        area = await self.area_clause(run, tr)
        items = await self._work_items_by_wiql(run, tr, "sprintEnd", self._sprint_query(it["path"], area, end),
                                               self._sprint_fields, as_of=end)
        cats = await self._sprint_categories(run, tr, items)
        calc = boards.sprint_velocity(self._filter_assigned(run, items), cats, self.s.story_points_field)
        return boards.annotate_sprint_progress(calc, it)

    async def m_test_coverage(self, run: Run, tr: Tracker) -> Calc:
        sc = run.scope
        if not self.s.coverage_build_definition_ids:
            raise SourceError(Status.NOT_CONFIGURED, "COVERAGE_BUILD_DEFINITION_IDS is not set", "dashboard")
        builds = await self.src(
            run, tr, ("coverageBuilds", sc.project, sc.start, sc.end, tuple(self.s.coverage_build_definition_ids)),
            lambda: self.ado.completed_builds(sc.project, sc.start, sc.end, self.s.coverage_build_definition_ids,
                                              branch=self.s.coverage_branch or None, result_filter="succeeded"),
        )
        candidates = sorted((b for b in builds if b.get("result") == "succeeded"),
                            key=lambda b: b.get("finishTime") or "", reverse=True)[:5]
        last: Calc | None = None
        for b in candidates:
            resp = await self.src(run, tr, ("coverage", sc.project, int(b["id"])),
                                  lambda b=b: self.ado.code_coverage(sc.project, int(b["id"])))
            last = pipelines.test_coverage(b, resp, self.s.coverage_stat_label)
            if last.status == Status.OK:
                return last
        return last or pipelines.test_coverage(None, None, self.s.coverage_stat_label)

    async def defect_rate_block(self, run: Run, tr: Tracker, count: int) -> dict:
        denom = self.s.defect_rate_denominator
        if not denom:
            return {"status": Status.NOT_CONFIGURED.value, "value": None,
                    "message": "DEFECT_RATE_DENOMINATOR not set; no denominator is assumed"}
        if denom != "completed_prs":
            return {"status": Status.UNSUPPORTED.value, "value": None, "message": f"Unsupported denominator '{denom}'"}
        try:
            n = len(await self._scoped_prs(run, tr))
        except SourceError as e:
            return {"status": e.status.value, "value": None, "message": str(e)}
        calc = defects.defect_rate(count, n, "completed PRs in same scope and range")
        return {"status": calc.status.value, "value": calc.value, "message": calc.message, **calc.summary}

    async def m_defect_count(self, run: Run, tr: Tracker) -> Calc:
        bugs = await self.bugs_created(run, tr)
        cats = await self.bug_categories(run, tr)
        calc = defects.defect_count(bugs, cats, self.s.defect_severity_field)
        calc.summary["population"] = "assigned to person" if run.scope.person else "created in range"
        calc.summary["defectRate"] = await self.defect_rate_block(run, tr, int(calc.value or 0))
        return calc

    async def _cursor_emails(self, run: Run, tr: Tracker) -> tuple[set[str] | None, dict]:
        """Map ADO identities to Cursor emails. None = no restriction (project scope).

        Exact normalized ADO email matches are authoritative. Explicit config aliases are
        allowed for a different UPN/alias, but their Cursor target must be a team member.
        """
        sc = run.scope
        if sc.level == "project":
            return None, {"scopeNote": "Whole Cursor team bound to the API key (not filtered by ADO project)"}
        members = await self.src(run, tr, ("cursorMembers",), self.cursor.team_members)
        cursor_emails = {str(m.get("email", "")).strip().casefold() for m in members if m.get("email")}
        people = [sc.person] if sc.person else await self.team_members(run, tr)
        mapped, unmapped, invalid_mappings = set(), [], []
        exact_count = config_count = 0
        for p in people:
            ado_id = str(p.get("id") or "").strip().casefold()
            unique_name = str(p.get("uniqueName") or "").strip().casefold()
            label = p.get("displayName") or p.get("uniqueName") or p.get("id")
            if "@" in unique_name and unique_name in cursor_emails:
                mapped.add(unique_name)
                exact_count += 1
            else:
                target = self.s.cursor_identity_mappings.get(ado_id) or self.s.cursor_identity_mappings.get(unique_name)
                if target and target in cursor_emails:
                    mapped.add(target)
                    config_count += 1
                else:
                    unmapped.append(label)
                    if target:
                        invalid_mappings.append(label)
        info = {
            "mappedIdentities": len(mapped),
            "exactEmailMatches": exact_count,
            "configuredMappings": config_count,
            "unmappedIdentities": unmapped,
            "invalidConfiguredMappings": invalid_mappings,
            "identityRule": "normalized ADO uniqueName email == Cursor member email; then CURSOR_IDENTITY_MAPPINGS by ADO id or uniqueName",
        }
        if not mapped:
            names = ", ".join(str(n) for n in unmapped[:10]) or "selected identity"
            raise SourceError(Status.ATTRIBUTION_UNAVAILABLE,
                              f"No Cursor identity mapping for: {names}. Add CURSOR_IDENTITY_MAPPINGS "
                              "using ADO identity=Cursor email (exact normalized emails are matched automatically).",
                              "cursor.teamMembers")
        return mapped, info

    async def m_cursor_token_usage(self, run: Run, tr: Tracker) -> Calc:
        sc = run.scope
        emails, info = await self._cursor_emails(run, tr)
        events = await self.src(run, tr, ("cursorEvents", sc.start, sc.end),
                                lambda: self.cursor.usage_events(sc.start, sc.end))
        calc = cursor_usage.token_usage(events, emails)
        calc.summary.update(info)
        return calc

    async def _commit_diff(self, project: str, repo_id: str, commit_id: str) -> dict:
        """Added-line hunks of one commit against its single parent (read from Azure Repos)."""
        commit = await self.ado.commit(project, repo_id, commit_id)
        parents = commit.get("parents") or []
        result = {"merge": len(parents) > 1, "message": commit.get("comment") or "", "hunks": [],
                  "excludedLines": 0, "skippedFiles": 0, "truncatedFiles": False}
        if result["merge"]:
            return result
        parent = parents[0] if parents else None
        cap = self.s.llm_max_files_per_commit
        changes = await self.ado.commit_changes(project, repo_id, commit_id, top=cap + 1)
        files = [c for c in changes if not (c.get("item") or {}).get("isFolder")
                 and (c.get("item") or {}).get("gitObjectType", "blob") == "blob"
                 and "delete" not in str(c.get("changeType", "")).lower()]
        if len(files) > cap:
            result["truncatedFiles"] = True
            result["skippedFiles"] += len(files) - cap
            files = files[:cap]
        hunks: list[code_diff.Hunk] = []
        for i, change in enumerate(files):
            path = change["item"]["path"]
            new = await self.ado.file_at_commit(project, repo_id, path, commit_id)
            if new is None or (new.get("contentMetadata") or {}).get("isBinary") \
                    or len(new.get("content") or "") > MAX_FILE_BYTES:
                result["skippedFiles"] += 1
                continue
            old_text = ""
            if parent and "add" not in str(change.get("changeType", "")).lower():
                old_path = change.get("sourceServerItem") or path
                old = await self.ado.file_at_commit(project, repo_id, old_path, parent)
                old_text = (old or {}).get("content") or ""
            file_hunks = code_diff.added_hunks(old_text, new.get("content") or "", path, id_prefix=f"f{i}h")
            if code_diff.is_excluded(path, self.s.llm_exclude_paths):
                result["excludedLines"] += sum(h.size for h in file_hunks)
            else:
                hunks.extend(file_hunks)
        result["hunks"] = hunks
        return result

    async def _llm_commit(self, run: Run, tr: Tracker, repo_id: str, cid: str, llm_state: dict
                          ) -> attribution.CommitAttribution:
        if llm_state.get("stop"):
            return attribution.unmeasured(cid, f"LLM analysis unavailable: {llm_state.get('message')}")
        project = run.scope.project
        try:
            diff = await self._bounded(lambda: self.src(
                run, tr, ("commitDiff", project, repo_id, cid), lambda: self._commit_diff(project, repo_id, cid)))
        except SourceError as e:
            llm_state["diffErrors"] = llm_state.get("diffErrors", 0) + 1
            return attribution.unmeasured(cid, f"Commit diff unavailable: {e}")
        if diff["merge"]:
            return attribution.merge_excluded(cid)
        sent, overflow = code_diff.split_by_line_budget(diff["hunks"], self.s.llm_max_lines_per_commit)
        overflow_lines = sum(h.size for h in overflow)
        skipped = (f"{diff['skippedFiles']} file(s) not measured (binary, too large or over the file cap)"
                   if diff["skippedFiles"] else None)
        if not sent:
            return attribution.from_llm(cid, [], {}, overflow_lines, diff["excludedLines"], skipped)
        key = (cid, self.llm.model_id, llm_attribution.PROMPT_VERSION)
        labels = self._llm_cache.get(key)
        if labels is None:
            try:
                async with self._llm_sem:
                    content = await self.llm.complete_json(llm_attribution.build_messages(diff["message"], sent))
            except SourceError as e:
                llm_state["failures"] = llm_state.get("failures", 0) + 1
                llm_state["lastError"] = {"status": e.status.value, "message": str(e)}
                if e.status in LLM_STOP_STATUSES:
                    llm_state["stop"] = True
                    llm_state["message"] = str(e)
                    llm_state["stopStatus"] = e.status.value
                return attribution.from_llm(cid, sent, {}, overflow_lines, diff["excludedLines"],
                                            f"LLM request failed: {e}")
            labels, invalid = llm_attribution.parse_labels(content, [h.id for h in sent], self.s.llm_min_confidence)
            if invalid:
                llm_state["invalidResponses"] = llm_state.get("invalidResponses", 0) + 1
            elif len(self._llm_cache) < LLM_CACHE_LIMIT:
                self._llm_cache[key] = labels
        else:
            llm_state["cacheHits"] = llm_state.get("cacheHits", 0) + 1
        return attribution.from_llm(cid, sent, labels, overflow_lines, diff["excludedLines"], skipped)

    async def m_ai_generated_pct(self, run: Run, tr: Tracker) -> Calc:
        sc = run.scope
        items = await self._scoped_prs(run, tr)
        commits = await self._pr_details(run, tr, items, "commits")

        index: attribution.CursorCommitIndex | None = None
        since = sc.start - timedelta(days=self.s.ai_commit_lookback_days)
        try:
            records = await self.src(run, tr, ("cursorAiCommits", since, sc.end),
                                     lambda: self.cursor.ai_commits(since, sc.end))
            index = attribution.CursorCommitIndex(records)
            cursor_state = {"status": "ok", "message": None, "commitRecords": len(index),
                            "lookbackDays": self.s.ai_commit_lookback_days}
        except SourceError as e:
            cursor_state = {"status": e.status.value, "message": str(e), "commitRecords": None,
                            "lookbackDays": self.s.ai_commit_lookback_days}

        llm_enabled = bool(self.llm and self.llm.configured)
        llm_state: dict = (
            {"status": "ok", "model": self.llm.model_id, "promptVersion": llm_attribution.PROMPT_VERSION,
             "minConfidence": self.s.llm_min_confidence, "maxCommits": self.s.llm_max_commits}
            if llm_enabled else
            {"status": Status.NOT_CONFIGURED.value,
             "message": "LLM code analysis is not enabled (LLM_CODE_ANALYSIS_ENABLED + provider settings)"}
        )

        plan = attribution.assign_commits(
            [(pr, [c.get("commitId", "") for c in cs]) for pr, cs in zip(items, commits)])
        results: dict[str, attribution.CommitAttribution] = {}
        pending: list[tuple[dict, str]] = []
        for pr, own, _shared in plan:
            for cid in own:
                record = index.match(cid) if index is not None else None
                if record is not None:
                    results[cid] = attribution.from_cursor(cid, record)
                else:
                    pending.append((pr, cid))

        if not llm_enabled:
            reason = "Not tracked by Cursor and LLM code analysis is not enabled"
            for _pr, cid in pending:
                results[cid] = attribution.unmeasured(cid, reason)
        else:
            analysable, over_cap = pending[: self.s.llm_max_commits], pending[self.s.llm_max_commits:]
            for _pr, cid in over_cap:
                results[cid] = attribution.unmeasured(cid, "Over LLM_MAX_COMMITS for one request")
            llm_state["commitsOverCap"] = len(over_cap)
            done = await asyncio.gather(*(self._llm_commit(run, tr, pr["repository"]["id"], cid, llm_state)
                                          for pr, cid in analysable))
            for (_pr, cid), res in zip(analysable, done):
                results[cid] = res
            if llm_state.get("stop"):
                llm_state["status"] = llm_state.pop("stopStatus")
            elif llm_state.get("failures") and llm_state["failures"] == len(analysable):
                llm_state["status"] = llm_state["lastError"]["status"]
                llm_state["message"] = llm_state["lastError"]["message"]
            llm_state.pop("stop", None)

        labels = set(self.s.ai_pr_labels)
        rows = [attribution.pr_row(pr, [results[c] for c in own], shared, labels) for pr, own, shared in plan]
        calc = attribution.ai_generated_pct(rows, list(results.values()), cursor_state, llm_state)
        calc.summary["aiLabels"] = sorted(labels)
        return calc

    # ---- orchestration -----------------------------------------------------
    async def compute(self, run: Run, metric_id: str) -> MetricResult:
        spec = SPECS[metric_id]
        sc = run.scope
        base = dict(id=spec.id, name=spec.name, unit=spec.unit, definition=spec.definition, sources=list(spec.sources),
                    scopeLevel=sc.level, timeRange={"from": sc.start.isoformat(), "to": sc.end.isoformat()})
        if (sc.level == "team" and not spec.team) or (sc.level == "person" and not spec.person):
            return MetricResult(**base, status=Status.ATTRIBUTION_UNAVAILABLE,
                                message=f"{spec.name} cannot be reliably attributed at {sc.level} level; "
                                        "view it at project level")
        tr = Tracker()
        try:
            calc: Calc = await getattr(self, f"m_{metric_id}")(run, tr)
        except SourceError as e:
            if e.status == Status.EMPTY:
                return MetricResult(**base, status=Status.EMPTY, message=e.message,
                                    fetchedAt=min(tr.fetched) if tr.fetched else None)
            return MetricResult(**base, status=e.status, message=str(e),
                                lastSuccessfulFetch=self.store.last_success(tr.failed_key))
        except Exception:
            log.exception("Metric %s failed", metric_id)
            return MetricResult(**base, status=Status.ERROR, message="Internal error while calculating this metric")
        fetched_at = min(tr.fetched) if tr.fetched else None
        stale = bool(fetched_at and (utcnow() - fetched_at).total_seconds() > self.s.freshness_window_minutes * 60)
        return MetricResult(**base, status=calc.status, value=calc.value, summary=calc.summary, details=calc.details,
                            sampleSize=calc.sample_size, message=calc.message, fetchedAt=fetched_at,
                            lastSuccessfulFetch=fetched_at, stale=stale)

    async def dashboard(self, scope: Scope, metric_ids: list[str] | None = None) -> DashboardResponse:
        run = Run(scope)
        ids = metric_ids or list(SPECS)
        results = await asyncio.gather(*(self.compute(run, m) for m in ids))
        return DashboardResponse(
            organization=self.s.ado_org_url, project=scope.project, team=scope.team, person=scope.person,
            timeRange={"from": scope.start.isoformat(), "to": scope.end.isoformat()},
            iteration=({"id": run.iteration.get("id"), "name": run.iteration.get("name"),
                        "path": run.iteration.get("path"), **(run.iteration.get("attributes") or {})}
                       if run.iteration else None),
            generatedAt=utcnow(), freshnessWindowMinutes=self.s.freshness_window_minutes, metrics=list(results),
        )
