import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.app import recover_expired_leases, schedule_once, worker_once
from backend.db import Attempt, Event, Ledger, Session, Task, now


def definition():
    return {
        "name": "order-processing",
        "description": "test pipeline",
        "tasks": [
            {"key": "validate_order", "type": "demo.validate_order", "input": {"delay": 0.08}},
            {"key": "check_inventory", "type": "demo.check_inventory", "input": {"delay": 0.08}},
            {"key": "process_payment", "type": "demo.process_payment",
             "depends_on": ["validate_order", "check_inventory"],
             "input": {"fail_first": 2, "amount": 12.5}, "max_attempts": 3, "retry_base_seconds": 0.01},
            {"key": "send_confirmation", "type": "demo.send_confirmation",
             "depends_on": ["process_payment"]},
        ],
    }


async def create_run(client, body=None):
    response = await client.post("/api/v1/workflows", json=body or definition())
    assert response.status_code == 201, response.text
    workflow = response.json()
    response = await client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"test": True}})
    assert response.status_code == 201, response.text
    return workflow, response.json()


@pytest.mark.asyncio
async def test_health_and_versioned_api_errors(client):
    assert (await client.get("/health/live")).json() == {"status": "alive"}
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/api/v1/health")).json()["database"] == "ready"
    assert (await client.get("/api/v1/runs/not-a-run")).status_code == 404
    assert (await client.get("/api/v1/workflows/not-a-workflow")).status_code == 404
    assert (await client.get("/api/v1/tasks/no-task/attempts")).status_code == 404


@pytest.mark.asyncio
async def test_create_inspect_run_and_execute_retry_pipeline(client):
    workflow, run_response = await create_run(client)
    run_id = run_response["id"]
    assert (await client.get(f"/api/v1/workflows/{workflow['id']}")).status_code == 200

    await schedule_once()
    async with Session() as session:
        tasks = (await session.execute(select(Task).where(Task.run_id == run_id))).scalars().all()
        status = {task.task_key: task.status for task in tasks}
        assert status["validate_order"] == status["check_inventory"] == "READY"
        assert status["process_payment"] == "BLOCKED"

    # Both independent nodes become claimable in the same scheduler tick.
    await worker_once("worker-a")
    await worker_once("worker-b")
    await schedule_once()
    await worker_once("worker-a")  # Payment attempt one fails.
    async with Session() as session:
        payment = (await session.execute(select(Task).where(Task.run_id == run_id, Task.task_key == "process_payment"))).scalar_one()
        assert payment.status == "RETRY_WAIT"
        payment.retry_at = now() - timedelta(seconds=1)
        await session.commit()
    await schedule_once()
    await worker_once("worker-a")  # Payment attempt two fails.
    async with Session() as session:
        payment = (await session.execute(select(Task).where(Task.run_id == run_id, Task.task_key == "process_payment"))).scalar_one()
        assert payment.status == "RETRY_WAIT"
        payment.retry_at = now() - timedelta(seconds=1)
        await session.commit()
    await schedule_once()
    await worker_once("worker-a")  # Third payment attempt writes one ledger record.
    await schedule_once()
    await worker_once("worker-a")  # Confirmation depends on payment.
    await schedule_once()

    response = await client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "SUCCEEDED"
    assert [task["status"] for task in result["tasks"]] == ["SUCCEEDED"] * 4
    assert sum(task["attempt_count"] for task in result["tasks"]) == 6
    assert len(result["attempts"]) == 6
    assert len(result["events"]) >= 15
    assert len([a for a in result["attempts"] if a["task_id"] == next(t["id"] for t in result["tasks"] if t["key"] == "process_payment")]) == 3

    payment = next(t for t in result["tasks"] if t["key"] == "process_payment")
    assert payment["output"]["charged"] == 12.5
    assert len((await client.get(f"/api/v1/tasks/{payment['id']}/attempts")).json()) == 3
    assert len((await client.get(f"/api/v1/runs/{run_id}/tasks")).json()) == 4
    assert len((await client.get(f"/api/v1/runs/{run_id}/events")).json()) == len(result["events"])
    assert len((await client.get("/api/v1/runs")).json()) == 1
    async with Session() as session:
        assert len((await session.execute(select(Ledger))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_permanent_parent_failure_skips_children(client):
    body = definition()
    body["tasks"][0]["input"] = {"valid": False}
    body["tasks"][0]["max_attempts"] = 3
    _, run = await create_run(client, body)
    await schedule_once()
    await worker_once("worker-a")
    await schedule_once()
    await worker_once("worker-b")  # The other independent root still runs.
    await schedule_once()
    response = await client.get(f"/api/v1/runs/{run['id']}")
    by_name = {task["key"]: task["status"] for task in response.json()["tasks"]}
    assert by_name["validate_order"] == "FAILED"
    assert by_name["process_payment"] == "SKIPPED"
    assert by_name["send_confirmation"] == "SKIPPED"
    assert response.json()["status"] == "FAILED"


@pytest.mark.asyncio
async def test_cancellation_stops_ready_work(client):
    _, run = await create_run(client)
    response = await client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert response.status_code == 202
    await schedule_once()
    result = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert result["status"] == "CANCELLED"
    assert all(task["status"] == "CANCELLED" for task in result["tasks"])


@pytest.mark.asyncio
async def test_expired_worker_lease_is_recovered(client):
    _, run = await create_run(client)
    await schedule_once()
    # Claim but simulate the process dying before task completion/heartbeat.
    async with Session() as session:
        task = (await session.execute(select(Task).where(Task.run_id == run["id"], Task.task_key == "validate_order"))).scalar_one()
        task.status, task.worker_id, task.lease_token = "RUNNING", "dead-worker", "expired-token"
        task.attempt_count, task.lease_expires_at = 1, now() - timedelta(seconds=20)
        session.add(Attempt(task_id=task.id, attempt_number=1, worker_id="dead-worker", status="RUNNING"))
        await session.commit()
    await recover_expired_leases()
    async with Session() as session:
        task = (await session.execute(select(Task).where(Task.run_id == run["id"], Task.task_key == "validate_order"))).scalar_one()
        attempts = (await session.execute(select(Attempt).where(Attempt.task_id == task.id))).scalars().all()
        assert task.status == "RETRY_WAIT"
        assert attempts[0].error == "Worker lease expired"
        events = (await session.execute(select(Event).where(Event.run_id == run["id"]))).scalars().all()
        assert any(event.event_type == "task.lease_expired" for event in events)


@pytest.mark.asyncio
async def test_generic_data_pipeline_passes_persisted_dependency_outputs(client):
    body = {"name": "data-pipeline", "metadata": {"owner": "test"}, "tasks": [
        {"key": "generate", "type": "demo.generate_data", "input": {"count": 5, "start": 1}},
        {"key": "transform", "type": "demo.transform_data", "input": {"multiplier": 3},
         "depends_on": ["generate"]},
        {"key": "summary", "type": "demo.summarize_data", "input": {},
         "depends_on": ["transform"]},
    ]}
    workflow, run = await create_run(client, body)
    for _ in range(12):
        await schedule_once()
        await worker_once("generic-worker")
        await schedule_once()
        await asyncio.sleep(0.02)
    result = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert result["status"] == "SUCCEEDED"
    assert result["definition_snapshot"]["metadata"] == {"owner": "test"}
    outputs = {task["key"]: task["output"] for task in result["tasks"]}
    assert outputs["generate"]["data"] == [1, 2, 3, 4, 5]
    assert outputs["transform"]["data"] == [3, 6, 9, 12, 15]
    assert outputs["summary"] == {"count": 5, "sum": 45, "average": 9.0, "min": 3, "max": 15}
    assert workflow["definition"]["metadata"] == {"owner": "test"}


@pytest.mark.asyncio
async def test_workflow_versions_preserve_run_snapshot_and_safe_delete(client):
    workflow, run = await create_run(client, {"name": "versioned", "tasks": [
        {"key": "echo", "type": "demo.echo", "input": {"version": 1}}]})
    updated = {"name": "versioned", "description": "next", "tasks": [
        {"key": "echo", "type": "demo.echo", "input": {"version": 2}}]}
    response = await client.post(f"/api/v1/workflows/{workflow['id']}/versions", json={"definition": updated})
    assert response.status_code == 201, response.text
    assert response.json()["version"] == 2
    second_run = await client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert second_run.status_code == 201
    compare = await client.get(f"/api/v1/runs/{run['id']}/compare/{second_run.json()['id']}")
    assert compare.status_code == 200
    assert compare.json()["tasks"][0]["key"] == "echo"
    new_version_run = await client.post(f"/api/v1/workflows/{response.json()['id']}/runs", json={})
    assert new_version_run.status_code == 201
    assert (await client.get(f"/api/v1/runs/{run['id']}/compare/{new_version_run.json()['id']}")).status_code == 409
    old_run = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert old_run["definition_snapshot"]["tasks"][0]["input"] == {"version": 1}
    assert old_run["definition_version"] == 1
    assert (await client.delete(f"/api/v1/workflows/{workflow['id']}")).status_code == 409
    duplicate = await client.post(f"/api/v1/workflows/{response.json()['id']}/duplicate", json={})
    assert duplicate.status_code == 201
    assert (await client.delete(f"/api/v1/workflows/{duplicate.json()['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_task_type_catalog_and_dashboard_are_database_backed(client):
    catalog = (await client.get("/api/v1/task-types")).json()
    assert {item["type"] for item in catalog} >= {"demo.generate_data", "demo.transform_data", "demo.fail_n_times"}
    dashboard = (await client.get("/api/v1/dashboard")).json()
    assert dashboard["workflow_count"] == 0
    assert dashboard["run_count"] == 0


@pytest.mark.asyncio
async def test_retry_scenario_reports_pass_only_from_persisted_evidence(client):
    started = await client.post("/api/v1/lab/scenarios/retries/runs")
    assert started.status_code == 201, started.text
    run_id = started.json()["runs"][0]["id"]
    for _ in range(80):
        await schedule_once()
        await worker_once("lab-worker")
        await schedule_once()
        result = await client.get(f"/api/v1/lab/scenarios/retries/results?run_ids={run_id}")
        if result.json()["status"] in {"PASS", "FAIL", "INCONCLUSIVE"}:
            break
        await asyncio.sleep(0.03)
    body = result.json()
    assert body["status"] == "PASS", body
    assert body["assertions"][0]["attempt_count"] == 3
    assert body["assertions"][0]["retry_event_count"] == 2


@pytest.mark.asyncio
async def test_permanent_failure_lab_checks_skip_and_error_classification(client):
    started = await client.post("/api/v1/lab/scenarios/permanent-failure/runs")
    run_id = started.json()["runs"][0]["id"]
    await schedule_once()
    await worker_once("lab-permanent")
    await schedule_once()
    result = (await client.get(f"/api/v1/lab/scenarios/permanent-failure/results?run_ids={run_id}")).json()
    assert result["status"] == "PASS"
    tasks = {task["key"]: task for task in result["runs"][0]["tasks"]}
    assert tasks["reject"]["error_classification"] == "PERMANENT"
    assert tasks["must_not_run"]["status"] == "SKIPPED"


@pytest.mark.asyncio
async def test_payment_idempotency_lab_reuses_committed_result(client):
    started = await client.post("/api/v1/lab/scenarios/idempotency/runs")
    run_id = started.json()["runs"][0]["id"]
    for _ in range(60):
        await schedule_once()
        await worker_once("lab-idempotency")
        await schedule_once()
        result = (await client.get(f"/api/v1/lab/scenarios/idempotency/results?run_ids={run_id}")).json()
        if result["status"] in {"PASS", "FAIL", "INCONCLUSIVE"}:
            break
        await asyncio.sleep(0.03)
    assert result["status"] == "PASS", result
    assert result["assertions"][0]["attempt_count"] == 2
    assert result["assertions"][0]["ledger_count"] == 1


@pytest.mark.asyncio
async def test_sqlite_parallel_scenario_is_inconclusive_not_a_false_pass(client):
    from backend.db import engine
    if engine.url.get_backend_name() != "sqlite":
        pytest.skip("SQLite-specific expectation")
    started = await client.post("/api/v1/lab/scenarios/parallel/runs")
    run_id = started.json()["runs"][0]["id"]
    for _ in range(10):
        await schedule_once()
        await worker_once("lab-serial")
        await schedule_once()
        result = (await client.get(f"/api/v1/lab/scenarios/parallel/results?run_ids={run_id}")).json()
        if result["runs"][0]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
    assert result["status"] == "INCONCLUSIVE", result


@pytest.mark.asyncio
async def test_concurrent_run_lab_checks_separate_persisted_state(client):
    started = await client.post("/api/v1/lab/scenarios/concurrent/runs")
    run_ids = [run["id"] for run in started.json()["runs"]]
    for _ in range(12):
        await schedule_once()
        await worker_once("concurrent-lab-worker")
        await schedule_once()
        result = await client.get("/api/v1/lab/scenarios/concurrent/results", params={"run_ids": run_ids})
        if result.json()["status"] in {"PASS", "FAIL", "INCONCLUSIVE"}:
            break
        await asyncio.sleep(0.03)
    assert result.json()["status"] == "PASS", result.json()


@pytest.mark.asyncio
async def test_lab_cancellation_scenario_uses_persisted_cancel_path(client):
    started = await client.post("/api/v1/lab/scenarios/cancellation/runs")
    run_id = started.json()["runs"][0]["id"]
    assert (await client.post(f"/api/v1/runs/{run_id}/cancel")).status_code == 202
    await schedule_once()
    result = (await client.get(f"/api/v1/lab/scenarios/cancellation/results?run_ids={run_id}")).json()
    assert result["status"] == "PASS"
    assert result["runs"][0]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_workflow_input_schema_errors_are_actionable(client):
    response = await client.post("/api/v1/workflows", json={"name": "bad-input", "tasks": [
        {"key": "wait", "type": "demo.delay", "input": {"seconds": 99}}]})
    assert response.status_code == 422
    assert "demo.delay" in response.json()["detail"]
    assert "seconds" in response.json()["detail"]
