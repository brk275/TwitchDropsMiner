"""Malformed live minute readings must not enter shared counter arithmetic."""

import asyncio
import logging
from decimal import Decimal
from enum import IntEnum
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.channel import Channel
from src.services.message_handlers import MessageHandlerService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_drop_ignore_policy import _campaign, _drop
from tests.test_special_game_watch import _channel


class Count(IntEnum):
    ONE = 1


class UnsafeInteger(int):
    def __sub__(self, other):
        raise AssertionError("Input arithmetic must not run")


INVALID = (True, False, None, 0.0, 1.5, float("nan"), float("inf"), float("-inf"),
           "9", "", [], {}, Decimal("9"), Fraction(1, 2), Count.ONE, UnsafeInteger(9))


def campaign_state():
    campaign = _campaign([_drop("reported", "Reported", required_minutes=60),
                          _drop("sibling", "Sibling", required_minutes=60)])
    reported, sibling = campaign.drops
    for drop in (reported, sibling):
        drop.real_current_minutes = 10
        drop.extra_current_minutes = 2
    return campaign, reported, sibling


@pytest.mark.parametrize("minutes", INVALID)
def test_non_integer_reading_keeps_reported_and_sibling_state(minutes):
    campaign, reported, _ = campaign_state()

    reported.update_minutes(minutes)

    assert [(drop.real_current_minutes, drop.extra_current_minutes) for drop in campaign.drops] == [(10, 2), (10, 2)]
    assert all(type(drop.real_current_minutes) is int for drop in campaign.drops)
    assert not any(drop.is_claimed for drop in campaign.drops)
    twitch = campaign._twitch
    twitch.gui.inv.update_drop.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.drop_history.record.assert_not_called()


def test_type_refusal_precedes_counter_arithmetic_and_eligibility(monkeypatch):
    campaign, reported, _ = campaign_state()
    policy = MagicMock(side_effect=AssertionError("Invalid reading must not evaluate policy"))
    monkeypatch.setattr(reported, "can_earn", policy)
    reported.real_current_minutes = object()
    reported.update_minutes(None)
    policy.assert_not_called()
    campaign._twitch.gui.inv.update_drop.assert_not_called()


@pytest.mark.parametrize("minutes", [-5, 0, 1, 10, 20, 60, 61, 2**63])
def test_plain_integer_corrections_keep_existing_bounds_and_sibling_semantics(minutes):
    campaign, reported, sibling = campaign_state()
    reported.update_minutes(minutes)
    expected = min(60, max(0, minutes))
    delta = expected - 10
    assert reported.real_current_minutes == expected
    assert sibling.real_current_minutes == (10 if 10 + delta < 0 else min(60, 10 + delta))
    assert [drop.extra_current_minutes for drop in campaign.drops] == ([2, 2] if not delta else [0, 0])
    assert not any(drop.is_claimed for drop in campaign.drops)


@pytest.mark.parametrize("minutes", [-5, 0, 5, 8, 10, 20, 61, 2**63])
def test_integer_corrections_keep_existing_unequal_sibling_underflow_guard(minutes):
    _, reported, sibling = campaign_state()
    sibling.real_current_minutes = 2
    reported.update_minutes(minutes)
    expected = min(60, max(0, minutes))
    delta = expected - 10
    assert reported.real_current_minutes == expected
    assert sibling.real_current_minutes == (2 if 2 + delta < 0 else min(60, 2 + delta))
    assert reported.extra_current_minutes == (2 if not delta else 0)
    assert sibling.extra_current_minutes == (2 if not delta or 2 + delta < 0 else 0)


def transport_state():
    campaign, reported, sibling = campaign_state()
    twitch = campaign._twitch
    channel = _channel(twitch)
    twitch.watching_channel = AwaitableValue()
    twitch.watching_channel.set(channel)
    twitch._drops = dict(campaign.timed_drops)
    twitch.is_mining_time.return_value = True
    twitch._inventory_service = SimpleNamespace(get_active_campaign=lambda watched: campaign)
    return twitch, channel, campaign, reported, sibling


def assert_initial(campaign):
    assert [(drop.real_current_minutes, drop.extra_current_minutes) for drop in campaign.drops] == [(10, 2), (10, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [False, 1.5, float("nan"), float("inf"), None, "9"])
async def test_websocket_ignores_bad_reading_then_accepts_next_integer(minutes, caplog):
    twitch, _, campaign, reported, sibling = transport_state()
    handler = MessageHandlerService(twitch)
    await handler.process_drops(42, {"type": "drop-progress", "data": {
        "drop_id": reported.id, "current_progress_min": minutes, "required_progress_min": 60,
    }})
    assert_initial(campaign)
    twitch.gui.inv.update_drop.assert_not_called()
    await handler.process_drops(42, {"type": "drop-progress", "data": {
        "drop_id": reported.id, "current_progress_min": 9, "required_progress_min": 60,
    }})
    assert reported.real_current_minutes == sibling.real_current_minutes == 9
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [False, 1.5, float("nan"), float("inf"), None, "9"])
async def test_progress_poll_survives_bad_reading_and_reconciles_next_integer(monkeypatch, minutes, caplog):
    twitch, _, campaign, reported, sibling = transport_state()
    clock, requests = [0.0], []
    watcher = WatchService(twitch)

    async def query(operation):
        requests.append(clock[0])
        assert_initial(campaign)
        return {"data": {"currentUser": {"dropCurrentSession": {
            "dropID": reported.id, "currentMinutesWatched": minutes if len(requests) == 1 else 9,
        }}}}

    async def advance(delay):
        clock[0] += delay
        if clock[0] >= 90:
            raise asyncio.CancelledError

    twitch.gql_request = AsyncMock(side_effect=query)
    monkeypatch.setattr(Channel, "send_watch", AsyncMock(return_value=True))
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(watcher, "watch_sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher.watch_loop(), 2)
    assert requests == [20.0, 80.0]
    assert reported.real_current_minutes == sibling.real_current_minutes == 9
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    twitch.close.assert_not_called()
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
