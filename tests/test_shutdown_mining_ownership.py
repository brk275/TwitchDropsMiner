"""Shutdown must join watch/maintenance owners before transport and identity teardown."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_session_lifecycle import miner


def shutdown_runtime(tmp_path, monkeypatch):
    client = miner(tmp_path, monkeypatch)
    client._auth_state.user_id = 42
    client.helper.stop = AsyncMock()
    client.login_browser.stop = AsyncMock()
    client.session_controller.stop = AsyncMock()
    closed = asyncio.Event()
    observed = []

    async def close():
        observed.append(getattr(client._auth_state, "user_id", None))
        closed.set()

    client._browser.close = AsyncMock(side_effect=close)
    client._http_client = SimpleNamespace(close=AsyncMock(side_effect=close))
    return client, closed, observed


async def owners(client, kinds, release, failure=False):
    tasks, entered, draining, observed = [], [], [], []
    for kind in kinds:
        ready, cleanup = asyncio.Event(), asyncio.Event()

        async def work(kind=kind, ready=ready, cleanup=cleanup):
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                observed.append((kind, getattr(client._auth_state, "user_id", None)))
                if failure:
                    raise RuntimeError("test owner cleanup failure")

        task = asyncio.create_task(work())
        setattr(client, "_watching_task" if kind == "watch" else "_mnt_task", task)
        tasks.append(task)
        entered.append(ready)
        draining.append(cleanup)
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
    return tasks, draining, observed


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds", [("watch",), ("maintenance",), ("watch", "maintenance")])
@pytest.mark.parametrize("failure", [False, True])
async def test_shutdown_drains_owned_cleanup_before_closing_or_clearing(tmp_path, monkeypatch, kinds, failure):
    client, closed, transport_ids = shutdown_runtime(tmp_path, monkeypatch)
    release = asyncio.Event()
    tasks, draining, cleanup_ids = await owners(client, kinds, release, failure)
    shutdown = asyncio.create_task(client.shutdown())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in draining)), 1)
        assert not closed.is_set(), "Transport closed before mining owners finished cleanup"
        assert client._auth_state.user_id == 42 and not shutdown.done()
        assert all(not task.done() for task in tasks)
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert all(task.done() for task in tasks)
        assert sorted(cleanup_ids) == sorted((kind, 42) for kind in kinds)
        assert transport_ids == [42, 42]
        assert client._watching_task is None and client._mnt_task is None
        assert not hasattr(client._auth_state, "user_id")
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, shutdown, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_caller_cancellation_waits_for_owners_and_allows_teardown_retry(tmp_path, monkeypatch, cancellations):
    client, closed, transport_ids = shutdown_runtime(tmp_path, monkeypatch)
    release = asyncio.Event()
    tasks, draining, cleanup_ids = await owners(client, ("watch", "maintenance"), release)
    shutdown = asyncio.create_task(client.shutdown())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in draining)), 1)
        for _ in range(cancellations):
            shutdown.cancel("test caller cancellation")
            await asyncio.sleep(0)
        assert not shutdown.done() and not closed.is_set()
        assert all(not task.done() for task in tasks)
        assert client._auth_state.user_id == 42
        release.set()
        with pytest.raises(asyncio.CancelledError, match="test caller cancellation"):
            await asyncio.wait_for(shutdown, 2)
        assert all(task.done() for task in tasks)
        assert sorted(cleanup_ids) == [("maintenance", 42), ("watch", 42)]
        assert not closed.is_set() and client._auth_state.user_id == 42
        assert client._watching_task is None and client._mnt_task is None
        await asyncio.wait_for(client.shutdown(), 2)
        assert transport_ids == [42, 42] and not hasattr(client._auth_state, "user_id")
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, shutdown, return_exceptions=True)


@pytest.mark.asyncio
async def test_already_cancelling_owner_is_joined_without_interrupting_cleanup(tmp_path, monkeypatch):
    client, closed, _ = shutdown_runtime(tmp_path, monkeypatch)
    release = asyncio.Event()
    tasks, draining, observed = await owners(client, ("watch", "maintenance"), release)
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in draining)), 1)
    shutdown = asyncio.create_task(client.shutdown())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closed.is_set() and not shutdown.done()
        assert all(task.cancelling() == 1 and not task.done() for task in tasks)
        assert client._watching_task is tasks[0] and client._mnt_task is tasks[1]
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert sorted(observed) == [("maintenance", 42), ("watch", 42)]
    finally:
        release.set()
        await asyncio.gather(*tasks, shutdown, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["absent", "complete", "failed", "cancelled", "shared"])
async def test_completed_or_duplicate_owners_do_not_block_shutdown(tmp_path, monkeypatch, ending):
    client, _, transport_ids = shutdown_runtime(tmp_path, monkeypatch)
    task = None
    if ending != "absent":
        async def done():
            if ending == "failed":
                raise RuntimeError("private owner failure")

        task = asyncio.create_task(done())
        if ending == "cancelled":
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        client._watching_task = task
        if ending == "shared":
            client._mnt_task = task
    await asyncio.wait_for(client.shutdown(), 2)
    assert task is None or task.done()
    assert transport_ids == [42, 42]
    assert client._watching_task is None and client._mnt_task is None


@pytest.mark.asyncio
async def test_current_task_is_not_cancelled_or_joined_by_shutdown(tmp_path, monkeypatch):
    client, _, transport_ids = shutdown_runtime(tmp_path, monkeypatch)

    async def shutdown():
        client._watching_task = asyncio.current_task()
        await client.shutdown()

    task = asyncio.create_task(shutdown())
    await asyncio.wait_for(task, 2)
    assert not task.cancelled() and transport_ids == [42, 42]
