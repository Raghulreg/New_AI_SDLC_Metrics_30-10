"""AI-generated vs manual code attribution. All payloads are test-only."""
import json

from app.errors import Status
from app.metrics import attribution, code_diff, llm_attribution

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def _cc(sha, total, tab=0, composer=0, non_ai=None):
    return {"commitHash": sha, "totalLinesAdded": total, "tabLinesAdded": tab, "composerLinesAdded": composer,
            "nonAiLinesAdded": non_ai, "createdAt": "2026-10-01T00:00:00Z"}


def _pr(pid, closed="2026-10-05T00:00:00Z", labels=()):
    return {"pullRequestId": pid, "closedDate": closed, "title": f"PR{pid}", "repository": {"name": "r"},
            "createdBy": {"displayName": "Dev"}, "labels": [{"name": n, "active": True} for n in labels]}


OK = {"status": "ok", "message": None}
LLM_OFF = {"status": "not_configured", "message": "LLM code analysis is not enabled"}


def test_added_hunks_counts_only_new_and_replaced_lines():
    old = "a\nb\nc\n"
    new = "a\nB\nc\nd\ne\n"
    hunks = code_diff.added_hunks(old, new, "/src/x.py")
    assert [(h.start_line, h.lines) for h in hunks] == [(2, ("B",)), (4, ("d", "e"))]
    assert code_diff.added_hunks("", "x\ny", "/n.py")[0].size == 2
    assert code_diff.added_hunks("x\ny", "x", "/del.py") == []


def test_exclusion_and_line_budget():
    assert code_diff.is_excluded("/web/package-lock.json", ["package-lock.json"])
    assert code_diff.is_excluded("/a/b/app.min.js", ["*.min.js"])
    assert not code_diff.is_excluded("/a/b/app.js", ["*.min.js"])
    hunks = [code_diff.Hunk(f"h{i}", "/f", i, ("x",) * n) for i, n in enumerate([3, 4, 5])]
    sent, rest = code_diff.split_by_line_budget(hunks, 7)
    assert [h.id for h in sent] == ["h0", "h1"] and [h.id for h in rest] == ["h2"]


def test_cursor_counts_are_normalised_so_no_line_is_counted_twice():
    over = attribution.from_cursor(SHA_A, _cc(SHA_A, 10, tab=4, composer=6, non_ai=5))  # non-AI overlaps AI
    assert over.verified_ai == 10 and over.cursor_non_ai == 0 and over.classified == 10
    short = attribution.from_cursor(SHA_B, _cc(SHA_B, 3, composer=5))  # total smaller than AI lines
    assert short.verified_ai == 5 and short.cursor_non_ai == 0
    derived = attribution.from_cursor(SHA_C, _cc(SHA_C, 10, tab=2))
    assert derived.cursor_non_ai == 8


def test_llm_parse_low_confidence_missing_and_invalid_become_unknown():
    content = json.dumps({"hunks": [
        {"id": "h1", "label": "ai_generated", "confidence": 0.9, "rationale": "Repeated generated scaffold."},
        {"id": "h2", "label": "manual", "confidence": 0.4, "rationale": "Insufficient evidence."},
        {"id": "h3", "label": "certainly_ai", "confidence": 0.99, "rationale": "Unsupported label."},
        {"id": "zz", "label": "manual", "confidence": 1.0, "rationale": "Not a requested hunk."},
    ]})
    labels, invalid = llm_attribution.parse_labels(content, ["h1", "h2", "h3", "h4"], 0.7)
    assert not invalid
    assert labels == {"h1": "ai_generated", "h2": "unknown", "h3": "unknown", "h4": "unknown"}
    labels, invalid = llm_attribution.parse_labels("not json", ["h1"], 0.7)
    assert invalid and labels == {"h1": "unknown"}
    labels, invalid = llm_attribution.parse_labels(
        json.dumps({"hunks": [{"id": "h1", "label": "manual", "confidence": 0.9}]}), ["h1"], 0.7)
    assert not invalid and labels == {"h1": "unknown"}  # rationale is required for a usable estimate


def test_prompt_redacts_credentials_and_states_uncertainty():
    hunk = code_diff.Hunk("h1", "/cfg.py", 1, ('API_KEY = "abc123"', "token: " + "Z" * 45))
    messages = llm_attribution.build_messages("add config", [hunk])
    text = messages[1]["content"]
    assert "abc123" not in text and "Z" * 45 not in text and "[REDACTED]" in text
    assert "cannot be determined with certainty" in messages[0]["content"]


def test_commits_shared_by_prs_are_counted_once_in_earliest_pr():
    plan = attribution.assign_commits([
        (_pr(2, closed="2026-10-06T00:00:00Z"), [SHA_A.upper(), SHA_B]),
        (_pr(1, closed="2026-10-05T00:00:00Z"), [SHA_A, SHA_A]),
    ])
    by_pr = {pr["pullRequestId"]: (own, shared) for pr, own, shared in plan}
    assert by_pr[1] == ([SHA_A], [])
    assert by_pr[2] == ([SHA_B], [SHA_A])


def _hunks(*sizes):
    return [code_diff.Hunk(f"h{i}", "/f.py", 1, ("x",) * n) for i, n in enumerate(sizes, start=1)]


def test_percentages_combine_verified_and_estimated_without_unknown():
    cursor_commit = attribution.from_cursor(SHA_A, _cc(SHA_A, 20, tab=5, composer=5, non_ai=10))
    llm_commit = attribution.from_llm(SHA_B, _hunks(10, 6, 4, 8),
                                      {"h1": "ai_generated", "h2": "ai_assisted", "h3": "manual", "h4": "unknown"},
                                      unanalysed_lines=2, excluded_lines=50)
    merge = attribution.merge_excluded(SHA_C)
    commits = [cursor_commit, llm_commit, merge]
    rows = [attribution.pr_row(_pr(1), commits, [], {"ai-assisted"})]
    calc = attribution.ai_generated_pct(rows, commits, OK, {"status": "ok"})
    s = calc.summary
    # classified = 10 verified AI + 10 Cursor non-AI + 10 est. AI + 6 est. assisted + 4 est. manual = 40
    assert s["classifiedLines"] == 40 and calc.sample_size == 40
    assert calc.value == 50.0                      # (10 + 10) / 40
    assert s["verifiedAiPct"] == 25.0 and s["estimatedAiGeneratedPct"] == 25.0
    assert s["aiAssistedPct"] == 15.0 and s["manualPct"] == 35.0
    assert round(s["aiGeneratedPct"] + s["aiAssistedPct"] + s["manualPct"], 2) == 100.0
    assert s["lines"]["unknown"] == 10 and s["lines"]["excluded"] == 50   # 8 low-evidence + 2 over budget
    assert s["unknownPctOfMeasured"] == 20.0
    assert s["evidenceBasis"] == "cursor_verified+llm_estimate"
    assert s["commits"]["mergeExcluded"] == 1
    assert "LLM estimates, not verified" in calc.message
    assert calc.status == Status.OK


def test_cursor_401_without_llm_is_auth_error_not_zero():
    commits = [attribution.unmeasured(SHA_A, "no evidence")]
    rows = [attribution.pr_row(_pr(1), commits, [], set())]
    calc = attribution.ai_generated_pct(rows, commits, {"status": "auth_error", "message": "cursor: Invalid key"},
                                        LLM_OFF)
    assert calc.status == Status.AUTH_ERROR and calc.value is None
    assert "LLM code analysis is not enabled" in calc.message


def test_cursor_ok_but_untracked_and_llm_off_is_not_configured():
    commits = [attribution.unmeasured(SHA_A, "no evidence")]
    rows = [attribution.pr_row(_pr(1), commits, [], set())]
    calc = attribution.ai_generated_pct(rows, commits, OK, LLM_OFF)
    assert calc.status == Status.NOT_CONFIGURED and calc.value is None


def test_llm_auth_failure_without_classified_lines_surfaces_llm_status():
    commits = [attribution.from_llm(SHA_A, _hunks(5), {}, 0, 0, "LLM request failed")]
    rows = [attribution.pr_row(_pr(1), commits, [], set())]
    calc = attribution.ai_generated_pct(rows, commits, {"status": "auth_error", "message": "cursor 401"},
                                        {"status": "auth_error", "message": "llm: Unauthorized"})
    assert calc.status == Status.AUTH_ERROR and calc.value is None
    assert calc.summary["lines"]["unknown"] == 5


def test_all_unknown_is_undefined_not_zero_or_manual():
    commits = [attribution.from_llm(SHA_A, _hunks(5), {"h1": "unknown"}, 0, 0)]
    rows = [attribution.pr_row(_pr(1), commits, [], set())]
    calc = attribution.ai_generated_pct(rows, commits, OK, {"status": "ok"})
    assert calc.status == Status.EMPTY and calc.value is None
    assert calc.summary["manualPct"] is None and calc.summary["lines"]["unknown"] == 5


def test_no_prs_is_empty():
    calc = attribution.ai_generated_pct([], [], OK, LLM_OFF)
    assert calc.status == Status.EMPTY and calc.value is None


def test_self_reported_label_never_changes_percentages():
    commits = [attribution.from_cursor(SHA_A, _cc(SHA_A, 4, non_ai=4))]
    rows = [attribution.pr_row(_pr(1, labels=["AI-Assisted"]), commits, [], {"ai-assisted"})]
    calc = attribution.ai_generated_pct(rows, commits, OK, LLM_OFF)
    assert calc.value == 0.0 and calc.summary["manualPct"] == 100.0
    assert calc.summary["prsWithAiLabelSelfReported"] == 1
