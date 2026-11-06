"""Index recent collection outcomes for the shared operational API."""

from importlib import import_module

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    import_module("migrations.versions.0005_external_api_history").upgrade()
    op.create_index(
        "ix_collection_runs_source_started", "collection_runs", ["source", "started_at"]
    )
    op.create_index("ix_collection_runs_started", "collection_runs", ["started_at"])


def downgrade():
    op.drop_index("ix_collection_runs_started", table_name="collection_runs")
    op.drop_index("ix_collection_runs_source_started", table_name="collection_runs")
    import_module("migrations.versions.0005_external_api_history").downgrade()
