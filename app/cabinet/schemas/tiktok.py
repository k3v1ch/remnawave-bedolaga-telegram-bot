"""TikTok-программа: схемы для кабинета (отдельно от партнёрки)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ==================== User-facing ====================


class TikTokApplicationRequest(BaseModel):
    """Заявка на участие в TikTok-программе."""

    display_name: str | None = Field(None, max_length=255)
    tiktok_url: str | None = Field(None, max_length=500)
    other_platforms: str | None = Field(None, max_length=500)
    audience_size: int | None = Field(None, ge=0, le=2_000_000_000)
    content_topic: str | None = Field(None, max_length=255)
    description: str | None = Field(None, max_length=2000)


class TikTokApplicationInfo(BaseModel):
    """Информация о заявке для пользователя."""

    id: int
    status: str
    display_name: str | None = None
    tiktok_url: str | None = None
    other_platforms: str | None = None
    audience_size: int | None = None
    content_topic: str | None = None
    description: str | None = None
    admin_comment: str | None = None
    created_at: datetime
    processed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TikTokStatusResponse(BaseModel):
    """Статус TikTok-программы для текущего пользователя."""

    tiktok_status: str
    support_username: str
    total_earned_kopeks: int = 0
    latest_application: TikTokApplicationInfo | None = None


# ==================== Admin ====================


class AdminTikTokApplicationItem(BaseModel):
    """Заявка в админском списке."""

    id: int
    user_id: int
    username: str | None = None
    first_name: str | None = None
    telegram_id: int | None = None
    display_name: str | None = None
    tiktok_url: str | None = None
    other_platforms: str | None = None
    audience_size: int | None = None
    content_topic: str | None = None
    description: str | None = None
    status: str
    admin_comment: str | None = None
    created_at: datetime
    processed_at: datetime | None = None


class AdminTikTokApplicationsResponse(BaseModel):
    items: list[AdminTikTokApplicationItem]
    total: int


class TikTokApproveRequest(BaseModel):
    comment: str | None = Field(None, max_length=2000)


class TikTokRejectRequest(BaseModel):
    comment: str | None = Field(None, max_length=2000)


class AdminTikTokCreatorItem(BaseModel):
    """Одобренный TikTok-автор в админском списке."""

    user_id: int
    username: str | None = None
    first_name: str | None = None
    telegram_id: int | None = None
    display_name: str | None = None
    tiktok_url: str | None = None
    total_earned_kopeks: int = 0
    tiktok_status: str
    created_at: datetime


class AdminTikTokCreatorsResponse(BaseModel):
    items: list[AdminTikTokCreatorItem]
    total: int


class TikTokEarningItem(BaseModel):
    id: int
    amount_kopeks: int
    comment: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TikTokEarningsResponse(BaseModel):
    items: list[TikTokEarningItem]
    total_kopeks: int


class TikTokAddEarningRequest(BaseModel):
    amount_kopeks: int = Field(..., ge=-100_000_000, le=100_000_000)
    comment: str | None = Field(None, max_length=2000)


class TikTokStatsResponse(BaseModel):
    total_creators: int
    pending_applications: int
    total_earnings_kopeks: int
