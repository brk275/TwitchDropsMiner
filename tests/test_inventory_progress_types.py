"""Malformed inventory counters must be refused before replacing working state."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.exceptions import MinerException
from src.models.campaign import DropsCampaign
from src.services.inventory_service import InventoryService
from src.web.managers.inventory import InventoryManager
from tests.test_drop_watch_requirements import campaign_data, refresh_client
from tests.test_imported_session_retry import running_miner
from tests.test_inventory_refresh_races import installed
from tests.test_unavailable_campaign_details import metadata, response


class IntegerSubclass(int):
    pass


INVALID = [True, False, 0.0, 1.0, 1.5, float("nan"), float("inf"), "7", "", "private-minute-canary",
           [], [7], {}, {"private-minute-canary": 7}, -1, -60, IntegerSubclass(7)]


def with_minutes(value, *, claimed=False):
    data = campaign_data(60)
    data["timeBasedDrops"][0]["self"] = {
        "dropInstanceID": None, "isClaimed": claimed, "currentMinutesWatched": value,
    }
    return data


@pytest.mark.parametrize("minutes", INVALID)
@pytest.mark.parametrize("claimed", [False, True])
def test_malformed_watched_minutes_are_rejected_without_coercion_or_echo(minutes, claimed):
    twitch, _, _, _ = refresh_client(60)
    data = with_minutes(minutes, claimed=claimed)
    before = repr(data)
    with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
        DropsCampaign(twitch, data, {})
    assert repr(data) == before
    assert data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] is minutes


async def real_inventory_gui(twitch):
    broadcaster = SimpleNamespace(emit=AsyncMock())
    manager = InventoryManager(broadcaster, MagicMock())
    await manager.add_campaign(twitch.inventory[0])
    broadcaster.emit.reset_mock()
    manager.clear = MagicMock(wraps=manager.clear)
    manager.add_campaign = AsyncMock(wraps=manager.add_campaign)
    twitch.gui.inv = manager
    return manager, broadcaster


def responses(twitch, data, *, primary=False, details=None):
    twitch.gql_request.side_effect = [
        {"data": {"currentUser": {"inventory": {
            "dropCampaignsInProgress": [deepcopy(data)] if primary else [], "gameEventDrops": [],
        }}}},
        {"data": {"currentUser": {"dropCampaigns": [
            {key: data[key] for key in ("id", "game", "status")},
        ]}}},
        [{"data": {"user": {"dropCampaign": deepcopy(data if details is None else details)}}}],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", INVALID)
async def test_malformed_minutes_preserve_real_inventory_and_gui_before_publication(minutes):
    twitch, settings, auth, maintenance = refresh_client(60)
    manager, broadcaster = await real_inventory_gui(twitch)
    old_models = (twitch.inventory[:], twitch._campaigns.copy(), twitch._drops.copy())
    containers = (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers)
    ui = deepcopy(manager.get_campaigns())
    triggers = tuple(twitch._mnt_triggers)
    watched = twitch.watching_channel.get_with_default(None)
    manual = twitch._manual_target_channel, twitch._manual_target_game
    data = with_minutes(minutes)
    before = repr(data)
    responses(twitch, data)
    service = InventoryService(twitch)
    try:
        with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
            await service.fetch_inventory()
        assert (twitch.inventory, twitch._campaigns, twitch._drops) == old_models
        assert all(current is previous for current, previous in zip(
            (twitch.inventory, twitch._campaigns, twitch._drops, twitch._mnt_triggers), containers, strict=True,
        ))
        assert manager.get_campaigns() == ui and tuple(twitch._mnt_triggers) == triggers
        manager.clear.assert_not_called()
        manager.add_campaign.assert_not_awaited()
        broadcaster.emit.assert_not_awaited()
        maintenance.cancel.assert_not_called()
        assert twitch._mnt_task is maintenance
        assert twitch.settings is settings and twitch._auth_state is auth
        assert twitch.watching_channel.get_with_default(None) is watched
        assert (twitch._manual_target_channel, twitch._manual_target_game) == manual
        assert repr(data) == before and twitch.gql_request.await_count == 3
        assert not service.refresh_retry_pending
    finally:
        service.cancel_refresh_retry()
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", ["private-minute-canary", False, {}, -1])
async def test_valid_details_do_not_hide_malformed_primary_inventory_minutes(minutes):
    twitch, _, _, maintenance = refresh_client(60)
    manager, broadcaster = await real_inventory_gui(twitch)
    old = twitch.inventory[0]
    ui = deepcopy(manager.get_campaigns())
    responses(twitch, with_minutes(minutes), primary=True, details=with_minutes(15))
    service = InventoryService(twitch)
    try:
        with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
            await service.fetch_inventory()
        assert twitch.inventory == [old] and manager.get_campaigns() == ui
        manager.clear.assert_not_called()
        broadcaster.emit.assert_not_awaited()
        maintenance.cancel.assert_not_called()
        assert twitch.gql_request.await_count == 3
    finally:
        service.cancel_refresh_retry()
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_first", [False, True])
async def test_malformed_sibling_campaign_cannot_publish_a_partial_replacement(bad_first):
    twitch, _, _, maintenance = refresh_client(60)
    manager, _ = await real_inventory_gui(twitch)
    old = twitch.inventory[0]
    good, bad = with_minutes(15), with_minutes("private-minute-canary")
    good["id"], good["timeBasedDrops"][0]["id"] = "good", "good-drop"
    batch = [bad, good] if bad_first else [good, bad]
    twitch.gql_request.side_effect = [
        {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
        {"data": {"currentUser": {"dropCampaigns": [metadata(item) for item in batch]}}},
        [response(deepcopy(item)) for item in batch],
    ]
    service = InventoryService(twitch)
    try:
        with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
            await service.fetch_inventory()
        assert twitch.inventory == [old] and set(twitch._campaigns) == {old.id}
        assert [item["id"] for item in manager.get_campaigns()] == [old.id]
        manager.clear.assert_not_called()
        manager.add_campaign.assert_not_awaited()
        maintenance.cancel.assert_not_called()
    finally:
        service.cancel_refresh_retry()
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [None, 0, 1, 60, 61])
@pytest.mark.parametrize("claimed", [False, True])
async def test_supported_minutes_publish_using_existing_counter_and_observation_rules(minutes, claimed):
    twitch, _, _, maintenance = refresh_client(60)
    manager, _ = await real_inventory_gui(twitch)
    data = with_minutes(minutes, claimed=claimed)
    responses(twitch, data)
    service = InventoryService(twitch)
    try:
        await service.fetch_inventory()
        await twitch._mnt_task
        drop = twitch._drops["new-drop"]
        assert drop.real_current_minutes == (60 if claimed else 0 if minutes is None else minutes)
        assert drop.is_claimed is claimed
        assert drop.observed_minutes == (minutes if minutes is not None and minutes <= 60 else None)
        assert manager.get_campaigns()[0]["drops"][0]["progress_observation"] == drop.progress_observation
        manager.clear.assert_called_once_with()
        manager.add_campaign.assert_awaited_once()
        maintenance.cancel.assert_called_once()
        assert twitch.gql_request.await_count == 3
    finally:
        service.cancel_refresh_retry()
        if isinstance(twitch._mnt_task, asyncio.Task):
            twitch._mnt_task.cancel()
            await asyncio.gather(twitch._mnt_task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", ["private-minute-canary", False, 1.5, {}])
async def test_actual_imported_http_refuses_bad_minutes_then_accepts_a_fresh_valid_inventory(tmp_path, monkeypatch, minutes):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        twitch._state = State.INVENTORY_FETCH
        state = installed(twitch, tmp_path)
        manager, broadcaster = await real_inventory_gui(twitch)
        old = twitch.inventory[0]
        ui = deepcopy(manager.get_campaigns())
        service = twitch._inventory_service = state.service

        def queue(data):
            peer.responses.extend([
                {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [], "gameEventDrops": []}}}},
                {"data": {"currentUser": {"dropCampaigns": [metadata(data)]}}}, [response(deepcopy(data))],
            ])

        try:
            queue(with_minutes(minutes))
            with pytest.raises(MinerException, match="^Invalid drop watched minutes$"):
                await service.fetch_inventory()
            assert twitch.inventory == [old] and manager.get_campaigns() == ui
            manager.clear.assert_not_called()
            broadcaster.emit.assert_not_awaited()
            state.maintenance.cancel.assert_not_called()
            assert len(peer.calls) == 3 and not peer.responses
            assert not twitch._browser._lock.locked()
            assert twitch._browser.status()["state"] == "ready" and twitch._browser.status()["generation"] == 1
            queue(with_minutes(15))
            await service.fetch_inventory()
            await twitch._mnt_task
            current = twitch._drops["new-drop"]
            assert twitch.inventory != [old] and current.current_minutes == 15
            assert current.observed_minutes == 15 and current.progress_observation["source"] == "inventory"
            assert manager.get_campaigns()[0]["drops"][0]["progress_observation"] == current.progress_observation
            assert len(peer.calls) == 6 and not peer.responses
            assert not service.refresh_retry_pending and not twitch._browser._lock.locked()
            assert twitch._browser.status()["generation"] == 1 and twitch.drop_history.total_count == 0
        finally:
            service.cancel_refresh_retry()
            if isinstance(twitch._mnt_task, asyncio.Task):
                twitch._mnt_task.cancel()
                await asyncio.gather(twitch._mnt_task, return_exceptions=True)
            await asyncio.sleep(0)
