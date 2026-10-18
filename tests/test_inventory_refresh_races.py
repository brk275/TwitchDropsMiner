"""A pending inventory response must not erase a newer live observation or claim."""

import asyncio
from collections import OrderedDict, deque
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import GQL_OPERATIONS, State
from src.core.client import Twitch
from src.drop_history import DropHistory
from src.exceptions import ExitRequest, GQLException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from tests.test_deadline_inputs import client as runtime_client
from tests.test_imported_session_retry import running_miner
from tests.test_unavailable_campaign_details import campaign_data, client, metadata, response


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["websocket", "current_drop", "claim", "unchanged"])
async def test_refresh_cannot_overwrite_a_transition_received_after_its_inventory_read(change):
    known = campaign_data("known", minutes=10)
    twitch = client()
    previous = DropsCampaign(twitch, deepcopy(known), {})
    drop = previous.timed_drops["known-drop"]
    twitch.inventory.append(previous)
    twitch._campaigns[previous.id] = previous
    twitch._drops[drop.id] = drop
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    stale = campaign_data("known", minutes=2)
    latest_observation = None

    async def request(operation):
        nonlocal latest_observation
        if isinstance(operation, list):
            if change == "claim":
                assert await drop.claim() is True
            else:
                source = "websocket" if change == "unchanged" else change
                drop.update_minutes(10 if change == "unchanged" else 25, source=source)
            latest_observation = drop.progress_observation
            return [response(deepcopy(stale))]
        if operation["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [deepcopy(stale)], "gameEventDrops": [],
            }}}}
        if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            return {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
        return {"data": {"currentUser": {"dropCampaigns": [metadata(stale)]}}}

    twitch.gql_request = AsyncMock(side_effect=request)
    service = InventoryService(twitch)
    try:
        await service.fetch_inventory()
    finally:
        service.cancel_refresh_retry()
    if twitch._mnt_task is not None:
        await twitch._mnt_task
    current = twitch._drops[drop.id]
    assert current.is_claimed is (change == "claim")
    assert current.real_current_minutes == (60 if change == "claim" else 10 if change == "unchanged" else 25)
    assert current.progress_observation == latest_observation


@pytest.fixture
def live(tmp_path):
    state = installed(client(), tmp_path)
    yield state
    state.service.cancel_refresh_retry()


def installed(twitch, tmp_path):
    twitch.gui = MagicMock()
    twitch.gui.inv.add_campaign = AsyncMock()
    twitch._maintenance_service = SimpleNamespace(run_maintenance_task=AsyncMock())
    twitch._mnt_triggers = deque()
    previous = DropsCampaign(twitch, campaign_data("known", minutes=10), {})
    drop = previous.timed_drops["known-drop"]
    twitch.inventory.append(previous)
    twitch._campaigns[previous.id] = previous
    twitch._drops[drop.id] = drop
    twitch.drop_history = DropHistory(tmp_path)
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    maintenance = twitch._mnt_task = MagicMock()
    maintenance.done.return_value = False
    return SimpleNamespace(twitch=twitch, previous=previous, drop=drop,
                           service=InventoryService(twitch), maintenance=maintenance,
                           history_dir=tmp_path)


def reads(state, *, minutes=2, claimed=False, during=None):
    raw = campaign_data("known", minutes=minutes)
    raw["timeBasedDrops"][0]["self"]["isClaimed"] = claimed
    calls = []

    async def request(operation):
        name = "details" if isinstance(operation, list) else operation["operationName"]
        calls.append(name)
        if name == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            return {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
        if during is not None:
            await during(name)
        if name == "details":
            return [response(deepcopy(raw))]
        if name == GQL_OPERATIONS["Inventory"]["operationName"]:
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [deepcopy(raw)], "gameEventDrops": [],
            }}}}
        assert name == GQL_OPERATIONS["Campaigns"]["operationName"]
        return {"data": {"currentUser": {"dropCampaigns": [metadata(raw)]}}}

    state.twitch.gql_request = AsyncMock(side_effect=request)
    return calls


async def refresh(state):
    await state.service.fetch_inventory()
    if isinstance(state.twitch._mnt_task, asyncio.Task):
        await state.twitch._mnt_task


def retained(state):
    assert state.twitch.inventory == [state.previous]
    assert state.twitch._campaigns == {"known": state.previous}
    assert state.twitch._drops == {"known-drop": state.drop}
    state.twitch.gui.inv.clear.assert_not_called()
    state.twitch.gui.inv.add_campaign.assert_not_awaited()
    state.twitch.request_inventory_refresh.assert_not_called()
    assert state.service._refresh_retry is not None
    assert state.twitch._mnt_task is state.maintenance
    state.maintenance.cancel.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [GQL_OPERATIONS["Inventory"]["operationName"],
                                   GQL_OPERATIONS["Campaigns"]["operationName"]])
async def test_read_start_not_detail_dispatch_captures_the_live_revision(live, stage):
    async def update(name):
        if name == stage:
            live.drop.update_minutes(25, source="websocket")

    calls = reads(live, during=update)
    await refresh(live)
    retained(live)
    assert live.drop.real_current_minutes == 25
    assert live.drop.progress_observation["source"] == "websocket"
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "estimate", "bad_minutes"])
async def test_unsuperseded_inventory_can_correct_progress_downward(live, change):
    observation = live.drop.progress_observation

    async def update(name):
        if name == "details" and change == "estimate":
            live.drop._bump_minutes(None)
        elif name == "details" and change == "bad_minutes":
            live.drop.update_minutes(True, source="websocket")

    calls = reads(live, during=update)
    await refresh(live)
    current = live.twitch._drops[live.drop.id]
    assert current is not live.drop and current.real_current_minutes == 2
    assert current.extra_current_minutes == 0 and not current.is_claimed
    assert current.progress_observation["minutes"] == 2
    assert current.progress_observation["source"] == "inventory"
    assert live.drop.progress_observation == observation
    live.twitch.request_inventory_refresh.assert_not_called()
    live.twitch.gui.inv.clear.assert_called_once_with()
    live.twitch.gui.inv.add_campaign.assert_awaited_once()
    live.maintenance.cancel.assert_called_once_with()
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["websocket", "claim", "pending_claim"])
async def test_actual_imported_http_keeps_live_models_and_does_not_replay_claims(live, tmp_path, monkeypatch, change):
    raw = campaign_data("known", minutes=2)
    inventory = {"data": {"currentUser": {"inventory": {
        "dropCampaignsInProgress": [raw], "gameEventDrops": [],
    }}}}
    catalog = {"data": {"currentUser": {"dropCampaigns": [metadata(raw)]}}}
    confirmation = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    async with running_miner(tmp_path, monkeypatch) as (actual, peer, _):
        peer.responses.extend(([confirmation] if change == "pending_claim" else []) + [inventory]
                              + ([confirmation] if change == "claim" else []) + [catalog, [response(raw)]])

        async def query(operation):
            result = await actual.gql_request(operation)
            assert not actual._browser._lock.locked()
            if not isinstance(operation, list):
                name = operation["operationName"]
                if name == GQL_OPERATIONS["ClaimDrop"]["operationName"] and change == "pending_claim":
                    entered.set()
                    await release.wait()
                elif name == GQL_OPERATIONS["Inventory"]["operationName"]:
                    if change == "websocket":
                        live.drop.update_minutes(25, source="websocket")
                    elif change == "claim":
                        assert await live.drop.claim() is True
            return result

        live.twitch.gql_request = query
        try:
            if change == "pending_claim":
                task = asyncio.create_task(live.drop.claim())
                await asyncio.wait_for(entered.wait(), 2)
                assert live.previous._pending_claims == {live.drop.id: 1}
            await refresh(live)
            retained(live)
            if task is not None:
                release.set()
                assert await asyncio.wait_for(task, 2) is True
            assert live.drop.real_current_minutes == (25 if change == "websocket" else 60)
            assert live.drop.is_claimed is (change != "websocket")
            assert live.previous._pending_claims == {}
            assert DropHistory(live.history_dir).total_count == int(change != "websocket")
            assert len(peer.calls) == (3 if change == "websocket" else 4)
            assert not peer.responses and not actual._browser._lock.locked()
            assert actual._browser.status()["state"] == "ready"
            assert actual._browser.status()["generation"] == 1
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["websocket", "pending_claim"])
async def test_real_state_machine_runs_policy_between_deferred_refreshes(tmp_path, monkeypatch, change):
    twitch = runtime_client()
    twitch.inventory, twitch._campaigns, twitch._drops = [], {}, {}
    state = installed(twitch, tmp_path)
    if change == "websocket":
        state.drop.claim_id = None
    twitch._inventory_service = state.service
    twitch._state = State.INVENTORY_FETCH
    twitch._state_change = asyncio.Event()
    twitch._schedule_changed = asyncio.Event()
    twitch._inventory_refresh_pending = twitch._clear_cache_pending = twitch._games_update_pending = False
    twitch._inventory_loaded = True
    twitch.channels = OrderedDict()
    twitch.wanted_games = []
    twitch._watching_task = twitch._schedule_task = None
    twitch._watch_service = SimpleNamespace(watch_loop=AsyncMock())
    twitch._message_handler_service = MagicMock()
    twitch._ensure_api_clients = MagicMock()
    twitch.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    twitch.websocket = MagicMock(start=AsyncMock())
    twitch._stop_mining_schedule = AsyncMock()
    twitch._mining_schedule_loop = AsyncMock()
    twitch._stream_selector = SimpleNamespace(get_wanted_games=MagicMock(return_value=[]))
    twitch.restart_watching = twitch.stop_watching = MagicMock()
    twitch.print = MagicMock()
    twitch._games_update_pending = True
    loop, scheduled = asyncio.get_running_loop(), []
    original_later = loop.call_later

    def later(delay, callback, *args, context=None):
        if callback == state.service._retry_refresh:
            handle = MagicMock()
            scheduled.append((delay, callback, handle))
            return handle
        return original_later(delay, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_later", later)
    entered, rounds, claims = asyncio.Event(), [], []

    async def update(name):
        if name == "details" and change == "websocket":
            state.drop.update_minutes(10, source="websocket")

    first = reads(state, during=update)
    dispatch = twitch.gql_request

    async def request(operation):
        if not isinstance(operation, list):
            if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
                claims.append(True)
                entered.set()
                await asyncio.Event().wait()
            if operation["operationName"] == GQL_OPERATIONS["Inventory"]["operationName"]:
                rounds.append(True)
                assert len(rounds) <= 3, "Refreshes starved normal policy work"
        return await dispatch(operation)

    def transition(value):
        Twitch.change_state(twitch, value)
        if value is State.IDLE:
            policies = twitch._stream_selector.get_wanted_games.call_count
            assert policies == len(rounds) == len(scheduled)
            assert scheduled[-1][0] == 60
            if policies == 3:
                Twitch.change_state(twitch, State.EXIT)
            else:
                # Deliver the owned timer only after policy/cleanup reached idle.
                loop.call_soon(scheduled[-1][1])

    twitch.gql_request = request
    twitch.change_state = transition
    task = None
    try:
        if change == "pending_claim":
            task = asyncio.create_task(state.drop.claim())
            await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(twitch._run(), 2)
        assert len(rounds) == len(scheduled) == twitch._stream_selector.get_wanted_games.call_count == 3
        assert len(first) == 9 and len(claims) == int(change == "pending_claim")
        assert twitch.inventory == [state.previous] and state.drop.real_current_minutes == 10
        assert not twitch._inventory_refresh_pending and not twitch._games_update_pending
        assert DropHistory(tmp_path).total_count == 0
        twitch.gui.inv.clear.assert_not_called()
    finally:
        state.service.cancel_refresh_retry()
        for owned in (task, twitch._watching_task, twitch._schedule_task, twitch._mnt_task):
            if isinstance(owned, asyncio.Task):
                owned.cancel()
                await asyncio.gather(owned, return_exceptions=True)
    assert state.previous._pending_claims == {}
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "unconfirmed", "error", "cancel", "exit"])
async def test_pending_claim_defers_replacement_and_its_counter_always_unwinds(live, outcome):
    calls = reads(live)
    dispatch = live.twitch.gql_request
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        if not isinstance(operation, list) and operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            entered.set()
            await release.wait()
            if outcome == "error":
                raise GQLException("test-only request failure")
            if outcome == "exit":
                raise ExitRequest()
            if outcome == "unconfirmed":
                return {"data": None}
        return await dispatch(operation)

    live.twitch.gql_request = AsyncMock(side_effect=request)
    task = asyncio.create_task(live.drop.claim())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert live.previous._pending_claims == {live.drop.id: 1}
        await refresh(live)
        retained(live)
        assert not live.drop.is_claimed and live.drop.real_current_minutes == 10
        assert live.previous.progress_revision == 0 and len(calls) == 3
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == "exit":
                with pytest.raises(ExitRequest):
                    await task
            else:
                assert await task is (outcome == "confirmed")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert live.previous._pending_claims == {}
    assert live.drop.is_claimed is (outcome == "confirmed")
    assert DropHistory(live.history_dir).total_count == int(outcome == "confirmed")
    live.twitch.request_inventory_refresh.reset_mock()
    reads(live, minutes=60 if outcome == "confirmed" else 2, claimed=outcome == "confirmed")
    await refresh(live)
    assert live.twitch._drops[live.drop.id] is not live.drop
    assert live.twitch._drops[live.drop.id].is_claimed is (outcome == "confirmed")
    live.twitch.request_inventory_refresh.assert_not_called()
    assert DropHistory(live.history_dir).total_count == int(outcome == "confirmed")


@pytest.mark.asyncio
async def test_overlapping_claims_hold_replacement_until_the_last_result(live):
    reads(live)
    dispatch = live.twitch.gql_request
    entered, releases, count = asyncio.Event(), [asyncio.Event(), asyncio.Event()], []

    async def request(operation):
        if not isinstance(operation, list) and operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            index = len(count)
            count.append(True)
            if len(count) == 2:
                entered.set()
            await releases[index].wait()
        return await dispatch(operation)

    live.twitch.gql_request = AsyncMock(side_effect=request)
    tasks = [asyncio.create_task(live.drop.claim()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert live.previous._pending_claims == {live.drop.id: 2}
        releases[0].set()
        assert await tasks[0] is True
        assert live.previous._pending_claims == {live.drop.id: 1}
        await refresh(live)
        retained(live)
        assert live.previous.progress_revision == 1
        releases[1].set()
        assert await tasks[1] is True
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert live.previous._pending_claims == {} and live.previous.progress_revision == 1
    assert DropHistory(live.history_dir).total_count == 1
    live.twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_pending_notification_does_not_hold_a_settled_claims_inventory(live):
    reads(live, minutes=60, claimed=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def notify():
        entered.set()
        await release.wait()

    live.twitch.gui.broadcast_wanted_items_now.side_effect = notify
    task = asyncio.create_task(live.drop.claim())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert live.previous._pending_claims == {} and live.drop.is_claimed
        await refresh(live)
        assert live.twitch._drops[live.drop.id] is not live.drop
        assert live.twitch._drops[live.drop.id].is_claimed
        live.twitch.request_inventory_refresh.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert DropHistory(live.history_dir).total_count == 1


@pytest.mark.asyncio
async def test_newer_refresh_wins_when_the_older_read_returns_last(live):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(name):
        if name == GQL_OPERATIONS["Inventory"]["operationName"]:
            entered.set()
            await release.wait()

    first = reads(live, during=hold)
    task = asyncio.create_task(refresh(live))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = reads(live, minutes=40)
        await refresh(live)
        installed = live.twitch._drops[live.drop.id]
        release.set()
        await asyncio.wait_for(task, 2)
        assert live.twitch._drops[live.drop.id] is installed
        assert installed.real_current_minutes == 40
        assert len(first) == 1 and len(second) == 5
        live.twitch.gui.inv.clear.assert_called_once_with()
        live.twitch.gui.inv.add_campaign.assert_awaited_once()
        live.twitch.request_inventory_refresh.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["details", "gui"])
async def test_cache_clear_retires_an_older_refresh_and_its_maintenance(live, stage):
    async def clear(name):
        if name == stage:
            live.service.clear_cached_state()

    reads(live, during=clear)
    if stage == "gui":
        async def clear_gui(campaign):
            await clear("gui")

        live.twitch.gui.inv.add_campaign.side_effect = clear_gui
    await refresh(live)
    assert live.twitch.inventory == [] and live.twitch._campaigns == {} and live.twitch._drops == {}
    assert not live.twitch._mnt_triggers and live.twitch._mnt_task is None
    live.twitch._maintenance_service.run_maintenance_task.assert_not_awaited()
    live.twitch.request_inventory_refresh.assert_not_called()
