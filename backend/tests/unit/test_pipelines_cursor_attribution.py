from app.errors import Status
from app.metrics import cursor_usage, pipelines


def test_coverage_requires_published_label():
    b = {"id": 9, "buildNumber": "1", "definition": {"name": "ci"}}
    resp = {"coverageData": [{"coverageStats": [{"label": "Lines", "covered": 80, "total": 100},
                                                {"label": "Branches", "covered": 1, "total": 2}]},
                             {"coverageStats": [{"label": "Lines", "covered": 20, "total": 100}]}]}
    assert pipelines.test_coverage(b, resp, "Lines").value == 50.0
    missing = pipelines.test_coverage(b, {"coverageData": []}, "Lines")
    assert missing.status == Status.UNSUPPORTED and missing.value is None


def test_coverage_without_build_is_empty_not_zero():
    calc = pipelines.test_coverage(None, None, "Lines")
    assert calc.status == Status.EMPTY and calc.value is None


def _event(ts, email, tokens=None):
    e = {"timestamp": str(ts), "userEmail": email, "model": "m", "chargedCents": 1.0}
    if tokens is not None:
        e["isTokenBasedCall"] = True
        e["tokenUsage"] = {"inputTokens": tokens, "outputTokens": 1, "cacheWriteTokens": 0, "cacheReadTokens": 0}
    else:
        e["isTokenBasedCall"] = False
    return e


def test_token_usage_sums_real_tokens_and_does_not_estimate():
    events = [_event(1759300000000, "a@x.com", 10), _event(1759300000000, "a@x.com", 10),  # duplicate
              _event(1759300001000, "b@x.com", 5), _event(1759300002000, "a@x.com")]
    calc = cursor_usage.token_usage(events)
    assert calc.value == 17
    assert calc.summary["eventsWithoutTokenData"] == 1
    assert calc.summary["costUsd"] == 0.03  # three unique events at 1 cent; the duplicate is not added again
    assert cursor_usage.token_usage(events, {"b@x.com"}).value == 6
    assert cursor_usage.token_usage(events, {"b@x.com"}).summary["costUsd"] == 0.01


def test_token_usage_request_based_only_is_unsupported_not_zero():
    calc = cursor_usage.token_usage([_event(1759300000000, "a@x.com")])
    assert calc.status == Status.UNSUPPORTED and calc.value is None
    assert calc.summary["costUsd"] == 0.01


def test_token_usage_cost_is_charged_cents_divided_by_100():
    events = [
        _event(1759300000000, "a@x.com", 10),
        _event(1759300001000, "a@x.com", 4),
    ]
    events[0]["chargedCents"] = 150
    events[1]["chargedCents"] = 25.5
    calc = cursor_usage.token_usage(events)
    assert calc.value == 16
    assert calc.summary["chargedCents"] == 175.5
    assert calc.summary["costUsd"] == 1.755
    assert calc.summary["eventsWithoutCost"] == 0
    assert calc.details[0]["costUsd"] == 1.755


def test_missing_charged_cents_is_excluded_and_not_zero():
    priced = _event(1759300000000, "a@x.com", 3)
    priced["chargedCents"] = 0
    missing = _event(1759300001000, "a@x.com", 4)
    del missing["chargedCents"]
    blank = _event(1759300002000, "b@x.com", 5)
    blank["chargedCents"] = None
    invalid = _event(1759300003000, "b@x.com", 6)
    invalid["chargedCents"] = "not-a-cost"
    calc = cursor_usage.token_usage([priced, missing, blank, invalid])
    assert calc.summary["costUsd"] == 0
    assert calc.summary["chargedCents"] == 0
    assert calc.summary["eventsWithoutCost"] == 3
    assert "not reported as zero" not in (calc.message or "")
    assert "3 event(s) have no chargedCents" in calc.message
    none = cursor_usage.token_usage([missing, blank])
    assert none.summary["costUsd"] is None
    assert none.summary["chargedCents"] is None
    assert none.summary["eventsWithoutCost"] == 2
    assert none.details[0]["costUsd"] is None


def test_duplicate_usage_events_do_not_double_count_cost():
    event = _event(1759300000000, "A@x.com", 8)
    event["chargedCents"] = 250
    repeated = dict(event)
    calc = cursor_usage.token_usage([event, repeated, repeated])
    assert calc.summary["events"] == 1
    assert calc.summary["totalTokens"] == 9
    assert calc.summary["costUsd"] == 2.5
    assert calc.summary["eventsWithoutCost"] == 0
