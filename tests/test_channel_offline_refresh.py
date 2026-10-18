"""Confirmed offline bulk reads must replace stale online stream state."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api import GQLClient
from src.config import ClientType
from src.exceptions import GQLException
from src.models.channel import Channel
from src.services.channel_service import ChannelService
from src.services.watch_service import WatchService
from tests.test_stale_progress import make_campaign


GAME = {"id": "1", "name": "Game"}


def client():
    return SimpleNamespace(
        gui=MagicMock(), _campaigns={},
        settings=SimpleNamespace(drop_name_blacklist=[]),
    )


def channel(twitch, channel_id=100, *, online=True):
    result = Channel.from_directory(twitch, {
        "id": "123456", "game": GAME, "viewersCount": 42, "title": "Old stream",
        "broadcaster": {"id": str(channel_id), "login": f"fixture{channel_id}", "displayName": "Fixture"},
    }, drops_enabled=True)
    if not online:
        result._stream = None
    return result


def offline(channel_id):
    return {"data": {"user": {"id": str(channel_id), "stream": None}}}


def live(channel_id):
    return {"data": {"user": {
        "id": str(channel_id), "stream": {"id": "999999", "viewersCount": 60},
        "broadcastSettings": {"game": GAME, "title": "New stream"},
    }}}


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("batch_response", [False, True])
async def test_explicit_offline_response_clears_stream_without_extra_requests(online, batch_response):
    twitch = client()
    target = channel(twitch, online=online)
    payload = offline(target.id)
    twitch.gql_request = AsyncMock(return_value=[payload] if batch_response else payload)

    await ChannelService(twitch).bulk_check_online(iter([target]))

    assert target.offline and not target.online
    assert target.game is None and target.viewers is None
    assert not target.drops_enabled
    twitch.gql_request.assert_awaited_once_with([target.stream_gql])
    twitch.gui.channels.display.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [], [{"data": {"user": None}}], [offline(999)], [{"data": {"user": {"id": "999"}}}],
])
async def test_missing_or_unmatched_channel_is_not_confirmed_offline(rows):
    twitch = client()
    target = channel(twitch)
    original = target._stream
    twitch.gql_request = AsyncMock(return_value=rows)

    await ChannelService(twitch).bulk_check_online([target])

    assert target.online and target._stream is original


@pytest.mark.asyncio
async def test_matched_channel_with_missing_stream_still_raises():
    twitch = client()
    target = channel(twitch)
    twitch.gql_request = AsyncMock(return_value=[{"data": {"user": {"id": str(target.id)}}}])
    with pytest.raises(KeyError):
        await ChannelService(twitch).bulk_check_online([target])
    assert target.online


@pytest.mark.asyncio
async def test_known_offline_channel_is_no_longer_watchable_after_refresh():
    twitch = client()
    campaign = make_campaign(twitch)
    twitch.inventory = [campaign]
    twitch.wanted_games = [campaign.game]
    target = channel(twitch)
    assert WatchService(twitch).can_watch(target)
    twitch.gql_request = AsyncMock(return_value=[offline(target.id)])

    await ChannelService(twitch).bulk_check_online([target])

    assert not WatchService(twitch).can_watch(target)
    assert campaign.timed_drops["drop"].real_current_minutes == 10
    assert not campaign.timed_drops["drop"].is_claimed


@pytest.mark.asyncio
async def test_later_online_response_still_recovers_the_channel():
    twitch = client()
    target = channel(twitch)
    twitch.gql_request = AsyncMock(side_effect=[[offline(target.id)], [live(target.id)]])
    service = ChannelService(twitch)

    await service.bulk_check_online([target])
    assert target.offline
    await service.bulk_check_online([target])
    assert target.online and target.viewers == 60
    assert target._stream.broadcast_id == 999999
    assert twitch.gql_request.await_count == 2


@pytest.mark.asyncio
async def test_out_of_order_multi_batch_rows_update_only_their_matching_channels():
    twitch = client()
    targets = [channel(twitch, 100 + index) for index in range(21)]
    ids_by_login = {target._login: target.id for target in targets}

    async def request(operations):
        return [offline(ids_by_login[op["variables"]["channel"]]) for op in reversed(operations)]

    twitch.gql_request = AsyncMock(side_effect=request)
    await ChannelService(twitch).bulk_check_online(iter(targets))

    assert all(target.offline for target in targets)
    assert [len(call.args[0]) for call in twitch.gql_request.await_args_list] == [20, 1]


@pytest.mark.asyncio
async def test_failed_sibling_batch_does_not_publish_partial_offline_state():
    twitch = client()
    targets = [channel(twitch, 100 + index) for index in range(21)]
    first_done = asyncio.Event()

    async def request(operations):
        if len(operations) == 20:
            first_done.set()
            return [offline(100)]
        await first_done.wait()
        raise GQLException("synthetic failure")

    twitch.gql_request = AsyncMock(side_effect=request)
    with pytest.raises(GQLException):
        await ChannelService(twitch).bulk_check_online(targets)
    assert all(target.online for target in targets)


@pytest.mark.asyncio
async def test_cancelled_sibling_batch_is_drained_without_partial_offline_state():
    twitch = client()
    targets = [channel(twitch, 100 + index) for index in range(21)]
    entered, drained = asyncio.Event(), asyncio.Event()

    async def request(operations):
        if len(operations) == 20:
            return [offline(100)]
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    twitch.gql_request = AsyncMock(side_effect=request)
    parent = asyncio.create_task(ChannelService(twitch).bulk_check_online(targets))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert drained.is_set()
        assert all(target.online for target in targets)
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("broadcast_id", ["123456", "999999"])
async def test_delayed_offline_reply_cannot_erase_a_newer_stream_replacement(initially_online, broadcast_id):
    twitch = client()
    target = channel(twitch, online=initially_online)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [offline(target.id)]

    twitch.gql_request = AsyncMock(side_effect=request)
    parent = asyncio.create_task(ChannelService(twitch).bulk_check_online([target]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        newer = live(target.id)["data"]["user"]
        newer["stream"]["id"] = broadcast_id
        target.external_update(newer, [])
        fresh_stream = target._stream
        release.set()
        await parent
        assert target.online and target._stream is fresh_stream
        assert target.viewers == 60
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("browser", [False, True])
@pytest.mark.parametrize("errored", [False, True])
async def test_normalized_null_stream_is_offline_only_for_an_error_free_read(browser, errored):
    twitch = client()
    target = channel(twitch)
    original = target._stream
    response = live(target.id) if errored else offline(target.id)
    if errored:
        response["errors"] = [{"message": "server error", "path": ["user", "stream"]}]
    wire = AsyncMock(return_value=[response])

    @asynccontextmanager
    async def request(*args, **kwargs):
        yield SimpleNamespace(json=AsyncMock(return_value=await wire(*args, **kwargs)))

    identity = SimpleNamespace(browser_active=browser, headers=lambda **kwargs: {})
    identity._twitch = SimpleNamespace(_browser=SimpleNamespace(gql=wire))
    twitch.gql_request = GQLClient(SimpleNamespace(request=request),
                                  SimpleNamespace(validate=AsyncMock(return_value=identity)),
                                  ClientType.WEB).request

    await ChannelService(twitch).bulk_check_online([target])

    assert response["data"]["user"]["stream"] is None
    wire.assert_awaited_once()
    if errored:
        assert target.online and target._stream is original
    else:
        assert target.offline and target._stream is None
