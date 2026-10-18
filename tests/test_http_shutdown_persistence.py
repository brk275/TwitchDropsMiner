"""Persistence errors must not leak HTTP resources after miner-owner draining."""

import asyncio
import errno
import pickle
from unittest.mock import MagicMock

import pytest

from src.config import ClientType
from tests.test_http_client_close import transport as transport
from tests.test_shutdown_mining_ownership import owners, shutdown_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    PermissionError("test save denied"),
    OSError(errno.ENOSPC, "test filesystem full"),
    pickle.PicklingError("test serializer failed"),
])
@pytest.mark.parametrize("cancel_caller", [False, True])
@pytest.mark.parametrize("owner_failure", [False, True])
async def test_drained_shutdown_closes_http_after_cookie_failure(
    tmp_path, monkeypatch, transport, failure, cancel_caller, owner_failure,
):
    client, browser_closed, _ = shutdown_runtime(tmp_path, monkeypatch)
    http, session = transport
    connector = session.connector
    client._http_client = http
    save = MagicMock(side_effect=failure)
    monkeypatch.setattr(session.cookie_jar, "save", save)
    release = asyncio.Event()
    tasks, draining, observed = await owners(
        client, ("watch", "maintenance"), release, failure=owner_failure,
    )
    shutdown = asyncio.create_task(client.shutdown())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in draining)), 1)
        assert not session.closed and not connector.closed and not browser_closed.is_set()
        save.assert_not_called()
        if cancel_caller:
            for _ in range(2):
                shutdown.cancel("test shutdown caller cancellation")
                await asyncio.sleep(0)
            assert not shutdown.done()
        release.set()
        if cancel_caller:
            with pytest.raises(asyncio.CancelledError, match="test shutdown caller cancellation"):
                await asyncio.wait_for(shutdown, 2)
            assert not session.closed and not browser_closed.is_set()
            save.assert_not_called()
            shutdown = asyncio.create_task(client.shutdown())

        with pytest.raises(type(failure)) as raised:
            await asyncio.wait_for(shutdown, 2)
        assert raised.value is failure
        assert session.closed and connector.closed and http._session is None
        assert all(task.done() for task in tasks)
        assert sorted(observed) == [("maintenance", 42), ("watch", 42)]
        assert client._watching_task is None and client._mnt_task is None
        # Storage failure still propagates before the existing identity-clear boundary.
        assert client._auth_state.user_id == 42
        save.assert_called_once()
        await asyncio.wait_for(client.shutdown(), 2)
        save.assert_called_once()
        assert not hasattr(client._auth_state, "user_id")
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, shutdown, return_exceptions=True)


@pytest.mark.asyncio
async def test_browser_mode_shutdown_drains_without_legacy_cookie_write(
    tmp_path, monkeypatch, transport,
):
    client, _, _ = shutdown_runtime(tmp_path, monkeypatch)
    http, session = transport
    client._http_client = http
    http.enable_browser_mode(ClientType.WEB)
    save = MagicMock(side_effect=AssertionError("No legacy save in browser mode"))
    monkeypatch.setattr(session.cookie_jar, "save", save)
    release = asyncio.Event()
    tasks, draining, observed = await owners(client, ("watch", "maintenance"), release)
    shutdown = asyncio.create_task(client.shutdown())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in draining)), 1)
        assert not session.closed
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert session.closed and http._session is None
        assert sorted(observed) == [("maintenance", 42), ("watch", 42)]
        save.assert_not_called()
        assert not hasattr(client._auth_state, "user_id")
    finally:
        release.set()
        await asyncio.gather(*tasks, shutdown, return_exceptions=True)
