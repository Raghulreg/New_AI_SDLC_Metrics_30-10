from __future__ import annotations

from ..errors import Status
from ..models import Calc
from .boards import REMOVED, CategoryMap, category_of
from .common import display

UNSPECIFIED = "Unspecified"


def _row(wi: dict, categories: CategoryMap, severity_field: str | None = None) -> dict:
    f = wi.get("fields", {})
    row = {
        "id": wi.get("id"),
        "title": f.get("System.Title"),
        "state": f.get("System.State"),
        "stateCategory": category_of(wi, categories),
        "assignedTo": display(f.get("System.AssignedTo")),
        "createdAt": f.get("System.CreatedDate"),
    }
    if severity_field:
        row["severity"] = f.get(severity_field) or UNSPECIFIED
    return row


def eligible_bugs(bugs: list[dict], categories: CategoryMap) -> list[dict]:
    return [b for b in bugs if category_of(b, categories) != REMOVED]


def defect_count(bugs: list[dict], categories: CategoryMap, severity_field: str) -> Calc:
    eligible = eligible_bugs(bugs, categories)
    if not eligible:
        return Calc(Status.EMPTY, 0, sample_size=0, message="No bugs created in range")
    return Calc(Status.OK, len(eligible), details=[_row(b, categories, severity_field) for b in eligible],
                sample_size=len(eligible))


def defect_rate(defects: int, denominator: int, denominator_name: str) -> Calc:
    summary = {"defects": defects, "denominator": denominator, "denominatorDefinition": denominator_name}
    if denominator == 0:
        return Calc(Status.EMPTY, None, summary=summary, sample_size=0,
                    message=f"Denominator ({denominator_name}) is zero; rate is undefined")
    return Calc(Status.OK, round(defects / denominator, 4), summary=summary, sample_size=denominator)
