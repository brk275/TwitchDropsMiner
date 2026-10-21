"""Confirmed claims survive same-account refreshes, never account replacement."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.config import GQL_OPERATIONS
from src.models.campaign import DropsCampaign
from src.services import message_handlers
from src.services.inventory_service import InventoryService
from src.services.message_handlers import MessageHandlerService
from src.web.managers import inventory as inventory_module
from src.web.managers.inventory import InventoryManager
from tests.test_imported_session import bundle_data
from tests.test_imported_session_retry import running_miner
from tests.test_priority_fallback import _settings
from tests.test_unavailable_campaign_details import campaign_data
from tests.test_websocket_shutdown_ownership import attach


ACCEPTED = ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED")


@pytest_asyncio.fixture
async def state(tmp_path, monkeypatch):
    tasks, releases = [], []

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    monkeypatch.setattr(inventory_module, "asyncio", SimpleNamespace(
        create_task=schedule, Lock=asyncio.Lock,
    ))
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, clock):
        twitch.settings = _settings("Wanted")
        twitch.settings.proxy = ""
        twitch.gui.broadcast_wanted_items_now = AsyncMock()
        twitch._maintenance_service.run_maintenance_task = AsyncMock()
        data = campaign_data("same", minutes=12)
        sibling = deepcopy(data["timeBasedDrops"][0])
        sibling["id"] = "sibling"
        data["timeBasedDrops"].append(sibling)
        campaign = DropsCampaign(twitch, deepcopy(data), {})
        twitch.inventory[:] = [campaign]
        twitch._campaigns[campaign.id] = campaign
        twitch._drops.update(campaign.timed_drops)
        twitch.wanted_games = [campaign.game]
        manager = InventoryManager(MagicMock(emit=AsyncMock()), MagicMock())
        twitch.gui.inv = manager
        await manager.add_campaign(campaign)
        old = campaign.timed_drops["same-drop"]
        old._send_telegram_notification = AsyncMock()
        old._send_discord_notification = AsyncMock()
        dispatch = twitch.gql_request
        result = SimpleNamespace(
            twitch=twitch, peer=peer, clock=clock, data=data, old=old,
            manager=manager, tasks=tasks, releases=releases, pool=twitch.websocket,
            directory=tmp_path / "account", status="ELIGIBLE_FOR_ALL",
            reported=False, claim_id="claim-same", claim_gate=None, catalog_gate=None,
            claim_entered=asyncio.Event(), inventory_received=asyncio.Event(),
        )

        async def request(operation):
            name = operation["operationName"]
            if name == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
                result.claim_entered.set()
                if result.claim_gate is not None:
                    await result.claim_gate.wait()
                payload = {"data": {"claimDropRewards": {"status": result.status}}}
            elif name == GQL_OPERATIONS["Inventory"]["operationName"]:
                snapshot = deepcopy(result.data)
                snapshot["timeBasedDrops"][0]["self"].update(
                    isClaimed=result.reported, dropInstanceID=result.claim_id,
                )
                payload = {"data": {"currentUser": {"inventory": {
                    "dropCampaignsInProgress": [snapshot], "gameEventDrops": [],
                }}}}
            elif name == GQL_OPERATIONS["Campaigns"]["operationName"]:
                if result.catalog_gate is not None:
                    await result.catalog_gate.wait()
                payload = {"data": {"currentUser": {"dropCampaigns": []}}}
            elif name == GQL_OPERATIONS["CurrentDrop"]["operationName"]:
                payload = {"data": {"currentUser": {"dropCurrentSession": None}}}
            else:
                raise AssertionError("Unexpected test operation")
            peer.responses.append(payload)
            response = await dispatch(operation)
            if name == GQL_OPERATIONS["Inventory"]["operationName"]:
                result.inventory_received.set()
            return response

        twitch.gql_request = request
        try:
            yield result
        finally:
            for release in releases:
                release.set()
            if isinstance(twitch._mnt_task, asyncio.Task):
                tasks.append(twitch._mnt_task)
            try:
                for task in tasks:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                if tasks:
                    done, pending = await asyncio.wait(set(tasks), timeout=2)
                    await asyncio.gather(*done, return_exceptions=True)
                    assert not pending, "Claim ownership test leaked work"
            finally:
                await asyncio.wait_for(twitch.websocket.stop(clear_topics=True), 2)


async def refresh(state):
    await asyncio.wait_for(InventoryService(state.twitch).fetch_inventory(), 2)
    if state.twitch._mnt_task is not None:
        await asyncio.wait_for(asyncio.shield(state.twitch._mnt_task), 2)
    return state.twitch._drops[state.old.id]


def row(state, drop_id="same-drop"):
    return next(item for item in state.manager.get_campaigns()[0]["drops"]
                if item["id"] == drop_id)


def assert_claimed(state, claimed):
    current = state.twitch._drops[state.old.id]
    assert current.is_claimed is claimed
    assert row(state)["is_claimed"] is claimed
    assert current.real_current_minutes == (60 if claimed else 12)
    assert current.extra_current_minutes == 0
    assert (current.id in {drop.id for drop in current.campaign.mining_selection().targets}) is not claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [*ACCEPTED, "NOT_ELIGIBLE"])
@pytest.mark.parametrize("claim_id", [None, "claim-same"])
@pytest.mark.parametrize("ordering", ["claim-pending", "inventory-pending"])
async def test_confirmation_survives_imported_inventory_replacement(state, status, claim_id, ordering):
    state.status, state.claim_id = status, claim_id
    release = asyncio.Event()
    state.releases.append(release)
    if ordering == "claim-pending":
        state.claim_gate = release
        task = asyncio.create_task(state.old.claim())
        state.tasks.append(task)
        await asyncio.wait_for(state.claim_entered.wait(), 2)
        await refresh(state)
    else:
        state.catalog_gate = release
        task = asyncio.create_task(refresh(state))
        state.tasks.append(task)
        await asyncio.wait_for(state.inventory_received.wait(), 2)
        assert await state.old.claim() is (status in ACCEPTED)
    release.set()
    await asyncio.wait_for(task, 2)
    assert_claimed(state, status in ACCEPTED)
    assert state.twitch.drop_history.total_count == int(status in ACCEPTED)
    assert state.twitch._browser.status()["state"] == "ready"
    assert not state.peer.responses


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ACCEPTED)
async def test_refresh_and_repeated_claim_do_not_repeat_effects(state, status):
    state.status = status
    assert await state.old.claim()
    entries = state.twitch.drop_history.get_entries()
    current = await refresh(state)
    current._send_telegram_notification = AsyncMock()
    current._send_discord_notification = AsyncMock()
    assert await current.claim()
    assert_claimed(state, True)
    assert state.twitch.drop_history.get_entries() == entries
    assert sum(call["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]
               for call in state.peer.calls) == 1
    state.twitch.gui.broadcast_wanted_items_now.assert_awaited_once()
    state.old._send_telegram_notification.assert_awaited_once()
    state.old._send_discord_notification.assert_awaited_once()
    current._send_telegram_notification.assert_not_awaited()
    current._send_discord_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_rejected_claim_cannot_overwrite_current_sibling_or_claim(state):
    current = await refresh(state)
    sibling = current.campaign.timed_drops["sibling"]
    sibling.real_current_minutes = 40
    sibling._on_state_changed()
    state.reported = True
    current.is_claimed = True
    current.real_current_minutes = 60
    current._on_state_changed()
    state.status = "NOT_ELIGIBLE"
    assert await state.old.claim() is False
    assert row(state)["is_claimed"] is True
    assert row(state, "sibling")["current_minutes"] == 40
    assert sibling.real_current_minutes == 40
    assert state.twitch.drop_history.total_count == 0


async def renew(state):
    state.clock[0] += 100
    state.peer.armed = False
    try:
        await state.twitch._browser.install(
            bundle_data(state.clock[0], "renewed-integrity", "renewed-token"), expected_user_id=42,
        )
    finally:
        state.peer.armed = True


@pytest.mark.asyncio
async def test_same_account_renewal_preserves_receipt_through_stale_refresh(state):
    assert await state.old.claim()
    generation = state.twitch._browser.status()["generation"]
    await asyncio.wait_for(renew(state), 2)
    assert state.twitch._browser.status()["generation"] == generation + 1
    await refresh(state)
    assert_claimed(state, True)
    assert state.twitch.drop_history.total_count == 1


async def replace(state):
    async with state.twitch.authentication_change():
        state.peer.armed = False
        state.peer.user_id = "43"
        state.clock[0] += 100
        await state.twitch._browser.install(
            bundle_data(state.clock[0], "other-integrity", "other-token"), replace=True,
        )
    await state.twitch.get_auth()
    state.peer.armed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_id", [None, "other-instance"])
async def test_account_replacement_does_not_reuse_receipts_or_history(state, claim_id):
    assert await state.old.claim()
    entries = state.twitch.drop_history.get_entries()
    await asyncio.wait_for(replace(state), 2)
    state.claim_id = claim_id
    current = await refresh(state)
    assert_claimed(state, False)
    assert state.twitch.drop_history.get_entries() == entries
    if claim_id is not None:
        current._send_telegram_notification = AsyncMock()
        current._send_discord_notification = AsyncMock()
        assert await current.claim()
        current._send_telegram_notification.assert_awaited_once()
        current._send_discord_notification.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_retired_account_model_cannot_claim_or_publish(state, confirmed):
    if confirmed:
        assert await state.old.claim()
    await asyncio.wait_for(replace(state), 2)
    await refresh(state)
    calls = len(state.peer.calls)
    assert await state.old.claim() is False
    state.old._on_state_changed()
    assert len(state.peer.calls) == calls
    assert_claimed(state, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dashboard", "telegram", "discord"])
async def test_cancelled_delivery_keeps_receipt_and_drains_before_account_change(state, boundary):
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state.releases.append(release)
    identities = []

    async def delivery():
        assert state.old.is_claimed and state.twitch.drop_history.total_count == 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            identities.append(state.twitch._auth_state.user_id)

    if boundary == "dashboard":
        state.twitch.gui.broadcast_wanted_items_now = AsyncMock(side_effect=delivery)
    else:
        setattr(state.old, f"_send_{boundary}_notification", AsyncMock(side_effect=delivery))

    async def callback(*args):
        await state.old.claim()

    socket, _, task = await attach(state, callback)
    state.tasks.append(task)
    await asyncio.wait_for(entered.wait(), 2)
    replacement = asyncio.create_task(replace(state))
    state.tasks.append(replacement)
    await asyncio.wait_for(draining.wait(), 2)
    assert not replacement.done()
    release.set()
    await asyncio.wait_for(replacement, 2)
    assert task.cancelled() and not socket._callback_tasks and identities == [42]
    await refresh(state)
    assert_claimed(state, False)
    assert state.twitch.drop_history.total_count == 1


@pytest.mark.asyncio
async def test_cancelled_unanswered_claim_is_not_confirmed_or_replayed(state):
    state.claim_gate = asyncio.Event()
    state.releases.append(state.claim_gate)
    task = asyncio.create_task(state.old.claim())
    state.tasks.append(task)
    await asyncio.wait_for(state.claim_entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state.claim_gate.set()
    await refresh(state)
    assert_claimed(state, False)
    assert state.twitch.drop_history.total_count == 0
    assert all(call["operationName"] != GQL_OPERATIONS["ClaimDrop"]["operationName"]
               for call in state.peer.calls)


@pytest.mark.asyncio
async def test_queued_inventory_update_skips_superseded_snapshot(state):
    broadcaster = state.manager._broadcaster
    broadcaster.emit.reset_mock()
    await state.manager._send_lock.acquire()
    try:
        state.old.real_current_minutes = 20
        state.old._on_state_changed()
        assert await state.old.claim()
    finally:
        state.manager._send_lock.release()
    await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
    updates = [call.args[1] for call in broadcaster.emit.await_args_list
               if call.args[0] == "drop_update"]
    assert len(updates) == 1 and updates[0]["drop"]["is_claimed"]


@pytest.mark.asyncio
async def test_queued_campaign_add_still_introduces_campaign_after_new_drop_update(state):
    manager = state.manager
    manager._broadcaster.emit.reset_mock()
    await manager._send_lock.acquire()
    try:
        manager.clear()
        task = asyncio.create_task(manager.add_campaign(state.old.campaign))
        state.tasks.append(task)
        await asyncio.sleep(0)
        assert await state.old.claim()
    finally:
        manager._send_lock.release()
    await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
    adds = [call.args[1] for call in manager._broadcaster.emit.await_args_list
            if call.args[0] == "campaign_add"]
    assert len(adds) == 1 and adds[0]["drops"][0]["is_claimed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [*ACCEPTED, "NOT_ELIGIBLE"])
async def test_claim_callback_displays_installed_owner_after_refresh(state, monkeypatch, status):
    state.status = status
    state.claim_gate = asyncio.Event()
    state.releases.append(state.claim_gate)
    monkeypatch.setattr(message_handlers, "asyncio", SimpleNamespace(
        sleep=AsyncMock(), wait_for=asyncio.wait_for,
    ))
    task = asyncio.create_task(MessageHandlerService(state.twitch).process_drops(42, {
        "type": "drop-claim", "data": {"drop_id": state.old.id, "drop_instance_id": "instance"},
    }))
    state.tasks.append(task)
    await asyncio.wait_for(state.claim_entered.wait(), 2)
    current = await refresh(state)
    state.claim_gate.set()
    await asyncio.wait_for(task, 2)
    state.twitch.gui.display_drop.assert_called_once_with(current, countdown=True, subone=False)
    assert_claimed(state, status in ACCEPTED)
