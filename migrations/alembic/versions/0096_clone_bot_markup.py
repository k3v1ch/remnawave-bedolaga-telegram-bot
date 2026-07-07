"""clone_bots.pricing_markup_pct: наценка владельца-партнёра на цены тарифов в клоне

Процент 0–500 (валидируется в панели). Применяется ТОЛЬКО в контексте этого клона —
основной бот и другие клоны считают цены без изменений. Прямых выплат по наценке нет:
владелец-партнёр зарабатывает на выросших пополнениях через обычную клон-комиссию.

Revision ID: 0096
Revises: 0095
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'clone_bots',
        sa.Column('pricing_markup_pct', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('clone_bots', 'pricing_markup_pct')
