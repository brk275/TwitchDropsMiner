"""Account changes must drain callbacks from compacted-away websocket owners too."""

import asyncio
import gc
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import WebsocketTopic
from src.websocket.websocket import Websocket
from tests.test_inventory_refresh_races import installed
from tests.test_session_lifecycle import miner


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    twitch = miner(tmp_path, monkeypatch)
    owner = installed(twitch, tmp_path)
    twitch._mnt_task = None
    twitch.gui.login.stop_avatar = AsyncMock()
    twitch._auth_state.user_id = 42
    return SimpleNamespace(twitch=twitch, owner=owner, pool=twitch.websocket)


async def attach(runtime, callback):
    pool = runtime.pool
    ws = Websocket(pool, len(pool.websockets))
    topic = WebsocketTopic("User", "Drops", 42 + len(pool.websockets), callback)
    ws.add_topics({topic})
    pool.websockets.append(ws)
    ws._handle_message({"data": {"topic": str(topic), "message": json.dumps({"type": "test"})}})
    task, = ws._callback_tasks
    return ws, topic, task


def retire(pool, topic):
    before = asyncio.all_tasks()
    pool.remove_topics([str(topic)])
    return asyncio.all_tasks() - before


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_topics", [False, True])
async def test_active_socket_stop_still_drains_and_respects_topic_retention(runtime, clear_topics):
    entered = asyncio.Event()

    async def callback(*args):
        entered.set()
        await asyncio.Event().wait()

    ws, topic, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await runtime.pool.stop(clear_topics=clear_topics)
        await asyncio.sleep(0)
        assert callback_task.cancelled() and not ws._callback_tasks
        assert runtime.pool.websockets == [ws]
        assert bool(ws.topics) is not clear_topics
        if not clear_topics:
            assert ws.topics == {str(topic): topic}
    finally:
        callback_task.cancel()
        await asyncio.gather(callback_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_topics", [False, True])
async def test_pool_stop_waits_for_removed_socket_callback_cleanup(runtime, clear_topics):
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def callback(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    ws, topic, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    retired = retire(runtime.pool, topic)
    stop = None
    try:
        await asyncio.wait_for(draining.wait(), 1)
        assert not runtime.pool.websockets and len(retired) == 1
        stop = asyncio.create_task(runtime.pool.stop(clear_topics=clear_topics))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stop.done(), "Pool stop returned before the retired callback drained"
        assert not callback_task.done()
        release.set()
        await asyncio.wait_for(stop, 1)
        await asyncio.sleep(0)
        assert callback_task.cancelled() and not ws._callback_tasks
        assert all(task.done() for task in retired) and not ws.topics
    finally:
        release.set()
        callback_task.cancel()
        await asyncio.gather(callback_task, *retired, *([stop] if stop else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_real_authentication_change_cannot_cross_retired_pending_claim(runtime):
    twitch, owner = runtime.twitch, runtime.owner
    entered, draining, release, activated = [asyncio.Event() for _ in range(4)]
    cleanup_identities = []

    async def query(operation):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            cleanup_identities.append(getattr(twitch._auth_state, "user_id", None))

    twitch.gql_request = AsyncMock(side_effect=query)

    async def callback(*args):
        await owner.drop.claim()

    ws, topic, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    assert twitch._claim_requests == 1 and owner.drop.claim_pending
    retired = retire(runtime.pool, topic)

    async def change():
        async with twitch.authentication_change():
            assert twitch._claim_requests == 0 and not owner.drop.claim_pending
            activated.set()

    replacement = None
    try:
        await asyncio.wait_for(draining.wait(), 1)
        replacement = asyncio.create_task(change())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not replacement.done() and not activated.is_set()
        assert twitch._auth_state.user_id == 42 and twitch._changing_auth
        release.set()
        await asyncio.wait_for(replacement, 1)
        assert activated.is_set() and cleanup_identities == [42]
        assert callback_task.cancelled() and not ws._callback_tasks
        assert twitch._claim_requests == 0 and not owner.drop.claim_pending
        assert not owner.drop.is_claimed and twitch.drop_history.total_count == 0
        assert not hasattr(twitch._auth_state, "user_id") and not twitch._changing_auth
        assert twitch._resume_mining.is_set()
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        callback_task.cancel()
        await asyncio.gather(callback_task, *retired, *([replacement] if replacement else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["close", "handler"])
async def test_socket_stop_drains_callbacks_before_propagating_shutdown_failure(runtime, failure):
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    error = RuntimeError("private-websocket-canary")

    async def callback(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    ws, _, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "close":
        ws._ws.set(SimpleNamespace(close=AsyncMock(side_effect=error)))
    else:
        async def handle():
            raise error
        ws._handle_task = asyncio.create_task(handle())
        await asyncio.sleep(0)
    stop = asyncio.create_task(ws.stop(remove=True))
    try:
        await asyncio.wait_for(draining.wait(), 1)
        assert not stop.done() and not callback_task.done()
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await asyncio.wait_for(stop, 1)
        assert caught.value is error
        assert callback_task.cancelled() and not ws._callback_tasks
        assert ws._handle_task is None and not ws.connected and not ws.topics
    finally:
        release.set()
        callback_task.cancel()
        await asyncio.gather(callback_task, stop, return_exceptions=True)
        if ws._handle_task is not None:
            ws._handle_task.cancel()
            await asyncio.gather(ws._handle_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pool_waits_for_other_sockets_before_propagating_one_failure(runtime):
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    error = RuntimeError("private-websocket-canary")

    async def done(*args):
        pass

    broken, _, completed = await attach(runtime, done)
    await completed
    broken._ws.set(SimpleNamespace(close=AsyncMock(side_effect=error)))

    async def callback(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    healthy, _, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    stop = asyncio.create_task(runtime.pool.stop(clear_topics=True))
    try:
        await asyncio.wait_for(draining.wait(), 1)
        await asyncio.sleep(0)
        assert not stop.done() and not callback_task.done()
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await asyncio.wait_for(stop, 1)
        assert caught.value is error and callback_task.cancelled()
        assert not broken.connected and not healthy.connected
        assert not broken.topics and not healthy.topics
    finally:
        release.set()
        callback_task.cancel()
        await asyncio.gather(callback_task, stop, return_exceptions=True)


@pytest.mark.asyncio
async def test_retirement_admitted_during_pool_stop_is_also_joined(runtime):
    entered, draining, release, late_entered, late_release = [asyncio.Event() for _ in range(5)]

    async def callback(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    ws, topic, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    original, calls = ws.stop, []

    async def stop_socket(*, remove=False):
        calls.append(remove)
        if len(calls) == 2:
            late_entered.set()
            await late_release.wait()
        await original(remove=remove)

    ws.stop = stop_socket
    stop = asyncio.create_task(runtime.pool.stop())
    retired = set()
    try:
        await asyncio.wait_for(draining.wait(), 1)
        retired = retire(runtime.pool, topic)
        await asyncio.wait_for(late_entered.wait(), 1)
        release.set()
        await asyncio.wait_for(callback_task_done(callback_task), 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stop.done() and any(not task.done() for task in retired)
        late_release.set()
        await asyncio.wait_for(stop, 1)
        assert calls == [False, True] and not ws.topics
        assert all(task.done() for task in retired)
    finally:
        release.set()
        late_release.set()
        callback_task.cancel()
        await asyncio.gather(callback_task, stop, *retired, return_exceptions=True)


async def callback_task_done(task):
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_close_still_drains_receive_and_callback_tasks(runtime):
    entered, closing, draining, release = [asyncio.Event() for _ in range(4)]

    async def callback(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    async def close():
        closing.set()
        await asyncio.Event().wait()

    ws, _, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    handle = ws._handle_task = asyncio.create_task(asyncio.Event().wait())
    ws._ws.set(SimpleNamespace(close=close))
    stop = asyncio.create_task(ws.stop(remove=True))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        stop.cancel()
        await asyncio.wait_for(draining.wait(), 1)
        assert not stop.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 1)
        assert handle.cancelled() and callback_task.cancelled()
        assert ws._handle_task is None and not ws.connected and not ws.topics
    finally:
        release.set()
        callback_task.cancel()
        handle.cancel()
        await asyncio.gather(callback_task, handle, stop, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_stop_nowait_returns_the_joinable_shutdown_task(runtime, remove):
    entered = asyncio.Event()

    async def callback(*args):
        entered.set()
        await asyncio.Event().wait()

    ws, _, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    stop = ws.stop_nowait(remove=remove)
    try:
        assert isinstance(stop, asyncio.Task)
        await asyncio.wait_for(stop, 1)
        assert callback_task.cancelled() and bool(ws.topics) is not remove
    finally:
        callback_task.cancel()
        await asyncio.gather(callback_task, *([stop] if stop is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_background_retirement_failure_is_observed_and_log_omits_payload(runtime, caplog):
    async def done(*args):
        pass

    ws, topic, callback_task = await attach(runtime, done)
    await callback_task
    ws._ws.set(SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("private-websocket-canary"))))
    loop = asyncio.get_running_loop()
    previous, unhandled = loop.get_exception_handler(), []
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    retired = retire(runtime.pool, topic)
    try:
        await asyncio.wait(retired, timeout=1)
        await asyncio.sleep(0)
        assert all(task.done() for task in retired)
        assert not runtime.pool._retired_stop_tasks
        assert "Failed to stop a retired websocket" in caplog.text
        assert "private-websocket-canary" not in caplog.text
        retired.clear()
        gc.collect()
        await asyncio.sleep(0)
        assert not unhandled
    finally:
        await asyncio.gather(*retired, return_exceptions=True)
        loop.set_exception_handler(previous)
