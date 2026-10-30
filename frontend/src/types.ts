export type Status =
  | "ok"
  | "empty"
  | "error"
  | "auth_error"
  | "permission_denied"
  | "unsupported"
  | "not_configured"
  | "attribution_unavailable";

export interface MetricResult {
  id: string;
  name: string;
  status: Status;
  value: number | null;
  unit: string;
  summary: Record<string, unknown>;
  details: Record<string, unknown>[];
  sampleSize: number | null;
  message: string | null;
  definition: string;
  sources: string[];
  scopeLevel: "project" | "team" | "person";
  timeRange: { from: string; to: string } | null;
  fetchedAt: string | null;
  lastSuccessfulFetch: string | null;
  stale: boolean;
}

export interface DashboardResponse {
  organization: string;
  project: string;
  team: string | null;
  person: Person | null;
  timeRange: { from: string; to: string };
  iteration: (Iteration & Record<string, unknown>) | null;
  generatedAt: string;
  freshnessWindowMinutes: number;
  metrics: MetricResult[];
}

export interface Health {
  adoConfigured: boolean;
  adoAuthMode: "pat" | "default_azure_credential";
  organization: string | null;
  defaultProject: string | null;
  cursorConfigured: boolean;
  cursorKeySource: string | null;
  coverageConfigured: boolean;
  llmConfigured: boolean;
  llmProvider: string | null;
  llmModel: string | null;
  llmKeySource: string | null;
  defectRateDenominator: string | null;
  freshnessWindowMinutes: number;
  cacheTtlMinutes: number;
}

export interface Named {
  id: string;
  name: string;
}

export interface Person {
  id: string;
  displayName: string;
  uniqueName: string;
}

export interface Iteration extends Named {
  path: string;
  startDate?: string | null;
  finishDate?: string | null;
  timeFrame?: string;
}
