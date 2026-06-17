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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CloneBot, Tariff


logger = structlog.get_logger(__name__)


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
