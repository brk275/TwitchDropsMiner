"""Claim persistence, stale polling and direct observations must agree."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.operations import GQL_OPERATIONS
from src.drop_history import DropHistory
from src.web.managers.inventory import InventoryManager
from tests.test_claim_grace import END, at
from tests.test_drop_ignore_policy import _campaign, _drop
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


async def _inventory(twitch, campaign, tmp_path):
    twitch.drop_history = DropHistory(tmp_path)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    inventory = InventoryManager(SimpleNamespace(emit=AsyncMock()), MagicMock())
    twitch.gui.inv = inventory
    await inventory.add_campaign(campaign)
    return inventory


def _block_notification(drop, boundary, entered):
    async def block():
        entered.set()
        await asyncio.Event().wait()

    if boundary == "dashboard":
        drop._twitch.gui.broadcast_wanted_items_now = AsyncMock(side_effect=block)
    else:
        setattr(drop, f"_send_{boundary}_notification", AsyncMock(side_effect=block))


def _assert_claim_snapshot(drop, inventory, tmp_path, original_observation):
    assert drop.is_claimed
    assert drop.real_current_minutes == drop.required_minutes
    assert drop.extra_current_minutes == 0
    assert drop.progress_observation == original_observation
    entries = DropHistory(tmp_path).get_entries()
    assert [entry["id"] for entry in entries] == [drop.id]
    drop._twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)
    cached = next(
        item for item in inventory.get_campaigns()[0]["drops"] if item["id"] == drop.id
    )
    assert cached["is_claimed"] is True
    assert cached["current_minutes"] == drop.required_minutes
    assert cached["can_claim"] is False
    assert cached["progress_observation"] == original_observation


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_id", ["drop", "sibling"])
@pytest.mark.parametrize("response", ["session", "null", "error", "timeout"])
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
async def test_cancelled_claim_delivery_keeps_pending_poll_superseded(
    mining_state, tmp_path: Path, claim_id: str, response: str, boundary: str,
):
    twitch, channel, campaign = mining_state
    claimed = campaign.timed_drops[claim_id]
    other = next(drop for drop in campaign.drops if drop is not claimed)
    campaign.timed_drops["drop"].update_minutes(11, source="websocket")
    observations = {drop.id: drop.progress_observation for drop in campaign.drops}
    revision = campaign.progress_revision
    inventory = await _inventory(twitch, campaign, tmp_path)
    entered = asyncio.Event()
    _block_notification(claimed, boundary, entered)
    tasks = []

    async def confirm_during_poll(request):
        # The CurrentDrop request is already waiting; route the concurrent real claim
        # through its own mocked GraphQL response rather than bypassing claim logic.
        current_request = twitch.gql_request

        async def dispatch(operation):
            if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
                return {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
            return await current_request(operation)

        twitch.gql_request = dispatch
        tasks.append(asyncio.create_task(claimed.claim()))
        await asyncio.wait_for(entered.wait(), timeout=2)

    try:
        assert await poll(twitch, confirm_during_poll, returned_minutes=9, response=response) == [20]
        assert not tasks[0].done()
        assert campaign.progress_revision == revision + 1
        _assert_claim_snapshot(claimed, inventory, tmp_path, observations[claimed.id])
        assert other.real_current_minutes == 11
        assert other.extra_current_minutes == 0
        assert other.progress_observation == observations[other.id]
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    _assert_claim_snapshot(claimed, inventory, tmp_path, observations[claimed.id])
    assert await claimed.claim() is True
    assert campaign.progress_revision == revision + 1
    twitch.request_games_update.assert_called_once_with()
    _assert_claim_snapshot(claimed, inventory, tmp_path, observations[claimed.id])


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [3, 10])
@pytest.mark.parametrize("status", ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"])
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
async def test_retained_claim_cancellation_preserves_history_not_fake_observations(
    tmp_path: Path, days: int, status: str, boundary: str,
):
    with at(END + timedelta(days=days)):
        data = _drop("retained", "Retained", required_minutes=30)
        data["self"] = {
            "dropInstanceID": "claim-retained", "isClaimed": False,
            "currentMinutesWatched": 10,
        }
        campaign = _campaign([data])
        campaign.starts_at = END - timedelta(days=7)
        campaign.ends_at = END
        drop = campaign.timed_drops["retained"]
        drop.ends_at = END - timedelta(days=1)
        drop.extra_current_minutes = 2
        twitch = campaign._twitch
        twitch.settings.telegram_bot_token = ""
        twitch.settings.telegram_chat_id = ""
        twitch.settings.discord_webhook_url = ""
        twitch.gql_request = AsyncMock(return_value={
            "data": {"claimDropRewards": {"status": status}},
        })
        twitch.gui.broadcast_wanted_items_now = AsyncMock()
        inventory = await _inventory(twitch, campaign, tmp_path)
        observation = drop.progress_observation
        revision = campaign.progress_revision
        entered = asyncio.Event()
        _block_notification(drop, boundary, entered)
        task = asyncio.create_task(drop.claim())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert campaign.expired and not campaign.can_earn()
            assert drop.observed_minutes == 10
            assert campaign.progress_revision == revision + 1
            _assert_claim_snapshot(drop, inventory, tmp_path, observation)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await drop.claim() is True
        assert drop.observed_minutes == 10
        _assert_claim_snapshot(drop, inventory, tmp_path, observation)
        assert campaign.progress_revision == revision + 1
        twitch.request_games_update.assert_called_once_with()
        twitch.gql_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_poll_after_cancelled_claim_can_still_reconcile_sibling(
    mining_state, tmp_path: Path,
):
    twitch, channel, campaign = mining_state
    target, claimed = campaign.drops
    inventory = await _inventory(twitch, campaign, tmp_path)
    original = claimed.progress_observation
    claimed._twitch.gql_request = AsyncMock(return_value={
        "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}},
    })
    entered = asyncio.Event()
    _block_notification(claimed, "dashboard", entered)
    task = asyncio.create_task(claimed.claim())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def no_change(request):
        await asyncio.sleep(0)

    assert await poll(twitch, no_change, returned_minutes=9) == [20]
    assert target.observed_minutes == target.real_current_minutes == 9
    assert target.progress_observation["source"] == "current_drop"
    _assert_claim_snapshot(claimed, inventory, tmp_path, original)
    twitch.request_games_update.assert_called_once_with()
