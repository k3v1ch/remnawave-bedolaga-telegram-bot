"""clone_bot_links: рекламные ссылки клон-ботов (только статистика)

Ссылка вида ``t.me/<клон>?start=ad_<slug>``: владелец создаёт её в панели «Мои боты»,
клики и регистрации копятся в счётчиках, атрибуция юзера к ссылке — ``users.clone_link_id``
(для выручки/пополнений по ссылке). Бонусов за переход нет — в отличие от
``advertising_campaigns`` основного бота.

Revision ID: 0094
Revises: 0093
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0094'
down_revision: Union[str, None] = '0093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'clone_bot_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('clone_bot_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=32), nullable=False),
        sa.Column('clicks_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('registrations_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['clone_bot_id'], ['clone_bots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clone_bot_links_id', 'clone_bot_links', ['id'])
    op.create_index('ix_clone_bot_links_clone_bot_id', 'clone_bot_links', ['clone_bot_id'])
    op.create_index('ix_clone_bot_links_slug', 'clone_bot_links', ['slug'], unique=True)

    op.add_column('users', sa.Column('clone_link_id', sa.Integer(), nullable=True))
    op.create_index('ix_users_clone_link_id', 'users', ['clone_link_id'])
    op.create_foreign_key(
        'fk_users_clone_link_id', 'users', 'clone_bot_links', ['clone_link_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_users_clone_link_id', 'users', type_='foreignkey')
    op.drop_index('ix_users_clone_link_id', table_name='users')
    op.drop_column('users', 'clone_link_id')

    op.drop_index('ix_clone_bot_links_slug', table_name='clone_bot_links')
    op.drop_index('ix_clone_bot_links_clone_bot_id', table_name='clone_bot_links')
    op.drop_index('ix_clone_bot_links_id', table_name='clone_bot_links')
    op.drop_table('clone_bot_links')
