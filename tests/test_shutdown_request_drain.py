"""Real watch polling drains imported HTTP cleanup before final session closure."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_imported_session_retry import running_miner


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_shutdown", [False, True])
async def test_shutdown_drains_real_watch_request_before_clearing_session(tmp_path, monkeypatch, cancel_shutdown):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        client.helper.stop = AsyncMock()
        client.login_browser.stop = AsyncMock()
        client.session_controller.stop = AsyncMock()
        client.is_mining_time = lambda: True
        channel = MagicMock(id=123, online=True, banned=False)
        channel.send_watch = AsyncMock(return_value=True)
        channel._watch_current.return_value = True
        client.watching_channel.set(channel)
        clock = [0]
        monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])

        async def next_poll(delay):
            clock[0] += 30

        client._watch_service.watch_sleep = next_poll
        http = client._browser._transport._http
        request = http.request
        entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observations = []

        @asynccontextmanager
        async def held_request(*args, **kwargs):
            async with request(*args, **kwargs) as response:
                entered.set()
                try:
                    await asyncio.Event().wait()
                    yield response
                finally:
                    draining.set()
                    await release.wait()
                    observations.append((client._auth_state.user_id, http.closed))

        monkeypatch.setattr(http, "request", held_request)
        peer.responses.append({"data": {"currentUser": {"dropCurrentSession": None}}})
        watch = client._watching_task = asyncio.create_task(client._watch_service.watch_loop())
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            shutdown = asyncio.create_task(client.shutdown())
            await asyncio.wait_for(draining.wait(), 1)
            if cancel_shutdown:
                shutdown.cancel("test shutdown cancellation")
                await asyncio.sleep(0)
            assert not http.closed and client._auth_state.user_id == 42
            assert not shutdown.done() and not watch.done()
            release.set()
            if cancel_shutdown:
                with pytest.raises(asyncio.CancelledError, match="test shutdown cancellation"):
                    await asyncio.wait_for(shutdown, 2)
                assert not http.closed and client._auth_state.user_id == 42
                await client.shutdown()
            else:
                await asyncio.wait_for(shutdown, 2)
            assert watch.cancelled() and observations == [(42, False)]
            assert http.closed and not hasattr(client._auth_state, "user_id")
            assert client.drop_history.get_entries() == []
            assert len(peer.calls) == 1
            assert peer.calls[0]["operationName"] == "DropCurrentSessionContext"
        finally:
            release.set()
            watch.cancel()
            await asyncio.gather(watch, *([shutdown] if shutdown else []), return_exceptions=True)
