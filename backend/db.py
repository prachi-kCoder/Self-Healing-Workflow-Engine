import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def database_url_from_environment() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url
    host = os.getenv("DB_HOST")
    if host:
        return URL.create(
            "postgresql+asyncpg",
            username=os.getenv("DB_USER", "workflow"),
            password=os.environ["DB_PASSWORD"],
            host=host,
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "workflow"),
        ).render_as_string(hide_password=False)
    return "sqlite+aiosqlite:///./workflow.db"


DATABASE_URL = database_url_from_environment()
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Definition(Base):
    __tablename__ = "workflow_definitions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    definition_id: Mapped[str] = mapped_column(ForeignKey("workflow_definitions.id"))
    definition_snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    __tablename__ = "task_runs"
    __table_args__ = (UniqueConstraint("run_id", "task_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    task_key: Mapped[str] = mapped_column(String(100))
    task_type: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="BLOCKED", index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    retry_base_seconds: Mapped[float] = mapped_column(default=1)
    retry_policy: Mapped[str] = mapped_column(String(20), default="exponential")
    max_retry_delay_seconds: Mapped[float] = mapped_column(default=300)
    retry_on: Mapped[list] = mapped_column(JSON, default=lambda: ["RETRYABLE", "TIMEOUT", "LEASE_EXPIRED"])
    compensation_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    compensation_input: Mapped[dict] = mapped_column(JSON, default=dict)
    error_classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Attempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(Base):
    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task_runs.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class SagaCompensation(Base):
    __tablename__ = "saga_compensations"
    __table_args__ = (UniqueConstraint("task_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    task_key: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    handler_type: Mapped[str] = mapped_column(String(120))
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeadLetter(Base):
    __tablename__ = "dead_letter_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), index=True)
    task_key: Mapped[str] = mapped_column(String(100))
    error: Mapped[str] = mapped_column(Text)
    error_classification: Mapped[str] = mapped_column(String(20))
    attempt_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Ledger(Base):
    __tablename__ = "idempotency_records"
    operation_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
