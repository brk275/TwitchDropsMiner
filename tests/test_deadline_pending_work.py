"""Pending or transient asynchronous work invalidates experimental snapshots."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from src.config import State
from src.exceptions import GQLException
from src.services.deadline_context import DeadlineContextGuard
from src.services.inventory_service import InventoryService
from tests.test_deadline_channels import runtime as runtime
from tests.test_deadline_forecast import NOW
from tests.test_deadline_inputs import campaign as replacement_campaign
from tests.test_imported_session_retry import running_miner


@pytest.fixture
def state(runtime):
    twitch, campaign, _, guard, _ = runtime
    service = twitch._inventory_service = InventoryService(twitch)
    drop = campaign.timed_drops["reward"]
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    yield twitch, campaign, drop, guard, service
    service.cancel_refresh_retry()


def capture(state, channels):
    return state[3].capture(state[0], now=NOW, horizon=NOW + timedelta(days=3),
                            enabled=True, include_channels=channels)


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", [False, True])
@pytest.mark.parametrize("kind", ["claim", "timer"])
@pytest.mark.parametrize("operation", ["capture", "validate"])
async def test_pending_work_is_not_a_quiescent_checkpoint(state, channels, kind, operation):
    twitch, campaign, drop, guard, service = state
    before = capture(state, channels)
    assert before.reason == "captured"
    observation, revision = drop.progress_observation, campaign.progress_revision
    entered, task = asyncio.Event(), None

    async def pending(_operation):
        entered.set()
        await asyncio.Event().wait()

    try:
        if kind == "claim":
            twitch.gql_request = AsyncMock(side_effect=pending)
            task = asyncio.create_task(drop.claim())
            await asyncio.wait_for(entered.wait(), 2)
        else:
            service._defer_refresh()
        if operation == "capture":
            result = capture(state, channels)
            assert result.reason == "inactive" and result.bound is None
        else:
            assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "inactive"
        assert drop.progress_observation == observation and campaign.progress_revision == revision
        assert not drop.is_claimed
    finally:
        service.cancel_refresh_retry()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "superseded"
    assert capture(state, channels).reason == "captured"


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", [False, True])
@pytest.mark.parametrize("kind", ["claim", "timer"])
async def test_completed_transient_work_cannot_restore_an_older_binding(state, channels, kind):
    twitch, campaign, drop, guard, service = state
    before = capture(state, channels)
    assert before.reason == "captured"
    observation, revision = drop.progress_observation, campaign.progress_revision
    if kind == "claim":
        twitch.gql_request = AsyncMock(return_value={"data": None})
        assert await drop.claim() is False
    else:
        service._defer_refresh()
        service.cancel_refresh_retry()
    assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "superseded"
    assert drop.progress_observation == observation and campaign.progress_revision == revision
    assert not drop.is_claimed
    assert capture(state, channels).reason == "captured"


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", [False, True])
@pytest.mark.parametrize("change", ["service", "failed_read"])
async def test_inventory_service_identity_and_read_attempt_retire_bindings(state, channels, change):
    twitch, _, _, guard, service = state
    before = capture(state, channels)
    assert before.reason == "captured"
    if change == "service":
        twitch._inventory_service = InventoryService(twitch)
    else:
        twitch.gql_request = AsyncMock(side_effect=GQLException("test-only read failure"))
        with pytest.raises(GQLException):
            await service.fetch_inventory()
    assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "superseded"
    assert capture(state, channels).reason == "captured"


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", [False, True])
async def test_pending_retired_campaign_still_blocks_a_new_capture(state, channels):
    twitch, campaign, drop, guard, service = state
    before = capture(state, channels)
    entered = asyncio.Event()

    async def pending(_operation):
        entered.set()
        await asyncio.Event().wait()

    twitch.gql_request = AsyncMock(side_effect=pending)
    task = asyncio.create_task(drop.claim())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        twitch.inventory.clear()
        replacement = replacement_campaign(twitch, "replacement", "High", minutes=0)
        twitch._campaigns = {replacement.id: replacement}
        twitch._drops = dict(replacement.timed_drops)
        assert not replacement._pending_claims and campaign._pending_claims == {drop.id: 1}
        assert twitch._claim_requests == 1
        assert capture(state, channels).reason == "inactive"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        service.cancel_refresh_retry()
    assert twitch._claim_requests == 0 and not campaign._pending_claims
    assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "superseded"
    after = capture(state, channels)
    assert after.reason == "captured" and not after.bound.inputs.baseline_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", [False, True])
async def test_overlapping_result_awaits_release_only_after_the_last_claim(state, channels):
    twitch, campaign, drop, _, _ = state
    entered, releases, requests = asyncio.Event(), [asyncio.Event(), asyncio.Event()], []

    async def pending(_operation):
        index = len(requests)
        requests.append(True)
        if len(requests) == 2:
            entered.set()
        await releases[index].wait()
        return {"data": None}

    twitch.gql_request = AsyncMock(side_effect=pending)
    tasks = [asyncio.create_task(drop.claim()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert twitch._claim_requests == 2 and campaign._pending_claims == {drop.id: 2}
        releases[0].set()
        assert await tasks[0] is False
        assert twitch._claim_requests == 1 and capture(state, channels).reason == "inactive"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert twitch._claim_requests == 0 and not campaign._pending_claims
    assert capture(state, channels).reason == "captured"
    assert len(requests) == 2
    twitch.drop_history.record.assert_not_called()


@pytest.mark.parametrize("value", [None, True, -1, 1.0, "1", []])
@pytest.mark.parametrize("operation", ["capture", "validate"])
def test_unknown_claim_activity_is_rejected_without_coercion(state, value, operation):
    twitch, _, _, guard, _ = state
    before = capture(state, False)
    assert before.reason == "captured"
    twitch._claim_requests = value
    if operation == "capture":
        result = capture(state, False)
        assert result.reason == "invalid_context" and result.bound is None
    else:
        assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "invalid_context"
    twitch._claim_requests = 0
    assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "superseded"


@pytest.mark.parametrize("change", ["absent", "foreign_owner", "unknown_token"])
def test_inventory_service_requires_known_ownership_and_read_generation(state, change):
    twitch, _, _, guard, service = state
    before = capture(state, False)
    if change == "absent":
        twitch._inventory_service = None
    elif change == "foreign_owner":
        service._twitch = object()
    else:
        service._refresh_token = None
    assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "invalid_context"


@pytest.mark.asyncio
@pytest.mark.parametrize("channels", [False, True])
@pytest.mark.parametrize("kind", ["claim", "timer"])
async def test_input_projection_cannot_return_a_capture_after_transient_work(state, monkeypatch, channels, kind):
    twitch, campaign, drop, guard, service = state
    project = guard._adapter.capture
    observation, revision = drop.progress_observation, campaign.progress_revision

    def during_projection(*args, **kwargs):
        result = project(*args, **kwargs)
        if kind == "claim":
            with twitch.claim_request():
                pass
        else:
            service._defer_refresh()
            service.cancel_refresh_retry()
        return result

    monkeypatch.setattr(guard._adapter, "capture", during_projection)
    result = capture(state, channels)
    assert result.reason == "superseded" and result.bound is None
    assert twitch._claim_requests == 0 and drop.progress_observation == observation
    assert campaign.progress_revision == revision


@pytest.mark.asyncio
async def test_activity_registration_failure_unwinds_before_sending_a_claim(state, monkeypatch):
    twitch, campaign, drop, _, _ = state
    twitch.gql_request = AsyncMock()

    def fail():
        raise RuntimeError("test-only registration failure")

    monkeypatch.setattr(twitch, "invalidate_deadline_context", fail)
    with pytest.raises(RuntimeError, match="test-only registration failure"):
        await drop.claim()
    assert twitch._claim_requests == 0 and not campaign._pending_claims
    assert not drop.is_claimed and campaign.progress_revision == 0
    twitch.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unconfirmed", "confirmed", "cancel"])
async def test_actual_imported_claim_lifecycle_blocks_same_owners_context(state, tmp_path, monkeypatch, outcome):
    # The runtime fixture supplies fake clocks; the session/HTTP peer remain local.
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        twitch.settings.__dict__.update(deepcopy(vars(state[0].settings)))
        twitch.settings.games_to_watch = ["High"]
        twitch._state, twitch._inventory_loaded = State.CHANNEL_SWITCH, True
        twitch.gui._selected_channel_id = None
        twitch.gui.broadcast_wanted_items_now = AsyncMock()
        campaign = replacement_campaign(twitch, "one", "High", minutes=0)
        drop = campaign.timed_drops["reward"]
        drop._send_telegram_notification = drop._send_discord_notification = AsyncMock()
        twitch._campaigns, twitch._drops = {campaign.id: campaign}, dict(campaign.timed_drops)
        guard = DeadlineContextGuard(clock=lambda: 100.0)
        before = guard.capture(twitch, now=NOW, horizon=NOW + timedelta(days=3), enabled=True)
        assert before.reason == "captured"
        observation = drop.progress_observation
        peer.responses.append({"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
                              if outcome == "confirmed" else {"data": None})
        dispatch, entered, release = twitch.gql_request, asyncio.Event(), asyncio.Event()

        async def hold_result(operation):
            result = await dispatch(operation)
            assert not twitch._browser._lock.locked()
            entered.set()
            await release.wait()
            return result

        twitch.gql_request = hold_result
        task = asyncio.create_task(drop.claim())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert twitch._claim_requests == 1
            assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "inactive"
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert await asyncio.wait_for(task, 2) is (outcome == "confirmed")
            assert twitch._claim_requests == 0 and not campaign._pending_claims
            assert campaign.progress_revision == int(outcome == "confirmed")
            assert drop.progress_observation == observation
            assert drop.is_claimed is (outcome == "confirmed")
            assert guard.validate(before.bound, twitch, now=NOW, enabled=True) == "superseded"
            assert len(peer.calls) == 1 and not peer.responses
            assert not twitch._browser._lock.locked()
            assert twitch._browser.status()["state"] == "ready"
            assert twitch._browser.status()["generation"] == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
