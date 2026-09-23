"""risk_scores: nullable score, status, valid_hours

Revision ID: f0c9c93c7c21
Revises: d0ed9462fca9
Create Date: 2026-09-23 16:59:21.097101

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f0c9c93c7c21'
down_revision: Union[str, Sequence[str], None] = 'd0ed9462fca9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default lets the NOT NULL column be added to a table that
    # already has rows: every existing row was a judged day, so 'ok' is
    # correct for them. The default is then dropped, so new rows must state
    # their status explicitly.
    op.add_column(
        'risk_scores',
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ok'),
    )
    op.alter_column('risk_scores', 'status', server_default=None)

    op.add_column('risk_scores', sa.Column('valid_hours', sa.Integer(), nullable=True))

    # An unjudged day stores score = NULL rather than 0.0.
    op.alter_column('risk_scores', 'score',
               existing_type=sa.DOUBLE_PRECISION(precision=53),
               nullable=True)

    # Autogenerate does not detect CHECK constraints, so they are written out here.
    op.create_check_constraint(
        'ck_risk_scores_status',
        'risk_scores',
        "status IN ('ok', 'insufficient_data', 'no_data')",
    )
    op.create_check_constraint(
        'ck_risk_scores_score_matches_status',
        'risk_scores',
        "(status = 'ok') = (score IS NOT NULL)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_risk_scores_score_matches_status', 'risk_scores', type_='check')
    op.drop_constraint('ck_risk_scores_status', 'risk_scores', type_='check')
    # Rows with an unknown day have score NULL and cannot be represented by
    # the old schema, so they are removed rather than given a made-up 0.0.
    op.execute("DELETE FROM risk_scores WHERE score IS NULL")
    op.alter_column('risk_scores', 'score',
               existing_type=sa.DOUBLE_PRECISION(precision=53),
               nullable=False)
    op.drop_column('risk_scores', 'valid_hours')
    op.drop_column('risk_scores', 'status')
