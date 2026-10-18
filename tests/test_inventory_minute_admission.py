"""Inventory minutes must be usable before replacing published model and GUI state."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.exceptions import MinerException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from src.web.managers.inventory import InventoryManager
from tests.test_drop_watch_requirements import campaign_data, refresh_client
from tests.test_imported_session_retry import running_miner


class IntegerSubclass(int):
    pass


INVALID = [False, True, -1, -60, 0.0, 1.0, 1.5, float("nan"), float("inf"),
           "", "7", "private-minute", [], {}, [7], {"private-minute": 1}, IntegerSubclass(7)]


def with_minutes(minutes, claimed=False):
    data = campaign_data(60)
    data["timeBasedDrops"][0]["self"] = {
        "dropInstanceID": "opaque-claim", "isClaimed": claimed, "currentMinutesWatched": minutes,
    }
    return data


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("minutes", INVALID)
def test_invalid_minutes_refused_even_for_claimed_rewards(minutes, claimed):
    twitch, _, _, _ = refresh_client(60)
    data = with_minutes(minutes, claimed)
    original = repr(data)
    with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
        DropsCampaign(twitch, data, {})
    assert repr(data) == original


@pytest.mark.parametrize("minutes", [None, 0, 7, 60, 61])
@pytest.mark.parametrize("claimed", [False, True])
def test_supported_minutes_preserve_counter_and_claim_semantics(minutes, claimed):
    twitch, _, _, _ = refresh_client(60)
    data = with_minutes(minutes, claimed)
    drop = DropsCampaign(twitch, data, {}).timed_drops["new-drop"]
    assert drop.real_current_minutes == (60 if claimed else minutes or 0)
    assert type(drop.real_current_minutes) is int
    assert drop.extra_current_minutes == 0 and drop.is_claimed is claimed
    assert drop.claim_id == "opaque-claim"


def test_missing_current_minutes_keeps_zero_fallback_without_claiming():
    twitch, _, _, _ = refresh_client(60)
    data = with_minutes(None)
    del data["timeBasedDrops"][0]["self"]["currentMinutesWatched"]
    drop = DropsCampaign(twitch, data, {}).timed_drops["new-drop"]
    assert drop.real_current_minutes == 0 and not drop.is_claimed


async def published_gui(twitch):
    broadcaster = MagicMock(emit=AsyncMock())
    manager = InventoryManager(broadcaster, MagicMock())
    for campaign in twitch.inventory:
        await manager.add_campaign(campaign)
    manager.clear = MagicMock(wraps=manager.clear)
    manager.add_campaign = AsyncMock(wraps=manager.add_campaign)
    broadcaster.emit.reset_mock()
    twitch.gui.inv = manager
    return manager, broadcaster


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", INVALID)
async def test_invalid_minutes_preserve_actual_gui_and_previous_inventory(minutes):
    twitch, settings, auth, maintenance = refresh_client(60)
    old_data = campaign_data(60)
    old_data["id"] = "old"
    old = DropsCampaign(twitch, old_data, {})
    twitch.inventory[:] = [old]
    twitch._campaigns.clear()
    twitch._campaigns[old.id] = old
    twitch._drops.clear()
    twitch._drops.update(old.timed_drops)
    manager, broadcaster = await published_gui(twitch)
    before = deepcopy(manager.get_campaigns())
    containers = twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers
    values = [twitch.inventory[:], twitch._campaigns.copy(), twitch._drops.copy(), list(twitch._mnt_triggers)]
    twitch.gql_request.side_effect = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [{"id": "new", "game": {"id": "1"}, "status": "ACTIVE"}]}}},
        [{"data": {"user": {"dropCampaign": with_minutes(minutes)}}}],
    ]
    try:
        with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
            await InventoryService(twitch).fetch_inventory()
        assert all(current is previous for current, previous in zip(
            (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers), containers, strict=True,
        ))
        assert [twitch.inventory, twitch._campaigns, twitch._drops, list(twitch._mnt_triggers)] == values
        assert manager.get_campaigns() == before
        manager.clear.assert_not_called()
        manager.add_campaign.assert_not_awaited()
        broadcaster.emit.assert_not_awaited()
        maintenance.cancel.assert_not_called()
        assert twitch._mnt_task is maintenance and twitch.settings is settings and twitch._auth_state is auth
    finally:
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [False, "private-minute", {}, -1])
async def test_imported_inventory_refusal_then_explicit_fresh_read_recovers(tmp_path, monkeypatch, minutes):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        twitch._state = State.INVENTORY_FETCH
        old_data = campaign_data(60)
        old_data["id"] = "old"
        old = DropsCampaign(twitch, old_data, {})
        twitch.inventory.append(old)
        twitch._campaigns[old.id] = old
        twitch._drops.update(old.timed_drops)
        maintenance = twitch._mnt_task = MagicMock()
        maintenance.done.return_value = False
        twitch._maintenance_service.run_maintenance_task = AsyncMock()
        manager, broadcaster = await published_gui(twitch)
        before = deepcopy(manager.get_campaigns())

        def queue(data):
            peer.responses.extend([
                {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
                {"data": {"currentUser": {"dropCampaigns": [{key: data[key] for key in ("id", "game", "status")}]}}},
                [{"data": {"user": {"dropCampaign": data}}}],
            ])

        try:
            queue(with_minutes(minutes))
            with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
                await InventoryService(twitch).fetch_inventory()
            assert twitch.inventory == [old] and manager.get_campaigns() == before
            manager.clear.assert_not_called()
            broadcaster.emit.assert_not_awaited()
            maintenance.cancel.assert_not_called()
            assert len(peer.calls) == 3 and not peer.responses
            queue(with_minutes(7))
            await InventoryService(twitch).fetch_inventory()
            await twitch._mnt_task
            await asyncio.sleep(0)
            assert len(peer.calls) == 6 and not peer.responses
            assert twitch._drops["new-drop"].real_current_minutes == 7
            assert manager.get_campaigns()[0]["drops"][0]["current_minutes"] == 7
            assert twitch._browser.status()["state"] == "ready"
            assert not twitch._browser._lock.locked() and twitch.drop_history.total_count == 0
            maintenance.cancel.assert_called_once_with()
        finally:
            if isinstance(twitch._mnt_task, asyncio.Task):
                twitch._mnt_task.cancel()
                await asyncio.gather(twitch._mnt_task, return_exceptions=True)
            await asyncio.sleep(0)
