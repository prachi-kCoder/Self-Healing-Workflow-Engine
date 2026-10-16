import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './ui';

function mockResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return { queryClient, ...render(<QueryClientProvider client={queryClient}><App/></QueryClientProvider>) };
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); history.replaceState(null, '', '/'); });

describe('workflow dashboard API integration', () => {
  it('reports backend availability from the real health endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(503, { detail: 'Database unavailable' })));
    mount();
    expect(await screen.findByText('API unreachable')).toBeTruthy();
    expect(await screen.findByText(/Database unavailable/)).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith('/api/v1/health', expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('creates a definition, launches a real run, and loads its saved state', async () => {
    const runId = 'c5b45a2c-fbe2-4fc1-845f-b4647bdf2b6a';
    const calls: { url: string; method: string }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const target = String(url); const method = init?.method ?? 'GET'; calls.push({ url: target, method });
      if (target === '/api/v1/health') return mockResponse(200, { status: 'ready', database: 'ready' });
      if (target === '/api/v1/workflows' && method === 'GET') return mockResponse(200, []);
      if (target === '/api/v1/workflows' && method === 'POST') return mockResponse(201, { id: 'workflow-1', name: 'order-processing', description: '', version: 1, created_at: new Date().toISOString() });
      if (target === '/api/v1/workflows/workflow-1/runs') return mockResponse(201, { id: runId, status: 'PENDING', definition_id: 'workflow-1' });
      if (target === `/api/v1/runs/${runId}`) return mockResponse(200, { id: runId, definition_id: 'workflow-1', status: 'PENDING', input: {}, output: {}, created_at: new Date().toISOString(), started_at: null, finished_at: null, cancel_requested: false, definition_snapshot: { name: 'order-processing', tasks: [] }, tasks: [], attempts: [], events: [] });
      return mockResponse(404, { detail: `Unexpected request ${method} ${target}` });
    }));
    mount();
    const launchButton = await screen.findByRole('button', { name: 'Run preview workflow' });
    await waitFor(() => expect((launchButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(launchButton);
    await waitFor(() => expect(calls.some(call => call.url === '/api/v1/workflows' && call.method === 'GET')).toBe(true));
    await waitFor(() => expect(calls.some(call => call.url === '/api/v1/workflows' && call.method === 'POST')).toBe(true));
    await waitFor(() => expect(calls.some(call => call.url === '/api/v1/workflows/workflow-1/runs' && call.method === 'POST')).toBe(true));
    expect(await screen.findByDisplayValue(runId)).toBeTruthy();
    expect(calls.some(call => call.url === `/api/v1/runs/${runId}`)).toBe(true);
    expect(location.search).toContain(runId);
  });

  it('shows a clear not-found response for an inspected run ID', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => String(url) === '/api/v1/health'
      ? mockResponse(200, { status: 'ready', database: 'ready' })
      : mockResponse(404, { detail: 'Run not found' })));
    mount();
    fireEvent.change(screen.getByLabelText('Inspect a run'), { target: { value: 'missing-id' } });
    fireEvent.click(screen.getByRole('button', { name: 'Inspect run' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/Could not load this run.*Run not found/);
  });

  it('validates and saves a user-authored JSON workflow', async () => {
    const workflow = { name: 'custom-pipeline', description: '', tasks: [{ key: 'hello', type: 'demo.echo', input: { text: 'custom' } }] };
    const calls: { url: string; method: string; body?: string }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const target = String(url); const method = init?.method ?? 'GET'; calls.push({ url: target, method, body: init?.body as string | undefined });
      if (target === '/api/v1/health') return mockResponse(200, { status: 'ready', database: 'ready' });
      if (target === '/api/v1/dashboard') return mockResponse(200, { workflow_count: 0, run_count: 0, active_runs: 0, completed_runs: 0, failed_runs: 0, task_count: 0, attempt_count: 0, event_count: 0, recent_runs: [] });
      if (target === '/api/v1/workflows' && method === 'GET') return mockResponse(200, []);
      if (target === '/api/v1/task-types') return mockResponse(200, [{ type: 'demo.echo', description: 'Echo', input_schema: { type: 'object' } }]);
      if (target === '/api/v1/workflows/validate') return mockResponse(200, { valid: true, task_count: 1 });
      if (target === '/api/v1/workflows' && method === 'POST') return mockResponse(201, { id: 'custom-id', name: workflow.name, description: '', version: 1, created_at: new Date().toISOString() });
      return mockResponse(404, { detail: `Unexpected request ${method} ${target}` });
    }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Workflow builder' }));
    const editor = await screen.findByLabelText('Workflow definition JSON');
    fireEvent.change(editor, { target: { value: '{' } });
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/JSON syntax error/);
    fireEvent.change(editor, { target: { value: JSON.stringify(workflow, null, 2) } });
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByText('Valid workflow · 1 tasks')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Save definition' }));
    expect(await screen.findByText(/Saved custom-pipeline · version 1/)).toBeTruthy();
    expect(calls.some(call => call.url === '/api/v1/workflows/validate' && call.method === 'POST')).toBe(true);
    expect(calls.some(call => call.url === '/api/v1/workflows' && call.method === 'POST' && JSON.parse(call.body ?? '{}').name === 'custom-pipeline')).toBe(true);
  });

  it('renders a lab PASS only when its API result asserts persisted evidence', async () => {
    const runId = 'b2b025f7-388f-4f4c-91fc-58f60a06e927';
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const target = String(url);
      if (target === '/api/v1/health') return mockResponse(200, { status: 'ready', database: 'ready' });
      if (target === '/api/v1/dashboard') return mockResponse(200, { workflow_count: 0, run_count: 0, active_runs: 0, completed_runs: 0, failed_runs: 0, task_count: 0, attempt_count: 0, event_count: 0, recent_runs: [] });
      if (target === '/api/v1/workflows') return mockResponse(200, []);
      if (target === '/api/v1/lab/scenarios') return mockResponse(200, [{ id: 'retries', name: 'Automatic retries', description: 'Fail twice.', expected: 'Three attempts.', manual: false }]);
      if (target === '/api/v1/lab/scenarios/retries/runs' && init?.method === 'POST') return mockResponse(201, { scenario_id: 'retries', definition_id: 'definition-1', runs: [{ id: runId, status: 'PENDING' }] });
      if (target.startsWith(`/api/v1/lab/scenarios/retries/results?run_ids=${runId}`)) return mockResponse(200, { scenario_id: 'retries', status: 'PASS', expected: 'Three attempts.', assertions: [{ name: 'two retries then success', passed: true }], runs: [{ id: runId, status: 'SUCCEEDED', tasks: [{ id: 'task-1', key: 'flaky', status: 'SUCCEEDED', attempt_count: 3 }], attempts: [], events: [] }] });
      return mockResponse(404, { detail: `Unexpected request ${target}` });
    }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Demo & reliability lab' }));
    const scenarioSelect = await screen.findByLabelText('Reliability scenario');
    await waitFor(() => expect(scenarioSelect.querySelectorAll('option').length).toBeGreaterThan(1));
    fireEvent.change(scenarioSelect, { target: { value: 'retries' } });
    expect((scenarioSelect as HTMLSelectElement).value).toBe('retries');
    const runScenarioButton = screen.getByRole('button', { name: 'Run scenario' });
    await waitFor(() => expect((runScenarioButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(runScenarioButton);
    expect(await screen.findByText('Scenario result')).toBeTruthy();
    expect(screen.getByText('two retries then success')).toBeTruthy();
    expect(screen.getAllByText('PASS').length).toBeGreaterThan(0);
    expect(screen.getByText(/3 attempts/)).toBeTruthy();
  });
});

