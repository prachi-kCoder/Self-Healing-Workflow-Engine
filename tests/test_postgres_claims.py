import asyncio

import pytest
from sqlalchemy import select

from backend.app import worker_once
from backend.db import Attempt, Definition, Run, Session, Task, engine


@pytest.mark.asyncio
@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires a dedicated PostgreSQL test database")
async def test_postgres_workers_atomically_claim_distinct_tasks():
    async with Session() as session:
        definition = Definition(name="concurrency-test", description="", version=1,
                                definition={"name": "concurrency-test", "tasks": []})
        session.add(definition)
        await session.flush()
        run = Run(definition_id=definition.id, definition_snapshot={}, status="RUNNING")
        session.add(run)
        await session.flush()
        for key in ("one", "two"):
            session.add(Task(run_id=run.id, task_key=key, task_type="demo.validate_order",
                             input={"delay": 0.3}, dependencies=[], status="READY"))
        await session.commit()
        run_id = run.id

    assert await asyncio.gather(worker_once("pg-worker-one"), worker_once("pg-worker-two")) == [True, True]
    async with Session() as session:
        tasks = (await session.execute(select(Task).where(Task.run_id == run_id))).scalars().all()
        attempts = [(task.task_key, task.attempt_count, task.status) for task in tasks]
        assert all(count == 1 and status == "SUCCEEDED" for _, count, status in attempts)
        worker_ids = (await session.execute(select(Attempt.worker_id).join(Task).where(Task.run_id == run_id))).scalars().all()
        assert len(set(worker_ids)) == 2
        started = [task.started_at for task in tasks]
        assert abs((started[0] - started[1]).total_seconds()) < 0.2
