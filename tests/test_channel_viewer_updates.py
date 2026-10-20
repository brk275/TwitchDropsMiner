"""Malformed live viewer updates must not poison channel discovery sorting."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.channel_service import ChannelService
from src.web.managers.channels import ChannelListManager
from tests.test_priority_fallback import _channel
from tests.test_session_lifecycle import miner


class CountSubclass(int):
    pass


def setup_channels(tmp_path, monkeypatch):
    client = miner(tmp_path, monkeypatch)
    client.gui.channels = ChannelListManager(MagicMock(emit=AsyncMock()))
    current = _channel(client, 100, {"id": "1", "name": "Game"})
    peer = _channel(client, 101, {"id": "1", "name": "Game"})
    current.viewers = 42
    peer.viewers = 100
    client.channels.update({channel.id: channel for channel in (current, peer)})
    client.gql_request = AsyncMock(side_effect=AssertionError("No live Twitch requests"))
    return client, current, peer


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
    None, True, False, -1, -100, 0.0, 2.5, float("nan"), float("inf"),
    "73", "", [], {}, CountSubclass(42),
])
async def test_invalid_live_viewer_count_preserves_last_count_and_later_recovers(tmp_path, monkeypatch, value):
    client, current, peer = setup_channels(tmp_path, monkeypatch)
    stream = current._stream
    await client._message_handler_service.process_stream_state(
        current.id, {"type": "viewcount", "viewers": value},
    )
    assert current.viewers == 42 and type(current.viewers) is int
    assert current._stream is stream and current.online
    assert sorted([current, peer], key=ChannelService.get_viewers_key, reverse=True) == [peer, current]
    row = client.gui.channels.get_channels()[0]
    assert row["viewers"] == 42 and type(row["viewers"]) is int
    json.dumps(row, allow_nan=False)

    await client._message_handler_service.process_stream_state(
        current.id, {"type": "viewcount", "viewers": 150},
    )
    assert current.viewers == 150
    assert sorted([current, peer], key=ChannelService.get_viewers_key, reverse=True) == [current, peer]
    assert client.gui.channels.get_channels()[0]["viewers"] == 150
    client.gql_request.assert_not_awaited()
    assert client.drop_history.total_count == 0
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["73", {}, []])
async def test_malformed_live_count_cannot_crash_next_discovery_sort(tmp_path, monkeypatch, value):
    client, current, peer = setup_channels(tmp_path, monkeypatch)
    await client._message_handler_service.process_stream_state(
        current.id, {"type": "viewcount", "viewers": value},
    )
    assert sorted([current, peer], key=ChannelService.get_viewers_key, reverse=True) == [peer, current]
    client.gql_request.assert_not_awaited()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, 100_000_000])
async def test_plain_nonnegative_viewer_counts_remain_accepted(tmp_path, monkeypatch, value):
    client, current, _ = setup_channels(tmp_path, monkeypatch)
    await client._message_handler_service.process_stream_state(
        current.id, {"type": "viewcount", "viewers": value},
    )
    assert current.viewers == value and type(current.viewers) is int
    assert client.gui.channels.get_channels()[0]["viewers"] == value
    client.gql_request.assert_not_awaited()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 10, -1, "7", None])
async def test_assigning_viewers_to_offline_channel_does_not_create_a_stream(tmp_path, monkeypatch, value):
    client, current, _ = setup_channels(tmp_path, monkeypatch)
    current._stream = None
    current.viewers = value
    assert current.viewers is None and not current.online and current.game is None
    client.gql_request.assert_not_awaited()
