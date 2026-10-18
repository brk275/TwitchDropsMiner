"""Null campaign details retain only validated inventory and usable campaigns."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.exceptions import MinerException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from tests.test_imported_session_retry import running_miner
from tests.test_inventory_minute_admission import published_gui
from tests.test_unavailable_campaign_details import campaign_data, metadata, response


async def install_previous(twitch):
    twitch._state = State.INVENTORY_FETCH
    old = DropsCampaign(twitch, campaign_data("old", minutes=9), {})
    twitch.inventory.append(old)
    twitch._campaigns[old.id] = old
    twitch._drops.update(old.timed_drops)
    twitch._mnt_triggers.append(old.ends_at)
    maintenance = twitch._mnt_task = MagicMock()
    maintenance.done.return_value = False
    twitch._maintenance_service.run_maintenance_task = AsyncMock()
    manager, broadcaster = await published_gui(twitch)
    return old, maintenance, manager, broadcaster


async def cleanup(twitch):
    if isinstance(twitch._mnt_task, asyncio.Task):
        twitch._mnt_task.cancel()
        await asyncio.gather(twitch._mnt_task, return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_error", [False, True])
async def test_imported_null_details_publish_good_and_retained_inventory_only(tmp_path, monkeypatch, server_error):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, _ = await install_previous(twitch)
        known, good, missing = campaign_data("known", minutes=17), campaign_data("good", minutes=7), campaign_data("missing")
        catalog = [metadata(item) for item in (known, good, missing)]
        del known["status"]
        unavailable = response(None)
        if server_error:
            unavailable = {**response(missing), "errors": [{"message": "server error", "path": ["user", "dropCampaign"]}]}
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [known], "gameEventDrops": []}}}},
            {"data": {"currentUser": {"dropCampaigns": catalog}}},
            [response(None), response(good), unavailable],
        ])
        try:
            await InventoryService(twitch).fetch_inventory()
            await twitch._mnt_task
            await asyncio.sleep(0)
            assert set(twitch._campaigns) == {"known", "good"} and old not in twitch.inventory
            assert set(twitch._drops) == {"known-drop", "good-drop"}
            assert twitch._drops["known-drop"].real_current_minutes == 17
            assert twitch._drops["known-drop"].claim_id == "claim-known"
            assert not twitch._drops["known-drop"].is_claimed
            displayed = {campaign["id"]: campaign for campaign in manager.get_campaigns()}
            assert set(displayed) == {"known", "good"}
            assert displayed["good"]["drops"][0]["current_minutes"] == 7
            assert len(peer.calls) == 3 and not peer.responses
            assert twitch._browser.status()["state"] == "ready" and not twitch._browser._lock.locked()
            assert twitch.drop_history.total_count == 0
            manager.clear.assert_called_once_with()
            maintenance.cancel.assert_called_once_with()
        finally:
            await cleanup(twitch)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,error", [
    ("currentMinutesWatched", False, "Invalid drop watched minutes"),
    ("currentMinutesWatched", -1, "Invalid drop watched minutes"),
    ("currentMinutesWatched", {}, "Invalid drop watched minutes"),
    ("currentMinutesWatched", "7", "Invalid drop watched minutes"),
    ("isClaimed", "false", "Invalid drop claim state"),
    ("dropInstanceID", " \t", "Invalid drop claim state"),
    ("requiredMinutesWatched", None, "Invalid drop watch requirement"),
])
async def test_catalog_and_null_details_do_not_mask_bad_retained_inputs(tmp_path, monkeypatch, field, value, error):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, broadcaster = await install_previous(twitch)
        before = deepcopy(manager.get_campaigns())
        known = campaign_data("known", minutes=17)
        catalog = metadata(known)
        del known["status"]
        target = known["timeBasedDrops"][0]
        (target if field == "requiredMinutesWatched" else target["self"])[field] = value
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [known], "gameEventDrops": []}}}},
            {"data": {"currentUser": {"dropCampaigns": [catalog]}}},
            [response(None)],
        ])
        try:
            with pytest.raises(MinerException) as caught:
                await InventoryService(twitch).fetch_inventory()
            assert str(caught.value) == error
            assert twitch.inventory == [old] and twitch._campaigns == {old.id: old}
            assert twitch._drops == old.timed_drops and list(twitch._mnt_triggers) == [old.ends_at]
            assert manager.get_campaigns() == before
            manager.clear.assert_not_called()
            manager.add_campaign.assert_not_awaited()
            broadcaster.emit.assert_not_awaited()
            maintenance.cancel.assert_not_called()
            assert twitch._mnt_task is maintenance
            assert len(peer.calls) == 3 and not peer.responses
            assert twitch._browser.status()["state"] == "ready" and not twitch._browser._lock.locked()
            assert twitch.drop_history.total_count == 0
        finally:
            await cleanup(twitch)
