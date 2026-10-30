"""Unavailable progress observations must not terminate watching or invent progress."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue


def session(value):
    return {"data": {"currentUser": {"dropCurrentSession": value}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    None,
    [],
    {},
    {"data": None},
    {"data": []},
    {"data": {}},
    {"data": {"currentUser": None}},
    {"data": {"currentUser": []}},
    {"data": {"currentUser": {}}},
    session([]),
    session({}),
    session({"dropID": "drop"}),
    session({"dropID": [], "currentMinutesWatched": 4}),
    session({"dropID": "", "currentMinutesWatched": 4}),
    session({"dropID": "drop", "currentMinutesWatched": None}),
    session({"dropID": "drop", "currentMinutesWatched": "4"}),
    session({"dropID": "drop", "currentMinutesWatched": True}),
    session({"dropID": "drop", "currentMinutesWatched": -1}),
    session({"dropID": "drop", "currentMinutesWatched": 1.5}),
])
async def test_invalid_observation_is_skipped_and_next_poll_recovers(response):
    updates, bumps, polls, watches = await run_polls(response)
    assert polls == [20, 80, 140]
    assert len(watches) >= 15
    assert updates == [7, 7]
    assert bumps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first, expected_updates, expected_bumps", [
    (session(None), [7, 7], [20]),
    (session({"dropID": "other", "currentMinutesWatched": 3}), [7, 7], [20]),
    (session({"dropID": "drop", "currentMinutesWatched": 0}), [0, 7, 7], []),
    (session({"dropID": "drop", "currentMinutesWatched": 4}), [4, 7, 7], []),
])
async def test_valid_observations_preserve_existing_fallback_and_updates(
    first, expected_updates, expected_bumps,
):
    updates, bumps, polls, _ = await run_polls(first)
    assert polls == [20, 80, 140]
    assert updates == expected_updates
    assert bumps == expected_bumps


async def run_polls(first):
    clock = [0.0]
    updates, bumps, polls, watches = [], [], [], []
    channel = MagicMock()
    channel.online = True
    channel.banned = False
    channel.id = 123
    channel._watch_current.return_value = True

    async def send_watch():
        watches.append(clock[0])
        return True

    channel.send_watch = send_watch
    selected = AwaitableValue()
    selected.set(channel)
    drop = MagicMock()
    drop.can_earn.return_value = True
    drop.update_minutes.side_effect = updates.append
    campaign = MagicMock()
    campaign.bump_minutes.side_effect = lambda watched: bumps.append(clock[0])

    async def gql_request(operation):
        polls.append(clock[0])
        return first if len(polls) == 1 else session({
            "dropID": "drop", "currentMinutesWatched": 7,
        })

    twitch = SimpleNamespace(
        is_mining_time=lambda: True,
        watching_channel=selected,
        gui=SimpleNamespace(),
        gql_request=gql_request,
        _drops={"drop": drop},
        _inventory_service=SimpleNamespace(get_active_campaign=lambda watched: campaign),
    )
    service = WatchService(twitch)

    async def advance(delay):
        assert delay >= 0
        clock[0] += delay
        if clock[0] >= 160:
            raise asyncio.CancelledError

    with (
        patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
        patch.object(service, "watch_sleep", side_effect=advance),
        pytest.raises(asyncio.CancelledError),
    ):
        await service.watch_loop()
    return updates, bumps, polls, watches
