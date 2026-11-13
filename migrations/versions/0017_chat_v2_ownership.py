"""Add isolated V2 ownership, runs and atomic commit receipts.

Revision ID: 0017
Revises: 0016
Frozen DDL; no existing rows or V1 table definitions are changed.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade():
    op.execute("""
        CREATE TABLE chat_browser_sessions (
            session_id UUID NOT NULL,
            token_digest VARCHAR(64) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_chat_browser_sessions PRIMARY KEY (session_id),
            CONSTRAINT ck_chat_browser_sessions_digest CHECK (token_digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_chat_browser_sessions_expiry CHECK (expires_at > created_at),
            CONSTRAINT uq_chat_browser_sessions_token_digest UNIQUE (token_digest)
        )
    """)
    op.execute("""
        CREATE INDEX ix_chat_browser_sessions_expires_at ON
            chat_browser_sessions (expires_at)
    """)
    op.execute("""
        CREATE TABLE chat_conversations (
            conversation_id UUID NOT NULL,
            session_id UUID NOT NULL,
            trip_id UUID NOT NULL,
            state_version INTEGER NOT NULL,
            context JSONB NOT NULL,
            active_run_id UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_chat_conversations PRIMARY KEY (conversation_id),
            CONSTRAINT ck_chat_conversations_version CHECK (state_version >= 0),
            CONSTRAINT ck_chat_conversations_expiry CHECK (expires_at > created_at),
            CONSTRAINT ck_chat_conversations_context_schema CHECK (context->>'schema_version' =
            '1'),
            CONSTRAINT uq_chat_conversations_conversation_id UNIQUE (conversation_id, trip_id),
            CONSTRAINT fk_chat_conversations_session_id_chat_browser_sessions FOREIGN
            KEY(session_id) REFERENCES chat_browser_sessions (session_id),
            CONSTRAINT uq_chat_conversations_trip_id UNIQUE (trip_id),
            CONSTRAINT fk_chat_conversations_trip_id_chat_sessions FOREIGN KEY(trip_id) REFERENCES
            chat_sessions (trip_id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_chat_conversations_expires_at ON chat_conversations
            (expires_at)
    """)
    op.execute("""
        CREATE INDEX ix_chat_conversations_session_id ON chat_conversations
            (session_id)
    """)
    op.execute("""
        CREATE TABLE chat_runs (
            run_id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            trip_id UUID NOT NULL,
            request_id UUID NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            owner UUID NOT NULL,
            request JSONB NOT NULL,
            context JSONB NOT NULL,
            prepared JSONB,
            response JSONB,
            phase TEXT NOT NULL,
            completed_state_version INTEGER,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_chat_runs PRIMARY KEY (run_id),
            CONSTRAINT uq_chat_runs_request UNIQUE (conversation_id, request_id),
            CONSTRAINT uq_chat_runs_identity UNIQUE (conversation_id, run_id),
            CONSTRAINT uq_chat_runs_run_id UNIQUE (run_id, trip_id),
            CONSTRAINT fk_chat_runs_conversation_id_chat_conversations FOREIGN
            KEY(conversation_id, trip_id) REFERENCES chat_conversations (conversation_id,
            trip_id),
            CONSTRAINT ck_chat_runs_request_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_chat_runs_phase CHECK (phase IN
            ('running','preparing_commit','finished')),
            CONSTRAINT ck_chat_runs_response CHECK ((phase = 'finished') = (response IS NOT NULL)),
            CONSTRAINT ck_chat_runs_deadline CHECK (deadline_at > created_at)
        )
    """)
    op.execute("""
        CREATE TABLE chat_run_commits (
            run_id UUID NOT NULL,
            trip_id UUID NOT NULL,
            previous_version INTEGER NOT NULL,
            current_version INTEGER NOT NULL,
            result_id UUID,
            receipt JSONB NOT NULL,
            committed_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_chat_run_commits PRIMARY KEY (run_id),
            CONSTRAINT fk_chat_run_commits_run_id_chat_runs FOREIGN KEY(run_id, trip_id)
            REFERENCES chat_runs (run_id, trip_id),
            CONSTRAINT fk_chat_run_commits_trip_id_recommendation_results FOREIGN KEY(trip_id,
            result_id) REFERENCES recommendation_results (trip_id, result_id),
            CONSTRAINT fk_chat_run_commits_trip_id_chat_condition_versions FOREIGN KEY(trip_id,
            current_version) REFERENCES chat_condition_versions (trip_id, version),
            CONSTRAINT ck_chat_run_commits_version CHECK (previous_version >= 0 AND
            current_version = previous_version + 1)
        )
    """)
    op.execute("""
        ALTER TABLE chat_conversations ADD CONSTRAINT
            fk_chat_conversations_active_run FOREIGN KEY(conversation_id, active_run_id)
            REFERENCES chat_runs (conversation_id, run_id) DEFERRABLE INITIALLY DEFERRED
    """)


def downgrade():
    op.execute("ALTER TABLE chat_conversations DROP CONSTRAINT fk_chat_conversations_active_run")
    op.execute("DROP TABLE chat_run_commits")
    op.execute("DROP TABLE chat_runs")
    op.execute("DROP TABLE chat_conversations")
    op.execute("DROP TABLE chat_browser_sessions")
