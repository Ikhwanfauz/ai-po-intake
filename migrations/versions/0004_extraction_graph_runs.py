"""Persist automatic extraction review traces and staff draft candidates."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "extraction_graph_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("intake_id", sa.Integer(), sa.ForeignKey("intakes.id"), nullable=False),
        sa.Column("source_snapshot_id", sa.Integer(), sa.ForeignKey("draft_snapshots.id"), nullable=False),
        sa.Column("expected_active_snapshot_id", sa.Integer(), sa.ForeignKey("draft_snapshots.id")),
        sa.Column("adopted_snapshot_id", sa.Integer(), sa.ForeignKey("draft_snapshots.id"), unique=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("candidate", sa.JSON()),
        sa.Column("assessment", sa.JSON()),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("mode IN ('automatic', 'staff')"),
    )
    op.create_index("ix_extraction_graph_runs_intake_id", "extraction_graph_runs", ["intake_id"])


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM extraction_graph_runs)
            THEN RAISE EXCEPTION 'Graph review history exists. Restore a database backup to roll back.';
            END IF;
        END $$
    """)
    op.drop_table("extraction_graph_runs")
