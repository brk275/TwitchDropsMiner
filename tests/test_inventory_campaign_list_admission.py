"""Malformed campaign collections must not become successful empty inventories."""

import asyncio
from copy import deepcopy

import pytest

from src.exceptions import GQLException
from src.services.inventory_service import InventoryService
from tests.test_detail_null_guards import cleanup, install_previous
from tests.test_imported_session_retry import running_miner
from tests.test_unavailable_campaign_details import campaign_data, metadata, response


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "catalog"])
@pytest.mark.parametrize("bad", [
    False, True, 0, 1, 0.0, "", "private upstream text", {}, {"id": "wrong shape"},
    [None], [False], ["private row"], [[]], [{}], [{"id": None}], [{"id": 0}],
    [{"id": False}], [{"id": []}], [{"id": ""}], [{"id": " \t"}],
    [{"id": "duplicate"}, {"id": "duplicate"}],
])
async def test_invalid_collection_preserves_existing_models_and_gui(tmp_path, monkeypatch, stage, bad):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, broadcaster = await install_previous(twitch)
        before = deepcopy(manager.get_campaigns())
        inventory = {"data": {"currentUser": {"inventory": {
            "dropCampaignsInProgress": bad if stage == "inventory" else [], "gameEventDrops": [],
        }}}}
        catalog = {"data": {"currentUser": {"dropCampaigns": bad}}}
        peer.responses.extend([inventory] if stage == "inventory" else [inventory, catalog])
        try:
            with pytest.raises(GQLException, match="^Invalid campaign list response$") as caught:
                await InventoryService(twitch).fetch_inventory()
            assert "private" not in str(caught.value)
            assert twitch.inventory == [old] and twitch._campaigns == {old.id: old}
            assert twitch._drops == old.timed_drops
            assert list(twitch._mnt_triggers) == [old.ends_at]
            assert twitch._mnt_task is maintenance
            maintenance.cancel.assert_not_called()
            manager.clear.assert_not_called()
            assert manager.get_campaigns() == before
            assert twitch.drop_history.total_count == 0
            assert len(peer.calls) == (1 if stage == "inventory" else 2)
            assert not peer.responses
            assert twitch._browser.status()["state"] == "ready"
            assert not twitch._browser._lock.locked()
        finally:
            await cleanup(twitch)


@pytest.mark.asyncio
@pytest.mark.parametrize("ongoing", [None, []])
@pytest.mark.parametrize("available", [None, []])
async def test_nullable_and_empty_campaign_collections_remain_valid(tmp_path, monkeypatch, ongoing, available):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, _ = await install_previous(twitch)
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": ongoing, "gameEventDrops": [],
            }}}},
            {"data": {"currentUser": {"dropCampaigns": available}}},
        ])
        try:
            await InventoryService(twitch).fetch_inventory()
            await twitch._mnt_task
            assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
            assert manager.get_campaigns() == []
            assert old not in twitch.inventory
            maintenance.cancel.assert_called_once_with()
            assert len(peer.calls) == 2 and not peer.responses
        finally:
            await cleanup(twitch)


@pytest.mark.asyncio
async def test_valid_identifiers_preserve_inventory_precedence_and_expired_filter(tmp_path, monkeypatch):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        _, _, manager, _ = await install_previous(twitch)
        known, fresh, expired = campaign_data("known", minutes=17), campaign_data("fresh", minutes=7), campaign_data("expired")
        expired["status"] = "EXPIRED"
        stale = metadata(known)
        stale["name"] = "Catalog name must not replace inventory"
        peer.responses.extend([
            {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [known], "gameEventDrops": [],
            }}}},
            {"data": {"currentUser": {"dropCampaigns": [stale, metadata(fresh), expired]}}},
            [response(None), response(fresh)],
        ])
        try:
            await InventoryService(twitch).fetch_inventory()
            await twitch._mnt_task
            await asyncio.sleep(0)
            assert set(twitch._campaigns) == {"known", "fresh"}
            assert twitch._campaigns["known"].name == known["name"]
            assert twitch._drops["known-drop"].real_current_minutes == 17
            assert twitch._drops["fresh-drop"].real_current_minutes == 7
            assert len(manager.get_campaigns()) == 2
            assert len(peer.calls) == 3 and not peer.responses
        finally:
            await cleanup(twitch)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "catalog"])
async def test_duplicate_valid_campaigns_are_not_silently_overwritten(tmp_path, monkeypatch, stage):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        old, maintenance, manager, _ = await install_previous(twitch)
        first, second = campaign_data("duplicate", minutes=3), campaign_data("duplicate", minutes=19)
        peer.responses.append({"data": {"currentUser": {"inventory": {
            "dropCampaignsInProgress": [first, second] if stage == "inventory" else [],
            "gameEventDrops": [],
        }}}})
        if stage == "catalog":
            peer.responses.append({"data": {"currentUser": {"dropCampaigns": [metadata(first), metadata(second)]}}})
        try:
            with pytest.raises(GQLException, match="^Invalid campaign list response$"):
                await InventoryService(twitch).fetch_inventory()
            assert twitch.inventory == [old] and twitch._mnt_task is maintenance
            manager.clear.assert_not_called()
            assert len(peer.calls) == (1 if stage == "inventory" else 2)
        finally:
            await cleanup(twitch)


@pytest.mark.parametrize("campaign_id", ["opaque", "  preserve exactly  ", "0", "id-with-dashes"])
def test_admission_preserves_opaque_ids_and_input_records(campaign_id):
    record = {"id": campaign_id, "name": "Original"}
    original = deepcopy(record)
    result = InventoryService._campaigns_by_id([record])
    assert list(result) == [campaign_id] and result[campaign_id] is record
    assert record == original
