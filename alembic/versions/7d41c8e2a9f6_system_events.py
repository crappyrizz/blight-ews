"""system_events: what went wrong, in plain words

A table the system writes its problems to (backend/events.py): a sensor's
scoring that failed, an SMS not sent or not delivered, the leaf checker not
loaded, an unexpected server error; plus one 'info' row per risk-job run, so
the admin status can say when it last ran. Logs disappear when the hosted
server restarts; these rows stay.

node_id and farmer_id are plain labels without foreign keys: an event is
written in its own transaction while the risk job may hold a row lock on the
node, and a foreign-key check would wait on that lock.

Revision ID: 7d41c8e2a9f6
Revises: 5c2e9a71b3d4
Create Date: 2026-11-05 13:20:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7d41c8e2a9f6'
down_revision: str | Sequence[str] | None = '5c2e9a71b3d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'system_events',
        sa.Column('event_id', sa.String(length=32), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('node_id', sa.String(length=32), nullable=True),
        sa.Column('farmer_id', sa.String(length=32), nullable=True),
        sa.CheckConstraint("severity IN ('info', 'warning', 'error')", name='ck_system_events_severity'),
        sa.PrimaryKeyConstraint('event_id'),
    )
    op.create_index(op.f('ix_system_events_occurred_at'), 'system_events', ['occurred_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_system_events_occurred_at'), table_name='system_events')
    op.drop_table('system_events')
