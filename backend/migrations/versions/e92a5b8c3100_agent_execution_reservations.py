"""Track reserved, consumed and definitely unsubmitted autonomous attempts."""

import sqlalchemy as sa
from alembic import op

revision = "e92a5b8c3100"
down_revision = "d81a0c4e2100"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("executions", sa.Column("agent_quota_state", sa.String(), nullable=True))
    op.execute("""UPDATE executions SET agent_quota_state = CASE
        WHEN execution_state IN ('pending', 'preparing', 'waiting_external', 'blocked', 'failed', 'cancelled')
             AND (submitted_payload IS NULL OR submitted_payload = 'null') THEN 'reserved'
        ELSE 'consumed' END
        WHERE task_id IN (SELECT id FROM tasks WHERE agent_run_id IS NOT NULL)""")


def downgrade():
    raise RuntimeError("请使用迁移前备份恢复，避免丢失自主创作执行预约记录。")
