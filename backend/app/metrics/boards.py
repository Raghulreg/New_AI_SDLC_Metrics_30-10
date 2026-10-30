from __future__ import annotations

from ..errors import Status
from ..models import Calc
from .common import display

COMPLETED = "Completed"
REMOVED = "Removed"

CategoryMap = dict[tuple[str, str], str]


def build_category_map(states_by_type: dict[str, list[dict]]) -> CategoryMap:
    """(work item type, state name) -> ADO state category."""
    return {
        (wit.lower(), s["name"].lower()): s.get("category", "")
        for wit, states in states_by_type.items()
        for s in states
    }


def category_of(wi: dict, categories: CategoryMap) -> str | None:
    f = wi.get("fields", {})
    return categories.get((str(f.get("System.WorkItemType", "")).lower(), str(f.get("System.State", "")).lower()))


def _points(wi: dict, sp_field: str) -> float | None:
    val = wi.get("fields", {}).get(sp_field)
    try:
        return None if val is None else float(val)
    except (TypeError, ValueError):
        return None


def _row(wi: dict, sp_field: str, categories: CategoryMap) -> dict:
    f = wi.get("fields", {})
    points = _points(wi, sp_field)
    return {
        "id": wi.get("id"),
        "title": f.get("System.Title"),
        "type": f.get("System.WorkItemType"),
        "state": f.get("System.State"),
        "stateCategory": category_of(wi, categories),
        "assignedTo": display(f.get("System.AssignedTo")),
        "storyPoints": points,
        "pointsKnown": points is not None,
        "iterationPath": f.get("System.IterationPath"),
    }


def _unestimated_note(count: int) -> str | None:
    """Missing story points stay out of the totals. They are never stored as zero effort."""
    if count <= 0:
        return None
    noun = "item" if count == 1 else "items"
    verb = "has" if count == 1 else "have"
    return f"{count} completed {noun} {verb} no story points. Listed separately and left out of the point total."


def sprint_velocity(items_at_end: list[dict], categories: CategoryMap, sp_field: str) -> Calc:
    completed = [wi for wi in items_at_end if category_of(wi, categories) == COMPLETED]
    known_points: list[float] = []
    unestimated_ids: list = []
    for wi in completed:
        pts = _points(wi, sp_field)
        if pts is None:
            unestimated_ids.append(wi.get("id"))
        else:
            known_points.append(pts)
    summary = {
        "completedItems": len(completed),
        "unestimatedCount": len(unestimated_ids),
        "unestimatedItemIds": unestimated_ids,
        "itemsInSprint": len(items_at_end),
        "storyPointField": sp_field,
        "missingPointsExcluded": True,
    }
    rows = [_row(wi, sp_field, categories) for wi in items_at_end]
    if not completed:
        return Calc(Status.EMPTY, 0, summary=summary, details=rows, sample_size=0,
                    message="No completed work items in this sprint")
    note = _unestimated_note(len(unestimated_ids))
    if not known_points:
        return Calc(Status.EMPTY, None, summary=summary, details=rows, sample_size=len(completed),
                    message=f"{note} Velocity is undefined until those items have story points.")
    return Calc(Status.OK, round(sum(known_points), 2), summary=summary, details=rows,
                sample_size=len(completed), message=note)


IN_PROGRESS_MESSAGE = "Sprint is in progress. This result is interim and updates until the sprint ends."


def annotate_sprint_progress(calc: Calc, iteration: dict) -> Calc:
    """Label an active sprint. Point totals are left unchanged."""
    frame = str((iteration.get("attributes") or {}).get("timeFrame") or "").lower()
    in_progress = frame == "current"
    calc.summary["sprintProgress"] = "in_progress" if in_progress else ("completed" if frame == "past" else frame or "unknown")
    calc.summary["interim"] = in_progress
    if in_progress:
        calc.message = f"{calc.message} {IN_PROGRESS_MESSAGE}" if calc.message else IN_PROGRESS_MESSAGE
    return calc
