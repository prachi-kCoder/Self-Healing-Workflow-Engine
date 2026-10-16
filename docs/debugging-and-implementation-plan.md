# Debugging findings and implementation plan

## Confirmed findings

- Frontend: React 18 + TypeScript + Vite. Compose serves Vite on port 5173; local preview runs on port 5191. The proxy preserves `/api/v1/...` and forwards to `api:8000` in Compose or `localhost:8000` locally.
- Backend: FastAPI/Uvicorn, API port 8000, with separately runnable scheduler and worker (`python -m backend.app scheduler|worker`).
- The preview initially showed `API unreachable` and stale content. The API/frontend listeners could not be enumerated from the sandbox, and attempted duplicate starts reported ports 8000 and 5191 were already occupied. Reloading the browser tab loaded the current build and health returned ready. An initial transient connection failure plus stale browser content was confirmed; a persistent process outage was not.
- API routes were legacy `/workflows` and `/runs/...`, while the requested stable contract is `/api/v1/...`; frontend and backend had no shared typed client.
- `GET /health` only executed `SELECT 1`, so Compose could mark API healthy before migrations created tables. The API also started in parallel with the one-shot migration service.
- Alembic used the literal PostgreSQL URL from `alembic.ini`, ignoring `DATABASE_URL`; this breaks non-Compose development and test databases.
- A dashboard launch could reuse any same-name definition, including an obsolete definition without the intended retry scenario. It now checks the saved task graph and inputs before reuse and creates the current preview definition when they differ.
- SQLite ignores PostgreSQL's `FOR UPDATE SKIP LOCKED` row-lock behavior. A second local worker could therefore race a claim and hit the unique `(task_id, attempt_number)` constraint. SQLite worker processes are now serialized internally, and local documentation requires one SQLite worker process; Compose uses PostgreSQL for scaled workers.
- The Codex preview was open on port 5191 with stale page content. After reloading, the current Vite build became visible and its `/api/v1/health` request returned ready through the proxy.
- The Docker CLI and PostgreSQL service are unavailable here. Python dependencies and frontend dependencies were installed in workspace-local directories for verification.

## Fix plan

1. Make SQLAlchemy and Alembic use the same `DATABASE_URL`; retain PostgreSQL in Compose and support SQLite for local smoke/integration tests only.
2. Add versioned API routes and typed request/response contracts, readiness checks that validate migrated schema, run/task/attempt/event inspection, retries, and cancellation.
3. Harden worker execution and event persistence, bounded jittered retry timing, lease recovery, cancellation checks, and demo failure injection restricted to development.
4. Centralize frontend calls in a typed client; use the API health endpoint, create or reuse the demo definition, and show real task/attempt/event details with clear request errors.
5. Add API and SQLite pipeline tests; verify frontend typecheck/build and exercise the Vite proxy against an actual API process.

## Verification completed

- Backend: 12 tests passed; the PostgreSQL-only claim-concurrency test was skipped because PostgreSQL is unavailable.
- Ruff: all checks passed.
- Frontend: Vitest (3 tests), strict TypeScript build, and Vite production build passed.
- Browser through `http://localhost:5191`: health became ready; launching from the UI created run `098d9db9-09bc-4523-9a42-6de8379b0df7`, which reached `SUCCEEDED` with 4 tasks, 7 attempts, and 23 events. The payment retry behavior was visible in the dashboard.
- Docker/PostgreSQL were not available, so Compose image startup and PostgreSQL row-lock concurrency remain unverified.

## Verification boundaries

SQLite verifies route contracts and a full retrying task pipeline. PostgreSQL concurrency and Docker health remain unverified because Docker/PostgreSQL is unavailable in this environment.
