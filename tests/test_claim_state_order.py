"""Confirmed claims must be committed before optional asynchronous notifications."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.drop_history import DropHistory
from src.models.drop import BaseDrop, TimedDrop
from tests.test_drop_ignore_policy import _campaign, _drop


@pytest.fixture
def claimable_drop(tmp_path: Path) -> TimedDrop:
    campaign = _campaign([_drop("reward", "Reward", required_minutes=30)])
    drop = campaign.timed_drops["reward"]
    drop.update_claim("claim-reward")
    drop.real_current_minutes = 12
    drop.extra_current_minutes = 5
    twitch = campaign._twitch
    twitch.drop_history = DropHistory(tmp_path)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    twitch.gql_request = AsyncMock(return_value={
        "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}},
    })
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    return drop


def _notification(drop: TimedDrop, boundary: str) -> AsyncMock:
    if boundary == "dashboard":
        return drop._twitch.gui.broadcast_wanted_items_now
    return getattr(drop, f"_send_{boundary}_notification")


def _assert_committed(drop: TimedDrop, tmp_path: Path) -> None:
    assert drop.is_claimed is True
    assert drop.real_current_minutes == drop.required_minutes
    assert drop.extra_current_minutes == 0
    assert drop.remaining_minutes == 0
    assert drop.campaign.claimed_drops == 1
    entries = DropHistory(tmp_path).get_entries()
    assert [entry["id"] for entry in entries] == [drop.id]
    assert entries[0]["campaign_id"] == drop.campaign.id
    drop._twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)
    drop._twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
async def test_cancelled_notification_preserves_claim_before_retry(
    claimable_drop: TimedDrop, tmp_path: Path, boundary: str,
):
    drop = claimable_drop
    entered = asyncio.Event()
    committed_at_notification: list[tuple[bool, int, int, int]] = []

    async def block_notification():
        committed_at_notification.append((
            drop.is_claimed, drop.real_current_minutes, drop.extra_current_minutes,
            DropHistory(tmp_path).total_count,
        ))
        entered.set()
        await asyncio.Event().wait()

    _notification(drop, boundary).side_effect = block_notification
    task = asyncio.create_task(drop.claim())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert committed_at_notification == [(True, 30, 0, 1)]
    _assert_committed(drop, tmp_path)
    drop._twitch.gui.inv.update_drop.assert_called_once_with(drop)
    notification_counts = [
        _notification(drop, name).await_count
        for name in ("dashboard", "telegram", "discord")
    ]

    assert await drop.claim() is True
    _assert_committed(drop, tmp_path)
    assert [
        _notification(drop, name).await_count
        for name in ("dashboard", "telegram", "discord")
    ] == notification_counts
    drop._twitch.gql_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
async def test_notification_exception_does_not_discard_confirmed_state(
    claimable_drop: TimedDrop, tmp_path: Path, boundary: str,
):
    drop = claimable_drop
    _notification(drop, boundary).side_effect = RuntimeError("notification interrupted")

    with pytest.raises(RuntimeError, match="notification interrupted"):
        await drop.claim()

    _assert_committed(drop, tmp_path)
    drop._twitch.gui.inv.update_drop.assert_called_once_with(drop)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"])
async def test_first_confirmation_is_committed_before_inventory_publication(
    claimable_drop: TimedDrop, tmp_path: Path, status: str,
):
    drop = claimable_drop
    drop._twitch.gql_request.return_value = {
        "data": {"claimDropRewards": {"status": status}},
    }
    drop._twitch.gui.inv.update_drop.side_effect = lambda changed: _assert_committed(
        changed, tmp_path,
    )

    assert await drop.claim() is True
    assert await drop.claim() is True
    _assert_committed(drop, tmp_path)
    drop._twitch.gui.inv.update_drop.assert_any_call(drop)
    drop._twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    drop._send_telegram_notification.assert_awaited_once_with()
    drop._send_discord_notification.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_overlapping_confirmations_record_only_one_transition(
    claimable_drop: TimedDrop, tmp_path: Path,
):
    drop = claimable_drop
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def confirm(_operation):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        return {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}

    drop._twitch.gql_request.side_effect = confirm
    tasks = [asyncio.create_task(drop.claim()) for _ in range(2)]
    try:
        await asyncio.wait_for(both_started.wait(), timeout=2)
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=2) == [True, True]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    _assert_committed(drop, tmp_path)
    drop._twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    drop._send_telegram_notification.assert_awaited_once_with()
    drop._send_discord_notification.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancellation_before_twitch_confirmation_does_not_fabricate_claim(
    claimable_drop: TimedDrop, tmp_path: Path,
):
    drop = claimable_drop
    entered = asyncio.Event()

    async def pending_confirmation(_operation):
        entered.set()
        await asyncio.Event().wait()

    drop._twitch.gql_request.side_effect = pending_confirmation
    task = asyncio.create_task(drop.claim())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert drop.is_claimed is False
    assert (drop.real_current_minutes, drop.extra_current_minutes) == (12, 5)
    assert DropHistory(tmp_path).total_count == 0
    drop._twitch.request_games_update.assert_not_called()
    drop._twitch.gui.inv.update_drop.assert_not_called()
    for name in ("dashboard", "telegram", "discord"):
        _notification(drop, name).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"data": {"claimDropRewards": {"status": "NOT_ELIGIBLE"}}},
    {"data": {"claimDropRewards": None}},
    {"data": {"errors": [{"message": "not claimable"}]}},
])
async def test_rejected_claim_preserves_progress_without_history_or_alerts(
    claimable_drop: TimedDrop, tmp_path: Path, response: dict,
):
    drop = claimable_drop
    drop._twitch.gql_request.return_value = response

    assert await drop.claim() is False

    assert drop.is_claimed is False
    assert (drop.real_current_minutes, drop.extra_current_minutes) == (12, 5)
    assert DropHistory(tmp_path).total_count == 0
    drop._twitch.request_games_update.assert_not_called()
    drop._twitch.gui.inv.update_drop.assert_called_once_with(drop)
    for name in ("dashboard", "telegram", "discord"):
        _notification(drop, name).assert_not_awaited()


@pytest.mark.asyncio
async def test_claimed_inventory_does_not_create_new_history_or_notifications(
    claimable_drop: TimedDrop, tmp_path: Path,
):
    drop = claimable_drop
    drop.is_claimed = True

    assert await drop.claim() is True

    assert (drop.real_current_minutes, drop.extra_current_minutes) == (30, 0)
    assert DropHistory(tmp_path).total_count == 0
    drop._twitch.drop_history.record.assert_not_called()
    drop._twitch.request_games_update.assert_not_called()
    drop._twitch.gql_request.assert_not_awaited()
    drop._twitch.gui.inv.update_drop.assert_called_once_with(drop)
    for name in ("dashboard", "telegram", "discord"):
        _notification(drop, name).assert_not_awaited()


@pytest.mark.asyncio
async def test_base_drop_claim_remains_independent_of_timed_progress(
    claimable_drop: TimedDrop,
):
    campaign = claimable_drop.campaign
    drop = BaseDrop(campaign, _drop("base", "Base Reward"), {})
    drop.update_claim("claim-base")

    assert await drop.claim() is True
    assert await drop.claim() is True

    assert drop.is_claimed is True
    campaign._twitch.request_games_update.assert_called_once_with()
    campaign._twitch.drop_history.record.assert_not_called()
    campaign._twitch.gui.inv.update_drop.assert_not_called()
    campaign._twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
