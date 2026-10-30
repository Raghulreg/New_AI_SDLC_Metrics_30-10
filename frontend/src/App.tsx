import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { AiCodeAnalysis, DetailPanel, MetricCard } from "./components";
import {
  PRESET_LABEL, type Preset, fmtTime, individualFilterState, presetRange, reportingPeriodLabel, sprintTimeFrameLabel,
} from "./format";
import type { DashboardResponse, Health, Iteration, Named, Person } from "./types";

function errText(e: unknown): string {
  if (e instanceof ApiError) return `${e.status.replace("_", " ")}: ${e.message}`;
  return e instanceof Error ? e.message : String(e);
}

function useLookup<T>(fn: (() => Promise<T>) | null, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null);
    setError(null);
    if (!fn) return;
    let live = true;
    fn().then((d) => live && setData(d)).catch((e) => live && setError(errText(e)));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

const initialRange = presetRange("today");

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [project, setProject] = useState("");
  const [team, setTeam] = useState("");
  const [person, setPerson] = useState("");
  const [iteration, setIteration] = useState("");
  const [repo, setRepo] = useState("");
  const [from, setFrom] = useState(initialRange.from);
  const [to, setTo] = useState(initialRange.to);
  const applyPreset = (p: Preset) => { const r = presetRange(p); setFrom(r.from); setTo(r.to); };
  const [dash, setDash] = useState<DashboardResponse | null>(null);
  const [dashError, setDashError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    api.health().then((h) => {
      setHealth(h);
      if (h.defaultProject) setProject((p) => p || h.defaultProject!);
    }).catch((e) => setHealthError(errText(e)));
  }, []);

  const projects = useLookup<Named[]>(health?.adoConfigured ? () => api.projects() : null, [health?.adoConfigured]);
  const teams = useLookup<Named[]>(project ? () => api.teams(project) : null, [project]);
  const repos = useLookup<Named[]>(project ? () => api.repositories(project) : null, [project]);
  const members = useLookup<Person[]>(project && team ? () => api.members(project, team) : null, [project, team]);
  const iterations = useLookup<Iteration[]>(project && team ? () => api.iterations(project, team) : null, [project, team]);

  const individual = individualFilterState(team, members.data, members.error);
  const individualReady = individual.enabled;
  const individualHint = individual.hint;
  const individualPlaceholder = individual.placeholder;

  // Keep dependent selections only while they remain valid; the date range is never changed implicitly.
  useEffect(() => { if (members.data && person && !members.data.some((m) => m.id === person)) setPerson(""); }, [members.data, person]);
  useEffect(() => { if (iterations.data && iteration && !iterations.data.some((i) => i.id === iteration)) setIteration(""); }, [iterations.data, iteration]);

  const load = useCallback((refresh: boolean) => {
    if (!project) return;
    if (from > to) { setDashError("'From' must not be after 'To'"); return; }
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setDashError(null);
    api.dashboard({ project, team: team || undefined, person: person || undefined, iteration: iteration || undefined,
      repo: repo || undefined, from, to, refresh }, ctrl.signal)
      .then((d) => { setDash(d); })
      .catch((e) => { if (!ctrl.signal.aborted) setDashError(errText(e)); })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
  }, [project, team, person, iteration, repo, from, to]);

  useEffect(() => { load(false); }, [load]);

  const windowMinutes = dash?.freshnessWindowMinutes ?? health?.freshnessWindowMinutes ?? 60;
  const selectedMetric = dash?.metrics.find((m) => m.id === selected) ?? null;
  const aiMetric = dash?.metrics.find((m) => m.id === "ai_generated_pct") ?? null;

  return (
    <div className="app">
      <header>
        <h1>SDLC Metrics · Daily Dashboard</h1>
        <div className="muted">
          {health?.organization ?? "Azure DevOps organization not configured"}
          {health?.adoConfigured && ` · ADO auth: ${health.adoAuthMode === "pat" ? "Personal Access Token (local)" : "DefaultAzureCredential"}`}
          {" · "}live Azure DevOps and Cursor data only
        </div>
      </header>

      {healthError && <div className="banner banner-error">Backend unavailable: {healthError}</div>}
      {health && !health.adoConfigured && (
        <div className="banner banner-error">AZURE_DEVOPS_ORG_URL is not configured on the backend. No Azure DevOps metrics can be shown.</div>
      )}
      {health && !health.cursorConfigured && (
        <div className="banner banner-warn">Cursor API key is not configured; Cursor metrics will show “Not configured”.</div>
      )}
      {health && !health.llmConfigured && (
        <div className="banner banner-warn">
          LLM Code Analysis — Configuration required. Set the provider, model, endpoint and API key before estimates
          can run. Commits without Cursor attribution stay unmeasured and are not counted as manual.
        </div>
      )}

      <section className="filters">
        <label>Project
          <select value={project} onChange={(e) => { setProject(e.target.value); setTeam(""); setPerson(""); setIteration(""); setRepo(""); }}>
            <option value="">— select —</option>
            {project && !projects.data?.some((p) => p.name === project) && <option value={project}>{project}</option>}
            {projects.data?.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}
          </select>
          {projects.error && <span className="field-error">{projects.error}</span>}
        </label>
        <label>Team
          <select value={team} onChange={(e) => { setTeam(e.target.value); setPerson(""); }} disabled={!project}>
            <option value="">All (project level)</option>
            {teams.data?.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
          </select>
          {teams.error && <span className="field-error">{teams.error}</span>}
        </label>
        <label>Individual
          <select value={person} onChange={(e) => setPerson(e.target.value)} disabled={!individualReady}
            title={individualHint ?? undefined}>
            <option value="">{individualPlaceholder}</option>
            {members.data?.map((m) => <option key={m.id} value={m.id}>{m.displayName}</option>)}
          </select>
          {individualHint && <span className="field-hint">{individualHint}</span>}
          {members.error && <span className="field-error">{members.error}</span>}
        </label>
        <label>Sprint
          <select value={iteration} onChange={(e) => setIteration(e.target.value)} disabled={!team}>
            <option value="">Current / latest</option>
            {iterations.data?.map((i) => {
              const frame = sprintTimeFrameLabel(i.timeFrame);
              return <option key={i.id} value={i.id}>{i.name}{frame ? ` (${frame})` : ""}</option>;
            })}
          </select>
          {iterations.error && <span className="field-error">{iterations.error}</span>}
        </label>
        <label>Repository
          <select value={repo} onChange={(e) => setRepo(e.target.value)} disabled={!project}>
            <option value="">All repositories</option>
            {repos.data?.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </label>
        <div className="presets">
          {(Object.keys(PRESET_LABEL) as Preset[]).map((p) => {
            const r = presetRange(p);
            const active = r.from === from && r.to === to;
            return <button key={p} className={active ? "preset preset-active" : "preset"} onClick={() => applyPreset(p)}>{PRESET_LABEL[p]}</button>;
          })}
        </div>
        <label>From<input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To<input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} /></label>
        <button className="refresh" onClick={() => load(true)} disabled={!project || loading}>
          {loading ? "Updating…" : "Refresh from sources"}
        </button>
      </section>

      {dashError && <div className="banner banner-error">{dashError}</div>}
      {dash && (
        <div className="scope-line muted">
          <strong>Reporting date: {reportingPeriodLabel(dash.timeRange.from, dash.timeRange.to)}</strong>
          {" · "}{dash.project}{dash.team ? ` → ${dash.team}` : ""}{dash.person ? ` → ${dash.person.displayName}` : ""}
          {dash.iteration ? ` · Sprint: ${dash.iteration.name}${dash.iteration.timeFrame === "current" ? " (In Progress)" : ""}` : ""}
          {" · "}Generated {fmtTime(dash.generatedAt)} · Freshness window {windowMinutes} min
        </div>
      )}

      <main>
        {loading && (
          <div className="loading-bar" role="status">
            <span className="spinner" aria-hidden="true" />
            Updating figures. Filters stay available.
          </div>
        )}
        <div className={`cards ${loading && dash ? "is-refreshing" : ""}`}>
          {dash?.metrics.map((m) => (
            <MetricCard key={m.id} m={m} windowMinutes={windowMinutes} selected={m.id === selected}
              onSelect={() => setSelected(m.id === selected ? null : m.id)} />
          ))}
          {!dash && loading && Array.from({ length: 7 }, (_, i) => <div key={i} className="card card-skeleton" aria-hidden="true" />)}
          {!dash && !loading && dashError && <div className="empty-state">The dashboard could not be loaded. Adjust the filters or refresh.</div>}
          {!dash && !project && <div className="empty-state">Select a project to load metrics.</div>}
        </div>
        {selectedMetric && <DetailPanel m={selectedMetric} windowMinutes={windowMinutes} />}
        {aiMetric && <AiCodeAnalysis m={aiMetric} windowMinutes={windowMinutes} />}
      </main>
    </div>
  );
}
