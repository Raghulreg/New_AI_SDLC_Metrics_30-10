"""Formula tests. Fixtures below are test-only and never reachable from the running app."""
from datetime import datetime, timezone

from app.errors import Status
from app.metrics import prs

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
AUTHOR = {"id": "a1", "uniqueName": "author@x.com", "displayName": "Author"}
REVIEWER = {"id": "r1", "uniqueName": "rev@x.com", "displayName": "Reviewer"}


def pr(pid, created, closed, status="completed"):
    return {"pullRequestId": pid, "status": status, "creationDate": created, "closedDate": closed,
            "createdBy": AUTHOR, "repository": {"id": "repo", "name": "repo"}, "title": f"PR {pid}"}


def test_completed_in_range_dedupes_and_uses_closed_date():
    items = [
        pr(1, "2026-09-20T00:00:00Z", "2026-10-02T00:00:00.1234567Z"),
        pr(1, "2026-09-20T00:00:00Z", "2026-10-02T00:00:00.1234567Z"),  # duplicate page entry
        pr(2, "2026-10-01T00:00:00Z", "2026-11-02T00:00:00Z"),  # closed after range
        pr(3, "2026-10-01T00:00:00Z", "2026-10-05T00:00:00Z", status="abandoned"),
    ]
    result = prs.completed_in_range(items, START, END)
    assert [p["pullRequestId"] for p in result] == [1]


def test_cycle_time_median_and_excludes_invalid():
    items = [
        pr(1, "2026-10-01T00:00:00Z", "2026-10-01T10:00:00Z"),
        pr(2, "2026-10-01T00:00:00Z", "2026-10-02T06:00:00Z"),
        pr(3, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z"),
        pr(4, "2026-10-05T00:00:00Z", "2026-10-04T00:00:00Z"),  # closed before created
    ]
    calc = prs.pr_cycle_time(items)
    assert calc.status == Status.OK
    assert calc.value == 30.0
    assert calc.summary["excluded"] == 1
    assert calc.sample_size == 3


def test_cycle_time_no_valid_records_is_undefined_not_zero():
    calc = prs.pr_cycle_time([pr(1, None, "2026-10-01T00:00:00Z")])
    assert calc.status == Status.EMPTY and calc.value is None


def _vote_thread(author, when, vote="10"):
    return {"properties": {"CodeReviewThreadType": {"$value": "VoteUpdate"}, "CodeReviewVoteResult": {"$value": vote}},
            "comments": [{"author": author, "publishedDate": when, "commentType": "system"}]}


def test_first_review_ignores_reviewer_added_author_comments_and_vote_resets():
    p = pr(1, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
    threads = [
        {"properties": {"CodeReviewThreadType": {"$value": "ReviewersUpdate"}},
         "comments": [{"author": REVIEWER, "publishedDate": "2026-10-01T00:10:00Z", "commentType": "system"}]},
        {"comments": [{"author": AUTHOR, "publishedDate": "2026-10-01T01:00:00Z", "commentType": "text"}]},
        _vote_thread(REVIEWER, "2026-10-01T02:00:00Z", vote="0"),
        {"comments": [{"author": REVIEWER, "publishedDate": "2026-10-01T05:00:00Z", "commentType": "text"}]},
        _vote_thread(REVIEWER, "2026-10-01T08:00:00Z"),
    ]
    ts, who = prs.first_review(p, threads)
    assert ts.isoformat() == "2026-10-01T05:00:00+00:00"
    assert who["id"] == "r1"


def test_time_to_first_review_counts_unreviewed_separately():
    p1 = pr(1, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
    p2 = pr(2, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
    calc = prs.time_to_first_review([(p1, [_vote_thread(REVIEWER, "2026-10-01T04:00:00Z")]), (p2, [])])
    assert calc.value == 4.0
    assert calc.summary["noReviewCount"] == 1
    assert calc.sample_size == 1
