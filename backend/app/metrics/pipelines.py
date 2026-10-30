from __future__ import annotations

from ..errors import Status
from ..models import Calc


def coverage_stats(response: dict, label: str) -> tuple[int, int] | None:
    covered = total = 0
    found = False
    for module in response.get("coverageData") or []:
        for stat in module.get("coverageStats") or []:
            if str(stat.get("label", "")).lower() == label.lower():
                found = True
                covered += int(stat.get("covered") or 0)
                total += int(stat.get("total") or 0)
    return (covered, total) if found else None


def available_labels(response: dict) -> list[str]:
    return sorted({s.get("label") for m in response.get("coverageData") or [] for s in m.get("coverageStats") or []
                   if s.get("label")})


def test_coverage(build: dict | None, response: dict | None, label: str) -> Calc:
    if build is None:
        return Calc(Status.EMPTY, None, sample_size=0,
                    message="No successful build of the configured coverage pipeline(s) in range")
    meta = {"buildId": build.get("id"), "buildNumber": build.get("buildNumber"),
            "definition": (build.get("definition") or {}).get("name"), "finishTime": build.get("finishTime"),
            "statLabel": label}
    stats = coverage_stats(response or {}, label)
    if stats is None:
        labels = available_labels(response or {})
        msg = (f"Build {build.get('id')} published no coverage" if not labels
               else f"Coverage statistic '{label}' not published (available: {', '.join(labels)})")
        return Calc(Status.UNSUPPORTED, None, summary=meta, sample_size=0, message=msg)
    covered, total = stats
    meta.update({"covered": covered, "total": total})
    if total == 0:
        return Calc(Status.EMPTY, None, summary=meta, sample_size=0, message="Coverage total is zero; undefined")
    return Calc(Status.OK, round(covered / total * 100, 2), summary=meta, sample_size=total)
