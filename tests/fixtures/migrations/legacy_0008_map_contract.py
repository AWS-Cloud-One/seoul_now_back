"""Published map geometry, observations and stable parking facility identities."""

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "map_layer_versions",
        sa.Column("layer", sa.String(20), primary_key=True),
        sa.Column("geometry_version", sa.String(100), nullable=False),
        sa.Column("is_ready", sa.Boolean(), nullable=False),
        sa.Column("coverage", sa.String(500), nullable=False),
    )
    op.create_table(
        "map_features",
        sa.Column(
            "layer", sa.String(20), sa.ForeignKey("map_layer_versions.layer"), primary_key=True
        ),
        sa.Column("feature_id", sa.String(100), primary_key=True),
        sa.Column("geometry", Geometry("GEOMETRY", srid=4326), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "map_observations",
        sa.Column("layer", sa.String(20), primary_key=True),
        sa.Column("feature_id", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("collection_failed", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "parking_facilities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("identity_status", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "parking_source_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("parking_id", sa.Uuid(), sa.ForeignKey("parking_facilities.id"), nullable=False),
        sa.Column("legacy_record_id", sa.String(64), unique=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("reference_date", sa.String(10)),
        sa.Column("registered", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_parking_source_records_parking_id", "parking_source_records", ["parking_id"]
    )


def downgrade():
    op.drop_table("parking_source_records")
    op.drop_table("parking_facilities")
    op.drop_table("map_observations")
    op.drop_table("map_features")
    op.drop_table("map_layer_versions")
