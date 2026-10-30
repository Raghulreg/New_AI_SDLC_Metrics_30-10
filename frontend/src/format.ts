import type { MetricResult, Status } from "./types";

export const STATUS_LABEL: Record<Status, string> = {
  ok: "Live data",
  empty: "No eligible records",
  error: "Source error",
  auth_error: "Authentication failed",
  permission_denied: "Permission denied",
  unsupported: "Unsupported / unavailable",
  not_configured: "Not configured",
  attribution_unavailable: "Attribution unavailable",
};

const FAILURE: Status[] = ["error", "auth_error", "permission_denied", "unsupported", "not_configured", "attribution_unavailable"];

function summaryStatus(summary: Record<string, unknown>, key: string): string | null {
  const block = summary[key];
  if (!block || typeof block !== "object") return null;
  const status = (block as { status?: unknown }).status;
  return typeof status === "string" ? status : null;
}

/** Badge text. Coverage setup and Cursor AI access use their own wording; other statuses stay shared. */
export function statusLabel(m: Pick<MetricResult, "id" | "status" | "summary">): string {
  if (m.id === "test_coverage" && m.status === "not_configured") return "Setup required";
  const cursor = summaryStatus(m.summary, "cursorAttribution");
  if (m.id === "ai_generated_pct" && (m.status === "attribution_unavailable" || cursor === "attribution_unavailable")) {
    return "Access unavailable";
  }
  return STATUS_LABEL[m.status];
}

export function isFailure(status: Status): boolean {
  return FAILURE.includes(status);
}

/** Dollar display rounded to the nearest cent. The stored cost is not changed. */
export function formatUsdAmount(value: number): string {
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Actual Cursor cost for display. Missing cost is Unavailable, never $0.00. */
export function cursorCostAmount(m: Pick<MetricResult, "id" | "status" | "summary">): string | null {
  if (m.id !== "cursor_token_usage") return null;
  if (m.status === "empty" || (isFailure(m.status) && m.status !== "unsupported")) return null;
  const cost = m.summary.costUsd;
  if (typeof cost !== "number") return "Unavailable";
  return formatUsdAmount(cost);
}

/** A readable label derived from the Cursor account email. Digits stay as the email itself. */
export function memberLabel(email: string): { name: string; email: string } {
  const trimmed = email.trim();
  const local = trimmed.split("@")[0] ?? trimmed;
  const words = local.split(/[._-]+/).filter(Boolean);
  const nameLike = words.length > 0 && words.every((word) => /^[a-z]{2,}$/i.test(word));
  const name = nameLike
    ? words.map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()).join(" ")
    : trimmed;
  return { name, email: trimmed };
}

export function formatDayLabel(iso: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!match) return iso;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

export function formatTokens(value: number): string {
  return value.toLocaleString("en-US");
}

/** Official overall total, shown in the member table only when the rows partition that total. */
export function cursorMemberTotal(
  rows: Record<string, unknown>[],
  summary: Record<string, unknown>,
  totalCount: number,
): { tokens: number; events: number; costUsd: number | null } | null {
  if (rows.length === 0 || rows.length !== totalCount) return null;
  let tokens = 0;
  let events = 0;
  let cost = 0;
  let sawCost = false;
  for (const row of rows) {
    if (typeof row.totalTokens !== "number" || typeof row.events !== "number") return null;
    tokens += row.totalTokens;
    events += row.events;
    if (typeof row.costUsd === "number") {
      cost += row.costUsd;
      sawCost = true;
    } else if (row.costUsd != null) return null;
  }
  if (summary.totalTokens !== tokens || summary.events !== events) return null;
  const summaryCost = summary.costUsd;
  if (summaryCost == null) return sawCost ? null : { tokens, events, costUsd: null };
  if (typeof summaryCost !== "number" || Math.abs(cost - summaryCost) > 0.005) return null;
  return { tokens, events, costUsd: summaryCost };
}

export function cursorCostNote(m: Pick<MetricResult, "id" | "summary">): string | null {
  if (m.id !== "cursor_token_usage") return null;
  const missing = m.summary.eventsWithoutCost;
  if (typeof missing !== "number" || missing <= 0) return null;
  const records = missing === 1 ? "1 usage record has" : `${missing} usage records have`;
  return `${records} no cost from Cursor and ${missing === 1 ? "is" : "are"} not counted as $0.00.`;
}

export function displayValue(m: MetricResult): string {
  if (isFailure(m.status)) return "Unavailable";
  if (m.value === null || m.value === undefined) return "—";
  const v = m.value;
  if (m.unit.startsWith("%")) return `${v.toFixed(1)}%`;
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function isStale(fetchedAt: string | null, windowMinutes: number, now: Date = new Date()): boolean {
  if (!fetchedAt) return false;
  return now.getTime() - new Date(fetchedAt).getTime() > windowMinutes * 60_000;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  return new Date(iso).toLocaleString();
}

export function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** ADO timeFrame "current" is the sprint that is still running. */
export function sprintTimeFrameLabel(timeFrame?: string | null): string | null {
  if (!timeFrame) return null;
  return timeFrame === "current" ? "In Progress" : timeFrame;
}

export function unestimatedEffortNote(m: Pick<MetricResult, "id" | "summary">): string | null {
  const count = m.id === "sprint_velocity" ? m.summary.unestimatedCount : undefined;
  if (typeof count !== "number" || count <= 0) return null;
  return `${count} completed ${count === 1 ? "item" : "items"} without story points, listed separately`;
}

function pct(v: unknown): string {
  return typeof v === "number" ? `${v.toFixed(1)}%` : "—";
}

/** AI vs manual breakdown lines. Verified (Cursor) and estimated (LLM) shares are always shown apart. */
export function aiBreakdown(m: Pick<MetricResult, "id" | "summary">): string[] {
  if (m.id !== "ai_generated_pct") return [];
  const s = m.summary;
  const lines = (s.lines ?? {}) as Record<string, number>;
  const out = [
    `Cursor verified AI: ${pct(s.verifiedAiPct)} · LLM-estimated AI: ${pct(s.estimatedAiGeneratedPct)}`,
    `AI-assisted (estimate): ${pct(s.aiAssistedPct)} · Manual: ${pct(s.manualPct)}`,
  ];
  if (typeof lines.unknown === "number" && lines.unknown > 0) {
    out.push(`${lines.unknown.toLocaleString()} unknown / unclassified lines excluded from the percentages`);
  }
  return out;
}

export function includesEstimate(m: Pick<MetricResult, "id" | "summary">): boolean {
  return m.id === "ai_generated_pct" && typeof m.summary.estimatedLines === "number" && m.summary.estimatedLines > 0;
}

export interface CodeAnalysisRow {
  key: string;
  label: string;
  linesLabel: string;
  percentageLabel: string;
  shareOf: string;
  details: string[];
}

export interface CodeAnalysisView {
  /** unavailable: a source failed and nothing was classified. no_records: the range has no measured lines. */
  state: "unavailable" | "no_records" | "measured";
  rows: CodeAnalysisRow[];
  explanation: string | null;
  notes: string[];
}

function count(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function sourceStatus(summary: Record<string, unknown>, key: string): string | null {
  return summaryStatus(summary, key);
}

function sourceMessage(summary: Record<string, unknown>, key: string): string {
  const block = summary[key];
  if (!block || typeof block !== "object") return "";
  const message = (block as { message?: unknown }).message;
  return typeof message === "string" ? message : "";
}

const CLASSES: [string, string][] = [
  ["ai", "AI-generated"],
  ["assisted", "AI-assisted"],
  ["manual", "Manually written"],
  ["unknown", "Unknown / unclassified"],
];

function blankRows(linesLabel: string, percentageLabel: string): CodeAnalysisRow[] {
  return CLASSES.map(([key, label]) => ({ key, label, linesLabel, percentageLabel, shareOf: "", details: [] }));
}

/**
 * Presentation of the existing AI Generated % summary. Percentages are the API's own figures:
 * AI-generated, AI-assisted and manual are shares of classified lines; unknown is a share of
 * measured lines. This does not recompute the metric.
 */
export function codeAnalysisView(m: Pick<MetricResult, "status" | "summary" | "message">): CodeAnalysisView {
  const lines = (m.summary.lines ?? {}) as Record<string, unknown>;
  const verified = count(lines.verifiedAi);
  const estimatedAi = count(lines.estimatedAiGenerated);
  const assisted = count(lines.estimatedAiAssisted);
  const cursorManual = count(lines.cursorNonAi);
  const estimatedManual = count(lines.estimatedManual);
  const unknown = count(lines.unknown);
  const excluded = count(lines.excluded);
  const commits = (m.summary.commits ?? {}) as Record<string, unknown>;
  const measured = verified + estimatedAi + assisted + cursorManual + estimatedManual + unknown;
  const cursorOk = sourceStatus(m.summary, "cursorAttribution") === "ok";
  const llmStatus = sourceStatus(m.summary, "llmAnalysis");
  const llm = (m.summary.llmAnalysis ?? {}) as Record<string, unknown>;
  const llmConfigured = llmStatus !== null && llmStatus !== "not_configured";
  const llmOk = llmStatus === "ok";

  const notes: string[] = [];
  const cursorStatus = sourceStatus(m.summary, "cursorAttribution");
  const reported = sourceMessage(m.summary, "cursorAttribution");
  if (cursorStatus === "attribution_unavailable") {
    notes.push(["Cursor AI Code Attribution — Access unavailable.", reported,
      "The API key is not reported as invalid, and no percentage is shown. Cursor Token Usage is unaffected."]
      .filter(Boolean).join(" "));
  } else if (cursorStatus === "auth_error") {
    notes.push(["Cursor AI Code Attribution — Authentication failed. The credentials were rejected (HTTP 401).",
      reported, "Cursor Token Usage is unaffected."].filter(Boolean).join(" "));
  } else if (cursorStatus === "not_configured") {
    notes.push("Cursor AI Code Attribution — Not configured. No Cursor API key is set on the backend.");
  } else if (!cursorOk && cursorStatus) {
    notes.push("Cursor AI Code Attribution is unavailable for this request.");
  }
  if (!llmConfigured) {
    notes.push("LLM Code Analysis — Configuration required. Set LLM_PROVIDER, LLM_MODEL, LLM_ENDPOINT and LLM_API_KEY, then LLM_CODE_ANALYSIS_ENABLED=true. Until then, commits without Cursor attribution stay unmeasured and are not counted as manual.");
  } else if (!llmOk) {
    notes.push("LLM Code Analysis did not succeed, so no estimate is shown.");
  } else if (typeof llm.minConfidence === "number") {
    const estimatedLines = estimatedAi + assisted + estimatedManual;
    notes.push(`LLM estimates: ${estimatedLines.toLocaleString()} analyzed added lines; `
      + `only hunk labels at or above ${(llm.minConfidence * 100).toFixed(0)}% confidence are used. `
      + "Each accepted provider response includes a brief hunk rationale; estimates are not proof of authorship.");
  }
  if (!isFailure(m.status) && excluded > 0) {
    notes.push(`${excluded.toLocaleString()} lines in excluded paths (lock and minified files) were left out of every class.`);
  }
  if (!isFailure(m.status) && count(commits.unmeasured) > 0) {
    notes.push(`${count(commits.unmeasured).toLocaleString()} commit(s) have no attribution evidence. Their lines are not measured and are not counted as manual.`);
  }
  if (!isFailure(m.status) && count(commits.duplicatesAcrossPrs) > 0) {
    notes.push(`${count(commits.duplicatesAcrossPrs).toLocaleString()} commit(s) appeared in more than one pull request and were counted once, in the earliest completed pull request.`);
  }

  if (isFailure(m.status)) {
    return { state: "unavailable", rows: blankRows("Unavailable", "Unavailable"), explanation: m.message, notes };
  }
  if (measured === 0) {
    return { state: "no_records", rows: blankRows("—", "—"), explanation: m.message, notes };
  }

  const llmDetail = (n: number) => {
    if (!llmConfigured) return "LLM estimate: not enabled";
    if (!llmOk && n === 0) return "LLM estimate: unavailable";
    return `LLM estimate: ${n.toLocaleString()} lines`;
  };
  const row = (
    key: string, label: string, lineCount: number, percentage: unknown, shareOf: string, details: string[],
  ): CodeAnalysisRow => ({
    key, label, linesLabel: `${lineCount.toLocaleString()} lines`, percentageLabel: pct(percentage), shareOf, details,
  });
  return {
    state: "measured",
    explanation: m.message,
    notes,
    rows: [
      row("ai", "AI-generated", verified + estimatedAi, m.summary.aiGeneratedPct, "of classified lines", [
        cursorOk ? `Cursor verified: ${verified.toLocaleString()} lines` : "Cursor verified: unavailable",
        llmDetail(estimatedAi),
      ]),
      row("assisted", "AI-assisted", assisted, m.summary.aiAssistedPct, "of classified lines", [
        "Cursor does not report an AI-assisted class.",
        llmDetail(assisted),
      ]),
      row("manual", "Manually written", cursorManual + estimatedManual, m.summary.manualPct, "of classified lines", [
        cursorOk ? `Cursor-derived non-AI: ${cursorManual.toLocaleString()} lines` : "Cursor-derived non-AI: unavailable",
        llmDetail(estimatedManual),
      ]),
      row("unknown", "Unknown / unclassified", unknown, m.summary.unknownPctOfMeasured, "of measured lines", [
        "Left out of AI Generated %. Not counted as manual.",
      ]),
    ],
  };
}

/**
 * Individual scoping follows Project → Team → Individual: the backend resolves a person from the
 * selected team's members, so a team is required before an individual can be chosen.
 */
export function individualFilterState(
  team: string, members: { id: string }[] | null, error: string | null,
): { enabled: boolean; placeholder: string; hint: string | null } {
  if (!team) {
    return { enabled: false, placeholder: "Select a team first", hint: "Choose a team to filter by an individual member." };
  }
  if (error) return { enabled: false, placeholder: "Members unavailable", hint: null };
  if (members === null) return { enabled: false, placeholder: "Loading members…", hint: null };
  if (members.length === 0) return { enabled: false, placeholder: "No members in this team", hint: null };
  return { enabled: true, placeholder: "All team members", hint: null };
}

export type Preset = "today" | "yesterday" | "last7" | "last30";

export const PRESET_LABEL: Record<Preset, string> = {
  today: "Today", yesterday: "Yesterday", last7: "Last 7 days", last30: "Last 30 days",
};

/** Daily reporting ranges in UTC, matching the backend's UTC day boundaries. */
export function presetRange(preset: Preset, now: Date = new Date()): { from: string; to: string } {
  const day = (offset: number) => isoDate(new Date(now.getTime() - offset * 86_400_000));
  switch (preset) {
    case "today": return { from: day(0), to: day(0) };
    case "yesterday": return { from: day(1), to: day(1) };
    case "last7": return { from: day(6), to: day(0) };
    case "last30": return { from: day(29), to: day(0) };
  }
}

export function reportingPeriodLabel(from: string, to: string): string {
  const a = from.slice(0, 10);
  const b = to.slice(0, 10);
  return a === b ? `${a} (UTC day)` : `${a} → ${b} (UTC)`;
}
