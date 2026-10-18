"""Confirmed state survives cancelled delivery and supersedes older progress polls."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.drop_history import DropHistory
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
async def test_cancelled_delivery_keeps_history_and_retires_pending_poll(
    mining_state, tmp_path, boundary, reply,
):
    twitch, _, campaign = mining_state
    target, claimed = campaign.drops
    target.extra_current_minutes, claimed.extra_current_minutes = 2, 5
    revision = campaign.progress_revision
    twitch.drop_history = DropHistory(tmp_path)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    claimed._claim = AsyncMock(return_value=True)
    claimed._send_telegram_notification = AsyncMock()
    claimed._send_discord_notification = AsyncMock()
    delivery = {
        "dashboard": twitch.gui.broadcast_wanted_items_now,
        "telegram": claimed._send_telegram_notification,
        "discord": claimed._send_discord_notification,
    }
    entered = asyncio.Event()

    async def block_delivery():
        entered.set()
        assert DropHistory(tmp_path).total_count == 1
        assert (claimed.real_current_minutes, claimed.extra_current_minutes) == (60, 0)
        assert campaign.progress_revision == revision + 1
        await asyncio.Event().wait()

    delivery[boundary].side_effect = block_delivery

    async def during_request(request):
        task = asyncio.create_task(claimed.claim())
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    assert (target.real_current_minutes, target.extra_current_minutes) == (10, 2)
    assert (claimed.real_current_minutes, claimed.extra_current_minutes) == (60, 0)
    assert claimed.is_claimed
    assert campaign.progress_revision == revision + 1
    assert [row["id"] for row in DropHistory(tmp_path).get_entries()] == [claimed.id]
    twitch.drop_history.record.assert_called_once_with(claimed, campaign)
    counts = [method.await_count for method in delivery.values()]
    assert await claimed.claim()
    assert campaign.progress_revision == revision + 1
    assert [method.await_count for method in delivery.values()] == counts
    twitch.drop_history.record.assert_called_once_with(claimed, campaign)
    twitch.request_games_update.assert_called_once_with()
