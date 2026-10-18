"""Claim metadata must not fabricate completion or dispatch unusable claim IDs."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.config import GQL_OPERATIONS, State
from src.exceptions import MinerException
from src.models.campaign import DropsCampaign
from src.models.drop import BaseDrop, TimedDrop
from src.services.inventory_service import InventoryService
from src.services.message_handlers import MessageHandlerService
from tests.test_claim_response_shapes import reward as reward
from tests.test_claim_response_shapes import state
from tests.test_drop_watch_requirements import campaign_data, refresh_client
from tests.test_imported_session_retry import running_miner
from tests.test_inventory_progress_types import real_inventory_gui, responses
from tests.test_inventory_refresh_races import installed
from tests.test_unavailable_campaign_details import metadata, response
from tests.test_watch_drop_filtering import _campaign, _drop


INVALID_IDS = [None, False, True, 0, 1, 1.5, "", " \t\r\n", [], ["private-claim-canary"],
               {}, {"private-claim-canary": 1}]
INVALID_FLAGS = [None, 0, 1, 0.0, 1.0, "false", "true", "", "private-claim-canary", [], {}]
INVALID_SELF = [None, False, 0, "private-claim-canary", [], {},
                {"isClaimed": False}, {"dropInstanceID": None},
                *({"dropInstanceID": None, "isClaimed": value} for value in INVALID_FLAGS),
                *({"dropInstanceID": value, "isClaimed": False} for value in INVALID_IDS[1:])]


def with_claim_data(value):
    data = campaign_data(60)
    data["timeBasedDrops"][0]["self"] = deepcopy(value)
    return data


@pytest.mark.parametrize("kind", [BaseDrop, TimedDrop])
@pytest.mark.parametrize("account", INVALID_SELF)
def test_present_unusable_claim_metadata_is_refused_without_coercion_or_echo(kind, account):
    twitch, _, _, _ = refresh_client(60)
    campaign = twitch.inventory[0]
    data = with_claim_data(account)["timeBasedDrops"][0]
    before = deepcopy(data)
    with pytest.raises(MinerException, match="^Invalid drop claim state$"):
        kind(campaign, data, {})
    assert data == before


@pytest.mark.asyncio
@pytest.mark.parametrize("account", INVALID_SELF)
async def test_bad_claim_metadata_preserves_inventory_and_actual_gui(account):
    twitch, settings, auth, maintenance = refresh_client(60)
    manager, broadcaster = await real_inventory_gui(twitch)
    old = twitch.inventory[0]
    containers = (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers)
    ui = deepcopy(manager.get_campaigns())
    before = tuple(twitch._mnt_triggers)
    service = InventoryService(twitch)
    responses(twitch, with_claim_data(account))
    try:
        with pytest.raises(MinerException, match="^Invalid drop claim state$"):
            await service.fetch_inventory()
        assert twitch.inventory == [old] and twitch._campaigns == {old.id: old}
        assert twitch._drops == old.timed_drops
        assert all(current is previous for current, previous in zip(
            (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers), containers, strict=True,
        ))
        assert manager.get_campaigns() == ui and tuple(twitch._mnt_triggers) == before
        manager.clear.assert_not_called()
        manager.add_campaign.assert_not_awaited()
        broadcaster.emit.assert_not_awaited()
        maintenance.cancel.assert_not_called()
        assert twitch._mnt_task is maintenance and twitch.settings is settings and twitch._auth_state is auth
        assert twitch.gql_request.await_count == 3 and not service.refresh_retry_pending
    finally:
        service.cancel_refresh_retry()
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.parametrize("claim_id", [None, "opaque-claim", " opaque # / claim "])
@pytest.mark.parametrize("claimed", [False, True])
def test_valid_inventory_claim_fields_preserve_opaque_ids_and_counter_rules(claim_id, claimed):
    twitch, _, _, _ = refresh_client(60)
    data = with_claim_data({"dropInstanceID": claim_id, "isClaimed": claimed, "currentMinutesWatched": 7})
    before = deepcopy(data)
    campaign = DropsCampaign(twitch, data, {})
    drop = campaign.timed_drops["new-drop"]
    assert drop.claim_id == claim_id and drop.is_claimed is claimed
    assert drop.current_minutes == (60 if claimed else 7) and drop.observed_minutes == 7
    assert drop.can_claim is (claim_id is not None and not claimed)
    assert campaign.claimed_drops == int(claimed) and data == before


@pytest.mark.parametrize("awarded", [False, True])
def test_absent_self_keeps_existing_benefit_inference_without_inventing_a_claim_id(awarded):
    twitch, _, _, _ = refresh_client(60)
    data = campaign_data(60)
    claimed = {"benefit-new-drop": datetime.now(timezone.utc)} if awarded else {}
    drop = DropsCampaign(twitch, data, claimed).timed_drops["new-drop"]
    assert drop.is_claimed is awarded and drop.claim_id is None
    assert drop.observed_minutes is None and drop.current_minutes == (60 if awarded else 0)


@pytest.mark.parametrize("claim_id", INVALID_IDS)
def test_unusable_live_claim_id_does_not_replace_an_existing_one(claim_id):
    drop = _campaign("test", [_drop("test", "Test", 30)]).timed_drops["test"]
    drop.update_claim("known-id")
    before = state(drop), drop.campaign.progress_revision
    assert drop.update_claim(claim_id) is False
    assert (state(drop), drop.campaign.progress_revision) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_id", INVALID_IDS[1:])
async def test_claim_guard_does_not_dispatch_an_unusable_id_even_if_assigned_directly(reward, claim_id):
    drop, twitch = reward
    drop.claim_id = claim_id
    before = state(drop), drop.campaign.progress_revision
    assert not drop.can_claim and await drop.claim() is False
    assert (state(drop), drop.campaign.progress_revision) == before
    twitch.gql_request.assert_not_awaited()
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    drop._send_telegram_notification.assert_not_awaited()
    drop._send_discord_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_id", [None, "known-id"])
@pytest.mark.parametrize("claim_id", [*INVALID_IDS, pytest.param("MISSING", id="missing-field")])
async def test_unusable_claim_message_does_not_claim_replay_publish_sleep_or_refresh(
    monkeypatch, caplog, existing_id, claim_id,
):
    drop = _campaign("test", [_drop("test", "Test", 30)]).timed_drops["test"]
    drop.claim_id = existing_id
    twitch = drop._twitch
    twitch._drops = {drop.id: drop}
    twitch.watching_channel.get_with_default.return_value = None
    twitch.gql_request = AsyncMock()
    drop.claim = AsyncMock(wraps=drop.claim)
    sleep = AsyncMock()
    monkeypatch.setattr("src.services.message_handlers.asyncio.sleep", sleep)
    message = {"type": "drop-claim", "data": {"drop_id": drop.id}}
    if claim_id != "MISSING":
        message["data"]["drop_instance_id"] = claim_id
    original, before = deepcopy(message), state(drop)
    await MessageHandlerService(twitch).process_drops(42, message)
    assert state(drop) == before and message == original
    drop.claim.assert_not_awaited()
    twitch.gql_request.assert_not_awaited()
    twitch.gui.inv.update_drop.assert_not_called()
    twitch.restart_watching.assert_not_called()
    twitch.request_inventory_refresh.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.drop_history.record.assert_not_called()
    sleep.assert_not_awaited()
    assert "private-claim-canary" not in caplog.text
    assert "Exception in" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reward", [TimedDrop], indirect=True)
@pytest.mark.parametrize("claim_id", ["opaque-id", " opaque # / claim "])
async def test_valid_claim_message_still_sends_exact_id_and_settles_once(monkeypatch, reward, claim_id):
    drop, twitch = reward
    twitch._drops = {drop.id: drop}
    twitch.watching_channel.get_with_default.return_value = None
    twitch.gql_request.return_value = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    monkeypatch.setattr("src.services.message_handlers.asyncio.sleep", AsyncMock())
    message = {"type": "drop-claim", "data": {"drop_id": drop.id, "drop_instance_id": claim_id}}
    before = deepcopy(message)
    service = MessageHandlerService(twitch)
    await service.process_drops(42, message)
    await service.process_drops(42, message)
    assert drop.is_claimed is True and drop.claim_id == claim_id and message == before
    twitch.gql_request.assert_awaited_once_with(
        GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": claim_id}}),
    )
    assert drop.campaign.progress_revision == 1
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    drop._send_telegram_notification.assert_awaited_once_with()
    drop._send_discord_notification.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unknown_drop_message_does_not_log_claim_payload(caplog):
    twitch, _, _, _ = refresh_client(60)
    await MessageHandlerService(twitch).process_drops(42, {
        "type": "drop-claim", "data": {"drop_id": "unknown", "drop_instance_id": "private-claim-canary"},
    })
    assert "private-claim-canary" not in caplog.text
    twitch.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, {"dropInstanceID": None, "isClaimed": "false"},
                                   {"dropInstanceID": False, "isClaimed": False},
                                   {"dropInstanceID": " \t", "isClaimed": False}])
async def test_actual_imported_http_refuses_bad_claim_state_then_accepts_fresh_inventory(tmp_path, monkeypatch, account):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        twitch._state = State.INVENTORY_FETCH
        owner = installed(twitch, tmp_path)
        manager, broadcaster = await real_inventory_gui(twitch)
        old, ui = twitch.inventory[0], deepcopy(manager.get_campaigns())

        def queue(data):
            peer.responses.extend([
                {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
                {"data": {"currentUser": {"dropCampaigns": [metadata(data)]}}}, [response(deepcopy(data))],
            ])

        try:
            queue(with_claim_data(account))
            with pytest.raises(MinerException, match="^Invalid drop claim state$"):
                await owner.service.fetch_inventory()
            assert twitch.inventory == [old] and manager.get_campaigns() == ui
            manager.clear.assert_not_called()
            broadcaster.emit.assert_not_awaited()
            owner.maintenance.cancel.assert_not_called()
            assert len(peer.calls) == 3 and not peer.responses
            queue(with_claim_data({"dropInstanceID": " opaque # / claim ", "isClaimed": False, "currentMinutesWatched": 7}))
            await owner.service.fetch_inventory()
            await twitch._mnt_task
            drop = twitch._drops["new-drop"]
            assert drop.claim_id == " opaque # / claim " and drop.is_claimed is False
            assert drop.current_minutes == 7 and drop.observed_minutes == 7
            assert manager.get_campaigns()[0]["drops"][0]["is_claimed"] is False
            assert len(peer.calls) == 6 and not peer.responses
            assert twitch._browser.status()["state"] == "ready" and twitch._browser.status()["generation"] == 1
            assert not twitch._browser._lock.locked() and not owner.service.refresh_retry_pending
            assert twitch.drop_history.total_count == 0
        finally:
            owner.service.cancel_refresh_retry()
            if isinstance(twitch._mnt_task, asyncio.Task):
                twitch._mnt_task.cancel()
                await asyncio.gather(twitch._mnt_task, return_exceptions=True)
            await asyncio.sleep(0)
