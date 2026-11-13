"""Allow a recommendation receipt without changing conditions.

Revision ID: 0018
Revises: 0017
Only relaxes a CHECK; older headcount writers still increment by one.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade():
    op.execute("ALTER TABLE chat_run_commits DROP CONSTRAINT ck_chat_run_commits_version")
    op.execute("""
        ALTER TABLE chat_run_commits ADD CONSTRAINT ck_chat_run_commits_version
        CHECK (previous_version >= 0 AND current_version
               BETWEEN previous_version AND previous_version + 1)
    """)


def downgrade():
    # Refuse downgrade if recommend-only receipts exist; never discard them.
    op.execute("ALTER TABLE chat_run_commits DROP CONSTRAINT ck_chat_run_commits_version")
    op.execute("""
        ALTER TABLE chat_run_commits ADD CONSTRAINT ck_chat_run_commits_version
        CHECK (previous_version >= 0 AND current_version = previous_version + 1)
    """)
