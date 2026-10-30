import { describe, expect, it } from "vitest";
import {
  aiBreakdown, codeAnalysisView, cursorCostAmount, cursorCostNote, cursorMemberTotal, displayValue, formatDayLabel, formatTokens, formatUsdAmount, includesEstimate, individualFilterState, isStale, memberLabel, presetRange,
  reportingPeriodLabel, statusLabel,
  sprintTimeFrameLabel, unestimatedEffortNote,
} from "./format";
import type { MetricResult } from "./types";

const base: MetricResult = {
  id: "x", name: "X", status: "ok", value: 0, unit: "PRs", summary: {}, details: [], sampleSize: 0,
  message: null, definition: "", sources: [], scopeLevel: "project", timeRange: null, fetchedAt: null,
  lastSuccessfulFetch: null, stale: false,
};

describe("displayValue", () => {
  it("never shows zero for failure states", () => {
    for (const status of ["error", "auth_error", "permission_denied", "unsupported", "not_configured", "attribution_unavailable"] as const) {
      expect(displayValue({ ...base, status, value: null })).toBe("Unavailable");
      expect(displayValue({ ...base, status, value: 0 })).toBe("Unavailable");
    }
  });
  it("shows a valid empty count as 0 and an undefined rate as a dash", () => {
    expect(displayValue({ ...base, status: "empty", value: 0 })).toBe("0");
    expect(displayValue({ ...base, status: "empty", value: null, unit: "%" })).toBe("—");
  });
  it("formats percentages", () => {
    expect(displayValue({ ...base, value: 87.456, unit: "%" })).toBe("87.5%");
  });
});

describe("sprint labels", () => {
  it("names the active sprint In Progress and leaves a finished sprint unchanged", () => {
    expect(sprintTimeFrameLabel("current")).toBe("In Progress");
    expect(sprintTimeFrameLabel("past")).toBe("past");
    expect(sprintTimeFrameLabel(undefined)).toBeNull();
  });
  it("shows missing story points separately from the point total", () => {
    expect(unestimatedEffortNote({ id: "sprint_velocity", summary: { unestimatedCount: 2 } }))
      .toBe("2 completed items without story points, listed separately");
    expect(unestimatedEffortNote({ id: "sprint_velocity", summary: { unestimatedCount: 1 } }))
      .toBe("1 completed item without story points, listed separately");
    expect(unestimatedEffortNote({ id: "sprint_velocity", summary: { unestimatedCount: 0 } })).toBeNull();
    expect(unestimatedEffortNote({ id: "pr_cycle_time", summary: {} })).toBeNull();
  });
});

describe("AI vs manual breakdown", () => {
  const ai = {
    id: "ai_generated_pct",
    summary: {
      verifiedAiPct: 25, estimatedAiGeneratedPct: 25, aiAssistedPct: 15, manualPct: 35,
      lines: { unknown: 10 }, estimatedLines: 20,
    },
  };
  it("keeps Cursor-verified and LLM-estimated shares apart and reports unknown lines", () => {
    expect(aiBreakdown(ai)).toEqual([
      "Cursor verified AI: 25.0% · LLM-estimated AI: 25.0%",
      "AI-assisted (estimate): 15.0% · Manual: 35.0%",
      "10 unknown / unclassified lines excluded from the percentages",
    ]);
    expect(includesEstimate(ai)).toBe(true);
  });
  it("flags no estimate when only Cursor-verified lines exist", () => {
    expect(includesEstimate({ id: "ai_generated_pct", summary: { estimatedLines: 0 } })).toBe(false);
    expect(aiBreakdown({ id: "pr_cycle_time", summary: {} })).toEqual([]);
  });
  it("formats the unit-suffixed percentage", () => {
    expect(displayValue({ ...base, value: 76.923, unit: "% of classified lines" })).toBe("76.9%");
  });
});

describe("AI vs Manual section", () => {
  const lines = {
    verifiedAi: 10, estimatedAiGenerated: 10, estimatedAiAssisted: 6,
    cursorNonAi: 10, estimatedManual: 4, unknown: 10, excluded: 50,
  };
  it("shows lines and the API percentages, and keeps unknown out of manual", () => {
    const view = codeAnalysisView({
      status: "ok",
      message: "20 line(s) are LLM estimates, not verified measurements.",
      summary: {
        lines,
        aiGeneratedPct: 50, aiAssistedPct: 15, manualPct: 35, unknownPctOfMeasured: 20,
        cursorAttribution: { status: "ok" },
        llmAnalysis: { status: "ok", minConfidence: 0.7 },
        commits: { unmeasured: 0, duplicatesAcrossPrs: 1 },
      },
    });
    expect(view.state).toBe("measured");
    const byKey = Object.fromEntries(view.rows.map((row) => [row.key, row]));
    expect(byKey.ai).toMatchObject({ linesLabel: "20 lines", percentageLabel: "50.0%" });
    expect(byKey.ai.details).toEqual(["Cursor verified: 10 lines", "LLM estimate: 10 lines"]);
    expect(byKey.assisted).toMatchObject({ linesLabel: "6 lines", percentageLabel: "15.0%" });
    expect(byKey.manual).toMatchObject({ linesLabel: "14 lines", percentageLabel: "35.0%" });
    expect(byKey.manual.linesLabel).not.toContain("24");
    expect(byKey.unknown).toMatchObject({ linesLabel: "10 lines", percentageLabel: "20.0%", shareOf: "of measured lines" });
    expect(view.notes.some((note) => note.includes("counted once"))).toBe(true);
    expect(view.notes.some((note) => note.includes("50 lines"))).toBe(true);
    expect(view.notes.some((note) => note.includes("70% confidence"))).toBe(true);
  });

  it("shows unavailable instead of zeros when Cursor returns 401 and the LLM is off", () => {
    const view = codeAnalysisView({
      status: "auth_error",
      message: "No classified lines: Cursor attribution failed and LLM code analysis is not enabled.",
      summary: {
        lines: { verifiedAi: 0, cursorNonAi: 0, unknown: 0 },
        aiGeneratedPct: null, manualPct: null,
        cursorAttribution: { status: "auth_error", message: "HTTP 401" },
        llmAnalysis: { status: "not_configured" },
        commits: { unmeasured: 18 },
      },
    });
    expect(view.state).toBe("unavailable");
    expect(view.rows.map((row) => row.linesLabel)).toEqual(["Unavailable", "Unavailable", "Unavailable", "Unavailable"]);
    expect(view.rows.some((row) => row.percentageLabel === "0.0%" || row.linesLabel.startsWith("0"))).toBe(false);
    expect(view.explanation).toContain("LLM code analysis is not enabled");
    expect(view.notes.some((note) => note.includes("HTTP 401"))).toBe(true);
    expect(view.notes.some((note) => note.includes("not counted as manual"))).toBe(true);
  });

  it("labels a team-access refusal as Access unavailable and does not call the key invalid", () => {
    const view = codeAnalysisView({
      status: "attribution_unavailable",
      message: "No classified lines.",
      summary: {
        lines: {},
        cursorAttribution: {
          status: "attribution_unavailable",
          message: "You must be a member of an enterprise team to access this resource",
        },
        llmAnalysis: { status: "not_configured" },
      },
    });
    expect(view.state).toBe("unavailable");
    expect(view.notes[0]).toContain("Cursor AI Code Attribution — Access unavailable");
    expect(view.notes[0]).toContain("not reported as invalid");
    expect(view.notes[0]).toContain("member of an enterprise team");
    expect(view.notes[0]).not.toMatch(/purchase|buy/i);
    expect(view.notes[1]).toContain("LLM Code Analysis — Configuration required");
    expect(statusLabel({
      id: "ai_generated_pct", status: "attribution_unavailable",
      summary: { cursorAttribution: { status: "attribution_unavailable" } },
    })).toBe("Access unavailable");
  });

  it("shows dashes when the range has no completed pull requests", () => {
    const view = codeAnalysisView({
      status: "empty",
      message: "No completed pull requests in range",
      summary: { lines: {}, cursorAttribution: { status: "ok" }, llmAnalysis: { status: "not_configured" } },
    });
    expect(view.state).toBe("no_records");
    expect(view.rows.every((row) => row.linesLabel === "—" && row.percentageLabel === "—")).toBe(true);
  });

  it("reports an all-unknown result as unknown, with no manual percentage", () => {
    const view = codeAnalysisView({
      status: "empty",
      message: "No classified lines.",
      summary: {
        lines: { verifiedAi: 0, estimatedAiGenerated: 0, estimatedAiAssisted: 0, cursorNonAi: 0, estimatedManual: 0, unknown: 5 },
        aiGeneratedPct: null, aiAssistedPct: null, manualPct: null, unknownPctOfMeasured: 100,
        cursorAttribution: { status: "ok" },
        llmAnalysis: { status: "ok" },
      },
    });
    expect(view.state).toBe("measured");
    const manual = view.rows.find((row) => row.key === "manual");
    const unknown = view.rows.find((row) => row.key === "unknown");
    expect(manual?.percentageLabel).toBe("—");
    expect(unknown).toMatchObject({ linesLabel: "5 lines", percentageLabel: "100.0%" });
  });

  it("labels Cursor-only results so a missing LLM estimate is not shown as zero", () => {
    const view = codeAnalysisView({
      status: "ok",
      message: null,
      summary: {
        lines: { verifiedAi: 8, cursorNonAi: 2, estimatedAiGenerated: 0, estimatedAiAssisted: 0, estimatedManual: 0, unknown: 0 },
        aiGeneratedPct: 80, aiAssistedPct: 0, manualPct: 20, unknownPctOfMeasured: 0,
        cursorAttribution: { status: "ok" },
        llmAnalysis: { status: "not_configured" },
        commits: { unmeasured: 3 },
      },
    });
    const assisted = view.rows.find((row) => row.key === "assisted");
    expect(assisted?.details).toContain("LLM estimate: not enabled");
    expect(view.notes.some((note) => note.includes("3 commit(s)"))).toBe(true);
  });
});

describe("status labels", () => {
  it("uses Setup required for unconfigured coverage and leaves other badges unchanged", () => {
    expect(statusLabel({ id: "test_coverage", status: "not_configured", summary: {} })).toBe("Setup required");
    expect(statusLabel({ id: "test_coverage", status: "attribution_unavailable", summary: {} })).toBe("Attribution unavailable");
    expect(formatUsdAmount(70.789)).toBe("$70.79");
    expect(formatUsdAmount(70.089937)).toBe("$70.09");
    expect(formatTokens(506917072)).toBe("506,917,072");
    expect(formatDayLabel("2026-10-28")).toBe("Oct 28");
    expect(memberLabel("ashok@aigrev.com")).toEqual({ name: "Ashok", email: "ashok@aigrev.com" });
    expect(memberLabel("first.last@aigrev.com").name).toBe("First Last");
    expect(memberLabel("aigrev247@gmail.com").name).toBe("aigrev247@gmail.com");
    const rows = [
      { totalTokens: 10, events: 1, costUsd: 1.25 },
      { totalTokens: 5, events: 2, costUsd: 0.5 },
    ];
    expect(cursorMemberTotal(rows, { totalTokens: 15, events: 3, costUsd: 1.75 }, 2))
      .toEqual({ tokens: 15, events: 3, costUsd: 1.75 });
    expect(cursorMemberTotal(rows, { totalTokens: 99, events: 3, costUsd: 1.75 }, 2)).toBeNull();
    expect(cursorMemberTotal(rows.slice(0, 1), { totalTokens: 15, events: 3, costUsd: 1.75 }, 2)).toBeNull();
    expect(formatUsdAmount(1.5)).toBe("$1.50");
    expect(cursorCostAmount({ id: "cursor_token_usage", status: "ok", summary: { costUsd: 1.5, eventsWithoutCost: 0 } }))
      .toBe("$1.50");
    expect(cursorCostAmount({ id: "cursor_token_usage", status: "ok", summary: { costUsd: 70.089937 } }))
      .toBe("$70.09");
    expect(cursorCostAmount({ id: "cursor_token_usage", status: "ok", summary: { costUsd: 0.004 } }))
      .toBe("$0.00");
    expect(cursorCostNote({ id: "cursor_token_usage", summary: { eventsWithoutCost: 1 } }))
      .toBe("1 usage record has no cost from Cursor and is not counted as $0.00.");
    expect(cursorCostAmount({ id: "cursor_token_usage", status: "ok", summary: { costUsd: null, eventsWithoutCost: 2 } }))
      .toBe("Unavailable");
    expect(cursorCostAmount({ id: "cursor_token_usage", status: "empty", summary: { costUsd: null } })).toBeNull();
    expect(cursorCostAmount({ id: "cursor_token_usage", status: "auth_error", summary: { costUsd: 1 } })).toBeNull();
    expect(statusLabel({ id: "cursor_token_usage", status: "ok", summary: {} })).toBe("Live data");
    expect(statusLabel({ id: "cursor_token_usage", status: "auth_error", summary: {} })).toBe("Authentication failed");
    expect(statusLabel({
      id: "ai_generated_pct", status: "auth_error",
      summary: { cursorAttribution: { status: "auth_error" } },
    })).toBe("Authentication failed");
  });
});

describe("Individual filter", () => {
  it("asks for a team at project level instead of silently disabling", () => {
    expect(individualFilterState("", null, null)).toEqual({
      enabled: false, placeholder: "Select a team first", hint: "Choose a team to filter by an individual member.",
    });
  });
  it("enables once the selected team's members load", () => {
    expect(individualFilterState("Team", null, null)).toMatchObject({ enabled: false, placeholder: "Loading members…" });
    expect(individualFilterState("Team", [{ id: "u1" }], null)).toEqual({ enabled: true, placeholder: "All team members", hint: null });
    expect(individualFilterState("Team", [], null)).toMatchObject({ enabled: false, placeholder: "No members in this team" });
    expect(individualFilterState("Team", null, "permission denied")).toMatchObject({ enabled: false });
  });
});

describe("daily reporting ranges (UTC)", () => {
  const now = new Date("2026-10-23T01:30:00Z");
  it("computes presets on UTC day boundaries", () => {
    expect(presetRange("today", now)).toEqual({ from: "2026-10-23", to: "2026-10-23" });
    expect(presetRange("yesterday", now)).toEqual({ from: "2026-10-22", to: "2026-10-22" });
    expect(presetRange("last7", now)).toEqual({ from: "2026-10-17", to: "2026-10-23" });
    expect(presetRange("last30", now)).toEqual({ from: "2026-09-24", to: "2026-10-23" });
  });
  it("labels a single day as a reporting date", () => {
    expect(reportingPeriodLabel("2026-10-23T00:00:00Z", "2026-10-23T23:59:59Z")).toBe("2026-10-23 (UTC day)");
    expect(reportingPeriodLabel("2026-10-17", "2026-10-23")).toBe("2026-10-17 → 2026-10-23 (UTC)");
  });
});

describe("isStale", () => {
  it("flags data older than the freshness window", () => {
    const now = new Date("2026-10-23T12:00:00Z");
    expect(isStale("2026-10-23T10:30:00Z", 60, now)).toBe(true);
    expect(isStale("2026-10-23T11:30:00Z", 60, now)).toBe(false);
  });
});
