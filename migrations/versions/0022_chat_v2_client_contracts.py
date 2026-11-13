"""Ordered conversation history and one-use, server-bound question choices."""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None
backward_compatible = False


def upgrade():
    op.execute("""
        ALTER TABLE chat_runs ADD COLUMN sequence INTEGER,
          ADD COLUMN question JSONB, ADD COLUMN user_message JSONB,
          ADD COLUMN request_aliases JSONB NOT NULL DEFAULT '{}';
        WITH numbered AS (
          SELECT run_id, row_number() OVER (
            PARTITION BY conversation_id ORDER BY created_at, run_id) AS n FROM chat_runs
        ) UPDATE chat_runs SET sequence = numbered.n FROM numbered
          WHERE chat_runs.run_id = numbered.run_id;
        ALTER TABLE chat_runs ALTER COLUMN sequence SET NOT NULL,
          ADD CONSTRAINT uq_chat_runs_sequence UNIQUE (conversation_id, sequence),
          ADD CONSTRAINT ck_chat_runs_sequence CHECK (sequence > 0),
          ALTER COLUMN implementation_version SET DEFAULT 'p5-1';
        CREATE TABLE chat_actions (
          action_id UUID NOT NULL,
          conversation_id UUID NOT NULL,
          source_run_id UUID NOT NULL,
          clarification_id TEXT NOT NULL,
          key TEXT NOT NULL, label TEXT NOT NULL, description TEXT,
          decision JSONB NOT NULL,
          condition_version INTEGER NOT NULL, state_version INTEGER NOT NULL,
          created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
          consumed_by_run_id UUID,
          CONSTRAINT pk_chat_actions PRIMARY KEY (action_id),
          CONSTRAINT fk_chat_actions_source
            FOREIGN KEY (conversation_id, source_run_id)
            REFERENCES chat_runs(conversation_id, run_id),
          CONSTRAINT fk_chat_actions_consumed
            FOREIGN KEY (conversation_id, consumed_by_run_id)
            REFERENCES chat_runs(conversation_id, run_id),
          CONSTRAINT ck_chat_actions_expiry CHECK (expires_at > created_at),
          CONSTRAINT ck_chat_actions_versions CHECK (condition_version >= 0 AND state_version >= 0)
        );
        CREATE INDEX ix_chat_actions_conversation_id ON chat_actions(conversation_id);
        CREATE INDEX ix_chat_actions_source_run_id ON chat_actions(source_run_id);
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM chat_actions) OR EXISTS (
            SELECT 1 FROM chat_runs
            WHERE implementation_version NOT IN ('p2','p3-1','p4-1')
          ) THEN
            RAISE EXCEPTION 'P5 runs/actions must expire and be cleaned before downgrade';
          END IF;
        END $$;
        DROP TABLE chat_actions;
        ALTER TABLE chat_runs DROP CONSTRAINT uq_chat_runs_sequence,
          DROP CONSTRAINT ck_chat_runs_sequence,
          DROP COLUMN sequence, DROP COLUMN question, DROP COLUMN user_message,
          DROP COLUMN request_aliases,
          ALTER COLUMN implementation_version SET DEFAULT 'p4-1';
    """)
