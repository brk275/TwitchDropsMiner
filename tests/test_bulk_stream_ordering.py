"""Bulk discovery cannot replace newer channel stream observations."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.exceptions import ExitRequest, MinerException
from src.models.channel import Channel
from src.services.channel_service import ChannelService
from src.services.watch_service import WatchService
from tests.test_special_game_watch import TEST_GAME, _campaign


def live(channel_id=123, *, broadcast=456, game="1", viewers=1):
    return {
        "id": str(channel_id),
        "displayName": "Fixture",
        "stream": {"id": str(broadcast), "viewersCount": viewers},
        "broadcastSettings": {
            "game": {"id": game, "name": f"Game {game}"}, "title": "Fixture",
        },
    }


def make_channel(*, online=True):
    twitch = SimpleNamespace(
        gui=SimpleNamespace(channels=MagicMock()),
        on_channel_update=MagicMock(),
        _channel_tasks=set(),
    )
    channel = Channel(twitch, id=123, login="fixture", acl_based=True)
    if online:
        channel.external_update(live(), [])
    return twitch, channel


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("newer", [
    "offline", "external-offline", "removed", "same-broadcast", "new-broadcast",
    "direct-update", "viewers",
])
async def test_bulk_live_reply_does_not_replace_intervening_state(initially_online, newer):
    twitch, channel = make_channel(online=initially_online)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [{"data": {"user": live()}}]

    twitch.gql_request = AsyncMock(side_effect=request)
    task = asyncio.create_task(ChannelService(twitch).bulk_check_online(iter([channel])))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if newer == "offline":
            channel.set_offline()
        elif newer == "external-offline":
            channel.external_update({"stream": None}, [])
        elif newer == "removed":
            channel.remove()
        elif newer == "direct-update":
            twitch.gql_request = AsyncMock(return_value={
                "data": {"user": live(broadcast=999, game="2", viewers=77)},
            })
            await channel.update_stream()
        elif newer == "viewers":
            if not channel.online:
                channel.external_update(live(), [])
            channel.viewers = 77
        else:
            channel.external_update(live(
                broadcast=456 if newer == "same-broadcast" else 999,
                game="2", viewers=77,
            ), [])
        confirmed = channel._stream
        callbacks = twitch.on_channel_update.call_count
        release.set()
        await asyncio.wait_for(task, 1)
        assert channel._stream is confirmed
        assert twitch.on_channel_update.call_count == callbacks
        if newer == "viewers":
            assert channel.viewers == 77
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [False, True])
async def test_fresh_bulk_live_reply_still_updates_channel(online):
    twitch, channel = make_channel(online=online)
    payload = live(broadcast=999, game="2", viewers=77)
    original = deepcopy(payload)
    twitch.gql_request = AsyncMock(return_value=[{"data": {"user": payload}}])

    await ChannelService(twitch).bulk_check_online(iter([channel]))

    assert channel.online
    assert channel._stream.broadcast_id == 999
    assert channel.game.id == 2
    assert channel.viewers == 77
    assert payload == original
    twitch.gql_request.assert_awaited_once_with([channel.stream_gql])
    twitch.on_channel_update.assert_not_called()


@pytest.mark.asyncio
async def test_overlapping_bulk_queries_keep_first_applied_observation():
    twitch, channel = make_channel()
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [{"data": {"user": live()}}]

    twitch.gql_request = AsyncMock(side_effect=request)
    service = ChannelService(twitch)
    older = asyncio.create_task(service.bulk_check_online([channel]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        twitch.gql_request = AsyncMock(return_value=[{
            "data": {"user": live(broadcast=999, game="2")},
        }])
        await service.bulk_check_online([channel])
        confirmed = channel._stream
        release.set()
        await asyncio.wait_for(older, 1)
        assert channel._stream is confirmed
        assert channel._stream.broadcast_id == 999
    finally:
        older.cancel()
        await asyncio.gather(older, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_bulk_reply_cannot_restore_watch_eligibility_for_previous_game():
    twitch = MagicMock()
    twitch.settings.drop_name_blacklist = []
    twitch.settings.telegram_bot_token = ""
    twitch.settings.telegram_chat_id = ""
    campaign = _campaign(twitch, TEST_GAME)
    channel = Channel(twitch, id=100, login="fixture", acl_based=True)
    channel.external_update(live(100), [])
    service = WatchService(twitch)
    assert service.can_watch(channel)
    before = [(drop.real_current_minutes, drop.is_claimed, drop.claim_id)
              for drop in campaign.drops]
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [{"data": {"user": live(100)}}]

    twitch.gql_request = AsyncMock(side_effect=request)
    task = asyncio.create_task(ChannelService(twitch).bulk_check_online([channel]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        channel.external_update(live(100, game="2"), [])
        assert not service.can_watch(channel)
        release.set()
        await asyncio.wait_for(task, 1)
        assert not service.can_watch(channel)
        assert [(drop.real_current_minutes, drop.is_claimed, drop.claim_id)
                for drop in campaign.drops] == before
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_newer_state_only_retires_the_affected_channel():
    twitch, stale = make_channel()
    fresh = Channel(twitch, id=124, login="otherfixture")
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [{"data": {"user": live(channel_id)}} for channel_id in (123, 124)]

    twitch.gql_request = AsyncMock(side_effect=request)
    task = asyncio.create_task(ChannelService(twitch).bulk_check_online([stale, fresh]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        stale.set_offline()
        release.set()
        await asyncio.wait_for(task, 1)
        assert stale.offline
        assert fresh.online
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_direct_refresh_does_not_retire_a_usable_bulk_reply():
    twitch, channel = make_channel(online=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [{"data": {"user": live()}}]

    twitch.gql_request = AsyncMock(side_effect=request)
    task = asyncio.create_task(ChannelService(twitch).bulk_check_online([channel]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        twitch.gql_request = AsyncMock(side_effect=MinerException("Fixture"))
        with pytest.raises(MinerException):
            await channel.update_stream()
        release.set()
        await asyncio.wait_for(task, 1)
        assert channel.online
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "exit"])
async def test_bulk_abort_drains_batches_without_publishing(outcome):
    twitch, channel = make_channel()
    channels = [channel, *(Channel(twitch, id=i, login=f"fixture{i}") for i in range(200, 220))]
    entered, release = asyncio.Event(), asyncio.Event()
    requests = set()
    finished = []
    original = channel._stream

    async def request(operations):
        owner = asyncio.current_task()
        requests.add(owner)
        if len(requests) == 2:
            entered.set()
        try:
            await release.wait()
            if outcome == "exit":
                raise ExitRequest()
            return [{"data": {"user": live()}}]
        finally:
            finished.append(owner)

    twitch.gql_request = AsyncMock(side_effect=request)
    task = asyncio.create_task(ChannelService(twitch).bulk_check_online(channels))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if outcome == "cancel":
            task.cancel()
            expected = asyncio.CancelledError
        else:
            release.set()
            expected = ExitRequest
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
        assert len(finished) == 2
        assert all(request.done() for request in requests)
        assert channel._stream is original
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
