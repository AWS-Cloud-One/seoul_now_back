"""Enable the spatial database extension without defining application tables."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    # PostGIS may predate this application and be used by other tables.
    # Keep the shared extension when reverting the application's revision.
    pass
