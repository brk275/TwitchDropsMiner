"""Known campaign policy boundaries must not wait for a periodic inventory read."""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.inventory_service import InventoryService
from src.services.maintenance import MaintenanceService
from src.services.stream_selector import StreamSelector
from tests.test_priority_fallback import _channel, _drop
from tests.test_unavailable_campaign_details import campaign_data, client, metadata, response


BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    point = [BASE]

    class Clock:
        @staticmethod
        def now(tz=None):
            return point[0].astimezone(tz) if tz else point[0].replace(tzinfo=None)

    for module in ("src.models.campaign", "src.models.drop", "src.services.inventory_service",
                   "src.services.stream_selector", "src.services.maintenance"):
        monkeypatch.setattr(module + ".datetime", Clock)
    return point


async def inventory(*, interval=1440, start=90, drop_only=False, prerequisite_end=None,
                    policy=None):
    data = campaign_data("future")
    data["startAt"] = (BASE - timedelta(days=1) if drop_only else
                       BASE + timedelta(minutes=start)).isoformat()
    data["endAt"] = (BASE + timedelta(minutes=start + 60)).isoformat()
    data["status"] = "ACTIVE" if drop_only else "UPCOMING"
    data["timeBasedDrops"] = [_drop(
        "wanted", starts_at=(BASE + timedelta(minutes=start)).isoformat(),
        ends_at=(BASE + timedelta(minutes=start + 60)).isoformat(),
        precondition_drop_ids=["preparation"] if prerequisite_end else [],
    )]
    if prerequisite_end:
        data["timeBasedDrops"].insert(0, _drop(
            "preparation", benefit_type="BADGE", required_minutes=5,
            starts_at=(BASE - timedelta(minutes=1)).isoformat(),
            ends_at=(BASE + timedelta(minutes=prerequisite_end)).isoformat(),
        ))
    if policy == "unlinked":
        data["self"]["isAccountConnected"] = False
    owner = client(available=[metadata(data)], details=[response(data)])
    owner.settings.minimum_refresh_interval_minutes = interval
    owner.settings.priority_list_only = True
    if policy == "ignored":
        owner.settings.drop_name_blacklist = ["wanted"]
    elif policy == "disabled":
        owner.settings.mining_benefits = {"DIRECT_ENTITLEMENT": False}
    elif policy == "unlisted":
        owner.settings.games_to_watch = []
    await InventoryService(owner).fetch_inventory()
    await owner._mnt_task
    return owner


async def first_delay(owner, monkeypatch):
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("src.services.maintenance.asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await MaintenanceService(owner).run_maintenance_task()
    return sleep.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [120, 1440])
@pytest.mark.parametrize("drop_only", [False, True])
async def test_known_future_campaign_or_drop_wakes_at_horizon_entry(clock, monkeypatch, interval, drop_only):
    owner = await inventory(interval=interval, drop_only=drop_only)
    assert StreamSelector().get_wanted_games(owner.settings, owner.inventory) == []
    delay = await first_delay(owner, monkeypatch)
    assert delay == 30 * 60 + timedelta.resolution.total_seconds()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,start,preparation_end,snapshot", [
    (1440, 90, 60, 0), (30, 100, 50, 30),
])
async def test_prerequisite_admission_precedes_its_expiry(
    clock, monkeypatch, interval, start, preparation_end, snapshot,
):
    clock[0] = BASE + timedelta(minutes=snapshot)
    owner = await inventory(interval=interval, start=start, drop_only=True,
                            prerequisite_end=preparation_end)
    assert StreamSelector().get_wanted_games(owner.settings, owner.inventory) == []
    assert await first_delay(owner, monkeypatch) == (
        (start - 60 - snapshot) * 60 + timedelta.resolution.total_seconds()
    )
    clock[0] = BASE + timedelta(minutes=start - 60 + 1)
    campaign = owner.inventory[0]
    channel = _channel(owner, 100, {"id": "1", "name": "Wanted"})
    assert campaign.can_mine(channel)
    assert campaign.first_mining_drop(channel).id == "preparation"
    assert not campaign.timed_drops["wanted"].can_earn(channel)
    assert all(not drop.is_claimed for drop in campaign.drops)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["ignored", "unlinked", "disabled", "unlisted"])
async def test_future_timing_survives_mutable_policy_exclusion(clock, monkeypatch, policy):
    owner = await inventory(policy=policy)
    assert StreamSelector().get_wanted_games(owner.settings, owner.inventory) == []
    assert await first_delay(owner, monkeypatch) == 30 * 60 + timedelta.resolution.total_seconds()
    owner.settings.drop_name_blacklist = []
    owner.settings.allow_unlinked_campaigns = True
    owner.settings.mining_benefits = {"DIRECT_ENTITLEMENT": True}
    owner.settings.games_to_watch = ["Wanted"]
    clock[0] = BASE + timedelta(minutes=31)
    assert [game.name for game in StreamSelector().get_wanted_games(
        owner.settings, owner.inventory,
    )] == ["Wanted"]


@pytest.mark.asyncio
async def test_inventory_keeps_sorted_unique_future_timing(clock):
    owner = await inventory()
    triggers = list(owner._mnt_triggers)
    assert triggers == sorted(set(triggers))
    assert all(trigger > BASE for trigger in triggers)
    assert BASE + timedelta(minutes=90) in triggers
    assert BASE + timedelta(minutes=150) in triggers


@pytest.mark.asyncio
async def test_model_keeps_raw_times_and_handles_extreme_start(clock):
    owner = await inventory()
    campaign = owner.inventory[0]
    raw = campaign.time_triggers
    assert raw <= campaign.mining_time_triggers
    entry = BASE + timedelta(minutes=30, microseconds=1)
    assert entry in campaign.mining_time_triggers and entry not in raw
    campaign.starts_at = datetime.min.replace(tzinfo=timezone.utc)
    campaign.timed_drops["wanted"].starts_at = campaign.starts_at
    assert campaign.time_triggers <= campaign.mining_time_triggers


@pytest.mark.asyncio
async def test_local_wakeups_leave_periodic_deadline_and_next_cycle_setting(clock, monkeypatch):
    owner = MagicMock()
    owner.settings.minimum_refresh_interval_minutes = 120
    owner._mnt_triggers = deque([
        BASE + timedelta(minutes=30, microseconds=1), BASE + timedelta(minutes=90),
    ])
    delays = []

    async def advance(delay):
        delays.append(delay)
        clock[0] += timedelta(seconds=delay)
        owner.settings.minimum_refresh_interval_minutes = 1

    monkeypatch.setattr("src.services.maintenance.asyncio", SimpleNamespace(sleep=advance))
    await MaintenanceService(owner).run_maintenance_task()
    assert delays == [1800.000001, 3599.999999, 1800]
    assert owner.request_games_update.call_count == 2
    owner.change_state.assert_not_called()
    owner.request_inventory_refresh.assert_called_once_with()
    delays.clear()
    await MaintenanceService(owner).run_maintenance_task()
    assert delays == [60]
    assert owner.request_inventory_refresh.call_count == 2


@pytest.mark.asyncio
async def test_overdue_boundaries_are_coalesced_after_a_delayed_wakeup(clock, monkeypatch):
    owner = MagicMock()
    owner.settings.minimum_refresh_interval_minutes = 120
    owner._mnt_triggers = deque(BASE + timedelta(minutes=minute) for minute in (30, 40, 50, 90))
    delays = []

    async def advance(delay):
        delays.append(delay)
        clock[0] = BASE + timedelta(minutes=100 if len(delays) == 1 else 120)

    monkeypatch.setattr("src.services.maintenance.asyncio", SimpleNamespace(sleep=advance))
    await MaintenanceService(owner).run_maintenance_task()
    assert delays == [1800, 1200]
    owner.request_games_update.assert_called_once_with()
    owner.request_inventory_refresh.assert_called_once_with()


@pytest.mark.asyncio
async def test_periodic_deadline_tie_requests_only_inventory(clock, monkeypatch):
    owner = MagicMock()
    owner.settings.minimum_refresh_interval_minutes = 30
    owner._mnt_triggers = deque([BASE + timedelta(minutes=30)])

    async def advance(delay):
        clock[0] += timedelta(seconds=delay)

    monkeypatch.setattr("src.services.maintenance.asyncio", SimpleNamespace(sleep=advance))
    await MaintenanceService(owner).run_maintenance_task()
    owner.request_games_update.assert_not_called()
    owner.request_inventory_refresh.assert_called_once_with()


@pytest.mark.asyncio
async def test_cancelled_maintenance_cannot_enqueue_timing_work(clock, monkeypatch):
    owner = MagicMock()
    owner.settings.minimum_refresh_interval_minutes = 1440
    owner._mnt_triggers = deque([BASE + timedelta(minutes=30)])
    sleeping = asyncio.Event()

    async def sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("src.services.maintenance.asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(MaintenanceService(owner).run_maintenance_task())
    await asyncio.wait_for(sleeping.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    owner.request_games_update.assert_not_called()
    owner.request_inventory_refresh.assert_not_called()
