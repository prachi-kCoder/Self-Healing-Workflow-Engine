"""Persist retry strategy per task."""
import sqlalchemy as sa
from alembic import op

revision = "0002_retry_policy"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_runs", sa.Column("retry_policy", sa.String(length=20), nullable=False,
                                          server_default="exponential"))
    op.add_column("task_runs", sa.Column("max_retry_delay_seconds", sa.Float(), nullable=False,
                                          server_default="300"))
    op.add_column("task_runs", sa.Column("error_classification", sa.String(length=20), nullable=True))
    op.add_column("task_attempts", sa.Column("error_classification", sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column("task_runs", "max_retry_delay_seconds")
    op.drop_column("task_runs", "retry_policy")
    op.drop_column("task_runs", "error_classification")
    op.drop_column("task_attempts", "error_classification")
