from __future__ import annotations

from datetime import datetime

from ..errors import Status
from ..models import Calc
from .common import display, hours_between, parse_dt, summarize


def completed_in_range(prs: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Distinct completed PRs whose actual completion time falls inside [start, end]."""
    seen: dict[int, dict] = {}
    for pr in prs:
        if pr.get("status") != "completed":
            continue
        closed = parse_dt(pr.get("closedDate"))
        if closed is None or not (start <= closed <= end):
            continue
        seen[int(pr["pullRequestId"])] = pr
    return [seen[k] for k in sorted(seen)]


def _pr_row(pr: dict) -> dict:
    return {
        "pullRequestId": pr.get("pullRequestId"),
        "title": pr.get("title"),
        "repository": (pr.get("repository") or {}).get("name"),
        "author": display(pr.get("createdBy")),
        "createdAt": pr.get("creationDate"),
        "completedAt": pr.get("closedDate"),
    }


def pr_cycle_time(prs: list[dict]) -> Calc:
    hours: list[float] = []
    rows: list[dict] = []
    excluded = 0
    for pr in prs:
        created, closed = parse_dt(pr.get("creationDate")), parse_dt(pr.get("closedDate"))
        if created is None or closed is None or closed < created:
            excluded += 1
            continue
        h = hours_between(created, closed)
        hours.append(h)
        rows.append({**_pr_row(pr), "cycleTimeHours": round(h, 2)})
    stats = summarize(hours)
    summary = {**stats, "excluded": excluded}
    if not hours:
        return Calc(Status.EMPTY, None, summary=summary, sample_size=0,
                    message="No completed pull requests with valid timestamps")
    return Calc(Status.OK, stats["median"], summary=summary, details=rows, sample_size=len(hours))


def _prop(thread: dict, name: str) -> str | None:
    val = (thread.get("properties") or {}).get(name)
    if isinstance(val, dict):
        val = val.get("$value")
    return None if val is None else str(val)


def first_review(pr: dict, threads: list[dict]) -> tuple[datetime | None, dict | None]:
    """Earliest vote or human comment by someone other than the PR author.

    Adding reviewers (ReviewersUpdate) and other system events are not reviews.
    """
    author_id = str((pr.get("createdBy") or {}).get("id") or "").lower()
    best: tuple[datetime | None, dict | None] = (None, None)
    for thread in threads:
        thread_type = _prop(thread, "CodeReviewThreadType")
        is_vote = thread_type == "VoteUpdate"
        if is_vote and _prop(thread, "CodeReviewVoteResult") in (None, "0"):
            continue
        if thread_type and not is_vote:
            continue
        for comment in thread.get("comments") or []:
            if comment.get("isDeleted"):
                continue
            author = comment.get("author") or {}
            if str(author.get("id") or "").lower() == author_id or author.get("isContainer"):
                continue
            if not is_vote and comment.get("commentType") != "text":
                continue
            ts = parse_dt(comment.get("publishedDate"))
            if ts and (best[0] is None or ts < best[0]):
                best = (ts, author)
    return best


def time_to_first_review(prs_with_threads: list[tuple[dict, list[dict]]]) -> Calc:
    hours: list[float] = []
    rows: list[dict] = []
    no_review = 0
    for pr, threads in prs_with_threads:
        created = parse_dt(pr.get("creationDate"))
        ts, reviewer = first_review(pr, threads)
        if created is None or ts is None or ts < created:
            no_review += 1
            rows.append({**_pr_row(pr), "firstReviewAt": None, "firstReviewer": None, "hoursToFirstReview": None})
            continue
        h = hours_between(created, ts)
        hours.append(h)
        rows.append({**_pr_row(pr), "firstReviewAt": ts.isoformat(), "firstReviewer": display(reviewer),
                     "hoursToFirstReview": round(h, 2)})
    stats = summarize(hours)
    summary = {**stats, "noReviewCount": no_review}
    if not hours:
        return Calc(Status.EMPTY, None, summary=summary, details=rows, sample_size=0,
                    message="No pull requests with review activity by a non-author in range")
    return Calc(Status.OK, stats["median"], summary=summary, details=rows, sample_size=len(hours))
