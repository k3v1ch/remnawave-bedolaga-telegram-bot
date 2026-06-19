"""referral tracking counters (clicks + earned days) for SCR-REF stats

Adds two per-user counters used by the referral screen:
- ``users.referral_clicks_count`` — переходы по реф-ссылке (инкремент в /start).
- ``users.referral_days_earned`` — суммарно начислено бонусных дней за пополнения
  рефералов (инкремент в referral_service при начислении дней).

Revision ID: 0092
Revises: 0091
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0092'
down_revision: Union[str, None] = '0091'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('referral_clicks_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'users',
        sa.Column('referral_days_earned', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('users', 'referral_days_earned')
    op.drop_column('users', 'referral_clicks_count')
