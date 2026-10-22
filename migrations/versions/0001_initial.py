"""Initial durable workflow schema."""
from alembic import op
from sqlalchemy import MetaData
from backend.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    # Freeze the original schema shape here. Creating the live ORM metadata
    # would include columns introduced by 0002 and make a fresh upgrade fail
    # when that migration adds them.
    baseline = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in {"saga_compensations", "dead_letter_tasks"}:
            continue
        copied = table.to_metadata(baseline)
        if table.name == "task_runs":
            for name in ("retry_policy", "max_retry_delay_seconds", "error_classification",
                         "retry_on", "compensation_type", "compensation_input"):
                copied._columns.remove(copied.columns[name])
        elif table.name == "task_attempts":
            copied._columns.remove(copied.columns["error_classification"])
    baseline.create_all(bind=bind)

def downgrade():
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
