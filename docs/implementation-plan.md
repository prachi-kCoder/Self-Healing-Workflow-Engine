# Implementation plan and status

## Architecture

API, scheduler, and workers are separate processes in one modular Python service. PostgreSQL is authoritative. API run creation stores an immutable definition snapshot and task rows in one transaction. The scheduler checks dependencies and retry due times, propagates permanent dependency failures as `SKIPPED`, handles cancellation, and reclaims expired leases. Workers atomically claim rows in short PostgreSQL `FOR UPDATE SKIP LOCKED` transactions, execute allowlisted handlers outside those transactions, heartbeat leases, then conditionally finalize with the lease token. An idempotency ledger protects the demo payment effect. Redis is included for local infrastructure compatibility but is not yet used for event delivery; SSE reads the durable PostgreSQL event journal.

## State model

Task transitions are centralized in `backend/engine.py`. Normal path: `BLOCKED -> PENDING -> READY -> RUNNING -> SUCCEEDED`; attempts can go through `RETRY_WAIT -> READY`; permanent failure is `FAILED`; cancellation and failed dependency propagation end in `CANCELLED` and `SKIPPED`. Workflow runs move from `PENDING` to `RUNNING`, then one terminal state. Definitions are snapshotted on each run.

## Data model

Definitions, runs, task runs, attempts, events, and idempotency records are normalized tables. Task dependencies are serialized on the task row for this small prototype. Unique constraints protect task keys per run and attempt number per task. Alembic owns schema creation.

## Risks and test strategy

Critical risks are concurrent claims, stale worker completion, cancellation while a handler is active, and the boundary between an external side effect and task completion. PostgreSQL row locks and unique constraints protect claims; a UUID lease token rejects stale completions. External effects are at-least-once unless each handler uses an idempotency key. Unit tests cover DAG validation and transitions. Docker-backed integration tests and failure scenarios remain necessary for full recovery verification.

## Milestones

- [x] M0 inspect and document architecture / vertical slice.
- [x] M1 project structure, PostgreSQL/Redis compose, models, Alembic entry point, health API.
- [x] M2 REST workflow creation, dependency scheduling, registered worker handlers, persistent events.
- [ ] M3 harden lease recovery, timeouts, cancellation and idempotency fault scenarios with PostgreSQL integration tests.
- [x] M4 basic TypeScript monitoring graph and event journal; creation/controls and SSE client remain deferred.
- [ ] M5 reliable outbox/Redis broadcast, dashboards and measured benchmark results.
- [ ] M6 container build, migration, end-to-end and multiworker verification.

## First vertical slice

`make up`, `make migrate`, then `make demo`; the seed script creates four tasks with two independent roots, a join before payment, and confirmation after payment. Inspect returned run ID at `http://localhost:8000/runs/{id}` or enter it in the dashboard at `http://localhost:5173`.
