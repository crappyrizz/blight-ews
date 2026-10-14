"""diagnosis_results: risk_basis and risk_for_date

Records which weather risk r fusion used for each diagnosis (the learned 24 h
forecast, the day's Hutton score, a recent day's Hutton score, or the neutral
prior) and the day that risk describes.

risk_basis is NOT NULL with no default on purpose: no code wrote diagnoses
before this revision, and inventing a basis for a row would be a false
record. If hand-made rows exist, the upgrade fails and says so.

Revision ID: 8f8d68de0729
Revises: b181c764df90
Create Date: 2026-10-12 16:15:37.721041

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8f8d68de0729'
down_revision: str | Sequence[str] | None = 'b181c764df90'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('diagnosis_results', sa.Column('risk_basis', sa.String(length=20), nullable=False))
    op.add_column('diagnosis_results', sa.Column('risk_for_date', sa.Date(), nullable=True))
    op.create_check_constraint(
        'ck_diagnosis_results_risk_basis',
        'diagnosis_results',
        "risk_basis IN ('learned_24h', 'hutton_today', 'hutton_fallback', 'neutral_prior')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_diagnosis_results_risk_basis', 'diagnosis_results', type_='check')
    op.drop_column('diagnosis_results', 'risk_for_date')
    op.drop_column('diagnosis_results', 'risk_basis')
