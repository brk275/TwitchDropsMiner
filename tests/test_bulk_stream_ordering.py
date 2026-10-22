"""Bulk discovery must not replace newer stream or account observations."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.channel_service import ChannelService
from src.services.watch_service import WatchService
from tests.test_channel_offline_refresh import channel, client, live, offline
from tests.test_stale_progress import make_campaign


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("reply_online", [False, True])
@pytest.mark.parametrize("newer", [
    "same-broadcast", "new-broadcast", "offline-event", "offline-result",
    "removed", "account-generation",
])
async def test_bulk_reply_keeps_intervening_observation(initially_online, reply_online, newer):
    twitch = client()
    twitch._channel_tasks_generation = 0
    twitch.on_channel_update = MagicMock()
    target = channel(twitch, online=initially_online)
    entered, release = asyncio.Event(), asyncio.Event()
    captured = live(target.id) if reply_online else offline(target.id)
    if reply_online:
        captured["data"]["user"]["stream"]["id"] = "123456"

    async def request(operations):
        entered.set()
        await release.wait()
        return [captured]

    twitch.gql_request = AsyncMock(side_effect=request)
    parent = asyncio.create_task(ChannelService(twitch).bulk_check_online(iter([target])))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if newer in ("same-broadcast", "new-broadcast"):
            replacement = live(target.id)["data"]["user"]
            replacement["stream"]["id"] = "123456" if newer == "same-broadcast" else "888888"
            replacement["stream"]["viewersCount"] = 77
            replacement["broadcastSettings"] = {
                "title": "New confirmed state", "game": {"id": "2", "name": "Elsewhere"},
            }
            target.external_update(replacement, [])
        elif newer == "offline-event":
            target.set_offline()
        elif newer == "offline-result":
            target.external_update(offline(target.id)["data"]["user"], [])
        elif newer == "removed":
            target.remove()
        else:
            twitch._channel_tasks_generation += 1
        confirmed = target._stream
        generation = target._refresh_generation
        callbacks = twitch.on_channel_update.call_count
        release.set()
        await asyncio.wait_for(parent, 1)
        assert target._stream is confirmed
        assert target._refresh_generation == generation
        assert twitch.on_channel_update.call_count == callbacks
        twitch.gql_request.assert_awaited_once_with([target.stream_gql])
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
async def test_delayed_live_reply_does_not_make_newer_offline_channel_watchable(initially_online):
    twitch = client()
    twitch.on_channel_update = MagicMock()
    campaign = make_campaign(twitch)
    twitch.inventory = [campaign]
    twitch.wanted_games = [campaign.game]
    target = channel(twitch, online=initially_online)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [live(target.id)]

    twitch.gql_request = AsyncMock(side_effect=request)
    parent = asyncio.create_task(ChannelService(twitch).bulk_check_online([target]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        target.set_offline()
        assert not WatchService(twitch).can_watch(target)
        release.set()
        await asyncio.wait_for(parent, 1)
        assert campaign.timed_drops["drop"].real_current_minutes == 10
        assert not campaign.timed_drops["drop"].is_claimed
        assert not WatchService(twitch).can_watch(target)
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("reply_online", [False, True])
@pytest.mark.parametrize("batch_response", [False, True])
async def test_unchanged_observation_accepts_current_reply(initially_online, reply_online, batch_response):
    twitch = client()
    target = channel(twitch, online=initially_online)
    payload = live(target.id) if reply_online else offline(target.id)
    twitch.gql_request = AsyncMock(return_value=[payload] if batch_response else payload)

    await ChannelService(twitch).bulk_check_online([target])

    assert target.online is reply_online
    assert target.viewers == (60 if reply_online else None)
    twitch.gql_request.assert_awaited_once_with([target.stream_gql])
    twitch.gui.channels.display.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_intervening_observation_does_not_revoke_current_reply():
    twitch = client()
    target = channel(twitch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [live(target.id)]

    twitch.gql_request = AsyncMock(side_effect=request)
    parent = asyncio.create_task(ChannelService(twitch).bulk_check_online([target]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(KeyError):
            target.external_update({"stream": {"id": "broken"}}, [])
        release.set()
        await asyncio.wait_for(parent, 1)
        assert target.online and target.viewers == 60
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("newer_online", [False, True])
async def test_late_bulk_reply_keeps_completed_newer_bulk_read(initially_online, newer_online):
    twitch = client()
    target = channel(twitch, online=initially_online)
    service = ChannelService(twitch)
    entered, release = asyncio.Event(), asyncio.Event()
    captured = live(target.id)
    calls = 0

    async def request(operations):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return [captured]
        reply = live(target.id) if newer_online else offline(target.id)
        if newer_online:
            reply["data"]["user"]["stream"]["viewersCount"] = 77
        return [reply]

    twitch.gql_request = AsyncMock(side_effect=request)
    parent = asyncio.create_task(service.bulk_check_online([target]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await service.bulk_check_online([target])
        confirmed = target._stream
        generation = target._refresh_generation
        release.set()
        await asyncio.wait_for(parent, 1)
        assert target.online is newer_online
        assert target._stream is confirmed
        assert target._refresh_generation == generation
        assert twitch.gql_request.await_count == 2
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
