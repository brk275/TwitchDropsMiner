"""A completed but unsettled reward can still occupy the real ranked selector."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.services.channel_service import ChannelService
from src.services.deadline_forecast import DeadlineForecaster, MiningWindow, ViewingWindow
from src.services.watch_service import WatchService
from tests.test_deadline_channel_policy import simulate
from tests.test_deadline_channels import channel, index
from tests.test_deadline_context import state as context_state
from tests.test_deadline_forecast import NOW, reward, snapshot
from tests.test_deadline_inputs import campaign
from tests.test_deadline_timing import timing


@pytest.mark.asyncio
async def test_pending_high_rank_claim_blocks_actual_selection_and_changes_rescue(monkeypatch):
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].replace(tzinfo=None) if tz is None else clock[0].astimezone(tz)

    for module in ("src.models.campaign", "src.models.drop", "src.core.client"):
        monkeypatch.setattr(f"{module}.datetime", Clock)
    monkeypatch.setattr("src.models.channel.time", lambda: clock[0].timestamp())
    twitch, high, current, _, _ = context_state.__wrapped__()
    high_drop = high.timed_drops["reward"]
    high_drop.required_minutes = 1
    high.ends_at = high_drop.ends_at = NOW + timedelta(hours=48)
    urgent = campaign(twitch, "urgent", "Urgent", required=1, deadline=2.5 / 60)
    urgent_drop = urgent.timed_drops["reward"]
    urgent_drop.ends_at = urgent.ends_at
    other = channel(twitch, 20, urgent.game)
    current._stream.drops_enabled = True
    twitch.channels[other.id] = other
    twitch.wanted_games = [high.game, urgent.game]
    index(twitch)
    twitch._channel_service = ChannelService(twitch)
    twitch._watch_service = WatchService(twitch)

    async def noop():
        pass

    async def exit_step():
        twitch._state = State.EXIT

    twitch._ensure_api_clients = lambda: None
    twitch.get_auth = AsyncMock(return_value=twitch._auth_state)
    twitch.websocket = MagicMock(start=AsyncMock())
    twitch._message_handler_service = MagicMock()
    twitch._watching_task = twitch._schedule_task = None
    twitch._stop_mining_schedule = AsyncMock()
    twitch._mining_schedule_loop = noop
    twitch._watch_service.watch_loop = noop
    twitch.request_inventory_refresh = MagicMock()
    twitch._state_change = MagicMock(wait=exit_step)
    twitch.get_active_campaign = MagicMock(return_value=None)
    twitch.watch = MagicMock(wraps=twitch._watch_service.watch)

    # Actual CHANNEL_SWITCH steps on the same models/watched object. Claim
    # settlement is an explicit input, not a network or notification simulation.
    for seconds, settled, expected in ((0, False, current), (60, False, current), (180, True, None)):
        clock[0] = NOW + timedelta(seconds=seconds)
        high_drop.real_current_minutes = 1 if seconds else 0
        high_drop.is_claimed = settled
        twitch._state = State.CHANNEL_SWITCH
        twitch.watch.reset_mock()
        await asyncio.wait_for(twitch._run(), 1)
        await asyncio.gather(twitch._watching_task, twitch._schedule_task)
        twitch.watch.assert_not_called()
        assert (twitch.can_watch(current) or twitch.can_watch(other)) is (expected is not None)
        if expected is not None:
            assert twitch.watching_channel.get_with_default(None) is expected

    state = snapshot(reward("high", 1, 1, 48, claim_delay=timedelta(minutes=2)),
                     reward("urgent", 2, 1, 2.5 / 60), current_channel_id="1")
    before = repr(state)
    run = simulate(state)
    assert run.earned == {"high": NOW + timedelta(minutes=1)}
    result = DeadlineForecaster().analyze(state, enabled=True)
    assert result.reason == "rescue" and result.plan.drop_id == "urgent"
    assert result.plan.earned_at == NOW + timedelta(minutes=1)
    assert result.plan.protected_drop_ids == frozenset({"high"})
    assert repr(state) == before


def test_completed_high_rank_at_capture_keeps_its_channel_until_claim_settlement():
    state = snapshot(
        replace(reward("high", 1, 1, 48, claim_delay=timedelta(minutes=2)), observed_minutes=1),
        reward("urgent", 2, 1, 2.5 / 60), current_channel_id="1",
    )
    run = simulate(state)
    assert run.first.channel_id == "1" and not run.first.credits
    assert run.earned == {"high": NOW}


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(seconds=20), timedelta(minutes=2)])
@pytest.mark.parametrize("current", [None, "1", "2"])
def test_occupancy_wait_is_not_extra_progress_or_an_unconditional_global_pause(delay, current):
    state = snapshot(reward("high", 1, 1, 48, claim_delay=delay), reward("low", 2, 1, 4),
                     current_channel_id=current)
    run = simulate(state)
    assert run.earned == {"high": NOW + timedelta(minutes=1), "low": NOW + timedelta(minutes=2) + delay}


@pytest.mark.parametrize("seconds,expected", [(239, False), (240, False), (241, True)])
def test_claim_occupancy_respects_exclusive_reward_deadline(seconds, expected):
    state = snapshot(reward("high", 1, 1, 48, claim_delay=timedelta(minutes=2)),
                     reward("low", 2, 1, seconds / 3600), current_channel_id="1")
    run = simulate(state)
    assert ("low" in run.earned) is expected
    assert (DeadlineForecaster().analyze(state, enabled=True).plan is None) is expected


def test_pending_claim_does_not_prevent_declared_parallel_credit_on_same_channel():
    state = snapshot(replace(reward("high", 1, 1, 48, claim_delay=timedelta(minutes=5)), observed_minutes=1),
                     reward("low", 2, 1, 4), current_channel_id="shared")
    state = replace(state, viewing_windows=(
        ViewingWindow("shared", NOW, state.horizon, frozenset({"high", "low"}), True),
    ))
    assert simulate(state).earned == {"high": NOW, "low": NOW + timedelta(minutes=1)}


@pytest.mark.parametrize("boundary", ["expiry", "offline", "pause"])
def test_expiry_or_offline_releases_pending_claim_but_mining_pause_does_not_settle_it(boundary):
    state = snapshot(reward("high", 1, 1, 48, claim_delay=timedelta(hours=1)),
                     reward("low", 2, 1, 4), current_channel_id="1")
    release = NOW + timedelta(minutes=2)
    if boundary == "expiry":
        state = replace(state, drops=(replace(state.drops[0], ends_at=release), state.drops[1]))
    elif boundary == "offline":
        state = replace(state, viewing_windows=(replace(state.viewing_windows[0], ends_at=release),
                                                state.viewing_windows[1]))
    else:
        state = replace(state, mining_windows=(MiningWindow(NOW, release),
                                               MiningWindow(release + timedelta(minutes=1), state.horizon)))
    expected = NOW + timedelta(minutes=62) if boundary == "pause" else release + timedelta(minutes=1)
    assert simulate(state).earned["low"] == expected


def test_higher_ranked_new_work_can_preempt_a_completed_lower_rank_claim():
    state = snapshot(reward("high", 1, 1, 4),
                     replace(reward("low", 2, 1, 4, claim_delay=timedelta(hours=1)), observed_minutes=1),
                     current_channel_id="2")
    run = simulate(state)
    assert run.first.channel_id == "1"
    assert run.earned == {"high": NOW + timedelta(minutes=1), "low": NOW}


@pytest.mark.parametrize("promote", [False, True])
def test_pending_tied_current_retains_normal_stream_but_allows_acl_promotion(promote):
    state = snapshot(replace(reward("pending", 1, 1, 4, claim_delay=timedelta(minutes=2)), observed_minutes=1),
                     reward("other", 1, 1, 4), current_channel_id="current")
    state = replace(state, viewing_windows=(
        ViewingWindow("other", NOW, state.horizon, frozenset({"other"}), True, 0, promote),
        ViewingWindow("current", NOW, state.horizon, frozenset({"pending"}), True, 1),
    ))
    run = simulate(state)
    assert run.first.channel_id == ("other" if promote else "current")
    assert run.earned["other"] == NOW + timedelta(minutes=1 if promote else 3)


def test_last_pending_claim_on_a_shared_high_channel_controls_release():
    state = snapshot(
        reward("first", 1, 1, 4, claim_delay=timedelta(minutes=2)),
        reward("second", 1, 1, 4, claim_delay=timedelta(minutes=3)), reward("low", 2, 1, 4),
        current_channel_id="1",
    )
    assert simulate(state).earned == {
        "first": NOW + timedelta(minutes=1), "second": NOW + timedelta(minutes=1),
        "low": NOW + timedelta(minutes=5),
    }


def test_claim_settlement_unlocks_child_without_credit_during_occupancy():
    state = snapshot(reward("parent", 1, 1, 4, claim_delay=timedelta(minutes=2), wanted=False),
                     reward("child", 1, 1, 4, prerequisite_ids=("parent",)), reward("low", 2, 1, 4),
                     current_channel_id="1")
    assert simulate(state).earned == {
        "parent": NOW + timedelta(minutes=1), "child": NOW + timedelta(minutes=4),
        "low": NOW + timedelta(minutes=5, seconds=20),
    }


def test_finite_timing_variants_include_claim_occupancy_in_each_baseline():
    state = snapshot(reward("high", 1, 1, 48), reward("low", 2, 1, 2.5 / 60), current_channel_id="1")
    options = timing(state, claims={"high": (timedelta(seconds=20), timedelta(minutes=2))})
    result = DeadlineForecaster().analyze_robust(state, options, enabled=True)
    # One supplied baseline can already earn low, so there is no common rescue.
    assert result.reason == "no_safe_rescue" and result.plan is None
    assert result.baseline_earned_ids == frozenset({"high", "low"})
