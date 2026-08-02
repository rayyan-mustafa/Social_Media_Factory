"""add business_model column

Revision ID: 003
Revises: 002
Create Date: 2026-08-02 11:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('business_model', sa.String(length=64), server_default='YouTube_Shorts', nullable=False))


def downgrade() -> None:
    op.drop_column('jobs', 'business_model')
