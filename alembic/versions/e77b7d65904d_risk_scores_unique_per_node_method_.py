"""risk_scores: one row per node, method, source, horizon and day

Two scoring runs for the same node at the same moment (an admin re-score
pressed twice) each inserted a full set of rows, because both looked for an
existing row before either had committed. The code now locks the node while
it scores (backend/risk_job.score_node); this constraint makes the database
refuse a duplicate even if something bypasses that.

Existing duplicates are removed first. For each key the row kept is the one
an alert points to, else the newest; alerts that pointed to a removed copy
are re-pointed to the kept row, so no SMS record loses its link.

Revision ID: e77b7d65904d
Revises: a47f6053ec37
Create Date: 2026-10-26 11:05:00

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e77b7d65904d'
down_revision: str | Sequence[str] | None = 'a47f6053ec37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RANKED = """
    WITH ranked AS (
        SELECT s.score_id,
               first_value(s.score_id) OVER (
                   PARTITION BY s.node_id, s.method, s.data_source, s.horizon_hours, s.for_date
                   ORDER BY EXISTS (SELECT 1 FROM alerts a WHERE a.risk_score_id = s.score_id) DESC,
                            s.computed_at DESC, s.score_id DESC
               ) AS keep_id
        FROM risk_scores s
    )
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(RANKED + """
        UPDATE alerts SET risk_score_id = r.keep_id
        FROM ranked r
        WHERE alerts.risk_score_id = r.score_id AND r.score_id <> r.keep_id
    """)
    op.execute(RANKED + """
        DELETE FROM risk_scores
        WHERE score_id IN (SELECT score_id FROM ranked WHERE score_id <> keep_id)
    """)
    op.create_unique_constraint(
        'uq_risk_scores_node_method_source_horizon_day', 'risk_scores',
        ['node_id', 'method', 'data_source', 'horizon_hours', 'for_date'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_risk_scores_node_method_source_horizon_day', 'risk_scores', type_='unique')
