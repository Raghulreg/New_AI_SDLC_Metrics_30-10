from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from ..errors import Status
from ..models import Calc

TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheWriteTokens", "cacheReadTokens")


def _email(value: object) -> str:
    return str(value or "").strip().casefold()


def _charged_cents(value: object) -> float | None:
    """A real chargedCents value. Missing and non-numeric fields are unknown, not zero."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _event_key(e: dict) -> tuple:
    tu = e.get("tokenUsage") or {}
    return (e.get("timestamp"), _email(e.get("userEmail")), e.get("model"), e.get("conversationId"),
            e.get("requestsCosts"), e.get("chargedCents"), *(tu.get(f) for f in TOKEN_FIELDS))


def token_usage(events: list[dict], emails: set[str] | None = None) -> Calc:
    """Sum real token counts and chargedCents. Neither is estimated when the field is absent."""
    unique: dict[tuple, dict] = {}
    for e in events:
        if emails is not None and _email(e.get("userEmail")) not in emails:
            continue
        unique.setdefault(_event_key(e), e)

    totals = dict.fromkeys(TOKEN_FIELDS, 0)
    by_user: dict[str, dict] = defaultdict(lambda: {
        **dict.fromkeys(TOKEN_FIELDS, 0), "totalTokens": 0, "events": 0,
        "costCents": 0.0, "costEvents": 0, "eventsWithoutCost": 0,
    })
    by_day: dict[str, int] = defaultdict(int)
    by_model: dict[str, int] = defaultdict(int)
    without_tokens = 0
    charged_cents = 0.0
    cost_events = 0
    without_cost = 0
    for e in unique.values():
        cents = _charged_cents(e.get("chargedCents"))
        tu = e.get("tokenUsage")
        user = _email(e.get("userEmail")) or "unknown"
        by_user[user]["events"] += 1
        if cents is None:
            without_cost += 1
            by_user[user]["eventsWithoutCost"] += 1
        else:
            charged_cents += cents
            cost_events += 1
            by_user[user]["costCents"] += cents
            by_user[user]["costEvents"] += 1
        if not tu:
            without_tokens += 1
            continue
        event_total = 0
        for f in TOKEN_FIELDS:
            n = int(tu.get(f) or 0)
            totals[f] += n
            by_user[user][f] += n
            event_total += n
        by_user[user]["totalTokens"] += event_total
        by_model[str(e.get("model") or "unknown")] += event_total
        try:
            day = datetime.fromtimestamp(int(e["timestamp"]) / 1000, tz=timezone.utc).date().isoformat()
            by_day[day] += event_total
        except (KeyError, TypeError, ValueError):
            pass

    total = sum(totals.values())
    summary = {
        **totals,
        "totalTokens": total,
        "events": len(unique),
        "eventsWithoutTokenData": without_tokens,
        "eventsWithoutCost": without_cost,
        "chargedCents": None if cost_events == 0 else round(charged_cents, 6),
        "costUsd": None if cost_events == 0 else round(charged_cents / 100, 6),
        "byDay": dict(sorted(by_day.items())),
        "byModel": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
    }
    rows = []
    for user, values in sorted(by_user.items(), key=lambda kv: -kv[1]["totalTokens"]):
        known = values.pop("costEvents")
        cents = values.pop("costCents")
        values["chargedCents"] = None if known == 0 else round(cents, 6)
        values["costUsd"] = None if known == 0 else round(cents / 100, 6)
        rows.append({"userEmail": user, **values})
    cost_note = None
    if unique and without_cost == len(unique):
        cost_note = "No usage event has chargedCents; cost is unavailable and is not reported as zero"
    elif without_cost:
        cost_note = f"{without_cost} event(s) have no chargedCents and are excluded from the cost"
    if not unique:
        return Calc(Status.EMPTY, 0, summary=summary, sample_size=0, message="No Cursor usage events in range")
    if without_tokens == len(unique):
        message = ("Usage events exist but none are token-based (request-based billing); "
                   "token counts are not available from Cursor and are not estimated")
        if cost_note:
            message = f"{message}. {cost_note}"
        return Calc(Status.UNSUPPORTED, None, summary=summary, details=rows, sample_size=len(unique), message=message)
    token_note = (f"{without_tokens} request-based event(s) carry no token data and are not estimated"
                  if without_tokens else None)
    message = " ".join(note for note in (token_note, cost_note) if note) or None
    return Calc(Status.OK, total, summary=summary, details=rows, sample_size=len(unique), message=message)
