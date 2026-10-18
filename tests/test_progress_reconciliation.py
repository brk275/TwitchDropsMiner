"""Confirmed readings must replace estimates without weakening mining guards."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import MethodType
from unittest.mock import AsyncMock, patch

import pytest

from src.config import MAX_EXTRA_MINUTES, State
from src.core.client import Twitch
from src.services.message_handlers import MessageHandlerService
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_drop_ignore_policy import _campaign, _drop
from tests.test_special_game_watch import _channel


@pytest.fixture
def mining_state():
    campaign = _campaign([_drop("drop", "Reward", required_minutes=60)])
    twitch = campaign._twitch
    channel = _channel(twitch)
    selected = AwaitableValue()
    selected.set(channel)
    twitch.watching_channel = selected
    twitch._drops = dict(campaign.timed_drops)
    twitch._inventory_service.get_active_campaign.return_value = None
    twitch.is_mining_time.return_value = True
    twitch.gui.progress.minute_almost_done.return_value = True
    return twitch, campaign, channel


async def observe(source, twitch, channel, minutes):
    drop = twitch._drops["drop"]
    if source == "model":
        drop.update_minutes(minutes)
    elif source == "websocket":
        await MessageHandlerService(twitch).process_drops(1, {
            "type": "drop-progress",
            "data": {
                "drop_id": drop.id, "current_progress_min": minutes,
                "required_progress_min": drop.required_minutes,
            },
        })
    else:
        clock = [0.0]
        twitch.gql_request = AsyncMock(return_value={"data": {"currentUser": {
            "dropCurrentSession": {"dropID": drop.id, "currentMinutesWatched": minutes},
        }}})
        service = WatchService(twitch)

        async def advance(delay):
            clock[0] += delay
            if clock[0] >= 30:
                raise asyncio.CancelledError

        with (
            patch.object(type(channel), "send_watch", new=AsyncMock(return_value=True)),
            patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
            patch.object(service, "watch_sleep", side_effect=advance),
            pytest.raises(asyncio.CancelledError),
        ):
            await service.watch_loop()
        twitch.gql_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["model", "websocket", "poll"])
@pytest.mark.parametrize("extra", [0, 1, MAX_EXTRA_MINUTES - 1, MAX_EXTRA_MINUTES])
@pytest.mark.parametrize("minutes", [0, 2])
async def test_confirmed_reading_clears_estimates_and_recovers_capped_reward(
    mining_state, source, extra, minutes,
):
    twitch, campaign, channel = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = extra
    stamp = datetime.now(timezone.utc) + timedelta(hours=1)
    assert drop.can_earn(channel) is (extra < MAX_EXTRA_MINUTES)
    assert campaign.can_earn(channel) is (extra < MAX_EXTRA_MINUTES)
    assert campaign.can_earn_within(stamp) is (extra < MAX_EXTRA_MINUTES)
    assert drop.can_reconcile_progress(channel)

    await observe(source, twitch, channel, minutes)

    assert drop.real_current_minutes == minutes
    assert drop.extra_current_minutes == 0
    assert drop.can_earn(channel)
    assert campaign.can_earn(channel)
    assert campaign.can_earn_within(stamp)
    if extra >= MAX_EXTRA_MINUTES:
        twitch.request_games_update.assert_called_once_with()
    else:
        twitch.request_games_update.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["model", "websocket", "poll"])
async def test_repeated_readings_do_not_repeat_recovery_refresh(mining_state, source):
    twitch, campaign, channel = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES

    await observe(source, twitch, channel, 0)
    await observe(source, twitch, channel, 0)

    assert drop.real_current_minutes == 0
    assert drop.extra_current_minutes == 0
    twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["model", "websocket", "poll"])
@pytest.mark.parametrize(
    "blocker", ["claimed", "ignored", "prerequisite", "non_watch", "unlinked",
                "expired_drop", "upcoming_drop", "expired_campaign", "upcoming_campaign"],
)
async def test_reconciliation_keeps_non_estimate_eligibility_guards(mining_state, source, blocker):
    twitch, campaign, channel = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES
    now = datetime.now(timezone.utc)
    if blocker == "claimed":
        drop.is_claimed = True
    elif blocker == "ignored":
        twitch.settings.drop_name_blacklist = [drop.name]
    elif blocker == "prerequisite":
        drop.precondition_drops = ["missing"]
    elif blocker == "non_watch":
        drop.required_minutes = 0
    elif blocker == "unlinked":
        campaign.linked = False
    elif blocker == "expired_drop":
        drop.ends_at = now - timedelta(seconds=1)
    elif blocker == "upcoming_drop":
        drop.starts_at = now + timedelta(hours=1)
    elif blocker == "expired_campaign":
        campaign.ends_at = now - timedelta(seconds=1)
    elif blocker == "upcoming_campaign":
        campaign.starts_at = now + timedelta(hours=1)

    await observe(source, twitch, channel, 2)

    assert drop.real_current_minutes == 0
    assert drop.extra_current_minutes == MAX_EXTRA_MINUTES
    assert not drop.can_earn(channel)
    twitch.request_games_update.assert_not_called()
    twitch.gui.inv.update_drop.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["websocket", "poll"])
@pytest.mark.parametrize("blocker", ["wrong_category", "not_in_acl"])
async def test_reconciliation_keeps_channel_guards(mining_state, source, blocker):
    twitch, campaign, channel = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES
    if blocker == "wrong_category":
        campaign.game.id = 999
    else:
        campaign.allowed_channels = [_channel(twitch, id=999)]

    await observe(source, twitch, channel, 2)

    assert drop.real_current_minutes == 0
    assert drop.extra_current_minutes == MAX_EXTRA_MINUTES
    twitch.request_games_update.assert_not_called()


@pytest.mark.parametrize("minutes", [0, 2, 10, 99, -1])
def test_sibling_reconciliation_preserves_bounds_and_claims(minutes):
    campaign = _campaign([
        _drop("reported", "Reported reward", required_minutes=60),
        _drop("sibling", "Sibling reward", required_minutes=60),
        _drop("claimed", "Claimed reward", required_minutes=60, claimed=True),
        _drop("ignored", "Ignored reward", required_minutes=60),
        _drop("locked", "Locked reward", required_minutes=60, preconditions=("missing",)),
    ], blacklist=["Ignored"])
    reported, sibling, claimed, ignored, locked = campaign.drops
    for drop in (reported, sibling):
        drop.real_current_minutes = 10
        drop.extra_current_minutes = MAX_EXTRA_MINUTES

    reported.update_minutes(minutes)

    assert reported.real_current_minutes == sibling.real_current_minutes == min(60, max(0, minutes))
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    assert claimed.is_claimed and claimed.real_current_minutes == 60
    assert ignored.real_current_minutes == locked.real_current_minutes == 0
    campaign._twitch.request_games_update.assert_called_once_with()


def test_estimates_still_stop_at_the_cap_without_a_confirmed_reading(mining_state):
    twitch, campaign, channel = mining_state
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES - 1

    assert drop._bump_minutes(channel)
    assert not drop._bump_minutes(channel)

    assert drop.extra_current_minutes == MAX_EXTRA_MINUTES
    assert drop.real_current_minutes == 0
    assert not drop.can_earn(channel)
    assert not campaign.can_earn_within(datetime.now(timezone.utc) + timedelta(hours=1))
    twitch.request_games_update.assert_not_called()


def test_recovered_reward_queues_real_idle_policy_refresh(mining_state):
    twitch, campaign, channel = mining_state
    twitch._state = State.IDLE
    twitch._games_update_pending = False
    twitch._inventory_loaded = True
    twitch._state_change = asyncio.Event()
    twitch._schedule_changed = asyncio.Event()
    twitch.request_games_update = MethodType(Twitch.request_games_update, twitch)
    drop = campaign.timed_drops["drop"]
    drop.extra_current_minutes = MAX_EXTRA_MINUTES
    twitch.settings.games_to_watch = [campaign.game.name]
    twitch.settings.mining_benefits = {"DIRECT_ENTITLEMENT": True}
    assert StreamSelector().get_wanted_games(twitch.settings, [campaign]) == []

    drop.update_minutes(0)

    assert twitch._state is State.IDLE
    assert twitch._games_update_pending
    assert twitch._state_change.is_set()
    assert twitch._schedule_changed.is_set()
    Twitch._activate_pending_games_update(twitch)
    assert twitch._state is State.GAMES_UPDATE
    assert drop.can_earn(channel)
    assert StreamSelector().get_wanted_games(twitch.settings, [campaign]) == [campaign.game]


@pytest.mark.parametrize("blocker", ["ignored", "claimed", "prerequisite", "expired"])
def test_ineligible_reported_reward_cannot_update_an_eligible_sibling(blocker):
    campaign = _campaign([
        _drop("reported", "Reported reward", required_minutes=60),
        _drop("sibling", "Sibling reward", required_minutes=60),
    ])
    reported, sibling = campaign.drops
    if blocker == "ignored":
        campaign._twitch.settings.drop_name_blacklist = [reported.name]
    elif blocker == "claimed":
        reported.is_claimed = True
    elif blocker == "prerequisite":
        reported.precondition_drops = ["missing"]
    else:
        reported.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    sibling.extra_current_minutes = 1

    reported.update_minutes(2)

    assert reported.real_current_minutes == sibling.real_current_minutes == 0
    assert sibling.extra_current_minutes == 1
    campaign._twitch.request_games_update.assert_not_called()


@pytest.mark.parametrize("reported_extra", [0, MAX_EXTRA_MINUTES])
@pytest.mark.parametrize("minutes", [0, 5, 20])
def test_unequal_sibling_progress_reconciles_estimates_once(reported_extra, minutes):
    campaign = _campaign([
        _drop("reported", "Reported reward", required_minutes=60),
        _drop("sibling", "Sibling reward", required_minutes=60),
    ])
    reported, sibling = campaign.drops
    reported.real_current_minutes = 10
    reported.extra_current_minutes = reported_extra
    sibling.real_current_minutes = 2
    sibling.extra_current_minutes = MAX_EXTRA_MINUTES

    reported.update_minutes(minutes)

    expected_sibling = 2 + minutes - 10 if minutes >= 8 else 2
    assert reported.real_current_minutes == minutes
    assert sibling.real_current_minutes == expected_sibling
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    campaign._twitch.request_games_update.assert_called_once_with()
    campaign._twitch.gui.inv.update_drop.reset_mock()

    reported.update_minutes(minutes)

    assert sibling.real_current_minutes == expected_sibling
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    campaign._twitch.request_games_update.assert_called_once_with()
    campaign._twitch.gui.inv.update_drop.assert_not_called()
