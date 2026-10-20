"""Rejected campaign collections use bounded refresh recovery without admission."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.exceptions import GQLException
from tests.test_detail_null_guards import cleanup, install_previous
from tests.test_imported_session_retry import immediate_delays, running_miner


def inventory(records):
    return {"data": {"currentUser": {"inventory": {
        "dropCampaignsInProgress": records, "gameEventDrops": [],
    }}}}


def catalog(records):
    return {"data": {"currentUser": {"dropCampaigns": records}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "catalog"])
@pytest.mark.parametrize("bad", [False, [None], [{"id": ""}], [{"id": "duplicate"}] * 2])
@pytest.mark.parametrize("failed_attempts", [1, 2])
async def test_rejected_lists_recover_without_replacing_last_good_snapshot(
    tmp_path, monkeypatch, stage, bad, failed_attempts,
):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        old, maintenance, manager, _ = await install_previous(client)
        client._inventory_loaded = True
        client.is_mining_time = lambda: True
        before = deepcopy(manager.get_campaigns())
        waits = []

        async def wait(delay):
            assert client.inventory == [old] and client._campaigns == {old.id: old}
            assert client._drops == old.timed_drops
            assert client._mnt_task is maintenance
            maintenance.cancel.assert_not_called()
            manager.clear.assert_not_called()
            assert manager.get_campaigns() == before
            assert not client._browser._lock.locked()
            assert client.drop_history.total_count == 0
            waits.append(delay)

        client._wait_inventory_retry = wait
        failed = [inventory(bad)] if stage == "inventory" else [inventory([]), catalog(bad)]
        peer.responses.extend(deepcopy(failed) * failed_attempts + [inventory([]), catalog([])])
        try:
            assert await client._fetch_inventory_with_recovery()
            await client._mnt_task
            assert waits == [30, 60][:failed_attempts]
            assert client.inventory == [] and client._campaigns == {} and client._drops == {}
            assert client._inventory_loaded
            assert len(peer.calls) == len(failed) * failed_attempts + 2
            assert not peer.responses and client.drop_history.total_count == 0
            assert client._browser.status()["state"] == "ready"
            maintenance.cancel.assert_called_once_with()
            manager.clear.assert_called_once_with()
        finally:
            await cleanup(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "catalog"])
async def test_persistent_list_rejection_has_finite_budget_and_retains_snapshot(tmp_path, monkeypatch, stage):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        old, maintenance, manager, _ = await install_previous(client)
        client.is_mining_time = lambda: True
        client._wait_inventory_retry = AsyncMock()
        failed = [inventory([None])] if stage == "inventory" else [inventory([]), catalog([None])]
        peer.responses.extend(deepcopy(failed) * 3)
        try:
            with pytest.raises(GQLException, match="^Invalid campaign list response$"):
                await client._fetch_inventory_with_recovery()
            assert len(peer.calls) == len(failed) * 3 and not peer.responses
            assert [call.args for call in client._wait_inventory_retry.await_args_list] == [(30,), (60,)]
            assert client.inventory == [old] and client._mnt_task is maintenance
            assert client._campaigns == {old.id: old} and client._drops == old.timed_drops
            manager.clear.assert_not_called()
            maintenance.cancel.assert_not_called()
            assert client.drop_history.total_count == 0
        finally:
            await cleanup(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "close", "hours"])
async def test_rejected_list_recovery_stops_before_another_read(tmp_path, monkeypatch, action):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.is_mining_time = lambda: True
        client._state = State.INVENTORY_FETCH
        waiting = asyncio.Event()
        peer.responses.extend([inventory([None]), inventory([]), catalog([])])

        async def wait(delay):
            assert delay == 30 and not client._browser._lock.locked()
            waiting.set()
            await asyncio.Event().wait()

        client._wait_inventory_retry = wait
        if action == "cancel":
            task = asyncio.create_task(client._fetch_inventory_with_recovery())
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        else:
            async def interrupt(delay):
                if action == "close":
                    client.close()
                else:
                    client.is_mining_time = lambda: False
            client._wait_inventory_retry = interrupt
            assert not await client._fetch_inventory_with_recovery()
            if action == "hours":
                assert client._inventory_refresh_pending
        assert len(peer.calls) == 1 and len(peer.responses) == 2
        assert client.drop_history.total_count == 0


@pytest.mark.asyncio
async def test_real_runtime_survives_rejected_startup_list(tmp_path, monkeypatch):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.session_controller.start = MagicMock()
        client.session_controller.stop = AsyncMock()
        client.websocket.start = AsyncMock()
        client.websocket.add_topics = MagicMock()
        client.login_browser.stop = AsyncMock()
        client.helper.stop = AsyncMock()
        client.is_mining_time = lambda: True
        client._wait_inventory_retry = AsyncMock()

        async def watch():
            await asyncio.Event().wait()

        async def stop_after_inventory():
            client.close()

        client._watch_service.watch_loop = watch
        client.gui.broadcast_wanted_items.side_effect = client.close
        client._maintenance_service.run_maintenance_task = stop_after_inventory
        peer.responses.extend([inventory([None]), inventory([]), catalog([])])
        try:
            await asyncio.wait_for(client.run(), 2)
            assert client._inventory_loaded
            client._wait_inventory_retry.assert_awaited_once_with(30)
            assert len(peer.calls) == 3 and not peer.responses
            assert client.drop_history.total_count == 0
        finally:
            tasks = [task for task in (client._watching_task, client._mnt_task) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client._stop_mining_schedule()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "catalog"])
async def test_transport_and_list_failures_share_one_refresh_budget(tmp_path, monkeypatch, stage):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.is_mining_time = lambda: True
        client._wait_inventory_retry = AsyncMock()
        client._maintenance_service.run_maintenance_task = AsyncMock()
        rejected = [inventory([None])] if stage == "inventory" else [inventory([]), catalog([None])]
        peer.responses.extend([503, 502, 504, *rejected, inventory([]), catalog([])])
        transport_waits = immediate_delays(client._browser, monkeypatch)
        try:
            assert await client._fetch_inventory_with_recovery()
            await client._mnt_task
            assert [call.args for call in client._wait_inventory_retry.await_args_list] == [(30,), (60,)]
            assert transport_waits == [1, 2]
            assert len(peer.calls) == 5 + len(rejected) and not peer.responses
            assert client.drop_history.total_count == 0
        finally:
            await cleanup(client)


@pytest.mark.asyncio
async def test_alternating_failure_kinds_do_not_reset_the_attempt_budget(tmp_path, monkeypatch):
    from src.exceptions import GQLRequestFailed

    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.is_mining_time = lambda: True
        client._wait_inventory_retry = AsyncMock()
        peer.responses.extend([503, 502, 504, inventory([None]), 503, 502, 504, inventory([])])
        transport_waits = immediate_delays(client._browser, monkeypatch)
        with pytest.raises(GQLRequestFailed):
            await client._fetch_inventory_with_recovery()
        assert [call.args for call in client._wait_inventory_retry.await_args_list] == [(30,), (60,)]
        assert transport_waits == [1, 2, 1, 2]
        assert len(peer.calls) == 7 and len(peer.responses) == 1
        assert client.drop_history.total_count == 0
