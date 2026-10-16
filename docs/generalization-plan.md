# Generalization plan

## Existing shape

- FastAPI exposes versioned workflow/run routes backed by SQLAlchemy and Alembic.
- `WorkflowSpec` already validates bounded DAGs, dependencies, handler names are checked against a small dictionary, and a run stores an immutable definition snapshot.
- Scheduler and worker operate on database rows; PostgreSQL claims use `FOR UPDATE SKIP LOCKED`, attempts/events and leases are persisted, and payment idempotency uses a unique ledger key.
- The React dashboard currently launches only an order-processing sample. It has no saved-workflow selector, builder, generic catalog view, or scenario lab.
- Handler functions have a common call signature but do not receive dependency outputs, and schemas are not centrally described.
- Existing tests exercise API contracts and SQLite pipeline behavior. PostgreSQL claims are opt-in; Docker/PostgreSQL are not available in this environment.

## Implementation sequence

1. Turn the handler map into a typed allowlisted registry with bounded input validation, handler metadata, JSON output checks, and persisted dependency output context.
2. Extend retry configuration and workflow metadata, add version/duplicate/safe-delete/validation/catalog/dashboard API routes, with an Alembic migration.
3. Add generic demo handlers and the data-pipeline definition, preserving the existing order-processing preview.
4. Add a JSON-first Workflow Builder and a Demo & Reliability Lab using actual API-created definitions and persisted run inspections. Report unsupported infrastructure-dependent scenarios as inconclusive.
5. Add an API-driven reliability acceptance runner and tests, update architecture/format/state/lab/testing/benchmark docs, then run backend/frontend checks.
6. Build a clean ZIP, inspect its inventory and secret exclusions, extract to a temporary directory, and verify package integrity and applicable tests.

## Verification boundary

SQLite is suitable for isolated local API/pipeline tests but cannot prove PostgreSQL row-lock concurrency. Worker-kill recovery, multi-worker concurrency, clean PostgreSQL migration, and Docker health will be reported as unverified if infrastructure remains unavailable.
