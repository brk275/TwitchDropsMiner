"""Response validation must preserve observation, claim and snapshot boundaries."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_EXTRA_MINUTES
from src.exceptions import GQLException
from src.models.channel import Channel
from src.services.deadline_context import DeadlineContextGuard
from src.services.inventory_service import InventoryService
from src.services.message_handlers import MessageHandlerService
from src.services.watch_service import WatchService
from tests.test_current_drop_responses import response
from tests.test_deadline_channels import runtime as real_runtime
from tests.test_deadline_forecast import NOW
from tests.test_stale_progress import make_campaign
from tests.test_stale_progress import mining_state as base_mining


BAD_REPLIES = (
    {"data": {"currentUser": None}},
    response({"dropID": "drop"}),
    response({"dropID": [], "currentMinutesWatched": 9}),
    response({"dropID": "drop", "currentMinutesWatched": False}),
    response({"dropID": "drop", "currentMinutesWatched": 1.5}),
    {**response({"dropID": "drop", "currentMinutesWatched": 59}), "errors": [{"message": "test error"}]},
    {"data": {"currentUser": {}}},
    response({"dropID": "unknown", "currentMinutesWatched": None}),
)


@pytest.fixture
def mining():
    twitch, channel, campaign = base_mining.__wrapped__()
    channel._stream = object()
    channel._watch_current.return_value = True
    twitch.close = MagicMock()
    twitch.gui.progress.minute_almost_done.return_value = True
    campaign.bump_minutes = MagicMock(wraps=campaign.bump_minutes)
    for drop in campaign.drops:
        drop.extra_current_minutes = 2
    return twitch, channel, campaign


def snapshot(campaign):
    return deepcopy([(drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed,
                      drop.claim_id, drop.progress_observation) for drop in campaign.drops])


async def run_poll(twitch, monkeypatch, replies, *, during=None, until=30, clock=None, after=None,
                   query=None, cleanup=None):
    clock = [0.0] if clock is None else clock
    requests, watches = [], []
    channel = twitch.watching_channel.get_with_default(None)
    service = getattr(twitch, "_watch_service", None)
    if not isinstance(service, WatchService):
        service = WatchService(twitch)

    async def ask(operation):
        requests.append(clock[0])
        if during is not None:
            await during(len(requests))
        if query is not None:
            return await query(operation)
        reply = replies[min(len(requests) - 1, len(replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def send():
        watches.append(clock[0])
        if after is not None and requests:
            await after()
            raise asyncio.CancelledError
        return True

    async def advance(delay):
        clock[0] += delay
        if clock[0] >= until:
            raise asyncio.CancelledError

    if isinstance(channel, Channel):
        monkeypatch.setattr(Channel, "send_watch", AsyncMock(side_effect=send))
    else:
        monkeypatch.setattr(channel, "send_watch", send)
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "watch_sleep", advance)
    twitch.gql_request = ask
    tasks = asyncio.all_tasks()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(service.watch_loop(), 2)
    finally:
        if cleanup is not None:
            await cleanup()
    assert not {task for task in asyncio.all_tasks() - tasks if not task.done()}
    assert requests == ([20, 80] if until == 90 else [20])
    assert all(b - a <= 10 for a, b in zip(watches, watches[1:], strict=False))
    twitch.close.assert_not_called()
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", BAD_REPLIES)
@pytest.mark.parametrize("timer_ready", [False, True])
async def test_unusable_reply_preserves_evidence_then_fresh_poll_reconciles(mining, monkeypatch, broken, timer_ready):
    twitch, _, campaign = mining
    twitch.gui.progress.minute_almost_done.return_value = timer_ready
    before, revision, payload = snapshot(campaign), campaign.progress_revision, deepcopy(broken)

    async def before_reply(_):
        assert snapshot(campaign) == before
        assert campaign.progress_revision == revision

    await run_poll(twitch, monkeypatch, (broken, response({"dropID": "drop", "currentMinutesWatched": 9})),
                   during=before_reply, until=90)
    reported, sibling = campaign.drops
    assert campaign.progress_revision == revision + 1
    assert reported.real_current_minutes == sibling.real_current_minutes == 9
    assert reported.extra_current_minutes == sibling.extra_current_minutes == 0
    assert reported.observed_minutes == 9 and reported.progress_observation["source"] == "current_drop"
    assert sibling.progress_observation == before[1][-1]
    assert broken == payload
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", ["drop", "sibling"])
@pytest.mark.parametrize("minutes", [0, 10, 11])
@pytest.mark.parametrize("kind", ["malformed", "valid", "empty", "transport"])
async def test_new_websocket_observation_wins_without_false_fallback(mining, monkeypatch, observed, minutes, kind):
    twitch, _, campaign = mining
    revision, observations = campaign.progress_revision, [drop.progress_observation for drop in campaign.drops]
    replies = {
        "malformed": {"data": {"currentUser": None}},
        "valid": response({"dropID": "drop", "currentMinutesWatched": 9}),
        "empty": response(None), "transport": GQLException("test failure"),
    }

    async def during(_):
        await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
            "drop_id": observed, "current_progress_min": minutes, "required_progress_min": 60,
        }})

    await run_poll(twitch, monkeypatch, (replies[kind],), during=during)
    assert campaign.progress_revision == revision + 1
    assert [drop.real_current_minutes for drop in campaign.drops] == [minutes, minutes]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    for drop, previous in zip(campaign.drops, observations, strict=True):
        if drop.id == observed:
            assert drop.observed_minutes == minutes and drop.progress_observation["source"] == "websocket"
        else:
            assert drop.progress_observation == previous
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_id", ["drop", "sibling"])
@pytest.mark.parametrize("kind", ["malformed", "valid", "empty"])
async def test_claim_normalization_cannot_be_undone_or_mislabeled_by_pending_reply(mining, monkeypatch, claimed_id, kind):
    twitch, _, campaign = mining
    claimed = campaign.timed_drops[claimed_id]
    revision, before = campaign.progress_revision, snapshot(campaign)
    replies = {
        "malformed": {**response({"dropID": "drop", "currentMinutesWatched": 9}), "errors": [{"message": "test"}]},
        "valid": response({"dropID": "drop", "currentMinutesWatched": 9}), "empty": response(None),
    }
    claimed._claim = AsyncMock(return_value=True)

    async def during(_):
        assert await claimed.claim()

    await run_poll(twitch, monkeypatch, (replies[kind],), during=during)
    assert campaign.progress_revision == revision + 1
    for drop, previous in zip(campaign.drops, before, strict=True):
        assert drop.progress_observation == previous[-1]
        assert (drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed) == (
            (60, 0, True) if drop is claimed else previous[:3]
        )
    assert await claimed.claim()
    assert campaign.progress_revision == revision + 1
    twitch.drop_history.record.assert_called_once_with(claimed, campaign)
    twitch.request_games_update.assert_called_once_with()
    campaign.bump_minutes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 10, 11])
async def test_fresh_valid_reply_recovers_capped_work_after_bad_reply(mining, monkeypatch, minutes):
    twitch, _, campaign = mining
    for drop in campaign.drops:
        drop.extra_current_minutes = MAX_EXTRA_MINUTES
    before, revision = snapshot(campaign), campaign.progress_revision

    async def before_reply(_):
        assert snapshot(campaign) == before
        assert campaign.progress_revision == revision
        twitch.request_games_update.assert_not_called()

    await run_poll(twitch, monkeypatch, ({"data": {"currentUser": None}},
                                       response({"dropID": "drop", "currentMinutesWatched": minutes})),
                   during=before_reply, until=90)
    assert [drop.real_current_minutes for drop in campaign.drops] == [minutes, minutes]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.progress_revision == revision + 1
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    twitch.request_games_update.assert_called_once_with()
    campaign.bump_minutes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replacement", "removal", "fallback"])
async def test_same_id_or_fallback_replacement_cannot_receive_old_reply(mining, monkeypatch, change):
    twitch, _, campaign = mining
    other = make_campaign(twitch, minutes=12)
    before, other_before = snapshot(campaign), snapshot(other)

    async def during(_):
        if change == "replacement":
            twitch._drops = dict(other.timed_drops)
        elif change == "removal":
            del twitch._drops["drop"]
        else:
            twitch._inventory_service.get_active_campaign = lambda _: other

    reply = response(None) if change == "fallback" else response({"dropID": "drop", "currentMinutesWatched": 9})
    await run_poll(twitch, monkeypatch, (reply,), during=during)
    assert snapshot(campaign) == before and snapshot(other) == other_before
    assert campaign.progress_revision == other.progress_revision == 0
    campaign.bump_minutes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["malformed", "bad_number", "valid", "unknown"])
async def test_response_effects_on_bound_context_use_direct_evidence_not_estimates(monkeypatch, kind):
    twitch, campaign, _, _, _ = real_runtime.__wrapped__(monkeypatch)
    twitch.close = MagicMock()
    twitch._inventory_service = InventoryService(twitch)
    clock = [0.0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = NOW + timedelta(seconds=clock[0])
            return stamp.replace(tzinfo=None) if tz is None else stamp.astimezone(tz)

    for module in ("src.models.campaign", "src.models.drop", "src.core.client"):
        monkeypatch.setattr(f"{module}.datetime", Clock)
    monkeypatch.setattr("src.models.channel.time", lambda: Clock.now(timezone.utc).timestamp())
    guard = DeadlineContextGuard(clock=lambda: 100 + clock[0])
    saved = []
    before, revision = snapshot(campaign), campaign.progress_revision
    replies = {
        "malformed": {"data": {"currentUser": None}},
        "bad_number": response({"dropID": "reward", "currentMinutesWatched": False}),
        "valid": response({"dropID": "reward", "currentMinutesWatched": 0}),
        "unknown": response({"dropID": "not-in-inventory", "currentMinutesWatched": 0}),
    }

    async def during(_):
        result = guard.capture(twitch, now=Clock.now(timezone.utc), horizon=NOW + timedelta(days=3),
                               enabled=True, include_channels=True)
        assert result.reason == "captured"
        saved.append(result.bound)

    async def after():
        reason = guard.validate(saved[0], twitch, now=Clock.now(timezone.utc), enabled=True)
        assert reason == ("current" if kind in ("malformed", "bad_number") else "superseded")

    await run_poll(twitch, monkeypatch, (replies[kind],), during=during, clock=clock, after=after)
    drop = campaign.timed_drops["reward"]
    if kind == "valid":
        assert drop.progress_observation["source"] == "current_drop"
        assert drop.progress_observation["received_at"] == (NOW + timedelta(seconds=20)).isoformat()
        assert campaign.progress_revision == revision + 1
    else:
        assert drop.progress_observation == before[0][-1]
        assert campaign.progress_revision == revision
    assert drop.extra_current_minutes == (1 if kind == "unknown" else 0)
    assert saved[0].inputs.baseline_complete is saved[0].channels.allocation_known is False
