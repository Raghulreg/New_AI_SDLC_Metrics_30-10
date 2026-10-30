import type { DashboardResponse, Health, Iteration, Named, Person } from "./types";

export class ApiError extends Error {
  constructor(public status: string, message: string, public httpStatus: number) {
    super(message);
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const resp = await fetch(path, { signal, headers: { Accept: "application/json" } });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const status = body?.status ?? (resp.status === 401 ? "auth_error" : resp.status === 403 ? "permission_denied" : "error");
    const message = body?.message ?? body?.detail ?? `Request failed (HTTP ${resp.status})`;
    throw new ApiError(status, typeof message === "string" ? message : JSON.stringify(message), resp.status);
  }
  return body as T;
}

const enc = encodeURIComponent;

export const api = {
  health: () => get<Health>("/api/health"),
  projects: (refresh = false) => get<Named[]>(`/api/projects?refresh=${refresh}`),
  teams: (p: string, refresh = false) => get<Named[]>(`/api/projects/${enc(p)}/teams?refresh=${refresh}`),
  members: (p: string, t: string, refresh = false) =>
    get<Person[]>(`/api/projects/${enc(p)}/teams/${enc(t)}/members?refresh=${refresh}`),
  iterations: (p: string, t: string, refresh = false) =>
    get<Iteration[]>(`/api/projects/${enc(p)}/teams/${enc(t)}/iterations?refresh=${refresh}`),
  repositories: (p: string, refresh = false) => get<Named[]>(`/api/projects/${enc(p)}/repositories?refresh=${refresh}`),
  dashboard: (q: DashboardQuery, signal?: AbortSignal) => {
    const params = new URLSearchParams({ project: q.project, from: q.from, to: q.to, refresh: String(q.refresh) });
    if (q.team) params.set("team", q.team);
    if (q.person) params.set("person", q.person);
    if (q.iteration) params.set("iteration", q.iteration);
    if (q.repo) params.append("repo", q.repo);
    return get<DashboardResponse>(`/api/dashboard?${params}`, signal);
  },
};

export interface DashboardQuery {
  project: string;
  team?: string;
  person?: string;
  iteration?: string;
  repo?: string;
  from: string;
  to: string;
  refresh: boolean;
}
