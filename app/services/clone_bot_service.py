"""Service layer for white-label clone bots.

Two responsibilities:
  1. Provision / update / delete the Remnawave **external squad** for a reseller
     (its name + ``subscriptionSettings.profileTitle`` — the title shown in VPN clients).
  2. ``resolve_external_squad_uuid`` — the single source of truth for which external
     squad a panel user belongs to: a clone bot's squad overrides the tariff's squad.

The ``profileTitle`` key was confirmed against panel.vernovpn.com
(GET /api/subscription-settings → {"profileTitle": ...}); external squads override the
same field via ``subscriptionSettings``.
"""

from __future__ import annotations

import re

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CloneBot, Tariff


logger = structlog.get_logger(__name__)

_CHANNEL_USERNAME_RE = re.compile(r'^@?([A-Za-z0-9_]{4,32})$')
_CHANNEL_LINK_RE = re.compile(r'^(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})/?$')


def parse_channel_ref(raw: str) -> str | None:
    """@username / t.me/username / username → '@username' (или None, если не похоже)."""
    raw = raw.strip()
    m = _CHANNEL_LINK_RE.match(raw) or _CHANNEL_USERNAME_RE.match(raw)
    return f'@{m.group(1)}' if m else None


class CloneChannelVerifyError(Exception):
    """Канал обязательной подписки не прошёл проверку.

    ``code``: clone_unavailable / channel_not_found / not_a_channel / bot_not_admin /
    no_public_link. Тексты для людей рисуют вызывающие стороны (бот, кабинет).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def verify_clone_channel(clone: CloneBot, channel_ref: str | int) -> tuple[int, str, str | None]:
    """Проверить канал ЧЕРЕЗ САМОГО клон-бота: канал существует и клон-бот в нём админ
    (без админки Telegram не даёт боту звать getChatMember по участникам канала).

    Возвращает ``(chat_id, join_link, title)`` или бросает :class:`CloneChannelVerifyError`.
    """
    from app.bot_factory import create_bot
    from app.database.crud.clone_bot import get_decrypted_token

    try:
        probe = create_bot(token=get_decrypted_token(clone))
    except Exception:
        raise CloneChannelVerifyError('clone_unavailable')
    try:
        try:
            chat = await probe.get_chat(channel_ref)
        except Exception:
            raise CloneChannelVerifyError('channel_not_found')
        if chat.type != 'channel':
            raise CloneChannelVerifyError('not_a_channel')
        try:
            member = await probe.get_chat_member(chat.id, clone.bot_id)
        except Exception:
            member = None
        if member is None or member.status not in ('administrator', 'creator'):
            raise CloneChannelVerifyError('bot_not_admin')
        link = f'https://t.me/{chat.username}' if chat.username else (chat.invite_link or '')
        if not link:
            raise CloneChannelVerifyError('no_public_link')
        return chat.id, link, chat.title
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass


async def provision_squad(name: str, profile_title: str | None) -> tuple[str, str]:
    """Create an external squad for a reseller and apply its profile title.

    Returns ``(external_squad_uuid, external_squad_name)``. Raises on create failure;
    a profile-title failure is logged but not fatal (squad still usable, inherits global).
    """
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    async with service.get_api_client() as api:
        squad = await api.create_external_squad(name)
        if profile_title:
            try:
                squad = await api.update_external_squad(
                    squad.uuid,
                    subscription_settings={'profileTitle': profile_title},
                )
            except Exception:
                logger.warning(
                    'Failed to set profileTitle on external squad',
                    squad_uuid=squad.uuid,
                    exc_info=True,
                )
        logger.info('Provisioned external squad for clone bot', squad_uuid=squad.uuid, name=squad.name)
        return squad.uuid, squad.name


async def update_squad_profile_title(external_squad_uuid: str, profile_title: str) -> None:
    """Update the profile title of an existing reseller squad."""
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    async with service.get_api_client() as api:
        await api.update_external_squad(
            external_squad_uuid,
            subscription_settings={'profileTitle': profile_title},
        )


async def delete_squad(external_squad_uuid: str) -> bool:
    """Delete a reseller's external squad from the panel (best-effort)."""
    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    async with service.get_api_client() as api:
        return await api.delete_external_squad(external_squad_uuid)


async def cleanup_squad_on_delete(db: AsyncSession, clone: CloneBot) -> None:
    """Drop the clone's external squad from the panel, but only when it has no clients left.

    A clone's squad holds its subscribers' panel users, so deleting it for a live reseller
    would strip those clients of their squad assignment. We only clean up the leftover squad
    of an empty (e.g. test) clone. Best-effort: panel errors are logged, never raised.
    """
    squad_uuid = clone.external_squad_uuid
    if not squad_uuid:
        return

    from app.database.crud.clone_bot import count_brought_users

    clients = await count_brought_users(db, clone.id)
    if clients > 0:
        logger.info(
            'Keeping external squad on clone delete: clone still has clients',
            clone_id=clone.id,
            clients=clients,
            squad_uuid=squad_uuid,
        )
        return

    try:
        await delete_squad(squad_uuid)
        logger.info(
            'Deleted leftover external squad on clone delete',
            clone_id=clone.id,
            squad_uuid=squad_uuid,
        )
    except Exception:
        logger.warning(
            'Failed to delete external squad on clone delete',
            clone_id=clone.id,
            squad_uuid=squad_uuid,
            exc_info=True,
        )


async def resolve_external_squad_uuid(
    db: AsyncSession,
    *,
    clone_bot_id: int | None,
    tariff: Tariff | None,
) -> str | None:
    """Decide which external squad a panel user belongs to.

    White-label rule: if the user was brought in by a clone bot that has a provisioned
    squad, that squad wins (every clone user lands in the reseller's squad regardless of
    tariff). Otherwise fall back to the tariff's own external squad (existing behaviour).

    Returns ``None`` to mean "don't set/override the external squad" — callers must never
    forward ``None`` to the panel (it rejects null externalSquadUuid with error A039).

    Fast path: non-clone users (``clone_bot_id`` falsy) never hit the DB.
    """
    if clone_bot_id:
        clone = await db.get(CloneBot, clone_bot_id)
        if clone is not None and clone.external_squad_uuid:
            return clone.external_squad_uuid
    if tariff is not None and getattr(tariff, 'external_squad_uuid', None):
        return tariff.external_squad_uuid
    return None
