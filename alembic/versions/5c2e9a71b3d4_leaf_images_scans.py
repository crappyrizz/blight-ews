"""leaf_images: scans of several photos (first_image_id, probabilities)

A leaf can now be checked from 1-3 photos at once (POST /scans). Each photo
is still its own leaf_images row; the second and third point to the first
through first_image_id, and the scan's single diagnosis stays linked to the
first photo, as before. probabilities keeps each photo's own CNN output, so
the combined result can always be traced back to the photos.

Existing rows are single-photo scans: first_image_id NULL, probabilities
NULL (their output is in diagnosis_results.probabilities).

Revision ID: 5c2e9a71b3d4
Revises: e77b7d65904d
Create Date: 2026-11-05 10:40:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5c2e9a71b3d4'
down_revision: str | Sequence[str] | None = 'e77b7d65904d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leaf_images', sa.Column('first_image_id', sa.String(length=32), nullable=True))
    op.add_column('leaf_images', sa.Column('probabilities', postgresql.JSONB(astext_type=sa.Text()),
                                           nullable=True))
    op.create_index(op.f('ix_leaf_images_first_image_id'), 'leaf_images', ['first_image_id'], unique=False)
    op.create_foreign_key('leaf_images_first_image_id_fkey', 'leaf_images', 'leaf_images',
                          ['first_image_id'], ['image_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('leaf_images_first_image_id_fkey', 'leaf_images', type_='foreignkey')
    op.drop_index(op.f('ix_leaf_images_first_image_id'), table_name='leaf_images')
    op.drop_column('leaf_images', 'probabilities')
    op.drop_column('leaf_images', 'first_image_id')
