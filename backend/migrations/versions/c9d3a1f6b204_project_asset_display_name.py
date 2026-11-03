"""Project-scoped display name for an asset.

Asset.name stays the immutable original filename used by task history and
lineage. ProjectAsset.display_name only changes how the asset is labelled
inside one project.
"""

import sqlalchemy as sa
from alembic import op

revision = "c9d3a1f6b204"
down_revision = "b7d1e4c9a2f0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project_assets", sa.Column("display_name", sa.String(120), nullable=True))


def downgrade():
    raise RuntimeError("降级会丢失项目内素材显示名，请使用迁移前备份恢复。")
