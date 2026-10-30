from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..errors import Status
from ..models import Calc
from .code_diff import Hunk
from .common import display, parse_dt
from .llm_attribution import AI_ASSISTED, AI_GENERATED, MANUAL

MIN_PREFIX = 7

BASIS_CURSOR = "cursor"
BASIS_LLM = "llm"
BASIS_UNMEASURED = "unmeasured"
BASIS_MERGE = "merge_excluded"

CHART_LABELS = {
    "verifiedAi": "AI-generated · Cursor verified",
    "estimatedAiGenerated": "AI-generated · LLM estimate",
    "estimatedAiAssisted": "AI-assisted · LLM estimate",
    "cursorNonAi": "Manual · Cursor-derived non-AI",
    "estimatedManual": "Manual · LLM estimate",
    "unknown": "Unknown / unclassified",
}


class CursorCommitIndex:
    """Lookup of Cursor AI Code Tracking commit records by SHA (deduplicated)."""

    def __init__(self, commits: list[dict]):
        latest: dict[str, dict] = {}
        for c in commits:
            sha = str(c.get("commitHash") or "").lower()
            if not sha:
                continue
            prev = latest.get(sha)
            if prev is None or str(c.get("createdAt") or "") > str(prev.get("createdAt") or ""):
                latest[sha] = c
        self._full = latest
        self._short = [(sha, c) for sha, c in latest.items() if MIN_PREFIX <= len(sha) < 40]

    def __len__(self) -> int:
        return len(self._full)

    def match(self, commit_id: str) -> dict | None:
        sha = commit_id.lower()
        if sha in self._full:
            return self._full[sha]
        for short, c in self._short:
            if sha.startswith(short):
                return c
        return None


@dataclass
class CommitAttribution:
    """Added lines of one commit, each line counted in exactly one category."""

    commit_id: str
    basis: str
    verified_ai: int = 0
    tab: int = 0
    composer: int = 0
    cursor_non_ai: int = 0
    estimated_ai_generated: int = 0
    estimated_ai_assisted: int = 0
    estimated_manual: int = 0
    unknown: int = 0
    excluded: int = 0
    reason: str | None = None

    @property
    def classified(self) -> int:
        return (self.verified_ai + self.cursor_non_ai + self.estimated_ai_generated
                + self.estimated_ai_assisted + self.estimated_manual)

    @property
    def estimated(self) -> int:
        return self.estimated_ai_generated + self.estimated_ai_assisted + self.estimated_manual


def from_cursor(commit_id: str, record: dict) -> CommitAttribution:
    """Cursor counts, normalised so AI + non-AI == total (a line is never in both)."""
    tab = max(0, int(record.get("tabLinesAdded") or 0))
    composer = max(0, int(record.get("composerLinesAdded") or 0))
    ai = tab + composer
    total = max(int(record.get("totalLinesAdded") or 0), ai)
    return CommitAttribution(commit_id, BASIS_CURSOR, verified_ai=ai, tab=tab, composer=composer,
                             cursor_non_ai=total - ai)


def from_llm(commit_id: str, analysed: list[Hunk], labels: dict[str, str], unanalysed_lines: int,
             excluded_lines: int, reason: str | None = None) -> CommitAttribution:
    counts = Counter()
    for h in analysed:
        counts[labels.get(h.id, "unknown")] += h.size
    return CommitAttribution(
        commit_id, BASIS_LLM,
        estimated_ai_generated=counts[AI_GENERATED], estimated_ai_assisted=counts[AI_ASSISTED],
        estimated_manual=counts[MANUAL], unknown=counts["unknown"] + unanalysed_lines,
        excluded=excluded_lines, reason=reason,
    )


def unmeasured(commit_id: str, reason: str) -> CommitAttribution:
    return CommitAttribution(commit_id, BASIS_UNMEASURED, reason=reason)


def merge_excluded(commit_id: str) -> CommitAttribution:
    return CommitAttribution(commit_id, BASIS_MERGE, reason="Merge commit: its lines belong to the merged commits")


def assign_commits(prs_with_commits: list[tuple[dict, list[str]]]) -> list[tuple[dict, list[str], list[str]]]:
    """Give every commit to exactly one PR: the earliest completed PR that contains it.

    Returns (pr, owned commit ids, commit ids already counted under another PR).
    """
    def completed(pr: dict) -> str:
        dt = parse_dt(pr.get("closedDate"))
        return dt.isoformat() if dt else ""

    owner: dict[str, int] = {}
    ordered = sorted(prs_with_commits, key=lambda x: (completed(x[0]), int(x[0].get("pullRequestId") or 0)))
    plan: list[tuple[dict, list[str], list[str]]] = []
    for pr, ids in ordered:
        own, shared = [], []
        for cid in sorted({c.lower() for c in ids if c}):
            if cid in owner:
                shared.append(cid)
            else:
                owner[cid] = int(pr.get("pullRequestId") or 0)
                own.append(cid)
        plan.append((pr, own, shared))
    return plan


def _totals(commits: list[CommitAttribution]) -> dict[str, int]:
    return {
        "verifiedAi": sum(c.verified_ai for c in commits),
        "verifiedAiTab": sum(c.tab for c in commits),
        "verifiedAiComposer": sum(c.composer for c in commits),
        "cursorNonAi": sum(c.cursor_non_ai for c in commits),
        "estimatedAiGenerated": sum(c.estimated_ai_generated for c in commits),
        "estimatedAiAssisted": sum(c.estimated_ai_assisted for c in commits),
        "estimatedManual": sum(c.estimated_manual for c in commits),
        "unknown": sum(c.unknown for c in commits),
        "excluded": sum(c.excluded for c in commits),
    }


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 2) if whole else None


def _percentages(t: dict[str, int]) -> dict[str, float | None]:
    classified = (t["verifiedAi"] + t["cursorNonAi"] + t["estimatedAiGenerated"] + t["estimatedAiAssisted"]
                  + t["estimatedManual"])
    return {
        "classifiedLines": classified,
        "aiGeneratedPct": _pct(t["verifiedAi"] + t["estimatedAiGenerated"], classified),
        "verifiedAiPct": _pct(t["verifiedAi"], classified),
        "estimatedAiGeneratedPct": _pct(t["estimatedAiGenerated"], classified),
        "aiAssistedPct": _pct(t["estimatedAiAssisted"], classified),
        "manualPct": _pct(t["cursorNonAi"] + t["estimatedManual"], classified),
        "unknownPctOfMeasured": _pct(t["unknown"], classified + t["unknown"]),
    }


def _basis(t: dict[str, int]) -> str | None:
    verified = t["verifiedAi"] + t["cursorNonAi"] > 0
    estimated = t["estimatedAiGenerated"] + t["estimatedAiAssisted"] + t["estimatedManual"] > 0
    if verified and estimated:
        return "cursor_verified+llm_estimate"
    if verified:
        return "cursor_verified"
    if estimated:
        return "llm_estimate"
    return None


def pr_row(pr: dict, commits: list[CommitAttribution], shared: list[str], ai_labels: set[str]) -> dict:
    labels = [lbl.get("name", "") for lbl in pr.get("labels") or [] if lbl.get("active", True)]
    t = _totals(commits)
    p = _percentages(t)
    by_basis = Counter(c.basis for c in commits)
    return {
        "pullRequestId": pr.get("pullRequestId"),
        "title": pr.get("title"),
        "repository": (pr.get("repository") or {}).get("name"),
        "author": display(pr.get("createdBy")),
        "completedAt": pr.get("closedDate"),
        "evidenceBasis": _basis(t) or "none",
        "aiGeneratedPct": p["aiGeneratedPct"],
        "manualPct": p["manualPct"],
        "verifiedAiLines": t["verifiedAi"],
        "estimatedAiGeneratedLines": t["estimatedAiGenerated"],
        "estimatedAiAssistedLines": t["estimatedAiAssisted"],
        "manualLines": t["cursorNonAi"] + t["estimatedManual"],
        "unknownLines": t["unknown"],
        "commits": len(commits),
        "cursorTrackedCommits": by_basis[BASIS_CURSOR],
        "llmAnalysedCommits": by_basis[BASIS_LLM],
        "unmeasuredCommits": by_basis[BASIS_UNMEASURED],
        "mergeCommitsExcluded": by_basis[BASIS_MERGE],
        "commitsCountedInEarlierPr": len(shared),
        "aiLabel": any(lbl.lower() in ai_labels for lbl in labels),
    }


def ai_generated_pct(rows: list[dict], commits: list[CommitAttribution], cursor_state: dict,
                     llm_state: dict) -> Calc:
    """AI-generated share of classified added lines. Unknown lines are reported, never counted as manual."""
    t = _totals(commits)
    p = _percentages(t)
    by_basis = Counter(c.basis for c in commits)
    summary = {
        **p,
        "evidenceBasis": _basis(t),
        "lines": t,
        "linesByClassification": {CHART_LABELS[k]: t[k] for k in CHART_LABELS},
        "estimatedLines": t["estimatedAiGenerated"] + t["estimatedAiAssisted"] + t["estimatedManual"],
        "commits": {
            "counted": len(commits),
            "cursorTracked": by_basis[BASIS_CURSOR],
            "llmAnalysed": by_basis[BASIS_LLM],
            "unmeasured": by_basis[BASIS_UNMEASURED],
            "mergeExcluded": by_basis[BASIS_MERGE],
            "duplicatesAcrossPrs": sum(r["commitsCountedInEarlierPr"] for r in rows),
        },
        "completedPrs": len(rows),
        "prsWithAiLabelSelfReported": sum(1 for r in rows if r["aiLabel"]),
        "cursorAttribution": cursor_state,
        "llmAnalysis": llm_state,
    }
    if not rows:
        return Calc(Status.EMPTY, None, summary=summary, sample_size=0, message="No completed pull requests in range")

    cursor_ok = cursor_state.get("status") == "ok"
    llm_status = llm_state.get("status")
    llm_failed = llm_status not in ("ok", Status.NOT_CONFIGURED.value)
    notes: list[str] = []
    if summary["estimatedLines"]:
        notes.append(f"{summary['estimatedLines']} line(s) are LLM estimates, not verified measurements.")
    if not cursor_ok:
        notes.append(f"Cursor attribution unavailable: {cursor_state.get('message')}.")
    if llm_failed:
        notes.append(f"LLM analysis failed: {llm_state.get('message')}.")
    if t["unknown"]:
        notes.append(f"{t['unknown']} measured line(s) are unknown and excluded from the percentages.")
    if by_basis[BASIS_UNMEASURED]:
        notes.append(f"{by_basis[BASIS_UNMEASURED]} commit(s) have no attribution evidence (lines not measured).")

    classified = p["classifiedLines"]
    if classified == 0:
        if llm_failed:
            status, lead = Status(llm_status), "No classified lines."
        elif not cursor_ok and llm_status == Status.NOT_CONFIGURED.value:
            status = Status(cursor_state["status"])
            lead = "No classified lines: Cursor attribution is unavailable and LLM code analysis is not enabled."
        elif llm_status == Status.NOT_CONFIGURED.value:
            status = Status.NOT_CONFIGURED
            lead = ("No PR commit is tracked by Cursor AI Code Tracking and LLM code analysis is not enabled "
                    "(LLM_CODE_ANALYSIS_ENABLED).")
        else:
            status, lead = Status.EMPTY, "No classified lines."
        return Calc(status, None, summary=summary, details=rows, sample_size=0, message=" ".join([lead, *notes]))
    return Calc(Status.OK, p["aiGeneratedPct"], summary=summary, details=rows, sample_size=classified,
                message=" ".join(notes) or None)
