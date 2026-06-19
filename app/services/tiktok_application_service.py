"""Сервис обработки заявок TikTok-программы.

Полностью отделён от партнёрки/реферальной системы: одобрение НЕ выдаёт
реферальный код, комиссию или возможность вывода. После одобрения автор шлёт
результаты в поддержку, а заработок проставляется вручную админом через журнал
начислений (:class:`TikTokEarning`).
"""

from datetime import UTC, datetime

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TikTokApplication, TikTokApplicationStatus, TikTokEarning, User


logger = structlog.get_logger(__name__)


class TikTokApplicationService:
    """Сервис управления заявками TikTok-программы и журналом начислений."""

    async def submit_application(
        self,
        db: AsyncSession,
        user_id: int,
        display_name: str | None = None,
        tiktok_url: str | None = None,
        other_platforms: str | None = None,
        audience_size: int | None = None,
        content_topic: str | None = None,
        description: str | None = None,
    ) -> tuple[TikTokApplication | None, str]:
        """Подаёт заявку на участие в TikTok-программе.

        Возвращает (application, error_message).
        """
        user = await db.get(User, user_id)
        if not user:
            return None, 'Пользователь не найден'

        if user.tiktok_status == TikTokApplicationStatus.APPROVED.value:
            return None, 'Вы уже участвуете в TikTok-программе'

        if user.tiktok_status == TikTokApplicationStatus.PENDING.value:
            return None, 'У вас уже есть заявка на рассмотрении'

        application = TikTokApplication(
            user_id=user_id,
            display_name=display_name,
            tiktok_url=tiktok_url,
            other_platforms=other_platforms,
            audience_size=audience_size,
            content_topic=content_topic,
            description=description,
        )

        user.tiktok_status = TikTokApplicationStatus.PENDING.value

        db.add(application)
        await db.commit()
        await db.refresh(application)

        logger.info('📝 Подана заявка в TikTok-программу', user_id=user_id, application_id=application.id)

        return application, ''

    async def approve_application(
        self,
        db: AsyncSession,
        application_id: int,
        admin_id: int,
        comment: str | None = None,
    ) -> tuple[bool, str]:
        """Одобряет заявку. НЕ выдаёт реф-код/комиссию/вывод."""
        result = await db.execute(
            select(TikTokApplication).where(TikTokApplication.id == application_id).with_for_update()
        )
        application = result.scalar_one_or_none()
        if not application:
            return False, 'Заявка не найдена'

        if application.status != TikTokApplicationStatus.PENDING.value:
            return False, 'Заявка уже обработана'

        user_result = await db.execute(select(User).where(User.id == application.user_id).with_for_update())
        user = user_result.scalar_one_or_none()
        if not user:
            return False, 'Пользователь не найден'

        user.tiktok_status = TikTokApplicationStatus.APPROVED.value

        application.status = TikTokApplicationStatus.APPROVED.value
        application.admin_comment = comment
        application.processed_by = admin_id
        application.processed_at = datetime.now(UTC)

        await db.commit()

        logger.info(
            '✅ Заявка в TikTok-программу одобрена',
            application_id=application_id,
            user_id=application.user_id,
            admin_id=admin_id,
        )

        return True, ''

    async def reject_application(
        self,
        db: AsyncSession,
        application_id: int,
        admin_id: int,
        comment: str | None = None,
    ) -> tuple[bool, str]:
        """Отклоняет заявку TikTok-программы."""
        result = await db.execute(
            select(TikTokApplication).where(TikTokApplication.id == application_id).with_for_update()
        )
        application = result.scalar_one_or_none()
        if not application:
            return False, 'Заявка не найдена'

        if application.status != TikTokApplicationStatus.PENDING.value:
            return False, 'Заявка уже обработана'

        user_result = await db.execute(select(User).where(User.id == application.user_id).with_for_update())
        user = user_result.scalar_one_or_none()
        if user:
            user.tiktok_status = TikTokApplicationStatus.REJECTED.value

        application.status = TikTokApplicationStatus.REJECTED.value
        application.admin_comment = comment
        application.processed_by = admin_id
        application.processed_at = datetime.now(UTC)

        await db.commit()

        logger.info(
            '❌ Заявка в TikTok-программу отклонена',
            application_id=application_id,
            user_id=application.user_id,
            admin_id=admin_id,
        )

        return True, ''

    async def revoke(self, db: AsyncSession, user_id: int, admin_id: int) -> tuple[bool, str]:
        """Исключает автора из TikTok-программы (журнал начислений сохраняется)."""
        user = await db.get(User, user_id)
        if not user:
            return False, 'Пользователь не найден'

        if user.tiktok_status != TikTokApplicationStatus.APPROVED.value:
            return False, 'Пользователь не участвует в TikTok-программе'

        user.tiktok_status = TikTokApplicationStatus.NONE.value
        await db.commit()

        logger.info('🚫 Автор исключён из TikTok-программы', user_id=user_id, admin_id=admin_id)
        return True, ''

    async def get_latest_application(self, db: AsyncSession, user_id: int) -> TikTokApplication | None:
        """Последняя заявка пользователя."""
        result = await db.execute(
            select(TikTokApplication)
            .where(TikTokApplication.user_id == user_id)
            .order_by(desc(TikTokApplication.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_all_applications(
        self,
        db: AsyncSession,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TikTokApplication], int]:
        """Заявки с фильтрацией. Возвращает (items, total)."""
        query = select(TikTokApplication)
        count_query = select(func.count()).select_from(TikTokApplication)

        if status:
            query = query.where(TikTokApplication.status == status)
            count_query = count_query.where(TikTokApplication.status == status)

        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        query = query.order_by(desc(TikTokApplication.created_at)).offset(offset).limit(limit)
        result = await db.execute(query)

        return list(result.scalars().all()), total

    # ── Журнал начислений ──────────────────────────────────────────────────

    async def get_creator_earnings_map(self, db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
        """Сумма начислений (копейки) по каждому из переданных пользователей."""
        if not user_ids:
            return {}
        result = await db.execute(
            select(TikTokEarning.user_id, func.coalesce(func.sum(TikTokEarning.amount_kopeks), 0))
            .where(TikTokEarning.user_id.in_(user_ids))
            .group_by(TikTokEarning.user_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def list_earnings(self, db: AsyncSession, user_id: int) -> list[TikTokEarning]:
        """Журнал начислений автора (свежие сверху)."""
        result = await db.execute(
            select(TikTokEarning)
            .where(TikTokEarning.user_id == user_id)
            .order_by(desc(TikTokEarning.created_at))
        )
        return list(result.scalars().all())

    async def add_earning(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        admin_id: int,
        comment: str | None = None,
    ) -> tuple[TikTokEarning | None, str]:
        """Добавляет запись начисления автору. amount_kopeks может быть отрицательным (корректировка)."""
        user = await db.get(User, user_id)
        if not user:
            return None, 'Пользователь не найден'

        earning = TikTokEarning(
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            comment=comment,
            created_by=admin_id,
        )
        db.add(earning)
        await db.commit()
        await db.refresh(earning)

        logger.info(
            '💰 Начисление TikTok-автору',
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            admin_id=admin_id,
        )
        return earning, ''

    async def delete_earning(self, db: AsyncSession, earning_id: int) -> tuple[bool, str]:
        """Удаляет запись начисления."""
        earning = await db.get(TikTokEarning, earning_id)
        if not earning:
            return False, 'Запись не найдена'
        await db.delete(earning)
        await db.commit()
        return True, ''


# Синглтон сервиса
tiktok_application_service = TikTokApplicationService()
