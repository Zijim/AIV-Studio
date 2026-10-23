"""Record guided prompt provenance and the current planning stage."""

import sqlalchemy as sa
from alembic import op

revision = "a6c42f91d305"
down_revision = "7f2a18d4b901"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_turns", sa.Column("stage", sa.String(), nullable=True))
    op.add_column("plans", sa.Column("prompt_metadata", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("plans", "prompt_metadata")
    op.drop_column("agent_turns", "stage")
