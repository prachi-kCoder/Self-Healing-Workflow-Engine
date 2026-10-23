const configuredApiUrl = (import.meta as ImportMeta & { env: { VITE_API_URL?: string } }).env.VITE_API_URL;
// Strip trailing slashes so an origin ending in `/` does not produce `//api/v1`.
const API_BASE = `${(configuredApiUrl || '').replace(/\/+$/, '')}/api/v1`;

export type Health = { status: string; database: string };
export type WorkflowSummary = { id: string; name: string; description: string; version: number; created_at: string };
export type Workflow = WorkflowSummary & { definition: {
  name: string; description?: string; metadata?: Record<string, unknown>;
  tasks: { key: string; type: string; input: Record<string, unknown>; depends_on: string[];
    timeout_seconds?: number; max_attempts?: number; retry_base_seconds?: number;
    retry_policy?: string; max_retry_delay_seconds?: number; retry_on?: string[];
    compensation?: { type: string; input: Record<string, unknown> } }[];
} };
export type HandlerInfo = { type: string; description: string; input_schema: Record<string, unknown>; output_schema: Record<string, unknown> };
export type LabScenario = { id: string; name: string; description: string; expected: string; requires?: string; manual: boolean };
export type LabRun = { id: string; status: string };
export type Dashboard = {
  workflow_count: number; run_count: number; active_runs: number; completed_runs: number; failed_runs: number;
  task_count: number; attempt_count: number; event_count: number; dead_letter_count: number;
  recent_runs: { id: string; status: string; workflow_name: string; version: number; created_at: string }[];
};
export type Attempt = {
  id: string; task_id: string; attempt_number: number; worker_id: string; status: string;
  started_at: string; finished_at: string | null; duration_seconds: number | null;
  error: string | null; error_classification?: string | null; output: Record<string, unknown>;
};
export type Task = {
  id: string; run_id: string; key: string; type: string; status: string; input: Record<string, unknown>;
  output: Record<string, unknown>; dependencies: string[]; attempt_count: number; max_attempts: number;
  worker_id: string | null; heartbeat_at: string | null; started_at: string | null;
  lease_expires_at?: string | null; finished_at: string | null; duration_seconds: number | null; error: string | null;
  error_classification?: string | null; retry_policy?: string; retry_base_seconds?: number;
  max_retry_delay_seconds?: number;
  retry_on?: string[]; compensation_type?: string | null;
};
export type Event = { id: string; run_id: string; task_id: string | null; event_type: string; payload: Record<string, unknown>; created_at: string };
export type Run = {
  id: string; definition_id: string; definition_version: number; status: string; input: Record<string, unknown>; output: Record<string, unknown>;
  created_at: string; started_at: string | null; finished_at: string | null; cancel_requested: boolean;
  definition_snapshot: { name: string; tasks: { key: string; depends_on: string[] }[] };
  tasks: Task[]; attempts: Attempt[]; events: Event[];
  compensations?: { id: string; task_id: string; task_key: string; handler_type: string;
    status: string; attempt_count: number; output: Record<string, unknown>; error: string | null }[];
};
export type DeadLetter = { id: string; run_id: string; task_id: string; task_key: string;
  error: string; error_classification: string; attempt_count: number; created_at: string };

export class ApiError extends Error {
  constructor(message: string, readonly status?: number, readonly kind: 'http' | 'network' | 'timeout' = 'http') {
    super(message); this.name = 'ApiError';
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init, signal: controller.signal,
      headers: { Accept: 'application/json', ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
    });
    const body: unknown = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const raw = typeof body === 'object' && body !== null && 'detail' in body ? body.detail : null;
      const detail = Array.isArray(raw)
        ? raw.map(item => typeof item === 'object' && item !== null && 'msg' in item
          ? `${Array.isArray((item as { loc?: unknown }).loc) ? (item as { loc: unknown[] }).loc.join('.') + ': ' : ''}${String((item as { msg: unknown }).msg)}`
          : String(item)).join('; ')
        : raw == null ? `Request failed (${response.status})` : String(raw);
      throw new ApiError(detail, response.status, 'http');
    }
    return body as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('The API request timed out after 10 seconds.', undefined, 'timeout');
    }
    throw new ApiError('The browser could not connect to the API. Check that the API service is running and the /api proxy target is correct.', undefined, 'network');
  } finally { window.clearTimeout(timer); }
}

export const api = {
  health: () => request<Health>('/health'),
  dashboard: () => request<Dashboard>('/dashboard'),
  workflows: () => request<WorkflowSummary[]>('/workflows'),
  workflow: (id: string) => request<Workflow>(`/workflows/${encodeURIComponent(id)}`),
  taskTypes: () => request<HandlerInfo[]>('/task-types'),
  labScenarios: () => request<LabScenario[]>('/lab/scenarios'),
  startLabScenario: (id: string) => request<{ scenario_id: string; definition_id: string; runs: LabRun[] }>(`/lab/scenarios/${encodeURIComponent(id)}/runs`, { method: 'POST' }),
  labResult: (scenario: string, runIds: string[]) => request<{
    scenario_id: string; status: string; expected: string;
    assertions: { name: string; passed: boolean | null; detail?: string }[];
    runs: { id: string; status: string; tasks: Task[]; attempts: Attempt[]; events: Event[] }[];
  }>(`/lab/scenarios/${encodeURIComponent(scenario)}/results?${runIds.map(id => `run_ids=${encodeURIComponent(id)}`).join('&')}`),
  compareRuns: (left: string, right: string) => request<{
    definition_id: string; left: { id: string; status: string }; right: { id: string; status: string };
    tasks: { key: string; left_status: string; right_status: string; left_attempts: number;
      right_attempts: number; left_duration_seconds: number | null; right_duration_seconds: number | null }[];
  }>(`/runs/${encodeURIComponent(left)}/compare/${encodeURIComponent(right)}`),
  validateWorkflow: (workflow: unknown) => request<{ valid: boolean; task_count: number }>('/workflows/validate', { method: 'POST', body: JSON.stringify(workflow) }),
  createWorkflow: (workflow: unknown) => request<WorkflowSummary>('/workflows', { method: 'POST', body: JSON.stringify(workflow) }),
  createWorkflowVersion: (id: string, definition: unknown) => request<WorkflowSummary>(`/workflows/${encodeURIComponent(id)}/versions`, { method: 'POST', body: JSON.stringify({ definition }) }),
  duplicateWorkflow: (id: string, name?: string) => request<WorkflowSummary>(`/workflows/${encodeURIComponent(id)}/duplicate`, { method: 'POST', body: JSON.stringify(name ? { name } : {}) }),
  startRun: (workflowId: string) => request<{ id: string; status: string; definition_id: string }>(`/workflows/${encodeURIComponent(workflowId)}/runs`, { method: 'POST', body: JSON.stringify({ input: { source: 'dashboard-preview' } }) }),
  run: (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}`),
  deadLetters: () => request<DeadLetter[]>('/dead-letters'),
  cancelRun: (runId: string) => request<{ id: string; status: string; cancel_requested: boolean }>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
  retryTask: (taskId: string) => request<{ task_id: string; status: string; run_status: string }>(`/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' }),
};

export function runSocketUrl(runId: string): string {
  const url = new URL(`${API_BASE}/runs/${encodeURIComponent(runId)}/ws`, location.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}
