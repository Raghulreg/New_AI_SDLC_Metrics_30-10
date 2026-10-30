from __future__ import annotations

import difflib
import fnmatch
from dataclasses import dataclass


@dataclass(frozen=True)
class Hunk:
    """A contiguous block of lines added (or replaced) by one commit in one file."""

    id: str
    path: str
    start_line: int
    lines: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.lines)


def added_hunks(old_text: str, new_text: str, path: str, id_prefix: str = "h") -> list[Hunk]:
    """Added lines of `new_text` relative to `old_text`. Deleted lines are not counted."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[Hunk] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace") and j2 > j1:
            hunks.append(Hunk(f"{id_prefix}{len(hunks) + 1}", path, j1 + 1, tuple(new_lines[j1:j2])))
    return hunks


def is_excluded(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    full = path.lower()
    return any(fnmatch.fnmatch(name, p.lower()) or fnmatch.fnmatch(full, p.lower()) for p in patterns)


def split_by_line_budget(hunks: list[Hunk], max_lines: int) -> tuple[list[Hunk], list[Hunk]]:
    """Hunks that fit in the analysis budget, in order, and the remainder (whole hunks only)."""
    sent: list[Hunk] = []
    used = 0
    for i, h in enumerate(hunks):
        if used + h.size > max_lines:
            return sent, hunks[i:]
        sent.append(h)
        used += h.size
    return sent, []
