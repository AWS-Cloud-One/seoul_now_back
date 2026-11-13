"""Separate V2 result identity from V1's driving-only result constraints."""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None
# Old V2 writers cannot insert the new identity before their receipt.
backward_compatible = False


def upgrade():
    op.execute("""
        CREATE TABLE chat_results (
            result_id UUID NOT NULL,
            run_id UUID NOT NULL,
            trip_id UUID NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_chat_results PRIMARY KEY (result_id),
            CONSTRAINT uq_chat_results_run_id UNIQUE (run_id),
            CONSTRAINT uq_chat_results_trip_id UNIQUE (trip_id, result_id),
            CONSTRAINT fk_chat_results_run_id_chat_runs FOREIGN KEY (run_id, trip_id)
                REFERENCES chat_runs (run_id, trip_id)
        )
    """)
    op.execute("""
        INSERT INTO chat_results (result_id, run_id, trip_id, created_at)
        SELECT c.result_id, c.run_id, c.trip_id, r.created_at
        FROM chat_run_commits c JOIN recommendation_results r ON r.result_id = c.result_id
    """)
    op.execute("""
        ALTER TABLE chat_run_commits DROP CONSTRAINT
            fk_chat_run_commits_trip_id_recommendation_results
    """)
    op.execute("""
        ALTER TABLE chat_run_commits ADD CONSTRAINT fk_chat_run_commits_trip_id_chat_results
        FOREIGN KEY (trip_id, result_id) REFERENCES chat_results (trip_id, result_id)
    """)


def downgrade():
    # Fails atomically if regional-only results exist; never delete results to downgrade.
    op.execute("""
        ALTER TABLE chat_run_commits ADD CONSTRAINT
            fk_chat_run_commits_trip_id_recommendation_results
        FOREIGN KEY (trip_id, result_id)
            REFERENCES recommendation_results (trip_id, result_id)
    """)
    op.execute("""
        ALTER TABLE chat_run_commits DROP CONSTRAINT fk_chat_run_commits_trip_id_chat_results
    """)
    op.execute("DROP TABLE chat_results")
