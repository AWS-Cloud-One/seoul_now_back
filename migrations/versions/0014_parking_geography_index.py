"""Index published parking coordinates for meter-distance searches.

Revision ID: 0014
Revises: 0013
Only an index is added; existing records and geometry are unchanged.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX idx_map_features_parking_geography ON map_features
        USING gist ((geometry::geography(Geometry,4326)))
        WHERE layer = 'parking' AND is_active IS TRUE
    """)


def downgrade():
    op.execute("DROP INDEX idx_map_features_parking_geography")
