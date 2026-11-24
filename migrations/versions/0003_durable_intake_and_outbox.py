"""Add the durable extraction queue, Gmail receipt keys, and notification outbox."""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('extraction_jobs',
        sa.Column('intake_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(), server_default='pending', nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('claim_token', sa.String(), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempt_id', sa.Integer(), nullable=True),
        sa.CheckConstraint("(state = 'running') = (claim_token IS NOT NULL AND lease_until IS NOT NULL AND attempt_id IS NOT NULL)"),
        sa.CheckConstraint("state IN ('pending', 'running', 'complete', 'failed')"),
        sa.ForeignKeyConstraint(['attempt_id'], ['attempts.id'], ),
        sa.ForeignKeyConstraint(['intake_id'], ['intakes.id'], ),
        sa.PrimaryKeyConstraint('intake_id')
    )
    op.create_index('ix_extraction_jobs_ready', 'extraction_jobs', ['state', 'available_at'], unique=False)
    op.create_table('notification_intents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_key', sa.String(), nullable=False),
        sa.Column('intake_id', sa.Integer(), nullable=False),
        sa.Column('po_id', sa.Integer(), nullable=True),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('episode_scope', sa.String(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('channel', sa.String(), nullable=False),
        sa.Column('destination', sa.String(), nullable=True),
        sa.Column('destination_alias', sa.String(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(), server_default='pending', nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('current_attempt_id', sa.String(), nullable=True),
        sa.Column('retryable', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("channel IN ('telegram', 'gmail')"),
        sa.CheckConstraint("state IN ('pending', 'sending', 'sent', 'failed', 'unknown')"),
        sa.ForeignKeyConstraint(['intake_id'], ['intakes.id'], ),
        sa.ForeignKeyConstraint(['order_id'], ['sales_orders.id'], ),
        sa.ForeignKeyConstraint(['po_id'], ['purchase_orders.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_key'),
        sa.UniqueConstraint('order_id')
    )
    op.create_index(op.f('ix_notification_intents_intake_id'), 'notification_intents', ['intake_id'], unique=False)
    op.create_index(op.f('ix_notification_intents_po_id'), 'notification_intents', ['po_id'], unique=False)
    op.create_index('ix_notification_ready', 'notification_intents', ['state', 'available_at'], unique=False)
    op.create_index('one_open_review_episode', 'notification_intents', ['episode_scope'], unique=True, postgresql_where=sa.text('episode_scope IS NOT NULL AND closed_at IS NULL'))
    op.create_table('notification_attempts',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('notification_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deadline', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('outcome', sa.String(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('reconciliation', sa.JSON(), nullable=True),
        sa.Column('retry_authorization', sa.JSON(), nullable=True),
        sa.CheckConstraint("outcome IN ('sending', 'sent', 'failed', 'unknown')"),
        sa.ForeignKeyConstraint(['notification_id'], ['notification_intents.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('notification_id', 'number')
    )
    op.add_column('intakes', sa.Column('event_key', sa.String(), nullable=True))
    op.add_column('intakes', sa.Column('sender', sa.String(), nullable=True))
    op.add_column('intakes', sa.Column('gmail_message_id', sa.String(), nullable=True))
    op.add_column('intakes', sa.Column('gmail_attachment_id', sa.String(), nullable=True))
    op.create_unique_constraint('unique_intake_event', 'intakes', ['source', 'event_key'])
    op.execute("""
        UPDATE attempts
        SET state = 'failed', finished_at = clock_timestamp(),
            error = 'Foreground extraction interrupted by migration to the worker.'
        WHERE state = 'processing'
    """)
    op.execute("UPDATE intakes SET state = 'pending' WHERE state = 'processing'")
    op.execute("""
        INSERT INTO extraction_jobs (intake_id)
        SELECT id FROM intakes WHERE state = 'pending' AND canonical_id IS NULL
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM notification_intents)
                OR EXISTS (SELECT 1 FROM intakes WHERE event_key IS NOT NULL)
                OR EXISTS (SELECT 1 FROM extraction_jobs WHERE state = 'running')
            THEN RAISE EXCEPTION 'Milestone 3 workflow history exists. Restore a database backup to roll back.';
            END IF;
        END $$
    """)
    op.drop_constraint('unique_intake_event', 'intakes', type_='unique')
    op.drop_column('intakes', 'gmail_attachment_id')
    op.drop_column('intakes', 'gmail_message_id')
    op.drop_column('intakes', 'sender')
    op.drop_column('intakes', 'event_key')
    op.drop_table('notification_attempts')
    op.drop_index('one_open_review_episode', table_name='notification_intents', postgresql_where=sa.text('episode_scope IS NOT NULL AND closed_at IS NULL'))
    op.drop_index('ix_notification_ready', table_name='notification_intents')
    op.drop_index(op.f('ix_notification_intents_po_id'), table_name='notification_intents')
    op.drop_index(op.f('ix_notification_intents_intake_id'), table_name='notification_intents')
    op.drop_table('notification_intents')
    op.drop_index('ix_extraction_jobs_ready', table_name='extraction_jobs')
    op.drop_table('extraction_jobs')
