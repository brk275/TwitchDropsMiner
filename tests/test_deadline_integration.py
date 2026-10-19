"""Real state-machine discovery, safe wakeups and deadline switchback."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_CHANNELS, State
from tests.test_deadline_service import scenario
from tests.test_priority_fallback import _channel
from tests.test_session_lifecycle import miner


@pytest.mark.asyncio
@pytest.mark.parametrize("urgent_count", [1, MAX_CHANNELS])
async def test_real_discovery_keeps_urgent_channel_at_tracking_cap_then_switches_back(tmp_path, monkeypatch, urgent_count):
    _, owner, _, _, low, channels = scenario()
    client = miner(tmp_path, monkeypatch)
    client.settings = owner.settings
    client.inventory = owner.inventory
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client._drops = {d.id: d for c in client.inventory for d in c.drops}
    client._deadline_service.enabled = True
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=type("Identity", (), {"user_id": 42})())
    client.fetch_inventory = AsyncMock()
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    candidates = [_channel(client, 1000 + i, {"id": "1", "name": "High"})
                  for i in range(MAX_CHANNELS)]
    alternate = channels[1]
    urgent = [alternate] + [_channel(client, 2000 + i, {"id": "2", "name": "Low"})
                            for i in range(urgent_count - 1)]
    client.get_live_streams = AsyncMock(side_effect=lambda game, **kw: candidates if game.id == 1 else urgent)
    client.bulk_check_online = AsyncMock()
    client._channel_service.filter_banned_channels = AsyncMock(side_effect=lambda channels, **kw: list(channels))
    client.gql_request = AsyncMock(side_effect=AssertionError("No live Twitch calls"))
    watched = []
    original = client.watch

    def watch(channel, **kwargs):
        original(channel, **kwargs)
        if not kwargs.get("update_status", True):
            return
        watched.append(channel.game.name)
        if len(watched) == 1:
            assert channel.game == low.game
            low.timed_drops["low-drop"].is_claimed = True
            asyncio.get_running_loop().call_soon(client.request_games_update)
        else:
            asyncio.get_running_loop().call_soon(client.close)

    client.watch = MagicMock(side_effect=watch)
    try:
        await asyncio.wait_for(client._run(), 3)
        assert watched == ["Low", "High"]
        first_rows = client.gui.channels.batch_update.call_args_list[0].args[0]
        assert len(first_rows) == MAX_CHANNELS
        assert first_rows[0].game == low.game
        assert any(ch.game.name == "High" for ch in first_rows)
        assert client.settings.games_to_watch == ["High", "Low"]
        client.gql_request.assert_not_awaited()
        assert client.drop_history.total_count == 0
    finally:
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)


@pytest.mark.parametrize("state", list(State))
def test_pending_deadline_wakeup_never_overwrites_inventory_discovery_or_exit(tmp_path, monkeypatch, state):
    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    client._inventory_loaded = True
    client._deadline_update_pending = True
    client._state = state
    client._activate_pending_deadline_update()
    assert client._state == (State.CHANNEL_SWITCH if state in (State.IDLE, State.CHANNEL_SWITCH) else state)


@pytest.mark.asyncio
async def test_existing_minute_schedule_queues_reassessment_and_bounded_discovery(tmp_path, monkeypatch):
    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    client._inventory_loaded = True
    client._deadline_service.enabled = True
    client._watch_service.watch_loop = AsyncMock()
    client._deadline_service.discovery_due = MagicMock(side_effect=[False, True])
    observed = []

    async def sleep(awaitable, timeout):
        awaitable.close()
        observed.append((client._deadline_update_pending, client._games_update_pending, timeout))
        if len(observed) == 2:
            client._state = State.EXIT

    monkeypatch.setattr("src.core.client.asyncio.wait_for", sleep)
    try:
        await client._mining_schedule_loop()
        assert observed == [(True, False, 60.0), (True, True, 60.0)]
        assert not client._inventory_refresh_pending
    finally:
        if client._watching_task is not None:
            await client._watching_task


def test_paused_or_unloaded_state_cannot_activate_deadline_work(tmp_path, monkeypatch):
    client = miner(tmp_path, monkeypatch)
    client._state = State.IDLE
    client._deadline_update_pending = True
    client._inventory_loaded = False
    client._activate_pending_deadline_update()
    assert client._state is State.IDLE
    client._inventory_loaded = True
    client.is_mining_time = lambda: False
    client._activate_pending_deadline_update()
    assert client._state is State.IDLE
