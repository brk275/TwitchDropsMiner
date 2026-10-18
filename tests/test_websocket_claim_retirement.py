"""Retired delivery callbacks drain before account state is cleared."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.drop_history import DropHistory
from src.services.message_handlers import MessageHandlerService
from tests.test_websocket_shutdown_ownership import attach, retire
from tests.test_websocket_shutdown_ownership import runtime as runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_confirmed_retired_claim_keeps_history_and_drains_before_auth_clear(runtime, tmp_path, boundary, cancel):
    twitch, drop = runtime.twitch, runtime.drop
    entered, draining, release, drained = [asyncio.Event() for _ in range(4)]
    twitch.gql_request = AsyncMock(return_value={
        "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}},
    })
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()

    async def delivery():
        assert drop.is_claimed and twitch.drop_history.total_count == 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            assert twitch._auth_state.user_id == 42
            drained.set()

    deliveries = {
        "dashboard": twitch.gui.broadcast_wanted_items_now,
        "telegram": drop._send_telegram_notification,
        "discord": drop._send_discord_notification,
    }
    deliveries[boundary].side_effect = delivery

    async def callback(*args):
        await MessageHandlerService(twitch).process_drops(42, {
            "type": "drop-claim", "data": {"drop_id": drop.id, "drop_instance_id": "confirmed-claim"},
        })

    ws, topic, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    retired = retire(runtime.pool, topic)
    cleared_after_drain = []
    original_clear = twitch._auth_state.clear

    def clear():
        cleared_after_drain.append(drained.is_set())
        original_clear()

    twitch._auth_state.clear = clear

    async def replace():
        async with twitch.authentication_change():
            assert callback_task.done() and drained.is_set()

    replacement = None
    try:
        await asyncio.wait_for(draining.wait(), 1)
        replacement = asyncio.create_task(replace())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if cancel:
            replacement.cancel()
            await asyncio.sleep(0)
        assert not replacement.done() and not cleared_after_drain
        assert twitch._auth_state.user_id == 42
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(replacement, 1)
        else:
            await asyncio.wait_for(replacement, 1)
        assert callback_task.cancelled() and not ws._callback_tasks
        assert cleared_after_drain == [True]
        assert drop.is_claimed and (drop.real_current_minutes, drop.extra_current_minutes) == (60, 0)
        assert drop.campaign.progress_revision == 1
        assert [row["id"] for row in DropHistory(tmp_path).get_entries()] == [drop.id]
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        callback_task.cancel()
        await asyncio.gather(callback_task, *retired, *([replacement] if replacement else []), return_exceptions=True)
