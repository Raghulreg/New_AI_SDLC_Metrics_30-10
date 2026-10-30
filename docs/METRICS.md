# Metric Definitions

The dashboard reports exactly seven metrics. No approved business-definition
document was available in the workspace. Every definition below is the
**default implemented definition**. Where a rule is an assumption rather than an
approved rule it is marked **[ASSUMPTION]** and is configurable through `.env`
where that is safe. Please have these reviewed.

All date ranges are `[from 00:00:00Z, to 23:59:59.999Z]` UTC, inclusive. The
daily presets (Today, Yesterday, Last 7 days, Last 30 days) are UTC days.

API version: Azure DevOps REST `7.1` (coverage endpoint `7.1-preview.1`).

Every metric result carries `status`, `fetchedAt`, `lastSuccessfulFetch`,
`stale`, `timeRange` and `sources`. A failed source never renders as zero: the
value is `null` and the status is `error`, `auth_error`, `permission_denied`,
`unsupported`, `not_configured` or `attribution_unavailable`.

---

## 1. PR Cycle Time
* **Source**: `GET {org}/{project}/_apis/git/pullrequests?searchCriteria.status=completed&searchCriteria.queryTimeRangeType=closed&searchCriteria.minTime&searchCriteria.maxTime`, paginated with `$top/$skip`. Records are re-filtered client-side by `closedDate`.
* **Definition**: `closedDate − creationDate` in hours for completed PRs closed in the range. Reported as **median** and mean with sample count. **[ASSUMPTION]** median is the headline value.
* **Excluded**: records with missing timestamps or `closedDate < creationDate` (count shown as `excluded`).
* **[ASSUMPTION]** draft time is included (ADO does not expose a reliable "published" timestamp on the PR resource).

## 2. Time to First Review
* **Source**: PR records + `GET .../repositories/{repo}/pullRequests/{id}/threads`.
* **Definition**: `firstReviewAt − creationDate` (hours, median + mean), where `firstReviewAt` is the earliest of:
  * a vote cast by someone other than the PR author (system thread `CodeReviewThreadType = VoteUpdate`), or
  * a human (`commentType = text`) comment by someone other than the author.
* **Not a review**: adding/removing reviewers (`ReviewersUpdate`), policy/system comments, author's own comments.
* PRs with no review activity are reported in `noReviewCount` and excluded from the average.

## 3. Velocity
* **Source**: team iterations `GET {org}/{project}/{team}/_apis/work/teamsettings/iterations`; WIQL; `POST _apis/wit/workitemsbatch`; work item type states `GET _apis/wit/workitemtypes/{type}/states`.
* **Definition**: sum of story points of work items (types in `SPRINT_WORK_ITEM_TYPES`) under the iteration path whose state is in the **Completed** state category as of the sprint end (`ASOF min(finishDate end-of-day, now)`).
* **Story point field**: `STORY_POINTS_FIELD` (default `Microsoft.VSTS.Scheduling.StoryPoints`; Scrum uses `Microsoft.VSTS.Scheduling.Effort`, CMMI `Microsoft.VSTS.Scheduling.Size`).
* Items with no story points are listed in `unestimatedCount` and `unestimatedItemIds` and are **left out of the total**. When every completed item is unestimated, status is `empty` and `value` is null. A sprint with no completed items stays a genuine zero.
* When the iteration `timeFrame` is `current`, the result is labelled **In Progress** (`sprintProgress = in_progress`).
* Sprint-scoped, not date-scoped: requires a team (iterations are team-scoped).

## 4. Test Coverage
* **Source**: `GET {org}/{project}/_apis/test/codecoverage?buildId={id}&flags=1` for the **latest successful build** of `COVERAGE_BUILD_DEFINITION_IDS` finished in the range (optionally `COVERAGE_BRANCH`).
* **Definition**: `covered / total × 100` for the coverage statistic labelled `COVERAGE_STAT_LABEL` (default `Lines`), summed across modules.
* No build published coverage, or the label is absent → `unsupported`/`empty` with explanation. **Never inferred from passing tests.**
* `COVERAGE_BUILD_DEFINITION_IDS` empty → `not_configured`. Team or person scope → `attribution_unavailable`.

## 5. Defects
* **Source**: WIQL over `DEFECT_WORK_ITEM_TYPE` (default `Bug`) + work-item batch.
* **Eligible population**: bugs with `System.CreatedDate` in the range whose current state category is not `Removed`.
* **Value**: distinct eligible bug ids. Each record lists state and `DEFECT_SEVERITY_FIELD`.
* **Defect rate** (sub-value): only computed when `DEFECT_RATE_DENOMINATOR` is set. Supported value: `completed_prs` → `defects / completed PRs in the same scope and range`. **[ASSUMPTION]** Default is empty → rate shows `not_configured` rather than an invented denominator.

## 6. Cursor Token Usage
* **Source**: Cursor Admin API `POST https://api.cursor.com/teams/filtered-usage-events` (epoch-ms range, `page/pageSize≤1000`, 60 req/min).
* **Definition**: sum of `tokenUsage.inputTokens + outputTokens + cacheWriteTokens + cacheReadTokens` across events in the range; each component shown separately. Actual cost is the sum of valid `chargedCents` values divided by 100 (USD). Events with no numeric `chargedCents` are excluded from the cost and are not counted as zero. Cost is never derived from token counts or a model price.
* Events with `isTokenBasedCall = false` have no `tokenUsage`; they are counted as `eventsWithoutTokenData` and **not estimated**.
* Person scope: uses the person's ADO `uniqueName` when it is a normalized email address in `GET /teams/members`. When the ADO identity differs from the Cursor email, configure `CURSOR_IDENTITY_MAPPINGS` as comma-separated `ADO identity=Cursor email` pairs. The configured email must be a Cursor team member. Otherwise the result is `attribution_unavailable`; project-wide usage is never substituted.
* Requires a Cursor **Admin** API key. A User API key returns HTTP 401 → `auth_error`.

## 7. AI Generated %

**Headline**: `AI Generated % = (Cursor-verified AI lines + LLM-estimated AI-generated lines) / classified lines × 100`.

### Population
* Completed PRs in scope and range (same population as PR Cycle Time).
* Their commits from `GET .../pullRequests/{id}/commits`.
* **Merge commits** (more than one parent) are excluded: their diff repeats lines already counted in the merged commits.
* **A commit shared by several PRs** (e.g. cherry-picks, re-targeted PRs) is counted once, in the earliest-completed PR (`closedDate`, then `pullRequestId`). Later PRs list it in `commitsCountedInEarlierPr`.
* The unit is **added lines** of each commit.

### Evidence, in priority order (each commit uses exactly one)
1. **Cursor AI Code Tracking** (`GET /analytics/ai-code/commits`, Enterprise, alpha) — *verified*.
   * AI lines = `tabLinesAdded + composerLinesAdded`.
   * Remaining lines (`max(totalLinesAdded, AI) − AI`) are **Cursor-derived non-AI** and are reported as manual. Cursor's own `nonAiLinesAdded` is not added on top, so a line is never counted as both AI and non-AI.
   * A Cursor-tracked commit is never sent to the LLM.
2. **LLM estimate** (optional, `LLM_CODE_ANALYSIS_ENABLED=true`) — *estimate, never verified*.
   * Only for commits that Cursor did not track.
   * The diff is computed locally from Azure Repos: `GET .../commits/{id}` (parents, message), `GET .../commits/{id}/changes`, and `GET .../items?versionDescriptor.version=...&includeContent=true` for the file at the commit and at its parent. Added hunks come from a line diff; **line counts always come from the diff, never from the model**.
   * The model labels each hunk `ai_generated`, `ai_assisted`, `manual` or `unknown`, with a confidence and brief hunk rationale. Labels below `LLM_MIN_CONFIDENCE`, invalid labels, missing rationales/hunks and unparsable responses become **unknown**.
   * The prompt tells the model that authorship cannot be determined with certainty from code and to prefer `unknown`.
3. **No evidence** — the commit is **unmeasured** (no line counts, nothing classified). It is never counted as manual.

### Line classes
| Class | Source | Counted in the % denominator |
|-------|--------|------------------------------|
| AI-generated · Cursor verified | Cursor tab + composer lines | Yes |
| AI-generated · LLM estimate | LLM `ai_generated` hunks | Yes |
| AI-assisted · LLM estimate | LLM `ai_assisted` hunks | Yes |
| Manual · Cursor-derived non-AI | Cursor total − AI | Yes |
| Manual · LLM estimate | LLM `manual` hunks | Yes |
| Unknown / unclassified | Low-confidence, invalid or missing LLM labels; lines over `LLM_MAX_LINES_PER_COMMIT` | No (reported as `unknownPctOfMeasured`) |
| Excluded | Paths matching `LLM_EXCLUDE_PATHS` (lock files, minified files, maps, snapshots) | No |
| Skipped files | Binary, larger than 400 KB, or over `LLM_MAX_FILES_PER_COMMIT` | No (count in `skippedFiles`) |

### Reported values
* `aiGeneratedPct` (headline), `verifiedAiPct`, `estimatedAiGeneratedPct`, `aiAssistedPct`, `manualPct`. These five add up to 100% of classified lines (headline = verified + estimated AI-generated).
* `unknownPctOfMeasured` = unknown / (classified + unknown).
* `evidenceBasis`: `cursor_verified`, `llm_estimate` or `cursor_verified+llm_estimate`. The UI shows an **Includes LLM estimate** badge whenever any estimated line is included.
* Commit counts: `cursorTracked`, `llmAnalysed`, `unmeasured`, `mergeExcluded`, `duplicatesAcrossPrs`.
* PR labels in `AI_PR_LABELS` are shown as `prsWithAiLabelSelfReported` and **never change the percentages**.

### Status
* When AI Code Tracking answers HTTP 401/403 and the body identifies a team-access restriction, the result is `attribution_unavailable` and the dashboard says **Cursor AI Code Attribution — Access unavailable**, quoting that response. The key is not described as invalid, and no percentage is shown. The endpoint is not called again for `CACHE_TTL_MINUTES`. A 401 that does not identify that restriction stays `auth_error`.
* No completed PRs → `empty`.
* No classified lines:
  * the LLM call failed (e.g. 401) → that failure status;
  * Cursor failed and the LLM is not enabled → Cursor's status (e.g. `auth_error`);
  * the LLM is not enabled → `not_configured`;
  * otherwise (everything unknown) → `empty` with `value = null`.
* Otherwise `ok`, with notes for Cursor unavailability, LLM failures, unknown lines and unmeasured commits.

### Limits
* **The LLM cannot determine authorship from code alone.** Its output is a stylistic estimate, labelled as such everywhere, and never reported as verified.
* At most `LLM_MAX_COMMITS` commits per request are analysed (the rest are unmeasured); requests are rate-limited (`LLM_REQUESTS_PER_MINUTE`). Estimates are cached in memory per commit, model and prompt version for the process lifetime.
* Supported providers are `azure_openai`, `openai` and `gemini`. Gemini calls the official `generateContent` API using `LLM_API_KEY`; its default base endpoint is `https://generativelanguage.googleapis.com`, overrideable by `LLM_ENDPOINT`.
* Cursor attribution only covers code written in Cursor by team members, and the API is alpha.
* Token usage carries no line provenance and is never used for attribution.
* Detection accuracy is not claimed.

---

## Individual attribution

| Metric | Person role used | Individual support |
|--------|------------------|--------------------|
| PR Cycle Time | PR `createdBy` (author) | Yes |
| Time to First Review | PR author (time their PRs waited) | Yes |
| Velocity | `System.AssignedTo` of completed items | Yes (contribution) |
| Test Coverage | — | `attribution_unavailable` |
| Defects | `System.AssignedTo` (assigned population) | Yes, labelled "assigned to" |
| Cursor Token Usage | Cursor `userEmail` = ADO `uniqueName` (email) | Yes when emails match; else `attribution_unavailable` |
| AI Generated % | PR author | Yes |

## Removed metrics

PR Completion Count, Sprint Completion Rate, Build Success Rate, Deployment
Frequency, Open Defects, Defect Resolution Time, Defect Severity Distribution
and AI-Assisted PRs were removed with their APIs, settings and UI. The API
rejects their ids with HTTP 400. Severity is still listed per bug in the
Defects records.
