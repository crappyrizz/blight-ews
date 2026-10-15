"""sensor_nodes.last_risk_level; index alerts.provider_message_id

last_risk_level remembers the last judged risk level per node, so the
high-risk SMS fires only when the level CHANGES to high (backend/alerts.py).
The index makes the Africa's Talking delivery-report callback, which looks an
alert up by its provider message id, a direct lookup.

Revision ID: db88501d3229
Revises: 8f8d68de0729
Create Date: 2026-10-12 17:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'db88501d3229'
down_revision: str | Sequence[str] | None = '8f8d68de0729'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sensor_nodes', sa.Column('last_risk_level', sa.String(length=10), nullable=True))
    op.create_check_constraint(
        'ck_sensor_nodes_last_risk_level',
        'sensor_nodes',
        "last_risk_level IN ('low', 'medium', 'high')",
    )
    op.create_index('ix_alerts_provider_message_id', 'alerts', ['provider_message_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_alerts_provider_message_id', table_name='alerts')
    op.drop_constraint('ck_sensor_nodes_last_risk_level', 'sensor_nodes', type_='check')
    op.drop_column('sensor_nodes', 'last_risk_level')
