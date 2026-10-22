import { useQuery, useQueryClient } from '@tanstack/react-query';
import React from 'react';
import ReactFlow, { Background, Controls, Edge, Node } from 'reactflow';
import 'reactflow/dist/style.css';
import { ApiError, api, Run, runSocketUrl, Task, WorkflowSummary } from './api';

const demoDefinition = {
  name: 'order-processing',
  description: 'Four-step example with parallel checks, dependencies, retry, and idempotent payment.',
  tasks: [
    { key: 'validate_order', type: 'demo.validate_order', input: { order_id: 'preview-order' }, timeout_seconds: 10, max_attempts: 2 },
    { key: 'check_inventory', type: 'demo.check_inventory', input: { sku: 'preview-widget' }, timeout_seconds: 10, max_attempts: 3 },
    { key: 'process_payment', type: 'demo.process_payment', depends_on: ['validate_order', 'check_inventory'], input: { amount: 42, fail_first: 2 }, timeout_seconds: 15, max_attempts: 3, retry_base_seconds: 0.4 },
    { key: 'send_confirmation', type: 'demo.send_confirmation', depends_on: ['process_payment'], input: {}, timeout_seconds: 10, max_attempts: 3 },
  ],
};

function isCurrentPreviewDefinition(definition: Record<string, unknown> | null): boolean {
  const tasks = definition?.tasks;
  if (!Array.isArray(tasks) || tasks.length !== demoDefinition.tasks.length) return false;
  return demoDefinition.tasks.every((expected, index) => {
    const actual = tasks[index] as Record<string, unknown> | undefined;
    return actual?.key === expected.key && actual.type === expected.type
      && Object.entries(expected.input).every(([key, value]) =>
        JSON.stringify((actual.input as Record<string, unknown>)?.[key]) === JSON.stringify(value))
      && JSON.stringify(actual.depends_on ?? []) === JSON.stringify(expected.depends_on ?? [])
      && actual.max_attempts === expected.max_attempts
      && (expected.key !== 'process_payment' || (actual.input as Record<string, unknown>)?.fail_first === 2);
  });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : 'Unexpected request error';
}

export default function App() {
  const client = useQueryClient();
  const [runId, setRunId] = React.useState(new URLSearchParams(location.search).get('run') || '');
  const [runIdInput, setRunIdInput] = React.useState(runId);
  const [launching, setLaunching] = React.useState(false);
  const [action, setAction] = React.useState('');
  const [actionError, setActionError] = React.useState('');
  const [page, setPage] = React.useState<'dashboard' | 'builder' | 'lab'>('dashboard');
  const [workflowId, setWorkflowId] = React.useState('');
  const [compareIds, setCompareIds] = React.useState<string[]>([]);
  const health = useQuery({ queryKey: ['api-health'], queryFn: api.health, retry: false, refetchInterval: 5000 });
  const dashboard = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard, refetchInterval: 5000, retry: false });
  const deadLetters = useQuery({ queryKey: ['dead-letters'], queryFn: api.deadLetters,
    enabled: (dashboard.data?.dead_letter_count ?? 0) > 0, refetchInterval: 5000, retry: false });
  const workflowsQuery = useQuery({ queryKey: ['workflows'], queryFn: api.workflows, retry: false });
  const comparison = useQuery({ queryKey: ['run-comparison', compareIds],
    queryFn: () => api.compareRuns(compareIds[0], compareIds[1]), enabled: compareIds.length === 2, retry: false });
  const runQuery = useQuery<Run>({
    queryKey: ['run', runId], queryFn: () => api.run(runId), enabled: !!runId,
    retry: false,
  });
  const [liveStatus, setLiveStatus] = React.useState<'connecting' | 'connected' | 'reconnecting' | 'offline' | 'complete'>('offline');
  React.useEffect(() => {
    if (!runId || typeof WebSocket === 'undefined') { setLiveStatus('offline'); return; }
    let socket: WebSocket | undefined;
    let timer = 0;
    let stopped = false;
    let delay = 500;
    const connect = () => {
      if (stopped) return;
      setLiveStatus(delay === 500 ? 'connecting' : 'reconnecting');
      socket = new WebSocket(runSocketUrl(runId));
      socket.onopen = () => { delay = 500; setLiveStatus('connected'); };
      socket.onmessage = message => {
        try {
          const snapshot = JSON.parse(message.data) as Run & { error?: string };
          if (!snapshot.error) {
            client.setQueryData(['run', runId], snapshot);
            if (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(snapshot.status)) {
              stopped = true;
              setLiveStatus('complete');
              socket?.close();
            }
          }
        } catch { /* Ignore malformed frames and keep the connection alive. */ }
      };
      socket.onclose = () => {
        if (stopped) return;
        setLiveStatus('reconnecting');
        timer = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 10000);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { stopped = true; window.clearTimeout(timer); socket?.close(); };
  }, [runId, client]);
  const run = runQuery.data;
  const tasks = run?.tasks ?? [];
  const attempts = run?.attempts ?? [];
  const events = run?.events ?? [];

  async function startPreview() {
    if (launching) return;
    setLaunching(true); setActionError('');
    try {
      const connection = await api.health();
      if (connection.status !== 'ready') throw new ApiError('The API is not ready yet. Check database migrations and retry.');
      const workflows: WorkflowSummary[] = await api.workflows();
      // Names are not unique and older definitions may have a different DAG or
      // retry configuration. Reuse only a definition that matches this preview.
      let workflow: WorkflowSummary | undefined;
      for (const candidate of workflows.filter(item => item.name === demoDefinition.name)) {
        const saved = await api.workflow(candidate.id);
        if (isCurrentPreviewDefinition(saved.definition)) {
          workflow = candidate;
          break;
        }
      }
      if (!workflow) workflow = await api.createWorkflow(demoDefinition);
      const created = await api.startRun(workflow.id);
      setRunId(created.id); setRunIdInput(created.id);
      history.replaceState(null, '', `?run=${encodeURIComponent(created.id)}`);
      await client.invalidateQueries({ queryKey: ['api-health'] });
      await client.invalidateQueries({ queryKey: ['dashboard'] });
      await client.invalidateQueries({ queryKey: ['workflows'] });
    } catch (error) {
      setActionError(errorMessage(error));
      void health.refetch();
    } finally { setLaunching(false); }
  }

  function inspectRun(event: React.FormEvent) {
    event.preventDefault();
    const value = runIdInput.trim();
    if (!value || value.length > 100) { setActionError('Enter a valid run ID.'); return; }
    setActionError(''); setRunId(value);
    history.replaceState(null, '', `?run=${encodeURIComponent(value)}`);
    void client.invalidateQueries({ queryKey: ['run', value] });
  }

  async function inspectRunId(value: string) {
    setRunId(value); setRunIdInput(value); setPage('dashboard'); setActionError('');
    history.replaceState(null, '', `?run=${encodeURIComponent(value)}`);
    await client.invalidateQueries({ queryKey: ['run', value] });
  }

  async function startSelectedWorkflow() {
    if (!workflowId) return;
    setLaunching(true); setActionError('');
    try { const created = await api.startRun(workflowId); await inspectRunId(created.id); }
    catch (error) { setActionError(errorMessage(error)); }
    finally { setLaunching(false); }
  }

  async function cancelRun() {
    if (!run) return;
    setAction('cancel'); setActionError('');
    try { await api.cancelRun(run.id); await runQuery.refetch(); }
    catch (error) { setActionError(errorMessage(error)); }
    finally { setAction(''); }
  }

  async function retryTask(task: Task) {
    setAction(task.id); setActionError('');
    try { await api.retryTask(task.id); await runQuery.refetch(); }
    catch (error) { setActionError(errorMessage(error)); }
    finally { setAction(''); }
  }

  const nodes: Node[] = tasks.map((task, index) => ({
    id: task.key,
    position: { x: (index % 2) * 280, y: Math.floor(index / 2) * 150 },
    data: { label: <div className="node-label"><b>{task.key}</b><span className={`badge ${task.status.toLowerCase()}`}>{task.status}</span><small>{task.attempt_count}/{task.max_attempts} attempts</small></div> },
    style: { border: '1px solid #31455f', borderRadius: 12, padding: 14, background: '#111c2a', color: '#eef5ff', width: 205 },
  }));
  const edges: Edge[] = [];
  for (const item of run?.definition_snapshot.tasks ?? []) {
    for (const parent of item.depends_on) edges.push({ id: `${parent}-${item.key}`, source: parent, target: item.key, animated: true });
  }
  const healthText = health.isLoading ? 'Checking API…' : health.isError ? 'API unreachable' : `API ${health.data?.status ?? 'unknown'}`;
  const healthClass = health.isError ? 'offline' : health.data?.status === 'ready' ? 'online' : 'checking';

  return <main>
    <header>
      <div><small>ORCHESTRATION / OBSERVABILITY</small><h1>Workflow control plane</h1><p>Live execution state from the workflow API</p></div>
      <div className="header-actions"><div className={`connection ${healthClass}`}><i/>{healthText}</div>
        <button onClick={() => void health.refetch()} disabled={health.isFetching}>Refresh API</button>
        <button className="primary" onClick={startPreview} disabled={launching || health.isFetching}>{launching ? 'Starting…' : 'Run preview workflow'}</button></div>
    </header>
    <nav className="main-nav" aria-label="Workflow tools">
      <button className={page === 'dashboard' ? 'selected' : ''} onClick={() => setPage('dashboard')}>Control plane</button>
      <button className={page === 'builder' ? 'selected' : ''} onClick={() => setPage('builder')}>Workflow builder</button>
      <button className={page === 'lab' ? 'selected' : ''} onClick={() => setPage('lab')}>Demo &amp; reliability lab</button>
    </nav>
    {page === 'builder' ? <WorkflowBuilder onInspect={inspectRunId}/> : page === 'lab'
      ? <ReliabilityLab onInspect={inspectRunId}/> : <>
    <section className="stats global-stats">
      {dashboard.data ? <>
        <Metric label="SAVED WORKFLOWS" value={dashboard.data.workflow_count}/>
        <Metric label="ACTIVE RUNS" value={dashboard.data.active_runs}/>
        <Metric label="COMPLETED RUNS" value={dashboard.data.completed_runs}/>
        <Metric label="FAILED RUNS" value={dashboard.data.failed_runs}/>
        <Metric label="TOTAL TASKS" value={dashboard.data.task_count}/>
        <Metric label="ATTEMPTS" value={dashboard.data.attempt_count}/>
        <Metric label="EVENTS" value={dashboard.data.event_count}/>
        <Metric label="DEAD LETTERS" value={dashboard.data.dead_letter_count}/>
      </> : <div className="muted">{dashboard.isLoading ? 'Loading persisted engine statistics…' : `Dashboard statistics unavailable: ${errorMessage(dashboard.error)}`}</div>}
    </section>
    <section className="panel workflow-launch">
      <div><small>SAVED WORKFLOWS</small><h2>Run any definition</h2><p>Choose a saved workflow version, or build a new workflow.</p></div>
      <div className="actions"><select aria-label="Saved workflow" value={workflowId} onChange={event => setWorkflowId(event.target.value)}>
        <option value="">Select workflow</option>{(workflowsQuery.data ?? []).map(workflow => <option key={workflow.id} value={workflow.id}>{workflow.name} · v{workflow.version}</option>)}
      </select><button onClick={() => void startSelectedWorkflow()} disabled={!workflowId || launching}>{launching ? 'Starting…' : 'Run selected workflow'}</button>
        <button onClick={() => setPage('builder')}>Create workflow</button></div>
    </section>
    {dashboard.data?.recent_runs.length ? <section className="panel"><div className="section-title"><div><small>RECENT ACTIVITY</small><h2>Runs across workflows</h2></div><span>{dashboard.data.run_count} total runs</span></div>
      <div className="recent-runs">{dashboard.data.recent_runs.map(item => <div className="recent-run-card" key={item.id}><b>{item.workflow_name} · v{item.version}</b><span className={`badge ${item.status.toLowerCase()}`}>{item.status}</span><small>{item.id}</small><button onClick={() => void inspectRunId(item.id)}>Inspect</button><button onClick={() => setCompareIds(ids => ids.length === 2 ? [item.id] : ids.includes(item.id) ? ids : [...ids, item.id])}>{compareIds.includes(item.id) ? 'Selected for compare' : 'Compare'}</button></div>)}</div>
      {compareIds.length > 0 && <p className="muted">Select a second run using the same workflow version. <button onClick={() => setCompareIds([])}>Clear comparison</button></p>}
      {comparison.data && <div className="comparison"><h3>Run comparison</h3><p>{comparison.data.left.status} vs {comparison.data.right.status} · same definition version</p><div className="comparison-grid"><b>Task</b><b>Left: attempts / duration</b><b>Right: attempts / duration</b>{comparison.data.tasks.map(task => <React.Fragment key={task.key}><span>{task.key}</span><span>{task.left_attempts} / {task.left_duration_seconds?.toFixed(2) ?? '—'}s · {task.left_status}</span><span>{task.right_attempts} / {task.right_duration_seconds?.toFixed(2) ?? '—'}s · {task.right_status}</span></React.Fragment>)}</div></div>}
      {comparison.isError && <div className="notice error">Runs cannot be compared: {errorMessage(comparison.error)}</div>}
    </section> : null}
    {(dashboard.data?.dead_letter_count ?? 0) > 0 && <section className="panel dead-letter-panel" role="alert">
      <div className="section-title"><div><small>OPERATOR ACTION</small><h2>Critical task failures</h2></div>
        <span className="badge failed">{dashboard.data?.dead_letter_count} in dead letter queue</span></div>
      <p>These tasks exhausted their retry policy or could not be compensated. Review the error and inspect its run.</p>
      {(deadLetters.data ?? []).slice(0, 10).map(item => <div className="dead-letter-row" key={item.id}>
        <b>{item.task_key}</b><span className={`badge ${item.error_classification.toLowerCase()}`}>{item.error_classification}</span>
        <small>{item.attempt_count} attempts · {item.error}</small>
        <button onClick={() => void inspectRunId(item.run_id)}>Inspect run</button></div>)}
      {deadLetters.isError && <div className="notice error">Could not load dead letters: {errorMessage(deadLetters.error)}</div>}
    </section>}
    <form className="lookup panel" onSubmit={inspectRun}>
      <label htmlFor="run-id">Inspect a run</label>
      <div className="actions"><input id="run-id" value={runIdInput} onChange={event => setRunIdInput(event.target.value)} placeholder="Paste a run ID"/>
        <button type="submit" disabled={!runIdInput.trim()}>Inspect run</button></div>
    </form>
    {health.isError && <div className="notice error"><b>API unavailable.</b> {errorMessage(health.error)} Check the API service, database readiness, and Vite `/api` proxy target.</div>}
    {runQuery.isError && runId && <div className="notice error" role="alert"><b>Could not load this run.</b> {errorMessage(runQuery.error)} <button onClick={() => void runQuery.refetch()}>Retry request</button></div>}
    {actionError && <div className="notice error">{actionError}<button onClick={() => setActionError('')} aria-label="Dismiss error">Dismiss</button></div>}
    {run && <section className="stats run-stats">
      <article><small>RUN STATUS</small><strong>{run?.status ?? (runQuery.isLoading ? 'Loading' : '—')}</strong></article>
      <article><small>TASKS</small><strong>{run ? run.tasks.length : '—'}</strong></article>
      <article><small>ATTEMPTS</small><strong>{run ? attempts.length : '—'}</strong></article>
      <article><small>EVENTS</small><strong>{run ? events.length : '—'}</strong></article>
    </section>}
    {run && <div className="muted run-version">{run.definition_snapshot.name} · definition version {run.definition_version} · run {run.id}</div>}
    <section className="panel"><div className="section-title"><div><small>DEPENDENCY GRAPH</small><h2>Execution topology</h2></div>
      <span>{run ? (['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(run.status) ? 'Run complete' : `Live updates ${liveStatus}`) : 'Select or start a workflow run'}</span>
      {run && <button onClick={() => void runQuery.refetch()} disabled={runQuery.isFetching}>Refresh run</button>}</div>
      <div className="flow">{tasks.length ? <ReactFlow nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.25 }}><Background color="#27364a"/><Controls/></ReactFlow>
        : <div className="empty"><strong>{runQuery.isLoading ? 'Loading run…' : runQuery.isError ? 'Run unavailable' : 'No run selected'}</strong><p>Start the sample above or enter an existing run ID.</p></div>}</div>
    </section>
    {run && <section className="panel"><div className="section-title"><div><small>TASK DETAILS</small><h2>Execution results</h2></div>
      {['PENDING', 'RUNNING'].includes(run.status) && <button onClick={cancelRun} disabled={action === 'cancel'}>{action === 'cancel' ? 'Requesting…' : 'Cancel run'}</button>}</div>
      <div className="task-list">{tasks.map(task => <article className="task-card" key={task.id}>
        <div className="task-heading"><div><b>{task.key}</b><small>{task.type} · depends on {task.dependencies.length ? task.dependencies.join(', ') : 'nothing'}</small></div><span className={`badge ${task.status.toLowerCase()}`}>{task.status}</span></div>
        <div className="task-meta"><span>{task.attempt_count} attempt{task.attempt_count === 1 ? '' : 's'}</span><span>{task.duration_seconds == null ? 'Duration pending' : `${task.duration_seconds.toFixed(2)}s`}</span><span>{task.started_at ? `Started ${new Date(task.started_at).toLocaleTimeString()}` : 'Not started'}</span><span>{task.finished_at ? `Finished ${new Date(task.finished_at).toLocaleTimeString()}` : 'Not finished'}</span><span>{task.worker_id ? `Worker ${task.worker_id}` : 'No active worker'}</span>
          {task.status === 'FAILED' && <button onClick={() => void retryTask(task)} disabled={!!action}>{action === task.id ? 'Retrying…' : 'Retry task'}</button>}</div>
        {task.error && <p className="task-error">{task.error_classification ?? 'ERROR'}: {task.error}</p>}
        <small className="task-policy">Timeout {task.retry_policy}; retry on {(task.retry_on ?? ['RETRYABLE', 'TIMEOUT', 'LEASE_EXPIRED']).join(', ')} · base {task.retry_base_seconds}s; cap {task.max_retry_delay_seconds}s · lease {task.heartbeat_at ? `heartbeat ${new Date(task.heartbeat_at).toLocaleTimeString()}, expires ${task.lease_expires_at ? new Date(task.lease_expires_at).toLocaleTimeString() : 'unknown'}` : 'inactive'}</small>
        <details><summary>Task input</summary><pre>{JSON.stringify(task.input, null, 2)}</pre></details>
        {attempts.filter(attempt => attempt.task_id === task.id).length > 0 && <details><summary>Attempt history ({attempts.filter(attempt => attempt.task_id === task.id).length})</summary>
          {attempts.filter(attempt => attempt.task_id === task.id).map(attempt => <div className="attempt-row" key={attempt.id}>#{attempt.attempt_number} {attempt.status} · {attempt.worker_id} · {attempt.duration_seconds == null ? 'running' : `${attempt.duration_seconds.toFixed(2)}s`}{attempt.error ? ` · ${attempt.error_classification ?? 'ERROR'}: ${attempt.error}` : ''}</div>)}</details>}
        {Object.keys(task.output).length > 0 && <details><summary>Task output</summary><pre>{JSON.stringify(task.output, null, 2)}</pre></details>}
      </article>)}</div>
    </section>}
    {run && run.compensations?.length ? <section className="panel"><div className="section-title"><div><small>SAGA ROLLBACK</small><h2>Compensation actions</h2></div><span>{run.compensations.filter(item => item.status === 'SUCCEEDED').length}/{run.compensations.length} completed</span></div>
      {run.compensations!.map(item => <div className="compensation-row" key={item.id}><b>{item.task_key}</b><span>{item.handler_type}</span><span className={`badge ${item.status.toLowerCase()}`}>{item.status}</span>
        {item.error && <small className="task-error">{item.error}</small>}{Object.keys(item.output).length > 0 && <small>{JSON.stringify(item.output)}</small>}</div>)}
    </section> : null}
    <section className="panel"><div className="section-title"><div><small>EVENT JOURNAL</small><h2>Recent execution activity</h2></div><span>Persisted in the API</span></div>
      {events.length ? events.slice(0, 100).map(event => <div className="event" key={event.id}><i/>{event.event_type}<time>{new Date(event.created_at).toLocaleTimeString()}</time></div>)
        : <p className="muted">Events appear here for the inspected run.</p>}
    </section>
    </>}
  </main>;
}

function Metric({ label, value }: { label: string; value: number }) {
  return <article><small>{label}</small><strong>{value}</strong></article>;
}

const starterDefinition = {
  name: 'data-processing-pipeline',
  description: 'Generate data, transform dependency output, then calculate a summary.',
  metadata: { audience: 'demo' },
  tasks: [
    { key: 'generate', type: 'demo.generate_data', input: { count: 20, start: 1 }, max_attempts: 2 },
    { key: 'transform', type: 'demo.transform_data', input: { multiplier: 3 }, depends_on: ['generate'], max_attempts: 3 },
    { key: 'summarize', type: 'demo.summarize_data', input: {}, depends_on: ['transform'], max_attempts: 2 },
  ],
};

function WorkflowBuilder({ onInspect }: { onInspect: (id: string) => Promise<void> }) {
  const client = useQueryClient();
  const saved = useQuery({ queryKey: ['workflows'], queryFn: api.workflows });
  const handlers = useQuery({ queryKey: ['task-types'], queryFn: api.taskTypes });
  const [source, setSource] = React.useState(JSON.stringify(starterDefinition, null, 2));
  const [selected, setSelected] = React.useState('');
  const [loaded, setLoaded] = React.useState('');
  const [message, setMessage] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);

  async function loadWorkflow() {
    if (!selected) return;
    setBusy(true); setError('');
    try {
      const item = await api.workflow(selected);
      setSource(JSON.stringify(item.definition, null, 2)); setLoaded(item.id);
      setMessage(`Loaded ${item.name} · version ${item.version}`);
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  function parse() {
    try { return JSON.parse(source) as unknown; }
    catch (err) { throw new Error(`JSON syntax error: ${errorMessage(err)}`); }
  }

  async function validate() {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await api.validateWorkflow(parse());
      setMessage(`Valid workflow · ${result.task_count} tasks`);
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  async function save() {
    setBusy(true); setError(''); setMessage('');
    try {
      const definition = parse();
      await api.validateWorkflow(definition);
      const result = loaded ? await api.createWorkflowVersion(loaded, definition) : await api.createWorkflow(definition);
      setSelected(result.id); setLoaded(result.id);
      setMessage(`Saved ${result.name} · version ${result.version} · ${result.id}`);
      await Promise.all([client.invalidateQueries({ queryKey: ['workflows'] }), client.invalidateQueries({ queryKey: ['dashboard'] })]);
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  async function duplicate() {
    if (!selected) return;
    setBusy(true); setError('');
    try {
      const result = await api.duplicateWorkflow(selected);
      setSelected(result.id); setLoaded(result.id);
      const detail = await api.workflow(result.id); setSource(JSON.stringify(detail.definition, null, 2));
      setMessage(`Duplicated as ${result.name}`);
      await client.invalidateQueries({ queryKey: ['workflows'] });
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  async function runSaved() {
    if (!selected) return;
    setBusy(true); setError('');
    try { const run = await api.startRun(selected); await onInspect(run.id); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  return <section className="panel builder-page"><div className="section-title"><div><small>JSON-FIRST BUILDER</small><h2>Create, validate, version, and run workflows</h2></div><span>Allowlisted handlers only · no user code execution</span></div>
    <p>Edit the definition below. Dependency task outputs are available to downstream handlers in their execution context.</p>
    <div className="builder-toolbar"><select aria-label="Saved definition" value={selected} onChange={event => { setSelected(event.target.value); setLoaded(''); }}>
      <option value="">New workflow</option>{(saved.data ?? []).map(item => <option key={item.id} value={item.id}>{item.name} · v{item.version}</option>)}
    </select><button onClick={() => void loadWorkflow()} disabled={!selected || busy}>Load definition</button><button onClick={() => { setSource(JSON.stringify(starterDefinition, null, 2)); setSelected(''); setLoaded(''); setMessage('Loaded the data pipeline example.'); setError(''); }}>Load example</button>
      <button onClick={() => void validate()} disabled={busy}>Validate</button><button className="primary" onClick={() => void save()} disabled={busy}>Save definition</button>
      <button onClick={() => void runSaved()} disabled={!selected || busy}>Run saved workflow</button><button onClick={() => void duplicate()} disabled={!selected || busy}>Duplicate</button>
    </div>
    <label className="editor-label" htmlFor="workflow-json">Workflow definition JSON</label>
    <textarea id="workflow-json" className="json-editor" spellCheck={false} value={source} onChange={event => { setSource(event.target.value); setMessage(''); setError(''); }}/>
    {message && <div className="notice success" role="status">{message}</div>}{error && <div className="notice error" role="alert">{error}</div>}
    <section className="definition-graph-section"><div className="section-title"><div><small>WORKFLOW DAG</small><h3>Visual execution flow</h3></div><span>Preview from the current JSON definition</span></div>
      <DefinitionGraph source={source}/></section>
    <details className="handler-catalog"><summary>Registered handler types and input schemas ({handlers.data?.length ?? '…'})</summary>
      {(handlers.data ?? []).map(handler => <article key={handler.type}><b>{handler.type}</b><p>{handler.description}</p><small>Input schema</small><pre>{JSON.stringify(handler.input_schema, null, 2)}</pre><small>Output schema</small><pre>{JSON.stringify(handler.output_schema, null, 2)}</pre></article>)}
      {handlers.isError && <p className="task-error">Could not load handler catalog: {errorMessage(handlers.error)}</p>}
    </details>
  </section>;
}

function DefinitionGraph({ source }: { source: string }) {
  let definition: { tasks?: { key?: string; type?: string; depends_on?: string[] }[] } = {};
  try { definition = JSON.parse(source); } catch { /* Show an editor hint while JSON is incomplete. */ }
  const tasks = Array.isArray(definition.tasks) ? definition.tasks : [];
  const depth = new Map<string, number>();
  for (let pass = 0; pass < tasks.length; pass++) {
    let changed = false;
    for (const task of tasks) {
      const key = task.key ?? '';
      const parents = task.depends_on ?? [];
      if (!key || depth.has(key) || parents.some(parent => !depth.has(parent))) continue;
      depth.set(key, parents.length ? Math.max(...parents.map(parent => depth.get(parent) ?? 0)) + 1 : 0);
      changed = true;
    }
    if (!changed) break;
  }
  const laneCounts = new Map<number, number>();
  const nodes: Node[] = tasks.filter(task => !!task.key).map((task, index) => {
    const level = depth.get(task.key!) ?? 0;
    const lane = laneCounts.get(level) ?? 0;
    laneCounts.set(level, lane + 1);
    return { id: task.key!, position: { x: level * 250, y: lane * 110 },
      data: { label: <div className="node-label"><b>{task.key}</b><small>{task.type}</small></div> },
      style: { border: '1px solid #46617d', borderRadius: 10, padding: 12, background: '#111c2a', color: '#eef5ff', width: 205 },
    };
  });
  const keys = new Set(tasks.map(task => task.key));
  const edges: Edge[] = tasks.flatMap(task => (task.depends_on ?? []).filter(parent => keys.has(parent))
    .map(parent => ({ id: `${parent}-${task.key}`, source: parent, target: task.key! })));
  return <div className="flow definition-flow">{nodes.length
    ? <ReactFlow nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.25 }} nodesDraggable={false} nodesConnectable={false}>
      <Background color="#27364a"/><Controls/></ReactFlow>
    : <div className="empty"><strong>{source.trim() ? 'No valid tasks to display' : 'Add tasks to preview the DAG'}</strong><p>Each dependency becomes a directed edge.</p></div>}</div>;
}

function ReliabilityLab({ onInspect }: { onInspect: (id: string) => Promise<void> }) {
  const client = useQueryClient();
  const catalog = useQuery({ queryKey: ['lab-scenarios'], queryFn: api.labScenarios, retry: false });
  const [scenario, setScenario] = React.useState('');
  const [runIds, setRunIds] = React.useState<string[]>([]);
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const result = useQuery({ queryKey: ['lab-result', scenario, runIds],
    queryFn: () => api.labResult(scenario, runIds), enabled: !!scenario && runIds.length > 0, retry: false,
    refetchInterval: query => ['PASS', 'FAIL', 'INCONCLUSIVE'].includes(query.state.data?.status ?? '') ? false : 1000 });
  const selected = catalog.data?.find(item => item.id === scenario);

  async function launch() {
    if (!scenario) return;
    setBusy(true); setError(''); setRunIds([]);
    try {
      const started = await api.startLabScenario(scenario);
      setRunIds(started.runs.map(run => run.id));
      await Promise.all([client.invalidateQueries({ queryKey: ['dashboard'] }), client.invalidateQueries({ queryKey: ['workflows'] })]);
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  async function cancel(id: string) {
    setBusy(true); setError('');
    try { await api.cancelRun(id); await result.refetch(); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  return <section className="panel lab-page"><div className="section-title"><div><small>DEMO &amp; RELIABILITY LAB</small><h2>Exercise persisted engine behavior</h2></div><span>Results are checked against run, task, attempt, and event records.</span></div>
    {catalog.isError && <div className="notice error">Could not load reliability scenarios: {errorMessage(catalog.error)}</div>}
    <div className="lab-controls"><select aria-label="Reliability scenario" value={scenario} onChange={event => { setScenario(event.target.value); setRunIds([]); }}>
      <option value="">Choose a scenario</option>{(catalog.data ?? []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select><button className="primary" onClick={() => void launch()} disabled={!scenario || busy}>{busy ? 'Starting…' : 'Run scenario'}</button></div>
    {selected && <article className="scenario-card"><h3>{selected.name}</h3><p>{selected.description}</p><b>Expected:</b> <span>{selected.expected}</span>{selected.requires && <p className="muted">Environment: {selected.requires}</p>}
      {selected.manual && <p className="muted">This scenario requires a documented operator action; the result remains inconclusive until persisted evidence is present.</p>}</article>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {result.data && <section className="lab-result"><div className="result-heading"><h3>Scenario result</h3><span className={`badge ${result.data.status.toLowerCase()}`}>{result.data.status}</span></div>
      <p>{result.data.expected}</p>{result.data.assertions.map(assertion => <div className="assertion" key={assertion.name}><span>{assertion.passed === true ? 'PASS' : assertion.passed === false ? 'FAIL' : assertion.passed === null ? 'INCONCLUSIVE' : 'WAITING'}</span><b>{assertion.name}</b>{assertion.detail && <small>{assertion.detail}</small>}</div>)}
      {result.data.runs.map(run => <article className="lab-run" key={run.id}><div className="result-heading"><button className="link-button" onClick={() => void onInspect(run.id)}>Inspect run {run.id}</button><span className={`badge ${run.status.toLowerCase()}`}>{run.status}</span></div>
        <div className="recent-runs">{run.tasks.map(task => <div key={task.id}><b>{task.key}</b><span className={`badge ${task.status.toLowerCase()}`}>{task.status}</span><small>{task.attempt_count} attempts{task.error ? ` · ${task.error_classification}: ${task.error}` : ''}</small></div>)}</div>
        <details><summary>Persisted timeline ({run.events.length} events · {run.attempts.length} attempts)</summary>{run.events.map(event => <div className="event" key={event.id}><i/>{event.event_type}<time>{new Date(event.created_at).toLocaleTimeString()}</time></div>)}</details>
        {scenario === 'cancellation' && ['PENDING', 'RUNNING'].includes(run.status) && <button onClick={() => void cancel(run.id)} disabled={busy}>Cancel this run</button>}
      </article>)}
    </section>}
    {result.isError && <div className="notice error">Could not evaluate persisted scenario results: {errorMessage(result.error)}</div>}
    <p className="muted">Worker termination, service restart, and PostgreSQL concurrency must be performed against an isolated PostgreSQL stack. SQLite results for parallel overlap are reported as inconclusive.</p>
  </section>;
}
