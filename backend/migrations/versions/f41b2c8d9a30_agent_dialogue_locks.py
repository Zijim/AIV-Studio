"""Persist exact user dialogue by autonomous production target."""

import sqlalchemy as sa
from alembic import op

revision = "f41b2c8d9a30"
down_revision = "e92a5b8c3100"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "creative_agent_runs",
        sa.Column("dialogue_locks", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade():
    op.drop_column("creative_agent_runs", "dialogue_locks")
