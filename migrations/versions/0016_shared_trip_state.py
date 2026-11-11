"""Share trip search progress and origin drafts between API processes.

Revision ID: 0016
Revises: 0015
DDL is frozen here, independent of runtime models.
The tables hold disposable, expiring rows; downgrade drops them without data loss.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None
# Read by infra/cicd/db_compat.py: an image built before this revision keeps working
# on a database at this revision (new tables only), so a rollback may skip it.
backward_compatible = True


def upgrade():
    op.execute("""
        CREATE TABLE trip_progress (
        trip_id UUID NOT NULL,
        state JSONB,
        expired BOOLEAN DEFAULT false NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        lock_owner UUID,
        lock_until TIMESTAMP WITH TIME ZONE,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_trip_progress PRIMARY KEY (trip_id),
        CONSTRAINT ck_trip_progress_lock_pair CHECK ((lock_owner IS NULL) = (lock_until IS NULL))
        )
    """)
    op.execute("CREATE INDEX ix_trip_progress_expires_at ON trip_progress (expires_at)")
    op.execute("""
        CREATE TABLE trip_requests (
        trip_id UUID NOT NULL,
        request_id UUID NOT NULL,
        signature VARCHAR(64) NOT NULL,
        outcome JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_trip_requests PRIMARY KEY (trip_id, request_id),
        CONSTRAINT fk_trip_requests_trip_id_trip_progress FOREIGN KEY(trip_id)
        REFERENCES trip_progress (trip_id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE TABLE origin_contexts (
        token VARCHAR(64) NOT NULL,
        trip_id UUID NOT NULL,
        draft JSONB NOT NULL,
        used_by UUID,
        is_latest BOOLEAN DEFAULT false NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_origin_contexts PRIMARY KEY (token)
        )
    """)
    op.execute("CREATE INDEX ix_origin_contexts_expires_at ON origin_contexts (expires_at)")
    op.execute("""
        CREATE UNIQUE INDEX uq_origin_contexts_latest ON origin_contexts (trip_id)
        WHERE is_latest
    """)


def downgrade():
    op.execute("DROP TABLE origin_contexts")
    op.execute("DROP TABLE trip_requests")
    op.execute("DROP TABLE trip_progress")
