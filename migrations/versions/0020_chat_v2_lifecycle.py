"""Durable admission, fenced execution and ordered progress events."""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None
backward_compatible = False


def upgrade():
    op.execute("""
        ALTER TABLE chat_runs
          DROP CONSTRAINT ck_chat_runs_phase,
          ADD CONSTRAINT ck_chat_runs_phase
            CHECK (phase IN ('accepted','running','preparing_commit','finished')),
          ADD COLUMN started_at TIMESTAMPTZ,
          ADD COLUMN lease_expires_at TIMESTAMPTZ,
          ADD COLUMN cancel_requested_at TIMESTAMPTZ,
          ADD COLUMN implementation_version TEXT NOT NULL DEFAULT 'p3-1',
          ADD COLUMN stage TEXT NOT NULL DEFAULT 'accepted',
          ADD COLUMN event_seq INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN operation JSONB,
          ADD CONSTRAINT ck_chat_runs_event_seq CHECK (event_seq >= 0 AND event_seq <= 200)
    """)
    # Prior executions cannot be resumed under a different implementation.
    op.execute("UPDATE chat_runs SET implementation_version = 'p2', stage = phase")
    op.execute("""
        CREATE TABLE chat_run_events (
          run_id UUID NOT NULL REFERENCES chat_runs(run_id),
          event_seq INTEGER NOT NULL,
          type TEXT NOT NULL,
          state_version INTEGER NOT NULL,
          payload JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT pk_chat_run_events PRIMARY KEY (run_id, event_seq),
          CONSTRAINT ck_chat_run_events_sequence CHECK (event_seq > 0 AND event_seq <= 200)
        )
    """)


def downgrade():
    # Removing fences while durable executions exist would let old code misread them.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM chat_runs WHERE implementation_version <> 'p2') THEN
            RAISE EXCEPTION 'P3 runs must expire and be cleaned before downgrade';
          END IF;
        END $$
    """)
    op.execute("DROP TABLE chat_run_events")
    op.execute("""
        ALTER TABLE chat_runs
          DROP CONSTRAINT ck_chat_runs_event_seq,
          DROP CONSTRAINT ck_chat_runs_phase,
          ADD CONSTRAINT ck_chat_runs_phase
            CHECK (phase IN ('running','preparing_commit','finished')),
          DROP COLUMN started_at, DROP COLUMN lease_expires_at,
          DROP COLUMN cancel_requested_at, DROP COLUMN implementation_version,
          DROP COLUMN stage, DROP COLUMN event_seq, DROP COLUMN operation
    """)
