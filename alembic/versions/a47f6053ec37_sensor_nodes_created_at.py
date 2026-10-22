"""sensor_nodes.created_at

When a node (device) was registered. Existing nodes get the time this
migration runs, since their real registration time was never recorded.
last_sync_time already records when a node was last seen.

Revision ID: a47f6053ec37
Revises: db88501d3229
Create Date: 2026-10-22 09:26:27

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a47f6053ec37'
down_revision: str | Sequence[str] | None = 'db88501d3229'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sensor_nodes', sa.Column('created_at', sa.DateTime(timezone=True),
                                            server_default=sa.text('now()'), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sensor_nodes', 'created_at')
