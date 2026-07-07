"""clone_bots: обязательная подписка на канал владельца

Пять колонок для white-label «обяз. подписки»: включена ли, numeric chat id канала,
ссылка для кнопки «Подписаться», название и кастомный текст заглушки. Проверка
участников идёт через самого клон-бота (getChatMember), поэтому владелец обязан
сделать своего бота админом канала — это валидируется при привязке канала в панели.

Revision ID: 0093
Revises: 0092
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0093'
down_revision: Union[str, None] = '0092'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'clone_bots',
        sa.Column('channel_sub_enabled', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column('clone_bots', sa.Column('channel_sub_chat_id', sa.BigInteger(), nullable=True))
    op.add_column('clone_bots', sa.Column('channel_sub_link', sa.String(length=500), nullable=True))
    op.add_column('clone_bots', sa.Column('channel_sub_title', sa.String(length=255), nullable=True))
    op.add_column('clone_bots', sa.Column('channel_sub_text', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('clone_bots', 'channel_sub_text')
    op.drop_column('clone_bots', 'channel_sub_title')
    op.drop_column('clone_bots', 'channel_sub_link')
    op.drop_column('clone_bots', 'channel_sub_chat_id')
    op.drop_column('clone_bots', 'channel_sub_enabled')
