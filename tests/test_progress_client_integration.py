"""GraphQL path failures must preserve real progress and claim boundaries."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import GQL_OPERATIONS, MAX_EXTRA_MINUTES
from src.services.deadline_context import DeadlineContextGuard
from src.services.inventory_service import InventoryService
from src.services.message_handlers import MessageHandlerService
from tests.test_current_drop_responses import response
from tests.test_deadline_channels import runtime as real_runtime
from tests.test_deadline_forecast import NOW
from tests.test_gql_server_error_paths import gql as gql
from tests.test_gql_server_error_paths import server_error
from tests.test_progress_response_integration import mining as base_mining
from tests.test_progress_response_integration import run_poll, snapshot
from tests.test_stale_progress import make_campaign


REPLIES = {
    "null_root": server_error(None, ["currentUser", "dropCurrentSession"]),
    "null_parent": server_error({"currentUser": None}, ["currentUser", "dropCurrentSession"]),
    "missing_path": {"data": {}, "errors": [{"message": "server error"}]},
    "bad_index": server_error({"items": [{}]}, ["items", -1, "name"]),
    "returned_error": server_error(response(None)["data"], ["currentUser", "dropCurrentSession"]),
    "valid": response({"dropID": "drop", "currentMinutesWatched": 9}),
}


@pytest.fixture
def mining():
    return base_mining.__wrapped__()


async def run_client(twitch, gql, monkeypatch, payloads, *, on_read=None, until=30, clock=None, after=None):
    client, read = gql
    channel = twitch.watching_channel.get_with_default(None)

    async def reply(*args, **kwargs):
        if on_read is not None:
            await on_read(read.await_count)
        return payloads[min(read.await_count - 1, len(payloads) - 1)]

    async def cleanup():
        limiter = client._qgl_limiter
        task, limiter._reset_task = limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    read.side_effect = reply
    await run_poll(twitch, monkeypatch, (), query=client.request, cleanup=cleanup,
                   until=until, clock=clock, after=after)
    expected = 2 if until == 90 else 1
    assert read.await_count == client._auth_state.validate.await_count == expected
    if client._auth_state.browser_active:
        operations = [call.args[0] for call in read.await_args_list]
    else:
        operations = [call.kwargs["json"] for call in client.http_client.request.call_args_list]
    assert operations == [GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": str(channel.id)})] * expected
    assert client._qgl_limiter.concurrent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(REPLIES)[:-1])
async def test_request_failure_estimates_without_refreshing_evidence_then_fresh_reading_recovers(mining, gql, monkeypatch, kind):
    twitch, channel, campaign = mining
    before, revision = snapshot(campaign), campaign.progress_revision
    payload, failed = deepcopy(REPLIES[kind]), kind != "returned_error"
    saved = deepcopy(payload)

    async def on_read(index):
        if index == 2:
            assert campaign.progress_revision == revision
            assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
            assert [drop.extra_current_minutes for drop in campaign.drops] == [3 if failed else 2] * 2
            assert [drop.progress_observation for drop in campaign.drops] == [item[-1] for item in before]

    await run_client(twitch, gql, monkeypatch, (payload, deepcopy(REPLIES["valid"])), on_read=on_read, until=90)
    assert payload == saved
    assert campaign.progress_revision == revision + 1
    assert [drop.real_current_minutes for drop in campaign.drops] == [9, 9]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    assert campaign.bump_minutes.call_count == int(failed)
    if failed:
        campaign.bump_minutes.assert_called_once_with(channel)
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", ["drop", "sibling"])
@pytest.mark.parametrize("minutes", [0, 10, 11])
@pytest.mark.parametrize("kind", ["null_parent", "missing_path", "returned_error", "valid"])
async def test_newer_websocket_observation_supersedes_request_failure_or_stale_success(mining, gql, monkeypatch, observed, minutes, kind):
    twitch, _, campaign = mining
    before, revision = snapshot(campaign), campaign.progress_revision

    async def on_read(_):
        await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
            "drop_id": observed, "current_progress_min": minutes, "required_progress_min": 60,
        }})

    await run_client(twitch, gql, monkeypatch, (deepcopy(REPLIES[kind]),), on_read=on_read)
    assert campaign.progress_revision == revision + 1
    assert [drop.real_current_minutes for drop in campaign.drops] == [minutes, minutes]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    for drop, previous in zip(campaign.drops, before, strict=True):
        if drop.id == observed:
            assert drop.observed_minutes == minutes and drop.progress_observation["source"] == "websocket"
        else:
            assert drop.progress_observation == previous[-1]
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_id", ["drop", "sibling"])
@pytest.mark.parametrize("kind", ["null_parent", "returned_error", "valid"])
async def test_claim_transition_is_not_undone_or_relabelled_by_client_response(mining, gql, monkeypatch, claimed_id, kind):
    twitch, _, campaign = mining
    claimed = campaign.timed_drops[claimed_id]
    before, revision = snapshot(campaign), campaign.progress_revision
    claimed._claim = AsyncMock(return_value=True)

    async def on_read(_):
        assert await claimed.claim()

    await run_client(twitch, gql, monkeypatch, (deepcopy(REPLIES[kind]),), on_read=on_read)
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
@pytest.mark.parametrize("change", ["inventory", "fallback"])
@pytest.mark.parametrize("kind", ["null_parent", "valid"])
async def test_model_replacement_and_fallback_guess_changes_keep_distinct_guards(mining, gql, monkeypatch, change, kind):
    twitch, _, campaign = mining
    replacement = make_campaign(twitch, minutes=12)
    before, other_before = snapshot(campaign), snapshot(replacement)

    async def on_read(_):
        twitch._inventory_service.get_active_campaign = lambda _: replacement
        if change == "inventory":
            twitch._drops = dict(replacement.timed_drops)

    await run_client(twitch, gql, monkeypatch, (deepcopy(REPLIES[kind]),), on_read=on_read)
    assert snapshot(replacement) == other_before
    assert replacement.progress_revision == 0
    if change == "fallback" and kind == "valid":
        assert campaign.progress_revision == 1
        assert [drop.real_current_minutes for drop in campaign.drops] == [9, 9]
        assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
        assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
        assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    else:
        assert snapshot(campaign) == before
        assert campaign.progress_revision == 0
    campaign.bump_minutes.assert_not_called()
    twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 10, 11])
@pytest.mark.parametrize("kind", ["null_parent", "returned_error"])
async def test_valid_client_reading_recovers_capped_estimates_after_failure(mining, gql, monkeypatch, minutes, kind):
    twitch, _, campaign = mining
    for drop in campaign.drops:
        drop.extra_current_minutes = MAX_EXTRA_MINUTES
    before, revision = snapshot(campaign), campaign.progress_revision

    async def on_read(index):
        if index == 2:
            assert snapshot(campaign) == before
            assert campaign.progress_revision == revision
            twitch.request_games_update.assert_not_called()

    await run_client(twitch, gql, monkeypatch, (deepcopy(REPLIES[kind]),
                     response({"dropID": "drop", "currentMinutesWatched": minutes})), on_read=on_read, until=90)
    assert campaign.progress_revision == revision + 1
    assert [drop.real_current_minutes for drop in campaign.drops] == [minutes, minutes]
    assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
    assert campaign.timed_drops["drop"].progress_observation["source"] == "current_drop"
    assert campaign.timed_drops["sibling"].progress_observation == before[1][-1]
    assert campaign.bump_minutes.call_count == int(kind != "returned_error")
    twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["null_parent", "returned_error", "valid", "unknown"])
async def test_real_context_tracks_estimates_separately_from_direct_client_observations(gql, monkeypatch, kind):
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
    guard, saved = DeadlineContextGuard(clock=lambda: 100 + clock[0]), []
    before, revision = snapshot(campaign), campaign.progress_revision
    payload = response({"dropID": "reward" if kind == "valid" else "unknown", "currentMinutesWatched": 0})
    if kind in REPLIES and kind != "valid":
        payload = deepcopy(REPLIES[kind])

    async def on_read(_):
        captured = guard.capture(twitch, now=Clock.now(timezone.utc), horizon=NOW + timedelta(days=3),
                                 enabled=True, include_channels=True)
        assert captured.reason == "captured"
        saved.append(captured.bound)

    async def after():
        reason = guard.validate(saved[0], twitch, now=Clock.now(timezone.utc), enabled=True)
        assert reason == ("current" if kind == "returned_error" else "superseded")

    await run_client(twitch, gql, monkeypatch, (payload,), on_read=on_read, clock=clock, after=after)
    drop = campaign.timed_drops["reward"]
    assert campaign.progress_revision == revision + int(kind == "valid")
    if kind == "valid":
        assert drop.progress_observation["source"] == "current_drop"
        assert drop.progress_observation["received_at"] == (NOW + timedelta(seconds=20)).isoformat()
    else:
        assert drop.progress_observation == before[0][-1]
    assert drop.extra_current_minutes == int(kind in ("null_parent", "unknown"))
    assert saved[0].inputs.baseline_complete is saved[0].channels.allocation_known is False


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["websocket", "claim"])
async def test_external_cancellation_drains_client_without_erasing_concurrent_state(mining, gql, monkeypatch, transition):
    twitch, _, campaign = mining
    before, revision = snapshot(campaign), campaign.progress_revision
    entered, drained = asyncio.Event(), asyncio.Event()
    claimed = campaign.timed_drops["sibling"]
    claimed._claim = AsyncMock(return_value=True)

    async def on_read(_):
        if transition == "claim":
            assert await claimed.claim()
        else:
            await MessageHandlerService(twitch).process_drops(42, {"type": "drop-progress", "data": {
                "drop_id": "drop", "current_progress_min": 10, "required_progress_min": 60,
            }})
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    task = asyncio.create_task(run_client(twitch, gql, monkeypatch, (deepcopy(REPLIES["valid"]),), on_read=on_read))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert drained.is_set()
    assert campaign.progress_revision == revision + 1
    if transition == "claim":
        assert claimed.is_claimed and claimed.real_current_minutes == 60 and claimed.extra_current_minutes == 0
        assert snapshot(campaign)[0] == before[0]
        assert claimed.progress_observation == before[1][-1]
        twitch.drop_history.record.assert_called_once_with(claimed, campaign)
    else:
        assert [drop.real_current_minutes for drop in campaign.drops] == [10, 10]
        assert [drop.extra_current_minutes for drop in campaign.drops] == [0, 0]
        assert campaign.timed_drops["drop"].progress_observation["source"] == "websocket"
        assert claimed.progress_observation == before[1][-1]
        twitch.drop_history.record.assert_not_called()
    campaign.bump_minutes.assert_not_called()
    twitch.close.assert_not_called()
