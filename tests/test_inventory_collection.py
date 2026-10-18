"""Progress sampling can reuse inventory reads without replacing live state."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.exceptions import GQLException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from tests.test_inventory_receipt_times import FIRST
from tests.test_inventory_receipt_times import clock as clock
from tests.test_unavailable_campaign_details import campaign_data, client, metadata, response


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("minutes", [0, 7])
async def test_detached_collection_preserves_live_indexes_and_receipt_semantics(clock, claimed, minutes):
    data = campaign_data("one", minutes=minutes)
    data["timeBasedDrops"][0]["self"]["isClaimed"] = claimed
    twitch = client(available=[metadata(data)], details=[response(data)])
    old = DropsCampaign(twitch, campaign_data("old"), {})
    twitch.inventory.append(old)
    twitch._campaigns[old.id] = old
    twitch._drops.update(old.timed_drops)
    task = twitch._mnt_task = MagicMock()
    containers = twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers
    wire = deepcopy(data)
    collected = await InventoryService(twitch).collect_inventory()
    assert len(collected) == 1 and collected[0] is not old
    drop = collected[0].timed_drops["one-drop"]
    assert drop.real_current_minutes == (60 if claimed else minutes)
    assert drop.is_claimed is claimed
    assert drop.progress_observation == {"minutes": minutes, "source": "inventory", "received_at": FIRST.isoformat()}
    assert (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers) == containers
    assert all(actual is expected for actual, expected in zip(
        (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers), containers, strict=True,
    ))
    assert twitch.inventory == [old] and twitch._campaigns == {old.id: old} and twitch._drops == old.timed_drops
    assert twitch._mnt_task is task and data == wire
    task.cancel.assert_not_called()
    twitch.gui.status.update.assert_not_called()
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_awaited()
    twitch.gui.inv.update_drop.assert_not_called()
    twitch._maintenance_service.run_maintenance_task.assert_not_awaited()
    twitch.stop_watching.assert_not_called()


@pytest.mark.asyncio
async def test_collection_failure_does_not_publish_partial_campaigns():
    good, bad = campaign_data("good"), campaign_data("bad")
    twitch = client(available=[metadata(good), metadata(bad)], details=[response(good), {}])
    with pytest.raises(GQLException):
        await InventoryService(twitch).collect_inventory()
    assert twitch.inventory == [] and twitch._campaigns == {} and twitch._drops == {}
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_collection_drains_detail_children_without_publishing(monkeypatch):
    data = campaign_data("one")
    twitch = client(available=[metadata(data)])
    entered, cleaned = asyncio.Event(), asyncio.Event()
    children = []

    async def blocked(*args, **kwargs):
        children.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    service = InventoryService(twitch)
    monkeypatch.setattr(service, "fetch_campaigns", blocked)
    parent = asyncio.create_task(service.collect_inventory())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert cleaned.is_set() and all(task.done() for task in children)
        assert twitch.inventory == [] and twitch._campaigns == {} and twitch._drops == {}
        twitch.gui.inv.add_campaign.assert_not_awaited()
    finally:
        parent.cancel()
        await asyncio.gather(parent, *children, return_exceptions=True)


@pytest.mark.asyncio
async def test_normal_fetch_still_publishes_collected_models_and_restarts_maintenance(monkeypatch):
    twitch = client()
    data = DropsCampaign(twitch, campaign_data("one", minutes=7), {})
    service = InventoryService(twitch)
    collected = AsyncMock(return_value=[data])
    monkeypatch.setattr(service, "collect_inventory", collected)
    await service.fetch_inventory()
    await twitch._mnt_task
    collected.assert_awaited_once_with(status_update=twitch.gui.status.update)
    assert twitch.inventory == [data] and twitch._campaigns == {data.id: data}
    assert twitch._drops == data.timed_drops
    twitch.gui.inv.clear.assert_called_once()
    twitch.gui.inv.add_campaign.assert_awaited_once_with(data)
    twitch._maintenance_service.run_maintenance_task.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "1"])
@pytest.mark.parametrize("field", ["max_campaigns", "max_drops"])
async def test_invalid_collection_limits_reject_before_io(field, limit):
    twitch = client()
    with pytest.raises(ValueError, match="^Invalid inventory collection limit$"):
        await InventoryService(twitch).collect_inventory(**{field: limit})
    twitch.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("location", "field"), [
    (location, field)
    for location in ("inventory", "catalog", "details", "cumulative")
    for field in ("max_campaigns", "max_drops")
    if (location, field) != ("details", "max_campaigns")
])
async def test_collection_budget_rejects_without_publishing(location, field):
    first, second = campaign_data("one"), campaign_data("two")
    ongoing, available, details = [], [], []
    if location == "inventory":
        ongoing = [first, second]
    elif location == "catalog":
        available = [first, second]
    elif location == "details":
        # One catalog campaign can expand to more detailed drop records.
        first["timeBasedDrops"].append(deepcopy(second["timeBasedDrops"][0]))
        available, details = [metadata(first)], [response(first)]
    else:
        ongoing, available, details = [first], [metadata(second)], [response(second)]
    twitch = client(ongoing=ongoing, available=available, details=details)
    with pytest.raises(GQLException, match="^Inventory collection exceeds record budget$"):
        await InventoryService(twitch).collect_inventory(**{field: 1})
    assert twitch.inventory == [] and twitch._drops == {} and twitch._campaigns == {}
    twitch.gui.inv.clear.assert_not_called()
    twitch.gui.inv.add_campaign.assert_not_awaited()
