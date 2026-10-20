"""Discovery must not keep an untracked, stale stream as the watch owner."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_CHANNELS
from tests.test_deadline_service import scenario
from tests.test_priority_fallback import _channel
from tests.test_session_lifecycle import miner


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    "missing", "offline", "retained", "empty", "changed-game", "tracking-cap",
    "drops-disabled", "banned",
])
@pytest.mark.parametrize("manual", [False, True])
async def test_discovery_reconciles_watch_owner(tmp_path, monkeypatch, replacement, manual):
    _, owner, _, high, low, _ = scenario()
    client = miner(tmp_path, monkeypatch)
    client.settings = owner.settings
    client.inventory = [high, low]
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client._drops = {drop.id: drop for campaign in client.inventory for drop in campaign.drops}
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=type("Identity", (), {"user_id": 42})())
    client.fetch_inventory = AsyncMock()
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    client.bulk_check_online = AsyncMock()
    client._channel_service.filter_banned_channels = AsyncMock(
        side_effect=lambda channels, **kw: list(channels),
    )
    client.gql_request = AsyncMock(side_effect=AssertionError("No live Twitch calls"))
    old = _channel(client, 100, {"id": "1", "name": "High"})
    alternate = _channel(client, 101, {"id": "1", "name": "High"})
    refreshed = _channel(client, 100, {"id": "1", "name": "High"})
    if replacement == "offline":
        refreshed._stream = None
    elif replacement == "changed-game":
        refreshed._stream.game = low.game
    elif replacement == "drops-disabled":
        refreshed._stream.drops_enabled = False
    elif replacement == "banned":
        refreshed.update_ban_status({"data": {"user": {"id": "100", "self": {
            "banStatus": {"isPermanent": True},
        }}}})
    second = {
        "missing": [alternate], "offline": [refreshed, alternate],
        "retained": [refreshed, alternate], "empty": [],
        "changed-game": [refreshed, alternate],
        "drops-disabled": [refreshed, alternate], "banned": [refreshed, alternate],
        "tracking-cap": [refreshed, alternate],
    }[replacement]
    if replacement == "tracking-cap":
        refreshed._stream.viewers = 0
        alternate._stream.viewers = 1000
        second.extend(
            _channel(client, 1000 + i, {"id": "1", "name": "High"})
            for i in range(MAX_CHANNELS - 1)
        )
    batches = iter([[old], second])
    client.get_live_streams = AsyncMock(
        side_effect=lambda game, **kw: next(batches) if game == high.game else [],
    )
    observed = []

    def batch(channels):
        if client.gui.channels.batch_update.call_count == 1:
            if manual:
                client.enter_manual_mode(old)
            asyncio.get_running_loop().call_soon(client.request_games_update)
        else:
            def finish():
                observed.append(client.watching_channel.get_with_default(None))
                client.close()
            asyncio.get_running_loop().call_soon(finish)

    client.gui.channels.batch_update.side_effect = batch
    try:
        await asyncio.wait_for(client._run(), 3)
        expected = None if replacement == "empty" else refreshed if replacement == "retained" else alternate
        assert len(observed) == 1 and observed[0] is expected
        assert old.online
        if replacement == "tracking-cap":
            retained = client.gui.channels.batch_update.call_args.args[0]
            assert len(retained) == MAX_CHANNELS
            assert not any(channel.id == old.id for channel in retained)
        if manual and expected is not None:
            assert client._manual_target_channel is expected
            assert client._manual_target_game is old.game
        if manual and expected is None:
            assert not client.is_manual_mode()
            assert client._manual_target_game is None
        assert client.settings.games_to_watch == ["High", "Low"]
        client.gql_request.assert_not_awaited()
        assert client.drop_history.total_count == 0
    finally:
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)
