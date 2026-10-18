"""Bounded refresh recovery does not turn malformed replies into retryable reads."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSSLError

from src.auth.session_bundle import SessionError
from src.config import State
from src.exceptions import GQLException, GQLInvalidResponse, MinerException
from tests.test_imported_session_retry import immediate_delays, running_miner
from tests.test_session_lifecycle import miner


@pytest.mark.asyncio
async def test_real_runtime_recovers_exhausted_inventory_read(tmp_path, monkeypatch):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.session_controller.start = MagicMock()
        client.session_controller.stop = AsyncMock()
        client.websocket.start = AsyncMock()
        client.websocket.add_topics = MagicMock()
        client.login_browser.stop = AsyncMock()
        client.helper.stop = AsyncMock()
        client.is_mining_time = lambda: True
        waits = []

        async def watch():
            await asyncio.Event().wait()

        async def delay(seconds):
            assert not client._browser._lock.locked()
            waits.append(seconds)

        async def stop_after_inventory():
            client.close()

        client._watch_service.watch_loop = watch
        monkeypatch.setattr(client, "_wait_inventory_retry", delay, raising=False)
        client.gui.broadcast_wanted_items.side_effect = client.close
        client._maintenance_service.run_maintenance_task = stop_after_inventory
        peer.responses.extend([
            503, 502, 504,
            {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [], "gameEventDrops": [],
            }}}},
            {"data": {"currentUser": {"dropCampaigns": []}}},
        ])
        transport_waits = immediate_delays(client._browser, monkeypatch)
        try:
            await asyncio.wait_for(client.run(), 2)
            assert client._inventory_loaded
            assert waits == [30]
            assert transport_waits == [1, 2]
            assert len(peer.calls) == 5 and not peer.responses
            assert client._browser.status()["state"] == "ready"
            assert client.drop_history.get_entries() == []
            assert client._schedule_task is None
        finally:
            tasks = [t for t in (client._watching_task, client._mnt_task) if t]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client._stop_mining_schedule()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    GQLException("redacted"), GQLInvalidResponse(), SessionError("STALE"),
    MinerException("invalid model"), RuntimeError("unexpected"), asyncio.CancelledError(),
])
async def test_unrelated_failures_are_not_retried(tmp_path, monkeypatch, error):
    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    client.fetch_inventory = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await client._fetch_inventory_with_recovery()
    assert client.fetch_inventory.await_count == 1


@pytest.mark.asyncio
async def test_real_transport_exhaustion_preserves_previous_inventory(tmp_path, monkeypatch):
    from src.exceptions import GQLRequestFailed

    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.is_mining_time = lambda: True
        previous = object()
        client.inventory.append(previous)
        client._inventory_loaded = True
        client._wait_inventory_retry = AsyncMock()
        peer.responses.extend([503] * 9)
        transport_waits = immediate_delays(client._browser, monkeypatch)
        with pytest.raises(GQLRequestFailed):
            await client._fetch_inventory_with_recovery()
        assert client.inventory == [previous]
        assert client._inventory_loaded
        assert len(peer.calls) == 9 and not peer.responses
        assert transport_waits == [1, 2] * 3
        assert client._browser.status()["state"] == "ready"
        assert client.drop_history.get_entries() == []


@pytest.mark.asyncio
async def test_unrelated_state_events_do_not_shorten_retry_delay(tmp_path, monkeypatch):
    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    task = asyncio.create_task(client._wait_inventory_retry(60))
    try:
        await asyncio.sleep(0)
        client.request_inventory_refresh(clear_cache=True)
        await asyncio.sleep(0)
        assert not task.done()
        assert client._inventory_refresh_pending and client._clear_cache_pending
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 429])
async def test_nontransient_http_failure_is_not_retried(tmp_path, monkeypatch, status):
    from src.exceptions import GQLRequestFailed

    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.is_mining_time = lambda: True
        client._wait_inventory_retry = AsyncMock()
        peer.responses.append(status)
        with pytest.raises(GQLException) as caught:
            await client._fetch_inventory_with_recovery()
        assert type(caught.value) is not GQLRequestFailed
        assert len(peer.calls) == 1
        client._wait_inventory_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("private JSON"), ClientSSLError(None, OSError())])
async def test_decode_and_tls_failures_do_not_enter_recovery(tmp_path, monkeypatch, failure):
    from src.exceptions import GQLRequestFailed

    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.is_mining_time = lambda: True
        client._wait_inventory_retry = AsyncMock()
        request = MagicMock(side_effect=failure)
        monkeypatch.setattr(client._browser._transport._http, "request", request)
        with pytest.raises(GQLException) as caught:
            await client._fetch_inventory_with_recovery()
        assert type(caught.value) is not GQLRequestFailed
        assert "private JSON" not in str(caught.value)
        assert request.call_count == 1
        client._wait_inventory_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "close", "hours"])
async def test_retry_wait_is_interruptible(tmp_path, monkeypatch, action):
    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    client._state = State.INVENTORY_FETCH
    task = asyncio.create_task(client._wait_inventory_retry(60))
    await asyncio.sleep(0)
    if action == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if action == "close":
            client.close()
        else:
            client.is_mining_time = lambda: False
            client.change_state(State.IDLE)
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_recovery_has_finite_attempt_budget(tmp_path, monkeypatch):
    from src.exceptions import GQLRequestFailed

    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    client.fetch_inventory = AsyncMock(side_effect=GQLRequestFailed())
    client._wait_inventory_retry = AsyncMock()
    with pytest.raises(GQLRequestFailed):
        await client._fetch_inventory_with_recovery()
    assert client.fetch_inventory.await_count == 3
    assert [c.args for c in client._wait_inventory_retry.await_args_list] == [(30,), (60,)]


@pytest.mark.asyncio
async def test_pause_defers_refresh_without_another_request(tmp_path, monkeypatch):
    from src.exceptions import GQLRequestFailed

    client = miner(tmp_path, monkeypatch)
    open_hours = [True]
    client.is_mining_time = lambda: open_hours[0]
    client.fetch_inventory = AsyncMock(side_effect=GQLRequestFailed())

    async def close_hours(seconds):
        open_hours[0] = False

    client._wait_inventory_retry = close_hours
    assert not await client._fetch_inventory_with_recovery()
    assert client._inventory_refresh_pending
    assert client.fetch_inventory.await_count == 1


@pytest.mark.asyncio
async def test_cancel_during_recovery_does_not_start_another_fetch(tmp_path, monkeypatch):
    from src.exceptions import GQLRequestFailed

    client = miner(tmp_path, monkeypatch)
    client.is_mining_time = lambda: True
    client.fetch_inventory = AsyncMock(side_effect=GQLRequestFailed())
    waiting = asyncio.Event()

    async def wait(seconds):
        waiting.set()
        await asyncio.Event().wait()

    client._wait_inventory_retry = wait
    task = asyncio.create_task(client._fetch_inventory_with_recovery())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.fetch_inventory.await_count == 1
