import type { MetricResult } from "./types";
import {
  aiBreakdown, codeAnalysisView, cursorCostAmount, cursorCostNote, cursorMemberTotal, displayValue, formatDayLabel, formatTokens, formatUsdAmount, fmtTime, includesEstimate, isFailure, isStale,
  memberLabel, reportingPeriodLabel, statusLabel, unestimatedEffortNote,
} from "./format";

export function StatusBadge({ m, windowMinutes }: { m: MetricResult; windowMinutes: number }) {
  const stale = !isFailure(m.status) && (m.stale || isStale(m.fetchedAt, windowMinutes));
  const interim = m.summary.sprintProgress === "in_progress";
  return (
    <span className="badges">
      <span className={`badge badge-${m.status}`}>{statusLabel(m)}</span>
      {interim && <span className="badge badge-interim">In Progress</span>}
      {includesEstimate(m) && <span className="badge badge-estimate">Includes LLM estimate</span>}
      {stale && <span className="badge badge-stale">Stale</span>}
    </span>
  );
}

export function MetricCard({ m, windowMinutes, onSelect, selected }: {
  m: MetricResult; windowMinutes: number; onSelect: () => void; selected: boolean;
}) {
  const rate = m.id === "defect_count" ? (m.summary.defectRate as { status: string; value: number | null } | undefined) : undefined;
  const unestimated = unestimatedEffortNote(m);
  const cost = cursorCostAmount(m);
  return (
    <button className={`card ${selected ? "card-selected" : ""} ${isFailure(m.status) ? "card-failure" : ""}`} onClick={onSelect}>
      <div className="card-head">
        <span className="card-title">{m.name}</span>
        <StatusBadge m={m} windowMinutes={windowMinutes} />
      </div>
      <div className="card-value">{displayValue(m)}</div>
      <div className="card-unit">{isFailure(m.status) ? "" : m.id === "cursor_token_usage" ? "Total Tokens Used" : m.unit}</div>
      {cost && (
        <div className="card-cost">
          <div className="card-cost-label">Actual cost charged by Cursor (USD)</div>
          <div className="card-cost-value">{cost === "Unavailable" ? cost : `${cost} USD`}</div>
        </div>
      )}
      {rate && !isFailure(m.status) && (
        <div className="card-sub">Rate: {rate.value === null ? `— (${rate.status.replace("_", " ")})` : rate.value}</div>
      )}
      {unestimated && !isFailure(m.status) && <div className="card-sub">{unestimated}</div>}
      {!isFailure(m.status) && aiBreakdown(m).map((line) => <div className="card-sub" key={line}>{line}</div>)}
      {m.sampleSize !== null && !isFailure(m.status) && <div className="card-sub">n = {m.sampleSize}</div>}
      {m.message && <div className="card-msg">{m.message}</div>}
      <div className="card-foot">
        {m.timeRange && <div>Period: {reportingPeriodLabel(m.timeRange.from, m.timeRange.to)}</div>}
        <div>Source: {m.sources.join("; ")}</div>
        <div>{m.fetchedAt ? `Fetched ${fmtTime(m.fetchedAt)}` : `Last successful fetch: ${fmtTime(m.lastSuccessfulFetch)}`}</div>
      </div>
    </button>
  );
}

export function AiCodeAnalysis({ m, windowMinutes }: { m: MetricResult; windowMinutes: number }) {
  const view = codeAnalysisView(m);
  return (
    <section className="ai-analysis" aria-labelledby="ai-vs-manual-heading">
      <div className="ai-analysis-head">
        <h2 id="ai-vs-manual-heading">AI vs Manual Code Analysis</h2>
        <StatusBadge m={m} windowMinutes={windowMinutes} />
      </div>
      <p className="muted">
        Added lines in completed pull requests for the selected project, team, person, repository and date range.
        Each commit is counted once. Cursor-tracked lines are verified. LLM labels are estimates, not proof of authorship.
        Unknown lines are never counted as manual.
      </p>
      {view.explanation && (
        <div className={view.state === "unavailable" ? "banner banner-error" : "ai-note"}>{view.explanation}</div>
      )}
      <div className="ai-grid">
        {view.rows.map((row) => (
          <article key={row.key} className={`ai-tile ai-tile-${row.key} ${view.state === "unavailable" ? "ai-tile-off" : ""}`}>
            <h3>{row.label}</h3>
            <div className="ai-lines">{row.linesLabel}</div>
            <div className="ai-pct">
              {row.percentageLabel}
              {row.shareOf && row.percentageLabel !== "—" && row.percentageLabel !== "Unavailable" ? ` ${row.shareOf}` : ""}
            </div>
            {row.details.map((detail) => <div className="ai-detail" key={detail}>{detail}</div>)}
          </article>
        ))}
      </div>
      {view.notes.length > 0 && (
        <ul className="ai-notes">{view.notes.map((note) => <li key={note}>{note}</li>)}</ul>
      )}
      <div className="card-foot">
        {m.timeRange && <div>Period: {reportingPeriodLabel(m.timeRange.from, m.timeRange.to)}</div>}
        <div>Source: {m.sources.join("; ") || "—"}</div>
        <div>{m.fetchedAt ? `Fetched ${fmtTime(m.fetchedAt)}` : `Last successful fetch: ${fmtTime(m.lastSuccessfulFetch)}`}</div>
      </div>
    </section>
  );
}

export function BarChart({ data, title, caption, labelFor }: {
  data: Record<string, number>; title: string; caption?: string; labelFor?: (key: string) => string;
}) {
  const entries = Object.entries(data);
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div className="chart">
      <h4>{title}</h4>
      {caption && <p className="muted chart-caption">{caption}</p>}
      {entries.length === 0 && <p className="empty-inline">Nothing to chart for this selection.</p>}
      {entries.map(([k, v]) => {
        const label = labelFor ? labelFor(k) : k;
        const tokens = formatTokens(v);
        return (
          <div className="bar-row" key={k} title={`${k}: ${tokens} tokens`}>
            <span className="bar-label">{label}</span>
            <span className="bar-track" aria-hidden="true"><span className="bar" style={{ width: `${(v / max) * 100}%` }} /></span>
            <span className="bar-value">{tokens}</span>
          </div>
        );
      })}
    </div>
  );
}

const CHARTS: Record<string, [string, string, string?][]> = {
  cursor_token_usage: [
    ["byDay", "Tokens used each day", "Each bar is one day in UTC."],
    ["byModel", "Tokens used by model"],
  ],
  ai_generated_pct: [["linesByClassification", "Added lines by classification (estimates are labelled)"]],
};

const TECHNICAL_LABELS: Record<string, string> = {
  chargedCents: "Charged cents",
  costUsd: "Cost in USD (exact)",
  inputTokens: "Input tokens",
  outputTokens: "Output tokens",
  cacheWriteTokens: "Cache write tokens",
  cacheReadTokens: "Cache read tokens",
  totalTokens: "Total tokens",
  events: "Usage events",
  eventsWithoutTokenData: "Events without token counts",
  eventsWithoutCost: "Events without a cost",
  mappedIdentities: "Mapped people",
  exactEmailMatches: "Exact email matches",
  configuredMappings: "Configured identity mappings",
  unmappedIdentities: "People without a Cursor match",
  invalidConfiguredMappings: "Mappings that did not match a Cursor member",
  identityRule: "How people are matched",
};

function cell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

const MAX_ROWS = 500;

function exactField(key: string, value: unknown): string {
  if (key === "costUsd" || key === "chargedCents") return value === null || value === undefined ? "—" : String(value);
  return cell(value);
}

function CursorUsageDetail({ m, windowMinutes }: { m: MetricResult; windowMinutes: number }) {
  const cost = cursorCostAmount(m);
  const note = cursorCostNote(m);
  const failed = isFailure(m.status) && m.status !== "unsupported";
  const chartKeys = new Set((CHARTS[m.id] ?? []).map(([key]) => key));
  const technical = Object.entries(m.summary).filter(([key]) => !chartKeys.has(key));
  const members = m.details.slice(0, MAX_ROWS);
  const byDay = (m.summary.byDay ?? {}) as Record<string, number>;
  const byModel = (m.summary.byModel ?? {}) as Record<string, number>;
  const overall = cursorMemberTotal(members, m.summary, m.details.length);
  return (
    <section className="detail detail-usage">
      <div className="detail-head">
        <div>
          <h3>Cursor Token Usage</h3>
          <p className="muted detail-lead">Tokens used and the amount Cursor charged for this selection.</p>
        </div>
        <StatusBadge m={m} windowMinutes={windowMinutes} />
      </div>
      {failed && (
        <div className="banner banner-error" role="alert">
          {statusLabel(m)}{m.message ? `: ${m.message}` : ""}
        </div>
      )}
      {!failed && m.status === "empty" && (
        <div className="empty-state">No Cursor usage in this date range.</div>
      )}
      {!failed && m.status !== "empty" && (
        <div className="usage-highlights">
          <article className="usage-highlight">
            <div className="kicker">Total Tokens Used</div>
            <div className="figure">{typeof m.value === "number" ? formatTokens(m.value) : displayValue(m)}</div>
            <div className="kicker">Overall total for this selection</div>
          </article>
          <article className="usage-highlight usage-highlight-cost">
            <div className="kicker">Actual cost charged by Cursor (USD)</div>
            <div className="figure">{cost && cost !== "Unavailable" ? `${cost} USD` : "Unavailable"}</div>
            {cost && cost !== "Unavailable" && <div className="kicker">Rounded to the nearest cent. The exact amount is in Technical Details.</div>}
          </article>
        </div>
      )}
      {note && <p className="ai-note">{note}</p>}
      {!failed && m.message && <p className="card-msg">{m.message}</p>}
      <p className="muted detail-meta">
        {m.timeRange ? reportingPeriodLabel(m.timeRange.from, m.timeRange.to) : "No reporting period"}
        {" · "}Fetched {fmtTime(m.fetchedAt)}
      </p>
      {!failed && m.status !== "empty" && (
        <div className="chart-grid">
          <BarChart data={byDay} title="Daily usage" caption="Tokens used on each UTC day." labelFor={formatDayLabel} />
          <BarChart data={byModel} title="Usage by model" caption="Tokens used by each model." />
        </div>
      )}
      {!failed && m.status !== "empty" && (
        <>
          <h4>Cost by member{members.length ? ` (${m.details.length}${m.details.length > MAX_ROWS ? `, first ${MAX_ROWS}` : ""})` : ""}</h4>
          <p className="muted chart-caption">Each row is one person. This list is separate from the overall total above.</p>
          {members.length === 0 ? (
            <p className="empty-inline">No member usage in this selection.</p>
          ) : (
            <div className="table-wrap">
              <table className="grid member-table">
                <thead>
                  <tr><th>Member</th><th>Tokens Used</th><th>Actual Cost</th><th>Events</th></tr>
                </thead>
                <tbody>
                  {members.map((row, i) => {
                    const email = String(row.userEmail ?? "");
                    const person = memberLabel(email);
                    const tokens = row.totalTokens;
                    const memberCost = row.costUsd;
                    const events = row.events;
                    return (
                      <tr key={`${email}-${i}`}>
                        <td className="member-cell">
                          <span className="member-name">{person.name}</span>
                          {person.name !== person.email && <span className="member-email">{person.email}</span>}
                        </td>
                        <td className="num">{typeof tokens === "number" ? formatTokens(tokens) : "—"}</td>
                        <td className="num">{typeof memberCost === "number" ? formatUsdAmount(memberCost) : "Unavailable"}</td>
                        <td className="num">{typeof events === "number" ? formatTokens(events) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
                {overall && (
                  <tfoot>
                    <tr className="member-total">
                      <td>Overall total</td>
                      <td className="num">{formatTokens(overall.tokens)}</td>
                      <td className="num">{overall.costUsd === null ? "Unavailable" : `${formatUsdAmount(overall.costUsd)} USD`}</td>
                      <td className="num">{formatTokens(overall.events)}</td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          )}
        </>
      )}
      {technical.length > 0 && (
        <details className="technical">
          <summary>Technical Details</summary>
          <table className="kv"><tbody>
            {technical.map(([key, value]) => (
              <tr key={key}>
                <th>{TECHNICAL_LABELS[key] ?? key}</th>
                <td>{value && typeof value === "object" && !Array.isArray(value)
                  ? Object.entries(value as Record<string, unknown>).map(([nk, nv]) => `${TECHNICAL_LABELS[nk] ?? nk}: ${cell(nv)}`).join("; ")
                  : exactField(key, value)}</td>
              </tr>
            ))}
          </tbody></table>
        </details>
      )}
    </section>
  );
}

export function DetailPanel({ m, windowMinutes }: { m: MetricResult; windowMinutes: number }) {
  if (m.id === "cursor_token_usage") return <CursorUsageDetail m={m} windowMinutes={windowMinutes} />;
  const scalar = Object.entries(m.summary).filter(([, v]) => v === null || typeof v !== "object" || Array.isArray(v));
  const nested = Object.entries(m.summary).filter(([k, v]) => v && typeof v === "object" && !Array.isArray(v) &&
    !(CHARTS[m.id] ?? []).some(([ck]) => ck === k));
  const columns = m.details.length ? Object.keys(m.details[0]) : [];
  return (
    <section className="detail">
      <div className="detail-head">
        <h3>{m.name}</h3>
        <StatusBadge m={m} windowMinutes={windowMinutes} />
      </div>
      <p className="muted">{m.definition}</p>
      <dl className="meta">
        <dt>Value</dt><dd>{displayValue(m)} {isFailure(m.status) ? "" : m.unit}</dd>
        <dt>Scope</dt><dd>{m.scopeLevel}</dd>
        <dt>Reporting period</dt><dd>{m.timeRange ? reportingPeriodLabel(m.timeRange.from, m.timeRange.to) : "—"}</dd>
        <dt>Sources</dt><dd>{m.sources.join("; ")}</dd>
        <dt>Fetched</dt><dd>{fmtTime(m.fetchedAt)}</dd>
        <dt>Last successful fetch</dt><dd>{fmtTime(m.lastSuccessfulFetch)}</dd>
        {m.message && (<><dt>Message</dt><dd>{m.message}</dd></>)}
      </dl>
      {scalar.length > 0 && (
        <table className="kv"><tbody>
          {scalar.map(([k, v]) => <tr key={k}><th>{k}</th><td>{cell(v)}</td></tr>)}
        </tbody></table>
      )}
      {nested.map(([k, v]) => (
        <table className="kv" key={k}><caption>{k}</caption><tbody>
          {Object.entries(v as Record<string, unknown>).map(([nk, nv]) => <tr key={nk}><th>{nk}</th><td>{cell(nv)}</td></tr>)}
        </tbody></table>
      ))}
      {(CHARTS[m.id] ?? []).map(([key, title, caption]) => {
        const data = m.summary[key] as Record<string, number> | undefined;
        return data ? <BarChart key={key} title={title} caption={caption} data={data} /> : null;
      })}
      {m.details.length > 0 && (
        <>
          <h4>Source records ({m.details.length}{m.details.length > MAX_ROWS ? `, first ${MAX_ROWS} shown` : ""})</h4>
          <div className="table-wrap">
            <table className="grid">
              <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {m.details.slice(0, MAX_ROWS).map((row, i) => (
                  <tr key={i}>{columns.map((c) => <td key={c}>{cell(row[c])}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
