from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone

_FRACTION = re.compile(r"(\.\d+)")


def parse_dt(value: str | None) -> datetime | None:
    """Parse ADO/Cursor ISO timestamps (ADO may emit 7 fractional digits)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _FRACTION.sub(lambda m: m.group(1)[:7], text, count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # ADO uses 0001-01-01 / 9999-01-01 as sentinel "no date" values.
    if dt.year in (1, 9999):
        return None
    return dt


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "mean": None, "min": None, "max": None}
    return {
        "median": round(statistics.median(values), 2),
        "mean": round(statistics.fmean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def identity_key(identity: dict | None) -> tuple[str, str]:
    if not identity or not isinstance(identity, dict):
        return ("", "")
    return (str(identity.get("id") or "").lower(), str(identity.get("uniqueName") or "").lower())


def identity_matches(identity: dict | None, person: dict) -> bool:
    ident_id, ident_name = identity_key(identity)
    pid = str(person.get("id") or "").lower()
    pname = str(person.get("uniqueName") or "").lower()
    return bool((pid and ident_id == pid) or (pname and ident_name == pname))


def display(identity: dict | None) -> str | None:
    if not identity or not isinstance(identity, dict):
        return None
    return identity.get("displayName") or identity.get("uniqueName")
