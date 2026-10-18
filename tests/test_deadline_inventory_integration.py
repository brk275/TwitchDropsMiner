"""Inventory robustness must preserve observations and retire old planning inputs."""

import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import GQL_OPERATIONS
from src.exceptions import GQLException, MinerException
from src.models.channel import Channel
from src.services.deadline_certificate import (
    CertifiedDeadlineForecaster,
    DropTimingBounds,
    DurationBounds,
    TimingBounds,
)
from src.services.deadline_forecast import ViewingWindow
from src.services.inventory_service import InventoryService
from tests.test_deadline_channels import channel
from tests.test_deadline_channels import runtime as runtime
from tests.test_deadline_forecast import NOW
from tests.test_unavailable_campaign_details import campaign_data, metadata, response


def data(key, *, minutes=0, required=60, game_id=1, game="High", claimed=False):
    result = campaign_data(key, minutes=minutes)
    result["game"] = {"id": str(game_id), "name": game}
    result["startAt"] = (NOW - timedelta(days=1)).isoformat()
    result["endAt"] = (NOW + timedelta(days=2)).isoformat()
    drop = result["timeBasedDrops"][0]
    drop.update(startAt=result["startAt"], endAt=result["endAt"], requiredMinutesWatched=required)
    drop["self"]["isClaimed"] = claimed
    return result


@pytest.fixture
def combined(runtime):
    twitch, previous, current, guard, _ = runtime
    twitch._inventory_service = InventoryService(twitch)
    twitch.gui.inv.add_campaign = AsyncMock()
    twitch._maintenance_service = SimpleNamespace(run_maintenance_task=AsyncMock())
    twitch._mnt_triggers = deque([NOW + timedelta(hours=1)])
    maintenance = twitch._mnt_task = MagicMock()
    maintenance.done.return_value = False
    twitch.get_auth = AsyncMock(return_value=twitch._auth_state)
    return SimpleNamespace(twitch=twitch, previous=previous, current=current, guard=guard,
                           auth=twitch._auth_state, settings=twitch.settings, maintenance=maintenance)


def bind(state):
    result = state.guard.capture(state.twitch, now=NOW, horizon=NOW + timedelta(days=3),
                                 enabled=True, include_channels=True)
    assert result.reason == "captured" and result.bound is not None
    return result.bound


def replies(state, *, ongoing=(), available=(), details=()):
    state.twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": list(ongoing), "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": list(available)}}}, list(details),
    ])


async def refresh(state):
    try:
        await state.twitch.fetch_inventory()
    finally:
        if isinstance(state.twitch._mnt_task, asyncio.Task):
            await state.twitch._mnt_task


@pytest.mark.asyncio
@pytest.mark.parametrize("delay,reason", [(4, "no_safe_rescue"), (10, "unknown_progress")])
async def test_refresh_delay_does_not_renew_planning_freshness(combined, monkeypatch, delay, reason):
    state = combined
    current = [NOW]
    later = NOW + timedelta(minutes=delay)
    known = data("known", minutes=17)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].replace(tzinfo=None) if tz is None else current[0].astimezone(tz)

    monkeypatch.setattr("src.services.inventory_service.datetime", Clock)

    async def request(operations):
        if isinstance(operations, list):
            current[0] = later
            return [response(data("known", minutes=24))]
        if operations["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [known], "gameEventDrops": [],
            }}}}
        return {"data": {"currentUser": {"dropCampaigns": [metadata(known)]}}}

    state.twitch.gql_request = AsyncMock(side_effect=request)
    await refresh(state)
    result = state.guard.capture(state.twitch, now=later, horizon=NOW + timedelta(days=3), enabled=True)
    assert result.reason == "captured"
    inputs = result.bound.inputs
    projected, = inputs.drops
    assert projected.observed_at == NOW and projected.observed_minutes == 17
    zero = DurationBounds(timedelta(0), timedelta(0))
    remaining = DurationBounds(timedelta(minutes=43), timedelta(minutes=43))
    # Explicit hypothetical coverage tests freshness, not future Twitch availability.
    snapshot = inputs.to_forecast(claim_delays={projected.id: timedelta(0)}, viewing_windows=(
        ViewingWindow("test-channel", later, projected.ends_at, frozenset({projected.id}), allocation_known=True),
    ))
    decision = CertifiedDeadlineForecaster().certify(snapshot, TimingBounds((
        DropTimingBounds(projected.id, remaining, zero),
    ), zero), enabled=True, baseline_complete=True)
    assert decision.reason == reason and decision.certificate is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_details", [False, True])
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("minutes", [0, 17])
async def test_retained_progress_keeps_provenance_and_replaces_bound_model(
    combined, missing_details, claimed, minutes,
):
    state = combined
    before = bind(state)
    known = data("known", minutes=minutes, claimed=claimed)
    catalog = metadata(known)
    del known["status"]
    stale = data("known", minutes=0, claimed=not claimed)
    missing = data("missing", game_id=2, game="Urgent")
    sources = deepcopy((known, catalog, stale, missing))
    replies(state, ongoing=[known], available=[catalog, metadata(missing)],
            details=[response(None if missing_details else stale), response(None)])
    await refresh(state)
    twitch = state.twitch
    drop = twitch._drops["known-drop"]
    assert set(twitch._campaigns) == {"known"} and set(twitch._drops) == {"known-drop"}
    assert drop.real_current_minutes == (60 if claimed else minutes)
    assert drop.is_claimed is claimed and drop.claim_id == "claim-known"
    assert drop.progress_observation == {"minutes": minutes, "source": "inventory", "received_at": NOW.isoformat()}
    assert drop.extra_current_minutes == 0
    assert state.guard.validate(before, twitch, now=NOW, enabled=True) == "superseded"
    after = bind(state)
    assert after.inputs.baseline_complete is after.channels.baseline_complete is False
    assert after.channels.allocation_known is False
    assert [item.observed_minutes for item in after.inputs.drops] == ([] if claimed else [minutes])
    assert twitch.inventory[0] is not state.previous
    assert twitch._auth_state is state.auth and twitch.settings is state.settings
    assert (known, catalog, stale, missing) == sources


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [None, True, -1, "60"])
async def test_bad_sibling_keeps_old_observation_but_cannot_reuse_pre_refresh_binding(combined, required):
    state = combined
    before = bind(state)
    previous = state.previous.timed_drops["reward"]
    observation = previous.progress_observation
    containers = (state.twitch.inventory, state.twitch._campaigns, state.twitch._drops, state.twitch._mnt_triggers)
    valid, invalid, missing = data("valid", minutes=17), data("invalid", required=required), data("missing")
    sources = deepcopy((valid, invalid, missing))
    replies(state, available=[metadata(item) for item in (valid, invalid, missing)],
            details=[response(valid), response(invalid), response(None)])
    with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
        await refresh(state)
    assert all(actual is expected for actual, expected in zip(
        (state.twitch.inventory, state.twitch._campaigns, state.twitch._drops, state.twitch._mnt_triggers), containers, strict=True,
    ))
    assert state.twitch.inventory == [state.previous]
    assert state.twitch._drops == {"reward": previous}
    assert state.twitch._mnt_triggers == deque([NOW + timedelta(hours=1)])
    assert previous.progress_observation == observation and previous.extra_current_minutes == 0
    state.twitch.gui.inv.clear.assert_not_called()
    state.twitch.gui.inv.add_campaign.assert_not_awaited()
    state.maintenance.cancel.assert_not_called()
    assert state.twitch._mnt_task is state.maintenance
    assert state.twitch._auth_state is state.auth and state.twitch.settings is state.settings
    assert state.guard.validate(before, state.twitch, now=NOW, enabled=True) == "superseded"
    assert bind(state).inputs.drops[0].observed_minutes == observation["minutes"]
    assert (valid, invalid, missing) == sources


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_refresh_then_local_guess_does_not_turn_special_campaign_or_estimates_into_evidence(
    combined, monkeypatch, reported,
):
    state = combined
    high = data("high")
    special = data("special", required=1, game_id=509663, game="Special Events")
    special["allow"]["channels"] = [{"id": state.current.id, "name": state.current.login}]
    missing = data("missing")
    replies(state, available=[metadata(item) for item in (high, special, missing)],
            details=[response(high), response(special), response(None)])
    await refresh(state)
    before = bind(state)
    twitch = state.twitch
    wanted, unwanted = twitch._drops["high-drop"], twitch._drops["special-drop"]
    assert unwanted.can_earn(state.current)
    assert twitch.get_active_campaign(state.current) is wanted.campaign
    observations = wanted.progress_observation, unwanted.progress_observation
    twitch.gql_request = AsyncMock(return_value={"data": {"currentUser": {"dropCurrentSession": {
        "dropID": unwanted.id, "currentMinutesWatched": 1,
    } if reported else None}}})
    await one_poll(state, monkeypatch)
    assert wanted.real_current_minutes == 0 and wanted.extra_current_minutes == (0 if reported else 1)
    assert unwanted.real_current_minutes == (1 if reported else 0) and unwanted.extra_current_minutes == 0
    assert wanted.progress_observation == observations[0]
    assert unwanted.progress_observation == ({"minutes": 1, "source": "current_drop", "received_at": NOW.isoformat()}
                                             if reported else observations[1])
    assert state.guard.validate(before, twitch, now=NOW, enabled=True) == "superseded"
    assert [item.observed_minutes for item in bind(state).inputs.drops] == [0]


async def one_poll(state, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(Channel, "send_watch", AsyncMock(side_effect=[True, asyncio.CancelledError()]))

    async def advance(delay):
        clock[0] = 20.0

    monkeypatch.setattr(state.twitch._watch_service, "watch_sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(state.twitch._watch_service.watch_loop(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout", "cancel"])
async def test_pending_poll_cannot_overwrite_models_replaced_during_null_detail_refresh(combined, monkeypatch, reply):
    state = combined
    before = bind(state)
    previous = state.previous.timed_drops["reward"]
    known, missing = data("one", minutes=17), data("missing")
    known["timeBasedDrops"][0]["id"] = previous.id
    calls = []

    async def request(operations):
        if isinstance(operations, list):
            return [response(None) for _ in operations]
        name = operations["operationName"]
        calls.append(name)
        if name == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [known], "gameEventDrops": []}}}}
        if name == GQL_OPERATIONS["Campaigns"]["operationName"]:
            return {"data": {"currentUser": {"dropCampaigns": [metadata(known), metadata(missing)]}}}
        assert name == GQL_OPERATIONS["CurrentDrop"]["operationName"]
        await refresh(state)
        if reply == "error":
            raise GQLException("Test query failure")
        if reply == "timeout":
            raise TimeoutError
        if reply == "cancel":
            raise asyncio.CancelledError
        return {"data": {"currentUser": {"dropCurrentSession": {
            "dropID": previous.id, "currentMinutesWatched": 9,
        } if reply == "session" else None}}}

    state.twitch.gql_request = AsyncMock(side_effect=request)
    await one_poll(state, monkeypatch)
    installed = state.twitch._drops[previous.id]
    assert installed is not previous and installed.real_current_minutes == 17
    assert installed.extra_current_minutes == previous.extra_current_minutes == 0
    assert installed.progress_observation == {"minutes": 17, "source": "inventory", "received_at": NOW.isoformat()}
    assert previous.observed_minutes == 0
    assert set(state.twitch._campaigns) == {"one"}
    assert calls == [GQL_OPERATIONS[key]["operationName"] for key in ("CurrentDrop", "Inventory", "Campaigns")]
    assert state.guard.validate(before, state.twitch, now=NOW, enabled=True) == "superseded"
    assert bind(state).inputs.drops[0].observed_minutes == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_failed_refresh_does_not_suppress_a_current_poll_for_unchanged_models(combined, monkeypatch, reported):
    state = combined
    before = bind(state)
    previous = state.previous.timed_drops["reward"]
    invalid = data("invalid", required=None)

    async def request(operations):
        if isinstance(operations, list):
            return [response(invalid), response(None)]
        name = operations["operationName"]
        if name == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}}
        if name == GQL_OPERATIONS["Campaigns"]["operationName"]:
            return {"data": {"currentUser": {"dropCampaigns": [metadata(invalid), metadata(data("missing"))]}}}
        assert name == GQL_OPERATIONS["CurrentDrop"]["operationName"]
        # The test caller handles this failure explicitly. Production refresh
        # exception/shutdown behavior is not changed into automatic recovery.
        with pytest.raises(MinerException, match="^Invalid drop watch requirement$"):
            await refresh(state)
        return {"data": {"currentUser": {"dropCurrentSession": {
            "dropID": previous.id, "currentMinutesWatched": 9,
        } if reported else None}}}

    state.twitch.gql_request = AsyncMock(side_effect=request)
    await one_poll(state, monkeypatch)
    assert state.twitch._drops[previous.id] is previous
    assert previous.real_current_minutes == (9 if reported else 0)
    assert previous.extra_current_minutes == (0 if reported else 1)
    assert previous.observed_minutes == (9 if reported else 0)
    assert previous.progress_observation["source"] == ("current_drop" if reported else "inventory")
    assert state.guard.validate(before, state.twitch, now=NOW, enabled=True) == "superseded"
    assert bind(state).inputs.drops[0].observed_minutes == (9 if reported else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_id", [False, True])
async def test_explicit_lookup_after_refresh_uses_its_own_channel_and_direct_observation(combined, same_id):
    state = combined
    high, urgent, missing = data("high", minutes=17), data("urgent", minutes=9, game_id=2, game="Urgent"), data("missing")
    replies(state, available=[metadata(item) for item in (high, urgent, missing)],
            details=[response(high), response(urgent), response(None)])
    await refresh(state)
    high_campaign, urgent_campaign = state.twitch._campaigns["high"], state.twitch._campaigns["urgent"]
    state.twitch.wanted_games = [high_campaign.game, urgent_campaign.game]
    requested = channel(state.twitch, state.current.id if same_id else 20, urgent_campaign.game)
    assert state.twitch.get_active_campaign(requested) is urgent_campaign
    assert state.twitch.get_active_campaign() is high_campaign
    assert state.twitch.watching_channel.get_with_default(None) is state.current
    assert requested is not state.current
    assert state.twitch._drops["urgent-drop"].observed_minutes == 9
    assert state.twitch._drops["high-drop"].observed_minutes == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
async def test_zero_requirement_survives_refresh_without_becoming_watch_work(combined, claimed):
    state = combined
    subscription, high, missing = data("subscription", required=0, claimed=claimed), data("high"), data("missing")
    replies(state, available=[metadata(item) for item in (subscription, high, missing)],
            details=[response(subscription), response(high), response(None)])
    await refresh(state)
    drop = state.twitch._drops["subscription-drop"]
    assert drop.required_minutes == 0 and not drop.is_watch_drop and drop.is_claimed is claimed
    assert drop.observed_minutes == 0 and drop.progress_observation["source"] == "inventory"
    assert len(bind(state).inputs.drops) == 1
    assert state.twitch.get_active_campaign(state.current).id == "high"
