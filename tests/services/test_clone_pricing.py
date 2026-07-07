"""Тесты клон-наценки: главный инвариант — вне контекста клона цены НЕ меняются."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.clone_pricing import (  # noqa: E402
    apply_clone_markup,
    current_markup_pct,
    markup_context_for_user,
)
from app.utils.clone_context import reset_current_clone, set_current_clone  # noqa: E402


class TestApplyCloneMarkup:
    def test_no_clone_context_is_noop(self) -> None:
        assert apply_clone_markup(50000) == 50000
        assert current_markup_pct() == 0

    def test_markup_applied_in_clone_context(self) -> None:
        clone = MagicMock(pricing_markup_pct=20)
        token = set_current_clone(clone)
        try:
            assert current_markup_pct() == 20
            assert apply_clone_markup(50000) == 60000  # 500₽ → 600₽
            assert apply_clone_markup(0) == 0
        finally:
            reset_current_clone(token)

    def test_zero_markup_clone_is_noop(self) -> None:
        clone = MagicMock(pricing_markup_pct=0)
        token = set_current_clone(clone)
        try:
            assert apply_clone_markup(50000) == 50000
        finally:
            reset_current_clone(token)

    def test_markup_clamped_to_range(self) -> None:
        clone = MagicMock(pricing_markup_pct=9999)
        token = set_current_clone(clone)
        try:
            assert current_markup_pct() == 500
        finally:
            reset_current_clone(token)
        clone = MagicMock(pricing_markup_pct=-50)
        token = set_current_clone(clone)
        try:
            assert current_markup_pct() == 0
        finally:
            reset_current_clone(token)

    def test_explicit_pct(self) -> None:
        assert apply_clone_markup(10000, 50) == 15000
        assert apply_clone_markup(10000, 0) == 10000
        # Целочисленное округление вниз
        assert apply_clone_markup(99, 10) == 108  # 99*110//100

    def test_garbage_markup_value_is_noop(self) -> None:
        clone = MagicMock(pricing_markup_pct='not-a-number')
        token = set_current_clone(clone)
        try:
            assert current_markup_pct() == 0
        finally:
            reset_current_clone(token)


class TestPricingEngineMarkup:
    """Движок наценивает базу до скидок; вне клона — не трогает."""

    @pytest.mark.asyncio
    async def test_tariff_core_without_clone_unchanged(self) -> None:
        from app.services.pricing_engine import PricingEngine

        tariff = MagicMock()
        tariff.is_daily = False
        tariff.period_prices = {'30': 50000}
        tariff.device_price_kopeks = 0
        tariff.device_limit = 3
        tariff.id = 1
        engine = PricingEngine()
        result = await engine._calculate_tariff_core(tariff, 30, 3, user=None)
        assert result.final_total == 50000

    @pytest.mark.asyncio
    async def test_tariff_core_with_clone_markup(self) -> None:
        from app.services.pricing_engine import PricingEngine

        tariff = MagicMock()
        tariff.is_daily = False
        tariff.period_prices = {'30': 50000}
        tariff.device_price_kopeks = 0
        tariff.device_limit = 3
        tariff.id = 1
        engine = PricingEngine()
        clone = MagicMock(pricing_markup_pct=30)
        token = set_current_clone(clone)
        try:
            result = await engine._calculate_tariff_core(tariff, 30, 3, user=None)
        finally:
            reset_current_clone(token)
        assert result.final_total == 65000  # 500₽ +30%


class TestMarkupContextForUser:
    @pytest.mark.asyncio
    async def test_non_clone_user_no_context(self) -> None:
        db = MagicMock()
        user = MagicMock(clone_bot_id=None)
        async with markup_context_for_user(db, user):
            assert current_markup_pct() == 0

    @pytest.mark.asyncio
    async def test_clone_user_gets_markup(self) -> None:
        from unittest.mock import AsyncMock

        import app.services.clone_pricing as cp

        cp._markup_cache.clear()
        db = MagicMock()
        db.get = AsyncMock(return_value=MagicMock(pricing_markup_pct=25))
        user = MagicMock(clone_bot_id=42)
        async with markup_context_for_user(db, user):
            assert current_markup_pct() == 25
            assert apply_clone_markup(10000) == 12500
        assert current_markup_pct() == 0  # контекст снят
        cp._markup_cache.clear()

    @pytest.mark.asyncio
    async def test_existing_interactive_context_not_overridden(self) -> None:
        """Если contextvar уже стоит (интерактив клона) — фоновая обёртка его не трогает."""
        db = MagicMock()
        user = MagicMock(clone_bot_id=42)
        outer = MagicMock(pricing_markup_pct=10)
        token = set_current_clone(outer)
        try:
            async with markup_context_for_user(db, user):
                assert current_markup_pct() == 10
        finally:
            reset_current_clone(token)
        db.get.assert_not_called()
