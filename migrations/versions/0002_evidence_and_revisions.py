"""Add versioned PO review and preserve Milestone 1 approvals."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("intakes", sa.Column("canonical_id", sa.Integer()))
    op.add_column("intakes", sa.Column("context_issue", sa.String()))
    op.create_foreign_key("fk_intake_canonical", "intakes", "intakes", ["canonical_id"], ["id"])
    op.create_index("ix_intakes_canonical_id", "intakes", ["canonical_id"])

    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer", sa.String(), sa.ForeignKey("customers.reference"), nullable=False),
        sa.Column("po_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("active_version_id", sa.Integer()),
        sa.Column("active_snapshot_id", sa.Integer()),
        sa.UniqueConstraint("customer", "po_key", name="unique_purchase_order_key"),
        sa.CheckConstraint("(active_version_id IS NULL) = (active_snapshot_id IS NULL)"),
        sa.CheckConstraint("status IN ('needs_review', 'ready_for_approval', 'ordered')"),
    )
    op.create_table(
        "po_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("intake_id", sa.Integer(), sa.ForeignKey("intakes.id"), nullable=False, unique=True),
        sa.Column("po_id", sa.Integer(), sa.ForeignKey("purchase_orders.id")),
        sa.Column("number", sa.Integer()),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.UniqueConstraint("po_id", "number"),
        sa.UniqueConstraint("po_id", "id"),
        sa.UniqueConstraint("intake_id", "id"),
        sa.CheckConstraint("(po_id IS NULL) = (number IS NULL)"),
        sa.CheckConstraint("disposition IN ('unlinked', 'active', 'pending', 'equivalent', "
                           "'superseded', 'dismissed', 'post_order', 'exact_resend')"),
    )
    op.create_table(
        "draft_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("po_versions.id"), nullable=False),
        sa.Column("attempt_id", sa.Integer(), sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("parent_id", sa.Integer()),
        sa.Column("edit", sa.Integer(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("assessment", sa.JSON()),
        sa.UniqueConstraint("version_id", "id"),
        sa.UniqueConstraint("version_id", "edit"),
        sa.ForeignKeyConstraint(["version_id", "parent_id"], ["draft_snapshots.version_id", "draft_snapshots.id"]),
    )
    op.create_foreign_key("fk_po_active_version", "purchase_orders", "po_versions",
                          ["id", "active_version_id"], ["po_id", "id"])
    op.create_foreign_key("fk_po_active_snapshot", "purchase_orders", "draft_snapshots",
                          ["active_version_id", "active_snapshot_id"], ["version_id", "id"])
    op.create_table(
        "review_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("po_versions.id"), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("draft_snapshots.id")),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_review_events_version_id", "review_events", ["version_id"])
    op.add_column("sales_orders", sa.Column("po_id", sa.Integer()))
    op.add_column("sales_orders", sa.Column("version_id", sa.Integer()))

    op.execute("""
        INSERT INTO po_versions (intake_id, disposition)
        SELECT DISTINCT intake_id, 'unlinked' FROM attempts WHERE state = 'complete'
    """)
    op.execute("""
        INSERT INTO draft_snapshots (id, version_id, attempt_id, edit, author, reason, created_at, draft)
        SELECT a.id, v.id, a.id,
               row_number() OVER (PARTITION BY a.intake_id ORDER BY a.id), 'Ollama',
               'Original extraction migrated from Milestone 1', coalesce(a.finished_at, a.started_at), a.draft
        FROM attempts a JOIN po_versions v ON v.intake_id = a.intake_id WHERE a.state = 'complete'
    """)
    op.execute("""
        INSERT INTO purchase_orders (customer, po_key, status)
        SELECT customer, po_key, 'ordered' FROM sales_orders
    """)
    op.execute("""
        UPDATE po_versions v SET po_id = p.id, number = 1, disposition = 'active'
        FROM sales_orders s JOIN purchase_orders p ON (s.customer, s.po_key) = (p.customer, p.po_key)
        WHERE v.intake_id = s.intake_id
    """)
    op.execute("""
        UPDATE sales_orders s SET po_id = v.po_id, version_id = v.id
        FROM po_versions v WHERE v.intake_id = s.intake_id
    """)
    op.execute("""
        UPDATE purchase_orders p SET active_version_id = s.version_id, active_snapshot_id = s.snapshot_id
        FROM sales_orders s WHERE s.po_id = p.id
    """)

    op.drop_constraint("sales_orders_intake_id_snapshot_id_fkey", "sales_orders", type_="foreignkey")
    op.alter_column("sales_orders", "po_id", nullable=False)
    op.alter_column("sales_orders", "version_id", nullable=False)
    op.create_unique_constraint("uq_sales_orders_po_id", "sales_orders", ["po_id"])
    op.create_foreign_key("fk_order_po", "sales_orders", "purchase_orders", ["po_id"], ["id"])
    op.create_foreign_key("fk_order_po_version", "sales_orders", "po_versions",
                          ["po_id", "version_id"], ["po_id", "id"])
    op.create_foreign_key("fk_order_intake_version", "sales_orders", "po_versions",
                          ["intake_id", "version_id"], ["intake_id", "id"])
    op.create_foreign_key("fk_order_review_snapshot", "sales_orders", "draft_snapshots",
                          ["version_id", "snapshot_id"], ["version_id", "id"])


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM review_events) OR
               EXISTS (SELECT 1 FROM draft_snapshots WHERE assessment IS NOT NULL) OR
               EXISTS (SELECT 1 FROM intakes WHERE canonical_id IS NOT NULL OR context_issue IS NOT NULL)
            THEN RAISE EXCEPTION 'Milestone 2 review data exists. Restore a Milestone 1 database backup.';
            END IF;
        END $$
    """)
    for name in ("fk_order_review_snapshot", "fk_order_intake_version", "fk_order_po_version", "fk_order_po"):
        op.drop_constraint(name, "sales_orders", type_="foreignkey")
    op.drop_constraint("uq_sales_orders_po_id", "sales_orders", type_="unique")
    op.drop_column("sales_orders", "version_id")
    op.drop_column("sales_orders", "po_id")
    op.create_foreign_key("sales_orders_intake_id_snapshot_id_fkey", "sales_orders", "attempts",
                          ["intake_id", "snapshot_id"], ["intake_id", "id"])
    op.drop_table("review_events")
    op.drop_constraint("fk_po_active_snapshot", "purchase_orders", type_="foreignkey")
    op.drop_constraint("fk_po_active_version", "purchase_orders", type_="foreignkey")
    op.drop_table("draft_snapshots")
    op.drop_table("po_versions")
    op.drop_table("purchase_orders")
    op.drop_index("ix_intakes_canonical_id", table_name="intakes")
    op.drop_constraint("fk_intake_canonical", "intakes", type_="foreignkey")
    op.drop_column("intakes", "context_issue")
    op.drop_column("intakes", "canonical_id")
