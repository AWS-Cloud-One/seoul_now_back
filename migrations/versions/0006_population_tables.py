"""Create hotspot master and deduplicated population observation history."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "population_areas",
        sa.Column("area_code", sa.String(50), primary_key=True),
        sa.Column("area_name", sa.String(160), nullable=False),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("district", sa.String(100)),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_population_area_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_population_area_longitude"),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)", name="ck_population_area_coords"
        ),
    )
    op.create_index("ix_population_areas_district", "population_areas", ["district"])
    op.create_table(
        "floating_population_history",
        sa.Column(
            "area_code",
            sa.String(50),
            sa.ForeignKey("population_areas.area_code"),
            primary_key=True,
        ),
        sa.Column("base_time", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("population_min", sa.Integer(), nullable=False),
        sa.Column("population_max", sa.Integer(), nullable=False),
        sa.Column("congestion_level", sa.String(40)),
        sa.Column("male_rate", sa.Float()),
        sa.Column("female_rate", sa.Float()),
        sa.Column("age_20s_rate", sa.Float()),
        sa.Column("age_30s_rate", sa.Float()),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("population_min >= 0", name="ck_population_min_nonnegative"),
        sa.CheckConstraint("population_max >= population_min", name="ck_population_range"),
        sa.CheckConstraint("male_rate BETWEEN 0 AND 100", name="ck_population_male_rate"),
        sa.CheckConstraint("female_rate BETWEEN 0 AND 100", name="ck_population_female_rate"),
        sa.CheckConstraint("age_20s_rate BETWEEN 0 AND 100", name="ck_population_age20_rate"),
        sa.CheckConstraint("age_30s_rate BETWEEN 0 AND 100", name="ck_population_age30_rate"),
    )


def downgrade():
    op.drop_table("floating_population_history")
    op.drop_table("population_areas")
