"""HTTP control plane and durable execution services."""
import asyncio
import json
import logging
import os
import random
import socket
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from .db import (Attempt, DeadLetter, Definition, Event, Ledger, Run, SagaCompensation,
                 Session, Task, engine, now)
from .engine import TaskStatus, WorkflowSpec, transition
from .handlers import HANDLERS, REGISTRY, catalog, validate_handler_input, validate_output
from .scenarios import SCENARIOS, scenario_definition
from .schemas import (AttemptResponse, EventResponse, RunCreated, RunRequest, RunResponse,
                      TaskResponse, WorkflowDuplicate, WorkflowResponse, WorkflowUpdate)

logger = logging.getLogger("workflow_engine")
CLAIMS = Counter("workflow_task_claims_total", "Task claims")
FAILURES = Counter("workflow_task_failures_total", "Task attempt failures")
LEASE_SECONDS = max(3, int(os.getenv("LEASE_SECONDS", "15")))
MAX_WORKERS = max(1, min(32, int(os.getenv("WORKER_CONCURRENCY", "4"))))
# SQLite has no row-level SKIP LOCKED semantics. Keep a local SQLite worker
# process serial; multi-process concurrency is supported with PostgreSQL.
if engine.url.get_backend_name() == "sqlite":
    MAX_WORKERS = 1
POLL_INTERVAL = max(0.1, float(os.getenv("POLL_INTERVAL_SECONDS", "0.5")))
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()


def duration(started, finished):
    if not started or not finished:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return max(0.0, (finished - started).total_seconds())


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


async def record(session, run_id, event_type, task_id=None, payload=None):
    session.add(Event(run_id=run_id, task_id=task_id, event_type=event_type, payload=payload or {}))


def dead_letter_for(session, task: Task):
    session.add(DeadLetter(run_id=task.run_id, task_id=task.id, task_key=task.task_key,
                           error=task.error or "Task failed", 
                           error_classification=task.error_classification or "PERMANENT",
                           attempt_count=task.attempt_count))
    session.add(Event(run_id=task.run_id, task_id=task.id, event_type="dead_letter.created",
                      payload={"task_key": task.task_key,
                               "error_classification": task.error_classification or "PERMANENT"}))


def transition_to(task: Task, target: TaskStatus):
    transition(TaskStatus(task.status), target)
    task.status = target.value


def task_data(task: Task) -> dict[str, Any]:
    return TaskResponse(
        id=task.id, run_id=task.run_id, key=task.task_key, type=task.task_type,
        status=task.status, input=task.input, output=task.output, dependencies=task.dependencies,
        attempt_count=task.attempt_count, max_attempts=task.max_attempts, worker_id=task.worker_id,
        heartbeat_at=task.heartbeat_at, lease_expires_at=task.lease_expires_at,
        started_at=task.started_at, finished_at=task.finished_at,
        duration_seconds=duration(task.started_at, task.finished_at), error=task.error,
        error_classification=task.error_classification, retry_policy=task.retry_policy,
        retry_base_seconds=task.retry_base_seconds, max_retry_delay_seconds=task.max_retry_delay_seconds,
        retry_on=task.retry_on, compensation_type=task.compensation_type,
        compensation_input=task.compensation_input,
    ).model_dump(mode="json")


def attempt_data(attempt: Attempt) -> dict[str, Any]:
    return AttemptResponse(
        id=attempt.id, task_id=attempt.task_id, attempt_number=attempt.attempt_number,
        worker_id=attempt.worker_id, status=attempt.status, started_at=attempt.started_at,
        finished_at=attempt.finished_at, duration_seconds=duration(attempt.started_at, attempt.finished_at),
        error=attempt.error, error_classification=attempt.error_classification, output=attempt.output,
    ).model_dump(mode="json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting workflow API (environment=%s)", ENVIRONMENT)
    yield
    await engine.dispose()


app = FastAPI(title="Self-Healing Distributed Workflow Engine", version="0.2.0", lifespan=lifespan)
router = APIRouter()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://self-healing-workflow-engine.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request, exc: SQLAlchemyError):
    logger.exception("Database operation failed for %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable or schema is not migrated"})


@app.get("/")
def root():
    return {"message": "Hello"}

@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


async def database_readiness():
    try:
        async with Session() as session:
            await session.execute(select(Definition.id).limit(1))
        return {"status": "ready", "database": "ready"}
    except SQLAlchemyError as exc:
        logger.warning("Readiness check failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable or migrations have not run") from exc


@app.get("/health/ready")
async def readiness():
    return await database_readiness()


@router.get("/health")
async def versioned_health():
    return await database_readiness()


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post("/workflows", response_model=WorkflowResponse, status_code=201)
async def create_workflow(spec: WorkflowSpec):
    unknown = sorted(({task.type for task in spec.tasks} |
                      {task.compensation.type for task in spec.tasks if task.compensation}) - HANDLERS.keys())
    if unknown:
        raise HTTPException(422, detail=f"Unregistered task handler(s): {', '.join(unknown)}")
    tasks = []
    for task in spec.tasks:
        try:
            normalized = validate_handler_input(task.type, task.input)
            if task.compensation:
                if task.compensation.type not in REGISTRY:
                    raise ValueError(f"unregistered compensation handler {task.compensation.type}")
                validate_handler_input(task.compensation.type, task.compensation.input)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        is_fault_handler = task.type in {"demo.fail_once", "demo.fail_n_times", "demo.permanent_failure"}
        payment_fault = task.type == "demo.process_payment" and (
            normalized.get("fail_first", 0) > 0 or normalized.get("crash_after_commit", False))
        if ENVIRONMENT != "development" and (is_fault_handler or payment_fault):
            raise HTTPException(403, detail="Failure injection is only available in development")
        tasks.append(task.model_copy(update={"input": normalized}))
    spec = spec.model_copy(update={"tasks": tasks})
    async with Session() as session:
        definition = Definition(name=spec.name, description=spec.description, version=1,
                                definition=spec.model_dump(mode="json"))
        session.add(definition)
        await session.commit()
        return WorkflowResponse(id=definition.id, name=definition.name, description=definition.description,
                                version=definition.version, definition=definition.definition,
                                created_at=definition.created_at)


@router.get("/workflows", response_model=list[WorkflowResponse])
async def list_workflows():
    async with Session() as session:
        rows = (await session.execute(select(Definition).order_by(Definition.created_at.desc()))).scalars().all()
        return [WorkflowResponse(id=row.id, name=row.name, description=row.description,
                                 version=row.version, created_at=row.created_at) for row in rows]


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(workflow_id: str):
    async with Session() as session:
        definition = await session.get(Definition, workflow_id)
        if not definition:
            raise HTTPException(404, detail="Workflow not found")
        return WorkflowResponse(id=definition.id, name=definition.name, description=definition.description,
                                version=definition.version, definition=definition.definition,
                                created_at=definition.created_at)


@router.get("/task-types")
async def get_task_types():
    return catalog()


@router.post("/workflows/validate")
async def validate_workflow(spec: WorkflowSpec):
    for task in spec.tasks:
        if task.type not in REGISTRY:
            raise HTTPException(422, detail=f"Task {task.key}: unregistered handler {task.type}")
        try:
            validate_handler_input(task.type, task.input)
            if task.compensation:
                if task.compensation.type not in REGISTRY:
                    raise ValueError(f"unregistered compensation handler {task.compensation.type}")
                validate_handler_input(task.compensation.type, task.compensation.input)
        except ValueError as exc:
            raise HTTPException(422, detail=f"Task {task.key}: {exc}") from exc
        if ENVIRONMENT != "development" and task.type in {
            "demo.fail_once", "demo.fail_n_times", "demo.permanent_failure"
        }:
            raise HTTPException(403, detail="Failure injection is only available in development")
        if ENVIRONMENT != "development" and task.type == "demo.process_payment" and (
            task.input.get("fail_first", 0) or task.input.get("crash_after_commit", False)
        ):
            raise HTTPException(403, detail="Failure injection is only available in development")
    return {"valid": True, "task_count": len(spec.tasks)}


@router.post("/workflows/{workflow_id}/versions", response_model=WorkflowResponse, status_code=201)
async def create_workflow_version(workflow_id: str, payload: WorkflowUpdate):
    spec = WorkflowSpec.model_validate(payload.definition)
    unknown = sorted(({task.type for task in spec.tasks} |
                      {task.compensation.type for task in spec.tasks if task.compensation}) - HANDLERS.keys())
    if unknown:
        raise HTTPException(422, detail=f"Unregistered task handler(s): {', '.join(unknown)}")
    for task in spec.tasks:
        try:
            validate_handler_input(task.type, task.input)
            if task.compensation:
                if task.compensation.type not in REGISTRY:
                    raise ValueError(f"Task {task.key}: unregistered compensation handler {task.compensation.type}")
                validate_handler_input(task.compensation.type, task.compensation.input)
        except ValueError as exc:
            raise HTTPException(422, detail=f"Task {task.key}: {exc}") from exc
        if ENVIRONMENT != "development" and task.type in {
            "demo.fail_once", "demo.fail_n_times", "demo.permanent_failure"
        }:
            raise HTTPException(403, detail="Failure injection is only available in development")
        if ENVIRONMENT != "development" and task.type == "demo.process_payment" and (
            task.input.get("fail_first", 0) or task.input.get("crash_after_commit", False)
        ):
            raise HTTPException(403, detail="Failure injection is only available in development")
    async with Session() as session:
        source = await session.get(Definition, workflow_id)
        if not source:
            raise HTTPException(404, detail="Workflow not found")
        latest = await session.scalar(select(func.max(Definition.version)).where(Definition.name == source.name))
        definition = Definition(name=spec.name, description=spec.description, version=(latest or source.version) + 1,
                                definition=spec.model_dump(mode="json"))
        session.add(definition)
        await session.commit()
        return WorkflowResponse(id=definition.id, name=definition.name, description=definition.description,
                                version=definition.version, definition=definition.definition,
                                created_at=definition.created_at)


@router.post("/workflows/{workflow_id}/duplicate", response_model=WorkflowResponse, status_code=201)
async def duplicate_workflow(workflow_id: str, payload: WorkflowDuplicate | None = None):
    async with Session() as session:
        source = await session.get(Definition, workflow_id)
        if not source:
            raise HTTPException(404, detail="Workflow not found")
        spec = dict(source.definition)
        spec["name"] = payload.name if payload and payload.name else f"{source.name} copy"
        definition = Definition(name=spec["name"], description=spec.get("description", ""), version=1,
                                definition=spec)
        session.add(definition)
        await session.commit()
        return WorkflowResponse(id=definition.id, name=definition.name, description=definition.description,
                                version=definition.version, definition=definition.definition,
                                created_at=definition.created_at)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str):
    async with Session() as session:
        definition = await session.get(Definition, workflow_id)
        if not definition:
            raise HTTPException(404, detail="Workflow not found")
        used = await session.scalar(select(func.count(Run.id)).where(Run.definition_id == workflow_id))
        if used:
            raise HTTPException(409, detail="Workflow has run history and cannot be deleted")
        await session.delete(definition)
        await session.commit()
        return Response(status_code=204)


@router.get("/dashboard")
async def dashboard_summary():
    async with Session() as session:
        workflow_count = await session.scalar(select(func.count(Definition.id)))
        run_count = await session.scalar(select(func.count(Run.id)))
        active_count = await session.scalar(select(func.count(Run.id)).where(
            Run.status.in_(["PENDING", "RUNNING", "COMPENSATING"])))
        completed_count = await session.scalar(select(func.count(Run.id)).where(Run.status == "SUCCEEDED"))
        failed_count = await session.scalar(select(func.count(Run.id)).where(Run.status == "FAILED"))
        dead_letter_count = await session.scalar(select(func.count(DeadLetter.id)))
        task_count = await session.scalar(select(func.count(Task.id)))
        attempt_count = await session.scalar(select(func.count(Attempt.id)))
        event_count = await session.scalar(select(func.count(Event.id)))
        recent = (await session.execute(select(Run).order_by(Run.created_at.desc()).limit(10))).scalars().all()
        return {"workflow_count": workflow_count or 0, "run_count": run_count or 0,
                "active_runs": active_count or 0, "completed_runs": completed_count or 0,
                "failed_runs": failed_count or 0, "task_count": task_count or 0,
                "attempt_count": attempt_count or 0, "event_count": event_count or 0,
                "dead_letter_count": dead_letter_count or 0,
                "recent_runs": [{"id": run.id, "status": run.status,
                                 "workflow_name": run.definition_snapshot.get("name", "Workflow"),
                                 "version": (await session.get(Definition, run.definition_id)).version,
                                 "created_at": run.created_at} for run in recent]}


@router.get("/lab/scenarios")
async def list_lab_scenarios():
    return [{"id": scenario_id, "name": spec["name"], "description": spec["description"],
             "expected": spec["expected"], "requires": spec.get("requires"),
             "manual": spec.get("manual", False)} for scenario_id, spec in SCENARIOS.items()]


@router.post("/lab/scenarios/{scenario_id}/runs", status_code=201)
async def start_lab_scenario(scenario_id: str):
    if scenario_id not in SCENARIOS:
        raise HTTPException(404, detail="Unknown reliability scenario")
    if ENVIRONMENT != "development" and scenario_id in {"retries", "permanent-failure", "idempotency"}:
        raise HTTPException(403, detail="Failure-injection scenarios are development-only")
    spec = WorkflowSpec.model_validate(scenario_definition(scenario_id))
    for item in spec.tasks:
        validate_handler_input(item.type, item.input)
    run_count = 2 if scenario_id == "concurrent" else 1
    async with Session() as session:
        definition = Definition(name=spec.name, description=spec.description, version=1,
                                definition=spec.model_dump(mode="json"))
        session.add(definition)
        await session.flush()
        created = []
        for _ in range(run_count):
            scenario_key = f"lab-idempotency-{uuid4()}" if scenario_id == "idempotency" else None
            run = Run(definition_id=definition.id, definition_snapshot=definition.definition,
                      input={"scenario_id": scenario_id, **({"operation_key": scenario_key} if scenario_key else {})},
                      status="PENDING")
            session.add(run)
            await session.flush()
            for task_spec in spec.tasks:
                task_input = dict(task_spec.input)
                if scenario_key and task_spec.type == "demo.process_payment":
                    task_input["operation_key"] = scenario_key
                session.add(Task(run_id=run.id, task_key=task_spec.key, task_type=task_spec.type,
                                 status=TaskStatus.PENDING.value if not task_spec.depends_on else TaskStatus.BLOCKED.value,
                                 input=task_input, dependencies=task_spec.depends_on,
                                 max_attempts=task_spec.max_attempts, timeout_seconds=task_spec.timeout_seconds,
                                 retry_base_seconds=task_spec.retry_base_seconds,
                                 retry_policy=task_spec.retry_policy,
                                 max_retry_delay_seconds=task_spec.max_retry_delay_seconds,
                                 retry_on=task_spec.retry_on,
                                 compensation_type=task_spec.compensation.type if task_spec.compensation else None,
                                 compensation_input=task_spec.compensation.input if task_spec.compensation else {}))
            await record(session, run.id, "workflow.created", payload={"scenario_id": scenario_id,
                                                                        "definition_id": definition.id})
            created.append({"id": run.id, "status": run.status})
        await session.commit()
        return {"scenario_id": scenario_id, "definition_id": definition.id, "runs": created}


@router.get("/lab/scenarios/{scenario_id}/results")
async def lab_scenario_results(scenario_id: str, run_ids: list[str] = Query(min_length=1)):
    if scenario_id not in SCENARIOS:
        raise HTTPException(404, detail="Unknown reliability scenario")
    async with Session() as session:
        runs = []
        for run_id in run_ids:
            run = await session.get(Run, run_id)
            if not run or run.input.get("scenario_id") != scenario_id:
                raise HTTPException(404, detail=f"Run {run_id} does not belong to scenario {scenario_id}")
            runs.append(await run_data(session, run))
        spec = SCENARIOS[scenario_id]
        status = "RUNNING"
        assertions: list[dict[str, Any]] = []
        all_terminal = all(run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"} for run in runs)
        if scenario_id == "sequential":
            run = runs[0]
            tasks = {task["key"]: task for task in run["tasks"]}
            ordered = (all(task["status"] == "SUCCEEDED" for task in tasks.values()) and
                       tasks["start"]["finished_at"] <= tasks["process"]["started_at"] and
                       tasks["process"]["finished_at"] <= tasks["finish"]["started_at"])
            assertions.append({"name": "dependency ordering persisted", "passed": ordered})
        elif scenario_id == "parallel":
            if engine.url.get_backend_name() == "sqlite" and all_terminal:
                status = "INCONCLUSIVE"
                assertions.append({"name": "branch overlap", "passed": None,
                                   "detail": "SQLite is serial-only in this development setup."})
            elif all_terminal:
                tasks = {task["key"]: task for task in runs[0]["tasks"]}
                a, b, join = tasks["branch_a"], tasks["branch_b"], tasks["join"]
                overlap = (a["started_at"] < b["finished_at"] and b["started_at"] < a["finished_at"]
                           and join["started_at"] >= max(a["finished_at"], b["finished_at"]))
                assertions.append({"name": "independent branches overlap and join waits", "passed": overlap})
        elif scenario_id == "retries" and all_terminal:
            run = runs[0]
            task = next(task for task in run["tasks"] if task["key"] == "flaky")
            attempts = [a for a in run["attempts"] if a["task_id"] == task["id"]]
            retry_events = [e for e in run["events"] if e["event_type"] == "task.retry_scheduled"]
            valid = (run["status"] == "SUCCEEDED" and len(attempts) == 3 and
                     [a["status"] for a in attempts] == ["FAILED", "FAILED", "SUCCEEDED"] and
                     len(retry_events) == 2)
            assertions.append({"name": "two retries then success", "passed": valid,
                               "attempt_count": len(attempts), "retry_event_count": len(retry_events)})
        elif scenario_id == "permanent-failure" and all_terminal:
            run = runs[0]
            tasks = {task["key"]: task for task in run["tasks"]}
            valid = (run["status"] == "FAILED" and tasks["reject"]["status"] == "FAILED" and
                     tasks["reject"]["error_classification"] == "PERMANENT" and
                     tasks["must_not_run"]["status"] == "SKIPPED")
            assertions.append({"name": "permanent error skips dependent", "passed": valid})
        elif scenario_id == "idempotency" and all_terminal:
            run = runs[0]
            task = next(task for task in run["tasks"] if task["key"] == "payment")
            attempts = [a for a in run["attempts"] if a["task_id"] == task["id"]]
            ledger_count = await session.scalar(select(func.count()).select_from(Ledger).where(
                Ledger.operation_key == run["input"]["operation_key"]))
            valid = run["status"] == "SUCCEEDED" and len(attempts) == 2 and ledger_count == 1
            assertions.append({"name": "single payment ledger entry reused on retry", "passed": valid,
                               "attempt_count": len(attempts), "ledger_count": ledger_count})
        elif scenario_id == "concurrent" and all_terminal and len(runs) == 2:
            valid = (all(run["status"] == "SUCCEEDED" for run in runs) and
                     runs[0]["tasks"][0]["id"] != runs[1]["tasks"][0]["id"] and
                     set(event["id"] for event in runs[0]["events"]).isdisjoint(
                         {event["id"] for event in runs[1]["events"]}))
            assertions.append({"name": "runs have isolated persisted task and event identities", "passed": valid})
        elif scenario_id == "cancellation":
            assertions.append({"name": "run cancelled", "passed": runs[0]["status"] == "CANCELLED"})
        elif scenario_id in {"worker-recovery", "restart-recovery"}:
            if scenario_id == "restart-recovery":
                assertions.append({"name": "service restart verified", "passed": None,
                                   "detail": "A service restart cannot be inferred from persisted workflow events."})
                status = "INCONCLUSIVE"
            elif all_terminal:
                has_recovery = any(event["event_type"] == "task.lease_expired" for event in runs[0]["events"])
                recovered = (has_recovery and runs[0]["status"] == "SUCCEEDED" and
                             any(task["status"] == "SUCCEEDED" and task["attempt_count"] >= 2
                                 for task in runs[0]["tasks"]))
                assertions.append({"name": "lease expiry and task reclamation", "passed": True if recovered else None,
                                   "detail": "Stop the isolated worker process; no recovery evidence is currently persisted."
                                   if not recovered else None})
                status = "PASS" if recovered else "INCONCLUSIVE"
        if status != "INCONCLUSIVE":
            if assertions and all(item["passed"] is True for item in assertions):
                status = "PASS"
            elif all_terminal:
                status = "FAIL"
        return {"scenario_id": scenario_id, "status": status,
                "expected": spec["expected"], "assertions": assertions,
                "runs": [{"id": run["id"], "status": run["status"],
                          "tasks": run["tasks"], "attempts": run["attempts"], "events": run["events"]}
                         for run in runs]}


@router.post("/workflows/{workflow_id}/runs", response_model=RunCreated, status_code=201)
async def create_run(workflow_id: str, payload: RunRequest | None = None):
    async with Session() as session:
        definition = await session.get(Definition, workflow_id)
        if not definition:
            raise HTTPException(404, detail="Workflow not found")
        spec = WorkflowSpec.model_validate(definition.definition)
        run = Run(definition_id=definition.id, definition_snapshot=definition.definition,
                  input=(payload.input if payload else {}), status="PENDING")
        session.add(run)
        await session.flush()
        for item in spec.tasks:
            session.add(Task(
                run_id=run.id, task_key=item.key, task_type=item.type,
                status=TaskStatus.PENDING.value if not item.depends_on else TaskStatus.BLOCKED.value,
                input=item.input, dependencies=item.depends_on, max_attempts=item.max_attempts,
                timeout_seconds=item.timeout_seconds, retry_base_seconds=item.retry_base_seconds,
                retry_policy=item.retry_policy, max_retry_delay_seconds=item.max_retry_delay_seconds,
                retry_on=item.retry_on,
                compensation_type=item.compensation.type if item.compensation else None,
                compensation_input=item.compensation.input if item.compensation else {},
            ))
        await record(session, run.id, "workflow.created", payload={"definition_id": definition.id})
        await session.commit()
        return RunCreated(id=run.id, status=run.status, definition_id=definition.id)


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
                    status: str | None = None):
    async with Session() as session:
        query = select(Run).order_by(Run.created_at.desc()).offset(offset).limit(limit)
        if status:
            query = query.where(Run.status == status.upper())
        runs = (await session.execute(query)).scalars().all()
        return [await run_data(session, run) for run in runs]


async def run_data(session, run: Run) -> dict[str, Any]:
    definition = await session.get(Definition, run.definition_id)
    tasks = (await session.execute(select(Task).where(Task.run_id == run.id).order_by(Task.task_key))).scalars().all()
    attempts = (await session.execute(select(Attempt).join(Task).where(Task.run_id == run.id)
                                      .order_by(Attempt.started_at, Attempt.attempt_number))).scalars().all()
    events = (await session.execute(select(Event).where(Event.run_id == run.id)
                                    .order_by(Event.created_at, Event.id))).scalars().all()
    compensations = (await session.execute(select(SagaCompensation).where(
        SagaCompensation.run_id == run.id).order_by(SagaCompensation.sequence))).scalars().all()
    return RunResponse(
        id=run.id, definition_id=run.definition_id, definition_version=definition.version if definition else 1,
        status=run.status, input=run.input,
        output=run.output, created_at=run.created_at, started_at=run.started_at,
        finished_at=run.finished_at, cancel_requested=run.cancel_requested,
        definition_snapshot=run.definition_snapshot, tasks=[task_data(t) for t in tasks],
        attempts=[attempt_data(a) for a in attempts],
        events=[EventResponse(id=e.id, run_id=e.run_id, task_id=e.task_id, event_type=e.event_type,
                              payload=e.payload, created_at=e.created_at).model_dump(mode="json") for e in events],
        compensations=[{"id": c.id, "task_id": c.task_id, "task_key": c.task_key,
                        "handler_type": c.handler_type, "status": c.status,
                        "attempt_count": c.attempt_count, "output": c.output, "error": c.error}
                       for c in compensations],
    ).model_dump(mode="json")


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str):
    async with Session() as session:
        run = await session.get(Run, run_id)
        if not run:
            raise HTTPException(404, detail="Run not found")
        return await run_data(session, run)


@router.get("/dead-letters")
async def list_dead_letters(limit: int = Query(default=100, ge=1, le=500)):
    async with Session() as session:
        rows = (await session.execute(select(DeadLetter).order_by(
            DeadLetter.created_at.desc()).limit(limit))).scalars().all()
        return [{"id": row.id, "run_id": row.run_id, "task_id": row.task_id,
                 "task_key": row.task_key, "error": row.error,
                 "error_classification": row.error_classification,
                 "attempt_count": row.attempt_count, "created_at": row.created_at}
                for row in rows]


@router.websocket("/runs/{run_id}/ws")
async def watch_run(websocket: WebSocket, run_id: str):
    """Stream the latest durable run snapshot whenever its event journal changes."""
    await websocket.accept()
    last_event_count = None
    try:
        while True:
            async with Session() as session:
                run = await session.get(Run, run_id)
                if not run:
                    await websocket.send_json({"error": "Run not found"})
                    await websocket.close(code=4404)
                    return
                event_count = await session.scalar(
                    select(func.count(Event.id)).where(Event.run_id == run_id)
                )
                if last_event_count is None or event_count != last_event_count:
                    await websocket.send_json(await run_data(session, run))
                    last_event_count = event_count
                if run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    return
            await asyncio.sleep(POLL_INTERVAL)
    except WebSocketDisconnect:
        return


@router.get("/runs/{run_id}/compare/{other_run_id}")
async def compare_runs(run_id: str, other_run_id: str):
    async with Session() as session:
        left, right = await session.get(Run, run_id), await session.get(Run, other_run_id)
        if not left or not right:
            raise HTTPException(404, detail="Both runs must exist")
        if left.definition_id != right.definition_id:
            raise HTTPException(409, detail="Runs must use the same workflow definition version")
        left_data, right_data = await run_data(session, left), await run_data(session, right)
        l_tasks = {task["key"]: task for task in left_data["tasks"]}
        r_tasks = {task["key"]: task for task in right_data["tasks"]}
        return {"definition_id": left.definition_id, "left": {"id": left.id, "status": left.status},
                "right": {"id": right.id, "status": right.status},
                "tasks": [{"key": key, "left_status": l_tasks[key]["status"],
                           "right_status": r_tasks[key]["status"],
                           "left_attempts": l_tasks[key]["attempt_count"],
                           "right_attempts": r_tasks[key]["attempt_count"],
                           "left_duration_seconds": l_tasks[key]["duration_seconds"],
                           "right_duration_seconds": r_tasks[key]["duration_seconds"]}
                          for key in sorted(l_tasks.keys() | r_tasks.keys())
                          if key in l_tasks and key in r_tasks]}


@router.get("/runs/{run_id}/tasks", response_model=list[TaskResponse])
async def get_run_tasks(run_id: str):
    async with Session() as session:
        if not await session.get(Run, run_id):
            raise HTTPException(404, detail="Run not found")
        tasks = (await session.execute(select(Task).where(Task.run_id == run_id).order_by(Task.task_key))).scalars().all()
        return [task_data(task) for task in tasks]


@router.get("/runs/{run_id}/events", response_model=list[EventResponse])
async def get_run_events(run_id: str):
    async with Session() as session:
        if not await session.get(Run, run_id):
            raise HTTPException(404, detail="Run not found")
        events = (await session.execute(select(Event).where(Event.run_id == run_id)
                                        .order_by(Event.created_at, Event.id))).scalars().all()
        return [EventResponse(id=e.id, run_id=e.run_id, task_id=e.task_id, event_type=e.event_type,
                              payload=e.payload, created_at=e.created_at) for e in events]


@router.get("/tasks/{task_id}/attempts", response_model=list[AttemptResponse])
async def get_task_attempts(task_id: str):
    async with Session() as session:
        if not await session.get(Task, task_id):
            raise HTTPException(404, detail="Task not found")
        attempts = (await session.execute(select(Attempt).where(Attempt.task_id == task_id)
                                          .order_by(Attempt.attempt_number))).scalars().all()
        return [attempt_data(attempt) for attempt in attempts]


async def reopen_failed_run(session, run: Run, tasks: list[Task], failed_ids: set[str] | None = None):
    if run.status == "COMPENSATING":
        raise HTTPException(409, detail="Saga compensation is already in progress")
    failed = [task for task in tasks if task.status == TaskStatus.FAILED and
              (failed_ids is None or task.id in failed_ids)]
    if not failed:
        raise HTTPException(409, detail="Run has no failed tasks")
    await session.execute(delete(SagaCompensation).where(SagaCompensation.run_id == run.id))
    for task in failed:
        transition_to(task, TaskStatus.READY)
        task.error, task.finished_at = None, None
        task.max_attempts += 1  # An explicit manual retry grants one additional attempt.
    for task in tasks:
        if task.status == TaskStatus.SKIPPED:
            transition_to(task, TaskStatus.BLOCKED if task.dependencies else TaskStatus.PENDING)
            task.error, task.finished_at = None, None
    run.status, run.finished_at, run.cancel_requested = "RUNNING", None, False
    await record(session, run.id, "workflow.retry_requested")


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    async with Session() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()
        if not task:
            raise HTTPException(404, detail="Task not found")
        if task.status != TaskStatus.FAILED:
            raise HTTPException(409, detail="Only failed tasks can be retried")
        run = await session.get(Run, task.run_id)
        tasks = (await session.execute(select(Task).where(Task.run_id == task.run_id))).scalars().all()
        await reopen_failed_run(session, run, tasks, {task_id})
        await session.commit()
        return {"task_id": task.id, "status": task.status, "run_status": run.status}


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str):
    async with Session() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id).with_for_update())).scalar_one_or_none()
        if not run:
            raise HTTPException(404, detail="Run not found")
        if run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise HTTPException(409, detail="Run is already complete")
        if run.status == "COMPENSATING":
            raise HTTPException(409, detail="Saga compensation is already in progress")
        run.cancel_requested = True
        await record(session, run.id, "workflow.cancel_requested")
        await session.commit()
        return {"id": run.id, "status": run.status, "cancel_requested": True}


@router.get("/runs/{run_id}/events/stream", include_in_schema=False)
async def stream_events(run_id: str):
    async with Session() as session:
        if not await session.get(Run, run_id):
            raise HTTPException(404, detail="Run not found")

    async def stream():
        last = None
        while True:
            async with Session() as session:
                query = select(Event).where(Event.run_id == run_id)
                if last:
                    query = query.where(Event.created_at > last)
                events = (await session.execute(query.order_by(Event.created_at, Event.id))).scalars().all()
                for event in events:
                    last = event.created_at
                    yield f"data: {json.dumps(EventResponse(id=event.id, run_id=event.run_id, task_id=event.task_id, event_type=event.event_type, payload=event.payload, created_at=event.created_at).model_dump(mode='json'))}\n\n"
                run = await session.get(Run, run_id)
                if not run or run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    break
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str):
    async with Session() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id).with_for_update())).scalar_one_or_none()
        if not run:
            raise HTTPException(404, detail="Run not found")
        tasks = (await session.execute(select(Task).where(Task.run_id == run_id))).scalars().all()
        await reopen_failed_run(session, run, tasks)
        await session.commit()
        return {"id": run.id, "status": run.status}


@app.get("/health")
async def legacy_health():
    return await database_readiness()


@app.get("/ready")
async def legacy_ready():
    return await database_readiness()


# Keep the original routes available for existing scripts while the UI uses the versioned contract.
app.include_router(router, prefix="/api/v1")
app.include_router(router, include_in_schema=False)


async def schedule_once():
    async with Session() as session:
        candidates = (await session.execute(select(Task).where(Task.status.in_(
            [TaskStatus.BLOCKED.value, TaskStatus.PENDING.value, TaskStatus.RETRY_WAIT.value]
        )))).scalars().all()
        touched_runs = set()
        for task in candidates:
            run = await session.get(Run, task.run_id)
            if not run or run.cancel_requested:
                continue
            parents = (await session.execute(select(Task).where(Task.run_id == task.run_id,
                                          Task.task_key.in_(task.dependencies)))).scalars().all()
            parent_status = {parent.task_key: parent.status for parent in parents}
            if any(status in {TaskStatus.FAILED.value, TaskStatus.SKIPPED.value, TaskStatus.CANCELLED.value}
                   for status in parent_status.values()):
                transition_to(task, TaskStatus.SKIPPED)
                task.error = "A required dependency did not succeed"
                await record(session, task.run_id, "task.skipped_dependency_failed", task.id)
                touched_runs.add(task.run_id)
            elif len(parent_status) == len(task.dependencies) and all(
                status == TaskStatus.SUCCEEDED.value for status in parent_status.values()
            ):
                due = task.status != TaskStatus.RETRY_WAIT.value or (task.retry_at and as_utc(task.retry_at) <= now())
                if due:
                    transition_to(task, TaskStatus.READY)
                    task.retry_at = None
                    await record(session, task.run_id, "task.ready", task.id)
                    touched_runs.add(task.run_id)
        runs = (await session.execute(select(Run).where(
            Run.status.in_(["PENDING", "RUNNING", "COMPENSATING"])))).scalars().all()
        for run in runs:
            tasks = (await session.execute(select(Task).where(Task.run_id == run.id))).scalars().all()
            if run.status == "COMPENSATING":
                compensations = (await session.execute(select(SagaCompensation).where(
                    SagaCompensation.run_id == run.id))).scalars().all()
                if compensations and all(item.status in {"SUCCEEDED", "FAILED"} for item in compensations):
                    run.status, run.finished_at = "FAILED", now()
                    terminal_event = "workflow.compensation_failed" if any(
                        item.status == "FAILED" for item in compensations) else "workflow.compensated"
                    await record(session, run.id, terminal_event)
                continue
            if run.cancel_requested:
                for task in tasks:
                    if task.status not in {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value,
                                           TaskStatus.CANCELLED.value, TaskStatus.SKIPPED.value}:
                        transition_to(task, TaskStatus.CANCELLED)
                        task.worker_id = task.lease_token = None
                        task.lease_expires_at = None
                run.status, run.finished_at = "CANCELLED", now()
                await record(session, run.id, "workflow.cancelled")
            elif tasks and all(task.status in {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value,
                                               TaskStatus.CANCELLED.value, TaskStatus.SKIPPED.value}
                               for task in tasks):
                failed = any(task.status == TaskStatus.FAILED.value for task in tasks)
                if failed:
                    completed = [task for task in tasks if task.status == TaskStatus.SUCCEEDED.value
                                 and task.compensation_type]
                    completed.sort(key=lambda task: task.finished_at or task.started_at or now(), reverse=True)
                    for sequence, task in enumerate(completed):
                        session.add(SagaCompensation(run_id=run.id, task_id=task.id,
                            task_key=task.task_key, handler_type=task.compensation_type,
                            sequence=sequence, input=task.compensation_input,
                            status="PENDING", attempt_count=0,
                            output={}))
                    if completed:
                        run.status = "COMPENSATING"
                        await record(session, run.id, "workflow.compensation_started",
                                     payload={"tasks": [task.task_key for task in completed]})
                    else:
                        run.status, run.finished_at = "FAILED", now()
                        await record(session, run.id, "workflow.failed")
                else:
                    run.status, run.finished_at = "SUCCEEDED", now()
                    await record(session, run.id, "workflow.succeeded")
            elif any(task.status in {TaskStatus.READY.value, TaskStatus.RUNNING.value,
                                     TaskStatus.RETRY_WAIT.value, TaskStatus.SUCCEEDED.value} for task in tasks):
                run.status = "RUNNING"
                if not run.started_at:
                    run.started_at = now()
        await session.commit()


async def worker_once(worker_id: str | None = None):
    worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:8]}"
    async with Session() as session:
        statement = (select(Task).join(Run).where(Task.status == TaskStatus.READY.value,
                      Run.cancel_requested.is_(False)).order_by(Task.run_id, Task.task_key)
                     .with_for_update(skip_locked=True).limit(1))
        task = (await session.execute(statement)).scalar_one_or_none()
        if task is None:
            return False
        run = await session.get(Run, task.run_id)
        if not run or run.cancel_requested:
            return False
        token = str(uuid4())
        transition_to(task, TaskStatus.RUNNING)
        task.attempt_count += 1
        task.worker_id, task.lease_token = worker_id, token
        task.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
        task.heartbeat_at = now()
        task.started_at = task.started_at or now()
        if not run.started_at:
            run.started_at = now()
            run.status = "RUNNING"
        attempt = Attempt(task_id=task.id, attempt_number=task.attempt_count,
                          worker_id=worker_id, status="RUNNING")
        session.add(attempt)
        await record(session, task.run_id, "task.started", task.id,
                     {"attempt": task.attempt_count, "worker": worker_id})
        deps = (await session.execute(select(Task).where(
            Task.run_id == task.run_id, Task.task_key.in_(task.dependencies)
        ))).scalars().all()
        dependency_outputs = {parent.task_key: parent.output for parent in deps}
        await session.commit()
        claim = (task.id, task.run_id, task.task_type, task.input, task.attempt_count,
                 task.timeout_seconds, token, attempt.id, dependency_outputs)

    task_id, run_id, task_type, task_input, attempt_number, timeout, token, attempt_id, dependency_outputs = claim
    beat = asyncio.create_task(heartbeat(task_id, token))
    try:
        async with Session() as handler_session:
            result = await asyncio.wait_for(
                HANDLERS[task_type](task_input, {"session": handler_session, "task_id": task_id,
                                                "attempt": attempt_number, "run_id": run_id,
                                                "dependencies": dependency_outputs}), timeout=timeout,
            )
            result = validate_output(task_type, result)
            await handler_session.rollback()
        async with Session() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()
            run = await session.get(Run, run_id)
            if not task or task.lease_token != token or task.status != TaskStatus.RUNNING.value or not run or run.cancel_requested:
                return True
            attempt = await session.get(Attempt, attempt_id)
            transition_to(task, TaskStatus.SUCCEEDED)
            task.output, task.error, task.error_classification, task.finished_at = result, None, None, now()
            task.worker_id = task.lease_token = None
            task.lease_expires_at = None
            attempt.status, attempt.finished_at, attempt.output = "SUCCEEDED", now(), result
            await record(session, run_id, "task.succeeded", task_id, {"attempt": attempt_number})
            await session.commit()
    except Exception as exc:
        FAILURES.inc()
        error = f"Task timed out after {timeout} seconds" if isinstance(exc, asyncio.TimeoutError) else str(exc)
        permanent = getattr(exc, "permanent", False)
        async with Session() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()
            if task and task.lease_token == token and task.status == TaskStatus.RUNNING.value:
                attempt = await session.get(Attempt, attempt_id)
                attempt.status, attempt.finished_at, attempt.error = "FAILED", now(), error[:2000]
                classification = "TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else (
                    "PERMANENT" if permanent else "RETRYABLE")
                attempt.error_classification = classification
                task.error = error[:2000]
                task.error_classification = classification
                retry_allowed = classification in task.retry_on and not permanent
                if task.attempt_count < task.max_attempts and retry_allowed:
                    exponent = task.attempt_count - 1 if task.retry_policy == "exponential" else 0
                    delay = min(task.max_retry_delay_seconds, task.retry_base_seconds * (2 ** exponent))
                    jitter = random.uniform(0, min(1.0, delay * 0.2))
                    delay += jitter
                    transition_to(task, TaskStatus.RETRY_WAIT)
                    task.retry_at = now() + timedelta(seconds=delay)
                    event, extra = "task.retry_scheduled", {"retry_in_seconds": round(delay, 3)}
                else:
                    transition_to(task, TaskStatus.FAILED)
                    task.finished_at = now()
                    dead_letter_for(session, task)
                    event, extra = "task.failed", {"attempts_exhausted": task.attempt_count >= task.max_attempts}
                task.worker_id = task.lease_token = None
                task.lease_expires_at = None
                await record(session, run_id, event, task_id,
                             {"attempt": attempt_number, "error": error[:2000], **extra})
                await session.commit()
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
    CLAIMS.inc()
    return True


async def heartbeat(task_id: str, token: str):
    while True:
        await asyncio.sleep(max(1.0, LEASE_SECONDS / 3))
        async with Session() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()
            run = await session.get(Run, task.run_id) if task else None
            if not task or not run or run.cancel_requested or task.lease_token != token or task.status != TaskStatus.RUNNING.value:
                return
            task.heartbeat_at, task.lease_expires_at = now(), now() + timedelta(seconds=LEASE_SECONDS)
            await session.commit()


async def recover_expired_leases():
    async with Session() as session:
        tasks = (await session.execute(select(Task).where(Task.status == TaskStatus.RUNNING.value,
                      Task.lease_expires_at < now()).with_for_update(skip_locked=True))).scalars().all()
        for task in tasks:
            old_worker = task.worker_id
            attempt = (await session.execute(select(Attempt).where(
                Attempt.task_id == task.id, Attempt.attempt_number == task.attempt_count
            ))).scalar_one_or_none()
            if attempt and attempt.status == "RUNNING":
                attempt.status, attempt.finished_at, attempt.error = "FAILED", now(), "Worker lease expired"
                attempt.error_classification = "LEASE_EXPIRED"
            if task.attempt_count >= task.max_attempts:
                transition_to(task, TaskStatus.FAILED)
                task.error, task.finished_at = "Worker lease expired; attempt limit reached", now()
                task.error_classification = "LEASE_EXPIRED"
                dead_letter_for(session, task)
                event, payload = "task.failed", {"reason": "lease_expired", "worker": old_worker}
            elif "LEASE_EXPIRED" not in task.retry_on:
                transition_to(task, TaskStatus.FAILED)
                task.error = "Worker lease expired; retry disabled by policy"
                task.finished_at = now()
                task.error_classification = "LEASE_EXPIRED"
                dead_letter_for(session, task)
                event, payload = "task.failed", {"reason": "lease_expired", "worker": old_worker,
                                                   "retry_disabled": True}
            else:
                transition_to(task, TaskStatus.RETRY_WAIT)
                task.error = "Previous worker lease expired"
                task.error_classification = "LEASE_EXPIRED"
                exponent = task.attempt_count - 1 if task.retry_policy == "exponential" else 0
                delay = min(task.max_retry_delay_seconds, task.retry_base_seconds * (2 ** exponent))
                delay = min(task.max_retry_delay_seconds, delay + random.uniform(0, min(1.0, delay * 0.2)))
                task.retry_at = now() + timedelta(seconds=delay)
                event, payload = "task.lease_expired", {"worker": old_worker, "retry_in_seconds": round(delay, 3)}
            task.worker_id = task.lease_token = None
            task.lease_expires_at = None
            await record(session, task.run_id, event, task.id, payload)
        await session.commit()


async def scheduler_loop():
    logger.info("Scheduler started (poll interval %.2fs)", POLL_INTERVAL)
    while True:
        try:
            await recover_expired_leases()
            await schedule_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed; will retry")
        await asyncio.sleep(POLL_INTERVAL)


async def worker_loop():
    logger.info("Worker started (concurrency %s)", MAX_WORKERS)
    active: set[asyncio.Task] = set()
    while True:
        active = {job for job in active if not job.done()}
        while len(active) < MAX_WORKERS:
            job = asyncio.create_task(safe_worker_once())
            active.add(job)
            await asyncio.sleep(0)
            if job.done() and not job.result():
                break
        if active:
            await asyncio.wait(active, timeout=POLL_INTERVAL, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(POLL_INTERVAL)


async def safe_worker_once():
    try:
        worked = await worker_once()
        return worked if worked else await compensation_once()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Worker iteration failed; will reconnect and retry")
        await asyncio.sleep(POLL_INTERVAL)
        return False


async def compensation_once(worker_id: str | None = None):
    worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:8]}"
    async with Session() as session:
        candidate = None
        saga_runs = (await session.execute(select(Run).where(Run.status == "COMPENSATING")
            .order_by(Run.created_at).with_for_update(skip_locked=True).limit(MAX_WORKERS))).scalars().all()
        for saga_run in saga_runs:
            candidate = (await session.execute(select(SagaCompensation).where(
                SagaCompensation.run_id == saga_run.id,
                SagaCompensation.status.not_in(["SUCCEEDED", "FAILED"])
            ).order_by(SagaCompensation.sequence).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
            if candidate:
                break
        if not candidate:
            return False
        if candidate.status == "RUNNING" and candidate.lease_expires_at and candidate.lease_expires_at > now():
            return False
        if candidate.status == "PENDING" and candidate.retry_at and candidate.retry_at > now():
            return False
        if candidate.attempt_count >= 3:
            candidate.status, candidate.finished_at = "FAILED", now()
            candidate.error = candidate.error or "Compensation worker lease expired"
            original = await session.get(Task, candidate.task_id)
            if original:
                session.add(DeadLetter(run_id=candidate.run_id, task_id=original.id,
                    task_key=original.task_key, error=f"Compensation failed: {candidate.error}",
                    error_classification="COMPENSATION", attempt_count=candidate.attempt_count))
                await record(session, candidate.run_id, "dead_letter.created", original.id,
                             {"task_key": original.task_key, "error_classification": "COMPENSATION"})
            await record(session, candidate.run_id, "saga.compensation_failed", candidate.task_id,
                         {"compensation": candidate.handler_type, "error": candidate.error})
            await session.commit()
            return True
        candidate.status = "RUNNING"
        candidate.attempt_count += 1
        candidate.started_at = candidate.started_at or now()
        candidate.retry_at = None
        candidate.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
        original = await session.get(Task, candidate.task_id)
        claim = (candidate.id, candidate.run_id, candidate.task_id, candidate.handler_type,
                 candidate.input, candidate.task_key, candidate.attempt_count,
                 original.output if original else {})
        await record(session, candidate.run_id, "saga.compensation_started", candidate.task_id,
                     {"task": candidate.task_key, "handler": candidate.handler_type,
                      "attempt": candidate.attempt_count, "worker": worker_id})
        await session.commit()

    comp_id, run_id, task_id, handler_type, handler_input, task_key, attempt, original_output = claim
    try:
        async with Session() as handler_session:
            result = await asyncio.wait_for(HANDLERS[handler_type](handler_input, {
                "session": handler_session, "task_id": task_id, "run_id": run_id,
                "attempt": attempt, "dependencies": {task_key: original_output},
                "compensation": True, "idempotency_key": f"{run_id}:{task_id}:compensation",
            }), timeout=30)
            result = validate_output(handler_type, result)
            await handler_session.rollback()
        async with Session() as session:
            compensation = (await session.execute(select(SagaCompensation).where(
                SagaCompensation.id == comp_id).with_for_update())).scalar_one_or_none()
            if compensation and compensation.status == "RUNNING":
                compensation.status, compensation.output = "SUCCEEDED", result
                compensation.finished_at, compensation.lease_expires_at = now(), None
                await record(session, run_id, "saga.compensation_succeeded", task_id,
                             {"task": task_key, "handler": handler_type, "attempt": attempt})
                await session.commit()
    except Exception as exc:
        error = str(exc)[:2000]
        async with Session() as session:
            compensation = (await session.execute(select(SagaCompensation).where(
                SagaCompensation.id == comp_id).with_for_update())).scalar_one_or_none()
            if compensation and compensation.status == "RUNNING":
                permanent = getattr(exc, "permanent", False)
                compensation.error, compensation.lease_expires_at = error, None
                if attempt >= 3 or permanent:
                    compensation.status, compensation.finished_at = "FAILED", now()
                    original = await session.get(Task, task_id)
                    if original:
                        session.add(DeadLetter(run_id=run_id, task_id=task_id, task_key=task_key,
                            error=f"Compensation failed: {error}", error_classification="COMPENSATION",
                            attempt_count=attempt))
                        await record(session, run_id, "dead_letter.created", task_id,
                                     {"task_key": task_key, "error_classification": "COMPENSATION"})
                    event = "saga.compensation_failed"
                else:
                    compensation.status = "PENDING"
                    compensation.retry_at = now() + timedelta(seconds=min(30, 2 ** (attempt - 1)))
                    event = "saga.compensation_retry_scheduled"
                await record(session, run_id, event, task_id,
                             {"task": task_key, "handler": handler_type, "error": error,
                              "attempt": attempt})
                await session.commit()
    return True


if __name__ == "__main__":
    import sys
    import uvicorn
    if len(sys.argv) > 1 and sys.argv[1] == "scheduler":
        asyncio.run(scheduler_loop())
    elif len(sys.argv) > 1 and sys.argv[1] == "worker":
        asyncio.run(worker_loop())
    else:
        # Running this module as __main__ and re-importing backend.app would register
        # the Prometheus counters twice in the same process.
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("API_PORT", "8000")))
