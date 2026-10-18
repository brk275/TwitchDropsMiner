"""Shutdown must join its workers before closing their shared resources."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from src.config import State
from src.core.client import Twitch


@pytest.fixture
def miner(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.client.DATA_DIR", tmp_path)
    monkeypatch.setattr("src.api.http_client.COOKIES_PATH", tmp_path / "cookies.jar")
    client = Twitch(SimpleNamespace(proxy="", connection_quality=1))
    client.gui = MagicMock()
    client.gui.login.stop_avatar = AsyncMock()
    client.helper.stop = AsyncMock()
    client.login_browser.stop = AsyncMock()
    client.session_controller.stop = AsyncMock()
    client.websocket.stop = AsyncMock()
    client._browser.close = AsyncMock()
    client._state = State.EXIT
    client._auth_state.user_id = 42
    return client


async def worker(entered, cleaning, release, cleaned, finish=None):
    entered.set()
    try:
        await asyncio.Event().wait()
    finally:
        cleaning.set()
        await release.wait()
        if finish is not None:
            await finish()
        cleaned.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("_watching_task",), ("_mnt_task",), ("_watching_task", "_mnt_task")])
async def test_shutdown_waits_for_workers_before_closing_http_and_clearing_account(miner, names):
    app = web.Application()

    async def respond(request):
        return web.Response(text="cleanup complete")

    app.router.add_get("/cleanup", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    session = await miner.get_session()
    requests = []

    async def finish():
        async with session.get(f"http://127.0.0.1:{port}/cleanup") as response:
            requests.append(await response.text())

    entered = [asyncio.Event() for _ in names]
    cleaning = [asyncio.Event() for _ in names]
    release = [asyncio.Event() for _ in names]
    cleaned = [asyncio.Event() for _ in names]
    tasks = [asyncio.create_task(worker(*events, finish))
             for events in zip(entered, cleaning, release, cleaned, strict=True)]
    for name, task in zip(names, tasks, strict=True):
        setattr(miner, name, task)
    stopping = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 5)
        stopping = asyncio.create_task(miner.shutdown())
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in cleaning)), 5)
        assert not session.closed
        assert miner._auth_state.user_id == 42
        miner.websocket.stop.assert_not_awaited()
        miner.gui.login.stop_avatar.assert_not_awaited()
        for name, task in zip(names, tasks, strict=True):
            assert getattr(miner, name) is task
        if len(tasks) == 2:
            release[0].set()
            await asyncio.wait_for(cleaned[0].wait(), 5)
            assert not session.closed and not stopping.done()
            assert not cleaned[1].is_set()
        for event in release:
            event.set()
        await asyncio.wait_for(stopping, 5)
        assert all(task.done() for task in tasks)
        assert all(event.is_set() for event in cleaned)
        assert requests == ["cleanup complete"] * len(tasks)
        assert miner._watching_task is None and miner._mnt_task is None
        assert session.closed
        assert not hasattr(miner._auth_state, "user_id")
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*(task for task in (stopping, *tasks) if task is not None), return_exceptions=True)
        await session.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["absent", "success", "error", "cancelled"])
async def test_shutdown_handles_absent_and_already_finished_workers(miner, outcome):
    tasks = []
    if outcome != "absent":
        async def finished():
            if outcome == "error":
                raise RuntimeError("synthetic worker failure")
            if outcome == "cancelled":
                raise asyncio.CancelledError

        tasks = [asyncio.create_task(finished()) for _ in range(2)]
        miner._watching_task, miner._mnt_task = tasks
        await asyncio.wait(tasks)
    try:
        await asyncio.wait_for(miner.shutdown(), 5)
        assert miner._watching_task is None and miner._mnt_task is None
        assert all(task.done() for task in tasks)
        miner.websocket.stop.assert_awaited_once_with(clear_topics=True)
        assert miner._http_client is None
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_worker_ownership_for_retry(miner):
    entered, cleaning, release, recancelled, cleaned = (asyncio.Event() for _ in range(5))

    async def slow_cleanup():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    recancelled.set()
            cleaned.set()

    task = miner._watching_task = asyncio.create_task(slow_cleanup())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        stopping = asyncio.create_task(miner.shutdown())
        await asyncio.wait_for(cleaning.wait(), 5)
        stopping.cancel()
        await asyncio.wait_for(recancelled.wait(), 5)
        assert not stopping.done()
        assert miner._watching_task is task
        miner.websocket.stop.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 5)
        assert task.done() and cleaned.is_set()
        assert miner._watching_task is task
        await asyncio.wait_for(miner.shutdown(), 5)
        assert miner._watching_task is None and miner._mnt_task is None
        miner.websocket.stop.assert_awaited_once_with(clear_topics=True)
    finally:
        release.set()
        await asyncio.gather(*(item for item in (stopping, task) if item is not None), return_exceptions=True)
