from __future__ import annotations

import json
import re

from .code_diff import Hunk

PROMPT_VERSION = "2026-10-23.2"

AI_GENERATED = "ai_generated"
AI_ASSISTED = "ai_assisted"
MANUAL = "manual"
UNKNOWN = "unknown"
LABELS = (AI_GENERATED, AI_ASSISTED, MANUAL, UNKNOWN)

SYSTEM_PROMPT = (
    "You review lines added by a single git commit and estimate, per hunk, how the code was most likely produced.\n"
    "Labels:\n"
    "- ai_generated: most likely produced as a whole by an AI coding assistant and accepted with little change.\n"
    "- ai_assisted: human-directed code that most likely includes AI completions or AI edits.\n"
    "- manual: most likely typed by a developer without AI generation.\n"
    "- unknown: the evidence is insufficient (short, trivial, tool-generated, configuration, formatting-only, "
    "or ambiguous).\n"
    "Authorship cannot be determined with certainty from code alone. Do not infer authorship from code quality, "
    "style or correctness by themselves. Prefer 'unknown' whenever you are not confident.\n"
    'Respond with JSON only: {"hunks": [{"id": "<hunk id>", "label": "<label>", "confidence": <0..1>, '
    '"rationale": "<brief evidence from this hunk, max 280 chars>"}]} '
    "with exactly one entry for every hunk id you were given."
)

_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd|pwd|connection[_-]?string)\s*[:=]\s*)(\S+)"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b"),
]


def redact(line: str) -> str:
    """Mask credential-like values before code text leaves the backend."""
    out = _SECRET_PATTERNS[0].sub(lambda m: m.group(1) + "[REDACTED]", line)
    return _SECRET_PATTERNS[1].sub("[REDACTED]", out)


def build_messages(commit_message: str, hunks: list[Hunk]) -> list[dict[str, str]]:
    parts = [f"Commit message: {redact((commit_message or '').strip()[:500])}", ""]
    for h in hunks:
        parts.append(f"### hunk {h.id} — file {h.path} (from line {h.start_line}, {h.size} added lines)")
        parts.extend("+" + redact(line) for line in h.lines)
        parts.append("")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n".join(parts)}]


def parse_labels(content: str, hunk_ids: list[str], min_confidence: float) -> tuple[dict[str, str], bool]:
    """Map hunk id -> label. Missing, malformed or low-confidence entries become 'unknown'.

    Returns (labels, invalid_response). Line counts never come from the model; they come from the diff.
    """
    labels = {hid: UNKNOWN for hid in hunk_ids}
    try:
        body = json.loads(content)
        entries = body["hunks"]
        if not isinstance(entries, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return labels, True
    wanted = set(hunk_ids)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hid, label, rationale = entry.get("id"), entry.get("label"), entry.get("rationale")
        try:
            confidence = float(entry.get("confidence"))
        except (TypeError, ValueError):
            continue
        if (hid in wanted and label in LABELS and confidence >= min_confidence
                and isinstance(rationale, str) and 0 < len(rationale.strip()) <= 280):
            labels[hid] = label
    return labels, False
