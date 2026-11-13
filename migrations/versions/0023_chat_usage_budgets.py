"""Shared public-chat admission and provider budgets."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None
# First public V2 baseline: older prototype writers do not enforce the shared
# admission/provider limits, so an image-only rollback must not restore them.
backward_compatible = False


def upgrade():
    op.create_table(
        "chat_usage_buckets",
        sa.Column("bucket", sa.String(160), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("used", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("used >= 0", name="usage"),
    )
    op.create_index("ix_chat_usage_buckets_expires_at", "chat_usage_buckets", ["expires_at"])


def downgrade():
    op.drop_table("chat_usage_buckets")
