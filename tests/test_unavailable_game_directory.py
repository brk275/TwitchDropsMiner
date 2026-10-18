"""A null category directory must not abort discovery of the remaining games."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api import GQLClient
from src.config import ClientType
from src.exceptions import GQLException, MinerException
from src.models.game import Game
from src.services.channel_service import ChannelService


def game(game_id=1):
    return Game({"id": str(game_id), "name": "Fixture game", "slug": f"fixture-{game_id}"})


@pytest.mark.asyncio
@pytest.mark.parametrize("drops_enabled", [False, True])
@pytest.mark.parametrize("transport", ["service", "http", "browser"])
async def test_null_directory_returns_no_channels_without_retry_or_query_changes(
    transport, drops_enabled,
):
    payload = {"data": {"game": None}}
    original = deepcopy(payload)
    wire = AsyncMock(return_value=payload)

    @asynccontextmanager
    async def request(*args, **kwargs):
        yield SimpleNamespace(json=AsyncMock(return_value=await wire(*args, **kwargs)))

    if transport == "service":
        query = wire
    else:
        identity = SimpleNamespace(browser_active=transport == "browser", headers=lambda **kwargs: {})
        identity._twitch = SimpleNamespace(_browser=SimpleNamespace(gql=wire))
        query = GQLClient(
            SimpleNamespace(request=request), SimpleNamespace(validate=AsyncMock(return_value=identity)),
            ClientType.WEB,
        ).request
    twitch = SimpleNamespace(gql_request=query)

    assert await ChannelService(twitch).get_live_streams(
        game(), limit=43, drops_enabled=drops_enabled,
    ) == []
    wire.assert_awaited_once()
    operation = wire.await_args.kwargs["json"] if transport == "http" else wire.await_args.args[0]
    variables = operation["variables"]
    assert variables["limit"] == 43
    assert variables["slug"] == "fixture-1"
    assert variables["options"]["includeRestricted"] == ["SUB_ONLY_LIVE"]
    assert variables["options"]["systemFilters"] == (["DROPS_ENABLED"] if drops_enabled else [])
    assert variables["includeCostreaming"] is False
    assert variables["imageWidth"] == 50
    assert variables["sortTypeIsRecency"] is False
    assert payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"game": {"streams": {"edges": []}}}, {}])
async def test_existing_empty_or_absent_directory_behavior_is_preserved(data):
    twitch = SimpleNamespace(gql_request=AsyncMock(return_value={"data": data}))
    assert await ChannelService(twitch).get_live_streams(game()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"data": None}, {"data": {"game": False}}, {"data": {"game": []}},
    {"data": {"game": {}}}, {"data": {"game": {"streams": None}}},
    {"data": {"game": {"streams": {"edges": None}}}},
])
async def test_malformed_directory_is_not_reclassified_as_unavailable(payload):
    twitch = SimpleNamespace(gql_request=AsyncMock(return_value=payload))
    with pytest.raises((KeyError, TypeError)):
        await ChannelService(twitch).get_live_streams(game())


@pytest.mark.asyncio
async def test_query_failure_preserves_existing_miner_exception_boundary():
    error = GQLException("synthetic query failure")
    twitch = SimpleNamespace(gql_request=AsyncMock(side_effect=error))
    with pytest.raises(MinerException) as raised:
        await ChannelService(twitch).get_live_streams(game())
    assert raised.value.__cause__ is error


@pytest.mark.asyncio
async def test_directory_cancellation_still_propagates():
    twitch = SimpleNamespace(gql_request=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await ChannelService(twitch).get_live_streams(game())


@pytest.mark.asyncio
async def test_discovery_continues_to_the_next_category_and_constructs_real_channels():
    node = {
        "id": "123456", "game": {"id": "2", "name": "Fixture game"},
        "viewersCount": 42, "title": "Fixture stream",
        "broadcaster": {"id": "99", "login": "fixture", "displayName": "Fixture"},
    }
    twitch = SimpleNamespace(gui=SimpleNamespace(channels=SimpleNamespace()), gql_request=AsyncMock(side_effect=[
        {"data": {"game": None}},
        {"data": {"game": {"streams": {"edges": [
            {"node": {"broadcaster": None}}, {"node": node},
        ]}}}},
    ]))
    service = ChannelService(twitch)
    channels = []
    for category in (game(1), game(2)):
        channels.extend(await service.get_live_streams(category))

    assert len(channels) == 1
    channel = channels[0]
    assert channel.id == 99
    assert channel.game == game(2)
    assert channel.online and channel.drops_enabled
    assert channel.viewers == 42
    assert twitch.gql_request.await_count == 2
