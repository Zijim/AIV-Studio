"""Independent conversations within projects, preserving all existing history."""

import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "7f2a18d4b901"
down_revision = "ec3ccb923e94"
branch_labels = None
depends_on = None

TABLES = ("messages", "agent_turns", "context_snapshots", "plans", "tasks")


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("auto_title", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index("ix_conversations_project", "conversations", ["project_id", "updated_at"])
    op.create_index(
        "uq_default_conversation", "conversations", ["project_id"], unique=True, sqlite_where=sa.text("is_default = 1")
    )
    for table in TABLES:
        # SQLite supports adding a nullable REFERENCES column directly; avoid
        # rebuilding tables which already have incoming execution/history FKs.
        op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN conversation_id VARCHAR(36) REFERENCES conversations(id)"))
        op.create_index(f"ix_{table}_conversation_id", table, ["conversation_id"])
    op.create_index("ix_messages_conversation_history", "messages", ["conversation_id", "created_at", "id"])
    connection = op.get_bind()
    for project in connection.execute(sa.text("SELECT id,created_at,updated_at FROM projects")).mappings():
        cid = str(uuid5(NAMESPACE_URL, "aiv:default-conversation:" + project["id"]))
        latest = connection.execute(
            sa.text("SELECT max(created_at) FROM messages WHERE project_id=:project"), {"project": project["id"]}
        ).scalar()
        updated = max(
            project["updated_at"],
            latest or project["updated_at"],
            key=lambda value: datetime.fromisoformat(value).timestamp(),
        )
        connection.execute(
            sa.text(
                "INSERT INTO conversations (id,project_id,title,is_default,auto_title,revision,created_at,updated_at) "
                "VALUES (:id,:project,'默认对话',1,0,1,:created,:updated)"
            ),
            {"id": cid, "project": project["id"], "created": project["created_at"], "updated": updated},
        )
        for table in TABLES:
            connection.execute(
                sa.text(f"UPDATE {table} SET conversation_id=:cid WHERE project_id=:project"),
                {"cid": cid, "project": project["id"]},
            )
        previous = connection.execute(
            sa.text("SELECT value FROM settings WHERE key=:key"), {"key": "project_memory:" + project["id"]}
        ).scalar()
        if previous:
            value = json.dumps(previous, ensure_ascii=False) if not isinstance(previous, str) else previous
            connection.execute(
                sa.text("INSERT INTO settings (key,value) VALUES (:key,:value)"),
                {"key": "conversation_memory:" + cid, "value": value},
            )


def downgrade():
    raise RuntimeError("降级会合并独立对话的历史，请使用迁移前的数据库备份恢复，禁止静默合并。")
