"""Share query cursors and P03 read replays between API processes.

Revision ID: 0015
Revises: 0014
DDL is frozen here, independent of runtime models.
Both tables hold disposable, expiring rows; downgrade drops them without data loss.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None
# Read by infra/cicd/db_compat.py: an image built before this revision keeps working
# on a database at this revision (new tables only), so a rollback may skip it.
backward_compatible = True


def upgrade():
    op.execute("""
        CREATE TABLE query_cursors (
        token VARCHAR(48) NOT NULL,
        scope JSONB NOT NULL,
        payload JSONB NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_query_cursors PRIMARY KEY (token),
        CONSTRAINT ck_query_cursors_token_length CHECK (char_length(token) = 48)
        )
    """)
    op.execute("CREATE INDEX ix_query_cursors_expires_at ON query_cursors (expires_at)")
    op.execute("""
        CREATE TABLE read_replays (
        replay_key TEXT NOT NULL,
        signature VARCHAR(64) NOT NULL,
        outcome JSONB NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_read_replays PRIMARY KEY (replay_key),
        CONSTRAINT ck_read_replays_replay_key_nonblank CHECK (btrim(replay_key) <> '')
        )
    """)
    op.execute("CREATE INDEX ix_read_replays_expires_at ON read_replays (expires_at)")


def downgrade():
    op.execute("DROP TABLE read_replays")
    op.execute("DROP TABLE query_cursors")
