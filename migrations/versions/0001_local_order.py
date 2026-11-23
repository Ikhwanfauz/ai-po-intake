"""Create the local intake, approval, and sales order tables."""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('customers',
        sa.Column('reference', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('reference')
    )
    op.create_table('documents',
        sa.Column('digest', sa.String(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('digest'),
        sa.UniqueConstraint('path')
    )
    op.create_table('skus',
        sa.Column('code', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('code')
    )
    op.create_table('customer_aliases',
        sa.Column('alias', sa.String(), nullable=False),
        sa.Column('customer', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['customer'], ['customers.reference'], ),
        sa.PrimaryKeyConstraint('alias')
    )
    op.create_table('intakes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('source', sa.String(), server_default='upload', nullable=False),
        sa.Column('customer_context', sa.String(), nullable=True),
        sa.Column('state', sa.String(), server_default='pending', nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("state IN ('pending', 'processing', 'complete', 'failed')"),
        sa.ForeignKeyConstraint(['customer_context'], ['customers.reference'], ),
        sa.ForeignKeyConstraint(['digest'], ['documents.digest'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('prices',
        sa.Column('customer', sa.String(), nullable=False),
        sa.Column('sku', sa.String(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint('amount >= 0'),
        sa.ForeignKeyConstraint(['customer'], ['customers.reference'], ),
        sa.ForeignKeyConstraint(['sku'], ['skus.code'], ),
        sa.PrimaryKeyConstraint('customer', 'sku', 'currency')
    )
    op.create_table('sku_aliases',
        sa.Column('alias', sa.String(), nullable=False),
        sa.Column('sku', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['sku'], ['skus.code'], ),
        sa.PrimaryKeyConstraint('alias')
    )
    op.create_table('attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('intake_id', sa.Integer(), nullable=False),
        sa.Column('pipeline', sa.String(), server_default='baseline', nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('settings', sa.JSON(), nullable=False),
        sa.Column('pages', sa.JSON(), nullable=True),
        sa.Column('raw', sa.String(), nullable=True),
        sa.Column('draft', sa.JSON(), nullable=True),
        sa.Column('validation', sa.JSON(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.CheckConstraint("state IN ('processing', 'complete', 'failed')"),
        sa.ForeignKeyConstraint(['intake_id'], ['intakes.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('intake_id', 'id')
    )
    op.create_index(op.f('ix_attempts_intake_id'), 'attempts', ['intake_id'], unique=False)
    op.create_index('one_processing_attempt', 'attempts', ['intake_id', 'pipeline'], unique=True, postgresql_where=sa.text("state = 'processing'"))
    op.create_table('sales_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer', sa.String(), nullable=False),
        sa.Column('po_key', sa.String(), nullable=False),
        sa.Column('po_number', sa.String(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False),
        sa.Column('intake_id', sa.Integer(), nullable=False),
        sa.Column('snapshot_id', sa.Integer(), nullable=False),
        sa.Column('reviewer', sa.String(), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['customer'], ['customers.reference'], ),
        sa.ForeignKeyConstraint(['intake_id', 'snapshot_id'], ['attempts.intake_id', 'attempts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('customer', 'po_key', name='one_order_per_customer_po'),
        sa.UniqueConstraint('snapshot_id')
    )
    op.create_table('order_lines',
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('sku', sa.String(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint('quantity > 0'),
        sa.CheckConstraint('unit_price >= 0'),
        sa.ForeignKeyConstraint(['order_id'], ['sales_orders.id'], ),
        sa.ForeignKeyConstraint(['sku'], ['skus.code'], ),
        sa.PrimaryKeyConstraint('order_id', 'position')
    )


def downgrade():
    op.drop_table('order_lines')
    op.drop_table('sales_orders')
    op.drop_index('one_processing_attempt', table_name='attempts', postgresql_where=sa.text("state = 'processing'"))
    op.drop_index(op.f('ix_attempts_intake_id'), table_name='attempts')
    op.drop_table('attempts')
    op.drop_table('sku_aliases')
    op.drop_table('prices')
    op.drop_table('intakes')
    op.drop_table('customer_aliases')
    op.drop_table('skus')
    op.drop_table('documents')
    op.drop_table('customers')
