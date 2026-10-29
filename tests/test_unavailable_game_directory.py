"""An unavailable Twitch game directory is empty, not a discovery crash."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.gql_client import GQLClient
from src.exceptions import ExitRequest, GQLException, MinerException
from src.models.game import Game
from src.services.channel_service import ChannelService


GAME = {"id": "1", "name": "Fixture Game", "slug": "fixture-game"}


def live_directory():
    return {"data": {"game": {"streams": {"edges": [
        {"node": {
            "id": "456", "game": GAME, "viewersCount": 12, "title": "Fixture",
            "broadcaster": {"id": "123", "login": "fixture", "displayName": "Fixture"},
        }},
        {"node": {"broadcaster": None}},
    ]}}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("drops_enabled", [False, True])
@pytest.mark.parametrize("data", [
    {}, {"game": None}, {"game": {"streams": {"edges": []}}},
])
async def test_unavailable_or_empty_directory_has_no_channels(data, drops_enabled):
    twitch = MagicMock()
    response = {"data": deepcopy(data)}
    twitch.gql_request = AsyncMock(return_value=response)

    channels = await ChannelService(twitch).get_live_streams(
        Game(GAME), limit=7, drops_enabled=drops_enabled,
    )

    assert channels == []
    assert response == {"data": data}
    operation = twitch.gql_request.call_args.args[0]
    variables = operation["variables"]
    assert variables["limit"] == 7
    assert variables["slug"] == "fixture-game"
    assert variables["options"]["includeRestricted"] == ["SUB_ONLY_LIVE"]
    assert variables["options"]["systemFilters"] == (["DROPS_ENABLED"] if drops_enabled else [])
    twitch.gql_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("drops_enabled", [False, True])
async def test_valid_directory_keeps_channel_metadata_and_filter(drops_enabled):
    twitch = MagicMock()
    response = live_directory()
    original = deepcopy(response)
    twitch.gql_request = AsyncMock(return_value=response)

    channels = await ChannelService(twitch).get_live_streams(
        Game(GAME), drops_enabled=drops_enabled,
    )

    assert len(channels) == 1
    channel = channels[0]
    assert channel.id == 123
    assert channel.online
    assert channel._stream.broadcast_id == 456
    assert channel.game == Game(GAME)
    assert channel.viewers == 12
    assert channel.drops_enabled is drops_enabled
    assert response == original


@pytest.mark.asyncio
async def test_unavailable_directory_does_not_prevent_next_game_lookup():
    twitch = MagicMock()
    twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"game": None}}, live_directory(),
    ])
    service = ChannelService(twitch)

    assert await service.get_live_streams(Game({**GAME, "slug": "unavailable"})) == []
    channels = await service.get_live_streams(Game(GAME))

    assert [channel.id for channel in channels] == [123]
    assert twitch.gql_request.await_count == 2


@pytest.mark.asyncio
async def test_graphql_failure_keeps_existing_context_and_cause():
    twitch = MagicMock()
    error = GQLException("Fixture failure")
    twitch.gql_request = AsyncMock(side_effect=error)

    with pytest.raises(MinerException, match="^Game: fixture-game$") as caught:
        await ChannelService(twitch).get_live_streams(Game(GAME))

    assert caught.value.__cause__ is error
    twitch.gql_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ExitRequest(), asyncio.CancelledError()])
async def test_process_exit_and_cancellation_are_not_empty_results(error):
    twitch = MagicMock()
    twitch.gql_request = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as caught:
        await ChannelService(twitch).get_live_streams(Game(GAME))

    assert caught.value is error
    twitch.gql_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_active", [False, True])
async def test_real_graphql_client_forwards_null_directory_without_retry(browser_active):
    payload = {"data": {"game": None}}
    response = SimpleNamespace(json=AsyncMock(return_value=payload))

    @asynccontextmanager
    async def request(*args, **kwargs):
        yield response

    http = SimpleNamespace(request=MagicMock(side_effect=request))
    browser = SimpleNamespace(gql=AsyncMock(return_value=payload))
    auth = SimpleNamespace(
        browser_active=browser_active,
        _twitch=SimpleNamespace(_browser=browser),
        headers=MagicMock(return_value={}),
    )
    state = SimpleNamespace(validate=AsyncMock(return_value=auth))
    client = GQLClient(http, state, SimpleNamespace(USER_AGENT="Fixture"))
    twitch = MagicMock()
    twitch.gql_request = client.request

    assert await ChannelService(twitch).get_live_streams(Game(GAME)) == []

    state.validate.assert_awaited_once()
    assert payload == {"data": {"game": None}}
    if browser_active:
        browser.gql.assert_awaited_once()
        http.request.assert_not_called()
        response.json.assert_not_awaited()
    else:
        http.request.assert_called_once()
        response.json.assert_awaited_once()
        browser.gql.assert_not_awaited()
