"""Persist configured retry classes, saga compensations, and dead letters."""
import sqlalchemy as sa
from alembic import op

revision = "0003_sagas_dead_letters"
down_revision = "0002_retry_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_runs", sa.Column(
        "retry_on", sa.JSON(), nullable=False,
        server_default='["RETRYABLE","TIMEOUT","LEASE_EXPIRED"]'))
    op.add_column("task_runs", sa.Column("compensation_type", sa.String(length=120), nullable=True))
    op.add_column("task_runs", sa.Column("compensation_input", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "saga_compensations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("task_key", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("handler_type", sa.String(length=120), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_saga_compensations_run_id", "saga_compensations", ["run_id"])
    op.create_index("ix_saga_compensations_task_id", "saga_compensations", ["task_id"])
    op.create_index("ix_saga_compensations_status", "saga_compensations", ["status"])
    op.create_index("ix_saga_compensations_lease_expires_at", "saga_compensations", ["lease_expires_at"])
    op.create_table(
        "dead_letter_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("task_key", sa.String(length=100), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("error_classification", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dead_letter_tasks_run_id", "dead_letter_tasks", ["run_id"])
    op.create_index("ix_dead_letter_tasks_task_id", "dead_letter_tasks", ["task_id"])
    op.create_index("ix_dead_letter_tasks_created_at", "dead_letter_tasks", ["created_at"])


def downgrade():
    op.drop_table("dead_letter_tasks")
    op.drop_table("saga_compensations")
    op.drop_column("task_runs", "compensation_input")
    op.drop_column("task_runs", "compensation_type")
    op.drop_column("task_runs", "retry_on")
