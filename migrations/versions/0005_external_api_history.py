"""Archive external responses and source observations without replacing latest tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_api_responses",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("run_id", UUID(), sa.ForeignKey("collection_runs.id"), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("operation", sa.String(120), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
    )
    op.create_index(
        "ix_external_api_responses_source_collected",
        "external_api_responses",
        ["source", "collected_at"],
    )
    op.create_index("ix_external_api_responses_run_id", "external_api_responses", ["run_id"])
    op.create_table(
        "external_api_observations",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "response_id", UUID(), sa.ForeignKey("external_api_responses.id"), nullable=False
        ),
        sa.Column("record_key", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("data", JSONB(), nullable=False),
        sa.UniqueConstraint("response_id", "record_key", name="uq_external_api_observation_record"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_api_observation_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_api_observation_longitude"),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)", name="ck_api_observation_coordinate_pair"
        ),
    )
    op.create_index(
        "ix_external_api_observations_record_time",
        "external_api_observations",
        ["record_key", "observed_at"],
    )


def downgrade():
    op.drop_table("external_api_observations")
    op.drop_table("external_api_responses")
