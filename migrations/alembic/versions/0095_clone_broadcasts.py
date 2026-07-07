"""clone_broadcasts: рассылки владельцев клон-ботов

Пост (текст/фото + опциональные кнопки), статус и счётчики доставки. Рассылка идёт
строго по юзерам одного клона (users.clone_bot_id) от имени самого клон-бота —
основной бот и соседние клоны не задеваются. Лимит — N создании в сутки на клона
(константа в crud, сейчас 10).

Revision ID: 0095
Revises: 0094
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0095'
down_revision: Union[str, None] = '0094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'clone_broadcasts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('clone_bot_id', sa.Integer(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=True),
        sa.Column('media_type', sa.String(length=20), nullable=True),
        sa.Column('media_file_id', sa.String(length=255), nullable=True),
        sa.Column('button_text', sa.String(length=64), nullable=True),
        sa.Column('button_url', sa.String(length=500), nullable=True),
        sa.Column('show_tariffs_button', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='in_progress'),
        sa.Column('total_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sent_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['clone_bot_id'], ['clone_bots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clone_broadcasts_id', 'clone_broadcasts', ['id'])
    op.create_index('ix_clone_broadcasts_clone_bot_id', 'clone_broadcasts', ['clone_bot_id'])
    op.create_index('ix_clone_broadcasts_created_at', 'clone_broadcasts', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_clone_broadcasts_created_at', table_name='clone_broadcasts')
    op.drop_index('ix_clone_broadcasts_clone_bot_id', table_name='clone_broadcasts')
    op.drop_index('ix_clone_broadcasts_id', table_name='clone_broadcasts')
    op.drop_table('clone_broadcasts')
