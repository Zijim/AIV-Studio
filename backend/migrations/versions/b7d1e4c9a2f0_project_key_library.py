"""Project-scoped key assets and user-defined tags.

The legacy JSON column on project_assets is kept as a rollback witness, but the
normalized tables become the only source read by the application after this
migration.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "b7d1e4c9a2f0"
down_revision = "a6c42f91d305"
branch_labels = None
depends_on = None

PALETTE = ("#e6a16e", "#8fb7a8", "#b6a1d8", "#d9a3a3", "#9fb8d9", "#c9bd8e", "#a8c28f", "#c39bc4")


def now():
    return datetime.now(timezone.utc).isoformat()


def upgrade():
    op.add_column("project_assets", sa.Column("curated_at", sa.String(36), nullable=True))
    op.create_index("ix_project_assets_curated", "project_assets", ["project_id", "curated_at"])

    op.create_table(
        "project_tags",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("color", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(36), nullable=False),
    )
    op.create_index("ix_project_tags_project", "project_tags", ["project_id", "position"])
    op.create_index("uq_project_tags_project_id", "project_tags", ["project_id", "id"], unique=True)
    # SQLite's default unique comparison is case-sensitive; tags are not.
    op.execute(
        "CREATE UNIQUE INDEX uq_project_tags_name ON project_tags(project_id, name COLLATE NOCASE)"
    )

    op.create_table(
        "project_asset_tags",
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("asset_id", sa.String(36), primary_key=True),
        sa.Column("tag_id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "asset_id"],
            ["project_assets.project_id", "project_assets.asset_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "tag_id"],
            ["project_tags.project_id", "project_tags.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_project_asset_tags_tag", "project_asset_tags", ["tag_id"])

    connection = op.get_bind()
    tags_by_name = {}
    for row in connection.execute(
        sa.text("SELECT project_id, asset_id, tags, created_at FROM project_assets")
    ).mappings():
        try:
            names = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
        except (TypeError, ValueError):
            names = []
        if not isinstance(names, list):
            continue
        cleaned = []
        for value in names:
            if not isinstance(value, str):
                continue
            name = " ".join(value.split())[:60]
            if name and name.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(name)
        if not cleaned:
            continue
        for name in cleaned:
            key = (row["project_id"], name.casefold())
            tag_id = tags_by_name.get(key)
            if not tag_id:
                tag_id = str(uuid4())
                connection.execute(
                    sa.text(
                        "INSERT INTO project_tags(id,project_id,name,color,position,created_at) "
                        "VALUES (:id,:project,:name,:color,:position,:created)"
                    ),
                    {
                        "id": tag_id,
                        "project": row["project_id"],
                        "name": name,
                        "color": PALETTE[len(tags_by_name) % len(PALETTE)],
                        "position": len(tags_by_name),
                        "created": now(),
                    },
                )
                tags_by_name[key] = tag_id
            connection.execute(
                sa.text(
                    "INSERT OR IGNORE INTO project_asset_tags(project_id,asset_id,tag_id,created_at) "
                    "VALUES (:project,:asset,:tag,:created)"
                ),
                {
                    "project": row["project_id"],
                    "asset": row["asset_id"],
                    "tag": tag_id,
                    "created": now(),
                },
            )
        connection.execute(
            sa.text("UPDATE project_assets SET curated_at=:curated WHERE project_id=:project AND asset_id=:asset"),
            {"curated": row["created_at"] or now(), "project": row["project_id"], "asset": row["asset_id"]},
        )


def downgrade():
    raise RuntimeError("降级会丢失项目标签与关键素材状态，请使用迁移前备份恢复。")
