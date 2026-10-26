"""Remove unused generic history tables after population-specific schema was added."""

from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE external_api_responses, external_api_observations IN ACCESS EXCLUSIVE MODE"
        )
    )
    for table in ("external_api_observations", "external_api_responses"):
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("Unused table cleanup stopped: stored records exist")
    op.drop_table("external_api_observations")
    op.drop_table("external_api_responses")


def downgrade():
    import_module("migrations.versions.0005_external_api_history").upgrade()
