"""Keep a confirmed primary result while its dependent reads are still running."""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None
backward_compatible = False


def upgrade():
    op.execute("""
        ALTER TABLE chat_runs
          DROP CONSTRAINT ck_chat_runs_phase,
          DROP CONSTRAINT ck_chat_runs_response,
          ADD CONSTRAINT ck_chat_runs_phase
            CHECK (phase IN ('accepted','running','preparing_commit','post_commit','finished')),
          ADD CONSTRAINT ck_chat_runs_response
            CHECK ((phase IN ('finished','post_commit')) = (response IS NOT NULL)),
          ALTER COLUMN implementation_version SET DEFAULT 'p4-1'
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM chat_runs
                     WHERE phase = 'post_commit'
                        OR implementation_version NOT IN ('p2','p3-1')) THEN
            RAISE EXCEPTION 'P4 runs must expire and be cleaned before downgrade';
          END IF;
        END $$
    """)
    op.execute("""
        ALTER TABLE chat_runs
          DROP CONSTRAINT ck_chat_runs_phase,
          DROP CONSTRAINT ck_chat_runs_response,
          ADD CONSTRAINT ck_chat_runs_phase
            CHECK (phase IN ('accepted','running','preparing_commit','finished')),
          ADD CONSTRAINT ck_chat_runs_response
            CHECK ((phase = 'finished') = (response IS NOT NULL)),
          ALTER COLUMN implementation_version SET DEFAULT 'p3-1'
    """)
