"""Claim completion must not become the active mining reward."""

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.message_handlers import MessageHandlerService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from src.web.gui_manager import WebGUIManager
from src.web.managers.campaigns import CampaignProgressManager
from tests.test_drop_ignore_policy import _campaign, _drop


def reward(key="reward", *, claimed=False):
    return _campaign([_drop(key, key, required_minutes=60, claimed=claimed)]).timed_drops[key]


@pytest.fixture
def progress():
    broadcaster = SimpleNamespace(emit=AsyncMock())
    return CampaignProgressManager(broadcaster), broadcaster.emit


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 30, 60])
async def test_unclaimed_rewards_remain_visible_even_at_full_local_progress(progress, minutes):
    manager, emit = progress
    drop = reward()
    drop.real_current_minutes = minutes
    manager.update(drop, (60 - minutes) * 60)
    await asyncio.sleep(0)
    data = manager.get_current_drop()
    assert data["drop_id"] == drop.id
    assert data["current_minutes"] == minutes
    emit.assert_awaited_once_with("drop_progress", data)
    assert not drop.is_claimed


@pytest.mark.asyncio
async def test_completed_reward_does_not_start_an_active_display(progress):
    manager, emit = progress
    manager.update(reward(claimed=True), 0)
    await asyncio.sleep(0)
    assert manager.get_current_drop() is None
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_claimed_current_reward_stops_instead_of_redisplaying_as_active(progress):
    manager, emit = progress
    drop = reward()
    manager.update(drop, 60)
    await asyncio.sleep(0)
    emit.reset_mock()
    drop.is_claimed = True
    manager.update(drop, 0)
    await asyncio.sleep(0)
    assert manager.get_current_drop() is None
    emit.assert_awaited_once_with("drop_progress_stop", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("same_id", [False, True])
async def test_completed_other_model_cannot_replace_or_clear_current_reward(progress, same_id):
    manager, emit = progress
    current = reward()
    old = reward(current.id if same_id else "another", claimed=True)
    assert old is not current
    manager.update(current, 60)
    await asyncio.sleep(0)
    before = manager.get_current_drop()
    emit.reset_mock()
    manager.update(old, 0)
    await asyncio.sleep(0)
    assert manager.get_current_drop() == before
    assert manager._current_drop is current
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_snapshot_omits_a_model_claimed_since_its_last_display(progress):
    manager, emit = progress
    drop = reward()
    manager.update(drop, 60)
    await asyncio.sleep(0)
    emit.reset_mock()
    drop.is_claimed = True
    assert manager.get_current_drop() is None
    assert manager._current_drop is drop
    emit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_explicit_empty_progress_update_notifies_existing_clients(progress, active):
    manager, emit = progress
    if active:
        manager.update(reward(), 60)
        await asyncio.sleep(0)
    emit.reset_mock()
    manager.update(None, 0)
    await asyncio.sleep(0)
    assert manager.get_current_drop() is None
    assert manager.minute_almost_done()
    emit.assert_awaited_once_with("drop_progress_stop", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_during_claim", [False, True])
async def test_real_websocket_claim_continuation_cannot_republish_completed_progress(
    progress, monkeypatch, switch_during_claim,
):
    manager, emit = progress
    drop, next_drop = reward(), reward("next")
    twitch = drop._twitch
    twitch._drops = {drop.id: drop}
    twitch.watching_channel = AwaitableValue()
    twitch.watching_channel.set(SimpleNamespace(id=7))
    twitch.gql_request = AsyncMock(return_value={"data": {"currentUser": {"dropCurrentSession": None}}})
    twitch.settings.telegram_bot_token = twitch.settings.telegram_chat_id = ""
    twitch.settings.discord_webhook_url = ""
    twitch.gui.progress = manager
    twitch.gui.display_drop = MethodType(WebGUIManager.display_drop, twitch.gui)
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    monkeypatch.setattr(drop.campaign, "can_mine", MagicMock(return_value=False))

    async def accepted():
        if switch_during_claim:
            twitch.gui.display_drop(next_drop)
        return True

    monkeypatch.setattr(drop, "_claim", AsyncMock(side_effect=accepted))
    twitch.gui.display_drop(drop)
    await asyncio.sleep(0)
    emit.reset_mock()
    real_sleep = asyncio.sleep
    with monkeypatch.context() as timers:
        timers.setattr("src.services.message_handlers.asyncio.sleep", AsyncMock())
        await MessageHandlerService(twitch).process_drops(42, {
            "type": "drop-claim",
            "data": {"drop_id": drop.id, "drop_instance_id": "FIXTURE-CLAIM"},
        })
    await real_sleep(0)
    assert drop.is_claimed and drop.current_minutes == drop.required_minutes
    twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)
    twitch.request_inventory_refresh.assert_called_once_with()
    if switch_during_claim:
        current = manager.get_current_drop()
        assert current["drop_id"] == next_drop.id
        emit.assert_awaited_once_with("drop_progress", current)
    else:
        assert manager.get_current_drop() is None
        emit.assert_awaited_once_with("drop_progress_stop", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [60, 0, -540])
async def test_stopped_countdown_is_zero_for_fallback_polling(progress, remaining):
    manager, emit = progress
    manager.update(reward(), remaining)
    await asyncio.sleep(0)
    emit.reset_mock()
    manager.stop_timer()
    await asyncio.sleep(0)
    assert manager.get_current_drop() is None
    assert manager._remaining_seconds == 0
    assert manager.minute_almost_done()
    emit.assert_awaited_once_with("drop_progress_stop", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("last_minutes", [59, 60])
async def test_claimed_display_clear_keeps_real_watch_loop_fallback_available(progress, monkeypatch, last_minutes):
    manager, _ = progress
    campaign = _campaign([_drop("done", "Done", required_minutes=60), _drop("next", "Next")])
    completed = campaign.timed_drops["done"]
    completed.real_current_minutes = last_minutes
    manager.update(completed, (60 - last_minutes) * 60)
    await asyncio.sleep(0)
    completed.is_claimed = True
    manager.update(completed, 0)
    twitch = completed._twitch
    twitch.gui.progress = manager
    twitch.gui.display_drop = MethodType(WebGUIManager.display_drop, twitch.gui)
    twitch._drops = dict(campaign.timed_drops)
    twitch.is_mining_time = lambda: True
    stream = object()
    channel = SimpleNamespace(id=7, online=True, banned=False, game=campaign.game, _stream=stream,
                              _watch_current=lambda observed: observed is stream,
                              send_watch=AsyncMock(return_value=True))
    twitch.watching_channel = AwaitableValue()
    twitch.watching_channel.set(channel)
    twitch.gql_request = AsyncMock(return_value={
        "data": {"currentUser": {"dropCurrentSession": {"dropID": "next", "currentMinutesWatched": 1}}},
    })
    ticks = [0.0]
    service = WatchService(twitch)

    async def advance(delay):
        ticks[0] += delay
        if ticks[0] >= 90:
            raise asyncio.CancelledError

    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: ticks[0])
    monkeypatch.setattr(service, "watch_sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await service.watch_loop()
    await asyncio.sleep(0)
    assert twitch.gql_request.await_count == 2
    assert channel.send_watch.await_count >= 8
    assert manager.get_current_drop()["drop_id"] == "next"
    assert manager._current_drop is campaign.timed_drops["next"]
