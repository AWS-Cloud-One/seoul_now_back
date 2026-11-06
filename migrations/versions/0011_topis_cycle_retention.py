"""Separate current TOPIS state and evidence from bounded cycle logs.

Revision ID: 0011
Revises: 0010
Existing observations, receipts and payloads are preserved; runtime retention prunes logs.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("raw_payloads", sa.Column("source", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_raw_payloads_source_data_sources",
        "raw_payloads",
        "data_sources",
        ["source"],
        ["source_code"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_raw_payloads_source", "raw_payloads", ["source"])
    op.execute("""
        UPDATE raw_payloads p SET source=r.source FROM collection_runs r WHERE p.run_id=r.id
    """)
    op.drop_constraint("fk_raw_payloads_run_id_collection_runs", "raw_payloads", type_="foreignkey")
    op.alter_column("raw_payloads", "run_id", nullable=True)
    op.create_foreign_key(
        "fk_raw_payloads_run_id_collection_runs",
        "raw_payloads",
        "collection_runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for table in ("road_traffic_latest", "map_road_observations"):
        op.add_column(table, sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(f"""
            UPDATE {table} t SET received_at=p.received_at
            FROM raw_payloads p WHERE t.payload_id=p.payload_id
        """)
    op.drop_constraint(
        "fk_map_road_observations_last_attempt_id_collection_runs",
        "map_road_observations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_map_road_observations_last_attempt_id_collection_runs",
        "map_road_observations",
        "collection_runs",
        ["last_attempt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "road_traffic_attempt_latest",
        sa.Column("link_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("observation_updated", sa.Boolean(), nullable=False),
        sa.CheckConstraint("status IN ('success','partial','failed')", name="status"),
        sa.CheckConstraint("attempts >= 1", name="attempts_positive"),
        sa.ForeignKeyConstraint(["link_id"], ["road_links.link_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_road_traffic_attempt_latest_run_id", "road_traffic_attempt_latest", ["run_id"]
    )
    op.execute("""
        INSERT INTO road_traffic_attempt_latest
            (link_id, run_id, attempted_at, status, error_code, attempts, observation_updated)
        SELECT DISTINCT ON (l.link_id)
            l.link_id, r.id, COALESCE(r.finished_at,r.started_at), r.status, r.error_code, 1,
            COALESCE(p.run_id=r.id, false)
        FROM collection_runs r JOIN road_links l ON r.target_key='link:' || l.link_id
        LEFT JOIN road_traffic_latest t ON t.link_id=l.link_id AND t.source='topis_traffic'
        LEFT JOIN raw_payloads p ON p.payload_id=t.payload_id
        WHERE r.source='topis_traffic' AND r.status <> 'running'
            AND r.run_kind IN ('collection','legacy')
        ORDER BY l.link_id, r.started_at DESC, r.id DESC
    """)
    op.create_table(
        "payload_file_deletions",
        sa.Column("file_ref", sa.Text(), primary_key=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    raise RuntimeError(
        "0011 cannot reconstruct pruned cycle logs; rollback requires restoring the DB "
        "and evidence backup together"
    )
