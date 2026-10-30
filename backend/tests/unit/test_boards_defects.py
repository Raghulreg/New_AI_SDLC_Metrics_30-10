from app.errors import Status
from app.metrics import boards, defects

SP = "Microsoft.VSTS.Scheduling.StoryPoints"
SEV = "Microsoft.VSTS.Common.Severity"
CATS = boards.build_category_map({
    "User Story": [{"name": "New", "category": "Proposed"}, {"name": "Active", "category": "InProgress"},
                   {"name": "Resolved", "category": "InProgress"},
                   {"name": "Closed", "category": "Completed"}, {"name": "Removed", "category": "Removed"}],
    "Bug": [{"name": "New", "category": "Proposed"}, {"name": "Active", "category": "InProgress"},
            {"name": "Resolved", "category": "Resolved"}, {"name": "Closed", "category": "Completed"},
            {"name": "Removed", "category": "Removed"}],
})


def story(i, state, points, path="P\\Sprint 1"):
    f = {"System.WorkItemType": "User Story", "System.State": state, "System.IterationPath": path}
    if points is not None:
        f[SP] = points
    return {"id": i, "fields": f}


def bug(i, state, created="2026-10-02T00:00:00Z", severity=None):
    f = {"System.WorkItemType": "Bug", "System.State": state, "System.CreatedDate": created, "System.Title": f"B{i}"}
    if severity:
        f[SEV] = severity
    return {"id": i, "fields": f}


def test_velocity_sums_completed_and_reports_unestimated():
    calc = boards.sprint_velocity([story(1, "Closed", 5), story(2, "Closed", None), story(3, "Active", 8)], CATS, SP)
    assert calc.value == 5
    assert calc.summary["unestimatedCount"] == 1
    assert calc.summary["completedItems"] == 2


def test_velocity_counts_completed_category_and_leaves_resolved_unresolved():
    calc = boards.sprint_velocity(
        [story(1, "Closed", 5), story(2, "Resolved", 8), story(3, "Active", 3)], CATS, SP)
    assert calc.status == Status.OK
    assert calc.value == 5
    assert calc.summary["completedItems"] == 1
    assert [row["id"] for row in calc.details if row["stateCategory"] == "Completed"] == [1]


def test_velocity_missing_points_are_listed_separately():
    mixed = boards.sprint_velocity([story(1, "Closed", 5), story(2, "Closed", None)], CATS, SP)
    assert mixed.value == 5
    assert mixed.summary["unestimatedItemIds"] == [2]
    assert mixed.summary["missingPointsExcluded"] is True
    assert mixed.details[1]["storyPoints"] is None
    assert mixed.details[1]["pointsKnown"] is False
    assert "left out of the point total" in mixed.message

    unknown = boards.sprint_velocity([story(2, "Closed", None), story(3, "Active", 8)], CATS, SP)
    assert unknown.status == Status.EMPTY
    assert unknown.value is None
    assert unknown.summary["completedItems"] == 1
    assert unknown.summary["unestimatedItemIds"] == [2]


def test_velocity_without_completed_items_is_a_genuine_zero():
    calc = boards.sprint_velocity([story(1, "Active", 5)], CATS, SP)
    assert calc.status == Status.EMPTY and calc.value == 0


def test_active_sprint_is_labelled_in_progress_and_completed_sprint_is_not():
    items = [story(1, "Closed", 5), story(2, "Active", 8)]
    current = {"name": "Sprint 5", "attributes": {"timeFrame": "current",
                                                  "startDate": "2026-10-22T00:00:00Z",
                                                  "finishDate": "2026-11-04T00:00:00Z"}}
    past = {"name": "Sprint 4", "attributes": {"timeFrame": "past",
                                               "startDate": "2026-10-08T00:00:00Z",
                                               "finishDate": "2026-10-21T00:00:00Z"}}
    active = boards.annotate_sprint_progress(boards.sprint_velocity(items, CATS, SP), current)
    done = boards.annotate_sprint_progress(boards.sprint_velocity(items, CATS, SP), past)
    assert active.value == 5 and done.value == 5
    assert active.summary["sprintProgress"] == "in_progress" and active.summary["interim"] is True
    assert "Sprint is in progress" in active.message
    assert done.summary["sprintProgress"] == "completed" and done.summary["interim"] is False
    assert done.message is None


def test_defect_count_excludes_removed():
    calc = defects.defect_count([bug(1, "New"), bug(2, "Removed"), bug(3, "Closed")], CATS, SEV)
    assert calc.value == 2


def test_defect_count_keeps_unspecified_severity_on_rows():
    calc = defects.defect_count([bug(1, "New", severity="1 - Critical"), bug(2, "Active")], CATS, SEV)
    assert [r["severity"] for r in calc.details] == ["1 - Critical", "Unspecified"]


def test_defects_empty_is_valid_zero():
    calc = defects.defect_count([], CATS, SEV)
    assert calc.status == Status.EMPTY and calc.value == 0


def test_defect_rate_zero_denominator_undefined():
    calc = defects.defect_rate(3, 0, "completed PRs")
    assert calc.status == Status.EMPTY and calc.value is None
