"""Chain subtotals must stay fresh through real progress and confirmed claims."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.drop_history import DropHistory
from tests.test_drop_ignore_policy import _campaign, _drop


def shared_campaign(tmp_path):
    campaign = _campaign([
        _drop("parent", "Parent"),
        _drop("left", "Left", preconditions=("parent",)),
        _drop("right", "Right", preconditions=("parent",)),
        _drop("target", "Target", preconditions=("left", "right")),
    ])
    twitch = campaign._twitch
    twitch.drop_history = DropHistory(tmp_path)
    twitch.gql_request = AsyncMock(return_value={
        "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}},
    })
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    for drop in campaign.drops:
        drop.update_claim("claim-" + drop.id)
        drop._send_telegram_notification = AsyncMock()
        drop._send_discord_notification = AsyncMock()
    return campaign


@pytest.mark.asyncio
async def test_real_progress_estimates_and_claims_recompute_shared_totals(tmp_path):
    campaign = shared_campaign(tmp_path)
    parent, left, right, target = campaign.drops
    assert target.total_required_minutes == 45
    assert target.total_remaining_minutes == 45

    revision = campaign.progress_revision
    parent.update_minutes(7)
    assert campaign.progress_revision == revision + 1
    assert target.total_remaining_minutes == 38
    parent._bump_minutes(None)
    assert target.total_remaining_minutes == 37

    assert await parent.claim()
    assert target.total_remaining_minutes == 30
    assert left.can_earn() and right.can_earn()
    left.update_minutes(5)
    assert left.real_current_minutes == right.real_current_minutes == 5
    assert target.total_remaining_minutes == 25
    assert await left.claim()
    assert target.total_remaining_minutes == 25
    assert await right.claim()
    assert target.total_remaining_minutes == 15
    assert target.total_required_minutes == 45
    assert target.can_earn()
    assert campaign._twitch.gql_request.await_count == 3
    assert {entry["id"] for entry in DropHistory(tmp_path).get_entries()} == {
        "parent", "left", "right",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
async def test_cancelled_claim_delivery_does_not_reuse_preclaim_subtotals(tmp_path, boundary):
    campaign = shared_campaign(tmp_path)
    parent = campaign.timed_drops["parent"]
    target = campaign.timed_drops["target"]
    assert target.total_remaining_minutes == 45
    entered = asyncio.Event()

    async def pending_delivery():
        assert target.total_remaining_minutes == 30
        assert DropHistory(tmp_path).total_count == 1
        entered.set()
        await asyncio.Event().wait()

    delivery = {
        "dashboard": campaign._twitch.gui.broadcast_wanted_items_now,
        "telegram": parent._send_telegram_notification,
        "discord": parent._send_discord_notification,
    }
    delivery[boundary].side_effect = pending_delivery
    task = asyncio.create_task(parent.claim())
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert target.total_remaining_minutes == 30
    assert target.total_required_minutes == 45
    assert await parent.claim()
    campaign._twitch.gql_request.assert_awaited_once()
    assert [entry["id"] for entry in DropHistory(tmp_path).get_entries()] == ["parent"]
