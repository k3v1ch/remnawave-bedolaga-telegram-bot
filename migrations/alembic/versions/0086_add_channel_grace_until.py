"""add users.channel_grace_until for channel-leave grace period

When a user leaves a required channel we no longer disable their VPN
immediately. Instead we record a deadline (now + CHANNEL_LEAVE_GRACE_HOURS)
and give them a chance to resubscribe. A background sweep disables the VPN
only once the deadline passes and the user is still unsubscribed.

This migration adds the nullable ``channel_grace_until`` timestamp column
that stores that pending deadline (NULL = no pending disable).

Revision ID: 0086
Revises: 0085
Create Date: 2026-06-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0086'
down_revision: Union[str, None] = '0085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(col['name'] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column('users', 'channel_grace_until'):
        op.add_column(
            'users',
            sa.Column('channel_grace_until', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column('users', 'channel_grace_until'):
        op.drop_column('users', 'channel_grace_until')
