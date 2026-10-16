# Testing

## Local checks

```powershell
python -m pytest
python -m ruff check backend migrations scripts tests
Set-Location frontend
pnpm test
pnpm build
```

Tests use an isolated SQLite database by default. To opt into PostgreSQL, set `WORKFLOW_TEST_DATABASE_URL` to a dedicated database whose name contains `test`; test fixtures drop and recreate tables. Never point this setting at a database with user data.

The PostgreSQL claim test verifies two workers claim separate ready rows and overlap. It is skipped when SQLite is configured. SQLite cannot validate row locks, multi-process claim races, or Postgres migration semantics.

## Reliability acceptance

Start the API, scheduler, and worker against an isolated development database, then run:

```powershell
make acceptance
```

The runner invokes `/api/v1/lab`, polls persisted state to a deadline, writes structured results to `artifacts/reliability-results.json`, and returns nonzero if a scenario is `FAIL`. A scenario reported `INCONCLUSIVE` does not become a green pass. Select scenarios or a different API with `python scripts/reliability.py --base-url http://localhost:8000 --scenarios retries permanent-failure`.

## Migration check

Compose runs Alembic after PostgreSQL becomes healthy and before API/scheduler/worker. To test a clean database manually, create a fresh database and run `alembic upgrade head`; do not test migrations by deleting a user's persistent database volume.
