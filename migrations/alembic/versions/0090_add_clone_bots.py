"""clone_bots (white-label reseller bots) + attribution columns

Adds the ``clone_bots`` table — one row per reseller-submitted bot token, all served
by the single shared ``cloner`` container (hot-swap, no per-bot container). Also adds
attribution FKs ``users.clone_bot_id`` and ``transactions.clone_bot_id`` so the CRM can
report "who brought how many users" and revenue per clone. Both attribution FKs are
``ON DELETE SET NULL`` so removing a clone never deletes user rows or transactions.

Revision ID: 0090
Revises: 0089
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0090'
down_revision: Union[str, None] = '0089'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'clone_bots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=False),
        sa.Column('bot_id', sa.BigInteger(), nullable=False),
        sa.Column('bot_username', sa.String(length=255), nullable=True),
        sa.Column('bot_title', sa.String(length=255), nullable=True),
        sa.Column('token_encrypted', sa.Text(), nullable=False),
        sa.Column('webhook_secret', sa.String(length=128), nullable=False),
        sa.Column('external_squad_uuid', sa.String(length=255), nullable=True),
        sa.Column('external_squad_name', sa.String(length=255), nullable=True),
        sa.Column('profile_title', sa.String(length=255), nullable=True),
        sa.Column('subpage_config_uuid', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clone_bots_id', 'clone_bots', ['id'])
    op.create_index('ix_clone_bots_owner_user_id', 'clone_bots', ['owner_user_id'])
    op.create_index('ix_clone_bots_status', 'clone_bots', ['status'])
    op.create_index('ix_clone_bots_bot_id', 'clone_bots', ['bot_id'], unique=True)
    op.create_index('ix_clone_bots_external_squad_uuid', 'clone_bots', ['external_squad_uuid'])

    op.add_column('users', sa.Column('clone_bot_id', sa.Integer(), nullable=True))
    op.create_index('ix_users_clone_bot_id', 'users', ['clone_bot_id'])
    op.create_foreign_key(
        'fk_users_clone_bot_id', 'users', 'clone_bots', ['clone_bot_id'], ['id'], ondelete='SET NULL'
    )

    op.add_column('transactions', sa.Column('clone_bot_id', sa.Integer(), nullable=True))
    op.create_index('ix_transactions_clone_bot_id', 'transactions', ['clone_bot_id'])
    op.create_foreign_key(
        'fk_transactions_clone_bot_id', 'transactions', 'clone_bots', ['clone_bot_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_transactions_clone_bot_id', 'transactions', type_='foreignkey')
    op.drop_index('ix_transactions_clone_bot_id', table_name='transactions')
    op.drop_column('transactions', 'clone_bot_id')

    op.drop_constraint('fk_users_clone_bot_id', 'users', type_='foreignkey')
    op.drop_index('ix_users_clone_bot_id', table_name='users')
    op.drop_column('users', 'clone_bot_id')

    op.drop_index('ix_clone_bots_external_squad_uuid', table_name='clone_bots')
    op.drop_index('ix_clone_bots_bot_id', table_name='clone_bots')
    op.drop_index('ix_clone_bots_status', table_name='clone_bots')
    op.drop_index('ix_clone_bots_owner_user_id', table_name='clone_bots')
    op.drop_index('ix_clone_bots_id', table_name='clone_bots')
    op.drop_table('clone_bots')
