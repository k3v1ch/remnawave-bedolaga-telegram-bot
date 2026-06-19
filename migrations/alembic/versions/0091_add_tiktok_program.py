"""tiktok program (separate creator track, no referral code / commission / withdrawal)

Adds ``users.tiktok_status`` plus the ``tiktok_applications`` and ``tiktok_earnings``
tables. The TikTok program is intentionally decoupled from the partner/referral
system: approval grants no referral code, no commission and no withdrawal — creators
send results to support, and earnings are filled in manually by an admin via the
``tiktok_earnings`` journal.

Revision ID: 0091
Revises: 0090
Create Date: 2026-06-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0091'
down_revision: Union[str, None] = '0090'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('tiktok_status', sa.String(length=20), nullable=False, server_default='none'),
    )
    op.create_index('ix_users_tiktok_status', 'users', ['tiktok_status'])

    op.create_table(
        'tiktok_applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('tiktok_url', sa.String(length=500), nullable=True),
        sa.Column('other_platforms', sa.String(length=500), nullable=True),
        sa.Column('audience_size', sa.Integer(), nullable=True),
        sa.Column('content_topic', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('admin_comment', sa.Text(), nullable=True),
        sa.Column('processed_by', sa.Integer(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['processed_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tiktok_applications_id', 'tiktok_applications', ['id'])
    op.create_index('ix_tiktok_applications_user_id', 'tiktok_applications', ['user_id'])
    op.create_index('ix_tiktok_applications_status', 'tiktok_applications', ['status'])

    op.create_table(
        'tiktok_earnings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tiktok_earnings_id', 'tiktok_earnings', ['id'])
    op.create_index('ix_tiktok_earnings_user_id', 'tiktok_earnings', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_tiktok_earnings_user_id', table_name='tiktok_earnings')
    op.drop_index('ix_tiktok_earnings_id', table_name='tiktok_earnings')
    op.drop_table('tiktok_earnings')

    op.drop_index('ix_tiktok_applications_status', table_name='tiktok_applications')
    op.drop_index('ix_tiktok_applications_user_id', table_name='tiktok_applications')
    op.drop_index('ix_tiktok_applications_id', table_name='tiktok_applications')
    op.drop_table('tiktok_applications')

    op.drop_index('ix_users_tiktok_status', table_name='users')
    op.drop_column('users', 'tiktok_status')
