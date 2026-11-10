"""Independent autonomous runtime; existing conversations remain assisted."""

import sqlalchemy as sa
from alembic import op

revision = "d81a0c4e2100"
down_revision = "c9d3a1f6b204"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("mode", sa.String(), nullable=False, server_default="assisted"))
    # Frozen DDL: future model changes must not change this migration.
    op.create_index("uq_conversation_project_id", "conversations", ["project_id", "id"], unique=True)
    op.execute("""CREATE TABLE creative_agent_runs (
	project_id VARCHAR(36) NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	parent_run_id VARCHAR(36),
	goal TEXT NOT NULL,
	goal_snapshot JSON NOT NULL,
	scope JSON NOT NULL,
	access JSON NOT NULL,
	limits JSON NOT NULL,
	state VARCHAR NOT NULL,
	control_version INTEGER NOT NULL,
	intent VARCHAR NOT NULL,
	work_plan JSON NOT NULL,
	issues JSON NOT NULL,
	clarifications JSON NOT NULL,
	deliverables JSON NOT NULL,
	completion JSON,
	stop_reason JSON,
	execution_count INTEGER NOT NULL,
	model_count INTEGER NOT NULL,
	tool_count INTEGER NOT NULL,
	no_progress INTEGER NOT NULL,
	active_ms INTEGER NOT NULL,
	updated_at VARCHAR NOT NULL,
	finished_at VARCHAR,
	id VARCHAR(36) NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(project_id, conversation_id) REFERENCES conversations (project_id, id),
	FOREIGN KEY(project_id) REFERENCES projects (id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id),
	FOREIGN KEY(parent_run_id) REFERENCES creative_agent_runs (id)
)""")
    op.execute("CREATE INDEX ix_creative_run_project ON creative_agent_runs (project_id, created_at)")
    op.execute(
        "CREATE UNIQUE INDEX uq_creative_active_conversation ON creative_agent_runs (conversation_id) WHERE state NOT IN ('completed', 'cancelled', 'failed')"
    )
    op.execute("""CREATE TABLE creative_agent_steps (
	run_id VARCHAR(36) NOT NULL,
	sequence INTEGER NOT NULL,
	control_version INTEGER NOT NULL,
	state VARCHAR NOT NULL,
	attempts INTEGER NOT NULL,
	request_data JSON NOT NULL,
	response_data JSON,
	error JSON,
	id VARCHAR(36) NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_id, sequence),
	UNIQUE (run_id, id),
	FOREIGN KEY(run_id) REFERENCES creative_agent_runs (id)
)""")
    op.execute("CREATE INDEX ix_creative_agent_steps_run_id ON creative_agent_steps (run_id)")
    op.execute("""CREATE TABLE creative_agent_calls (
	run_id VARCHAR(36) NOT NULL,
	step_id VARCHAR(36) NOT NULL,
	call_id VARCHAR NOT NULL,
	position INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	arguments TEXT NOT NULL,
	state VARCHAR NOT NULL,
	result JSON,
	asset_id VARCHAR(36),
	plan_id VARCHAR,
	request_hash VARCHAR,
	id VARCHAR(36) NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (step_id, call_id),
	UNIQUE (step_id, position),
	FOREIGN KEY(run_id, step_id) REFERENCES creative_agent_steps (run_id, id),
	FOREIGN KEY(run_id) REFERENCES creative_agent_runs (id),
	FOREIGN KEY(step_id) REFERENCES creative_agent_steps (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)""")
    op.execute("CREATE INDEX ix_creative_agent_calls_run_id ON creative_agent_calls (run_id)")
    op.execute("CREATE INDEX ix_creative_agent_calls_step_id ON creative_agent_calls (step_id)")
    op.add_column("plans", sa.Column("agent_run_id", sa.String(36), nullable=True))
    op.create_index("ix_plans_agent_run_id", "plans", ["agent_run_id"])
    op.add_column("tasks", sa.Column("agent_run_id", sa.String(36), nullable=True))
    op.add_column("tasks", sa.Column("agent_call_id", sa.String(36), nullable=True))
    op.create_index("ix_tasks_agent_run_id", "tasks", ["agent_run_id"])
    op.create_index("uq_tasks_agent_call", "tasks", ["agent_call_id"], unique=True)
    # SQLite cannot add foreign keys with ALTER TABLE. Triggers preserve ownership
    # without rebuilding the live task/plan tables and their inbound references.
    for table in ("plans", "tasks"):
        for action in ("INSERT", "UPDATE"):
            op.execute(f"""CREATE TRIGGER {table}_agent_owner_{action.lower()}
                BEFORE {action} ON {table} WHEN NEW.agent_run_id IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'agent ownership mismatch') WHERE NOT EXISTS
                (SELECT 1 FROM creative_agent_runs r WHERE r.id=NEW.agent_run_id
                AND r.project_id=NEW.project_id AND r.conversation_id=NEW.conversation_id); END""")
    for action in ("INSERT", "UPDATE"):
        op.execute(f"""CREATE TRIGGER tasks_agent_call_{action.lower()} BEFORE {action} ON tasks
            WHEN NEW.agent_call_id IS NOT NULL BEGIN SELECT RAISE(ABORT, 'agent call mismatch')
            WHERE NOT EXISTS (SELECT 1 FROM creative_agent_calls c WHERE c.id=NEW.agent_call_id
            AND c.run_id=NEW.agent_run_id); END""")


def downgrade():
    raise RuntimeError("请使用迁移前备份恢复，避免丢失自主创作记录。")
