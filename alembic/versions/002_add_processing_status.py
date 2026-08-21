"""add processing_status to youtube_uploads

Revision ID: 002
Revises: 001
Create Date: 2026-08-02 10:24:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('youtube_uploads', sa.Column('processing_status', sa.String(length=32), server_default='pending', nullable=False))


def downgrade() -> None:
    op.drop_column('youtube_uploads', 'processing_status')
