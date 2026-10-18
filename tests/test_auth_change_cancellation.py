"""Cancelled account changes drain all old owners before clearing or resuming."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.web.managers.login import LoginFormManager
from tests.test_session_controller import controller as controller
from tests.test_session_controller import seed
from tests.test_session_lifecycle import miner
from tests.test_shutdown_mining_ownership import owners, shutdown_runtime
from tests.test_websocket_shutdown_ownership import attach
from tests.test_websocket_shutdown_ownership import runtime as runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_change_preserves_old_identity_until_all_drains(tmp_path, monkeypatch, cancellations):
    client, _, _ = shutdown_runtime(tmp_path, monkeypatch)
    release, activated = asyncio.Event(), asyncio.Event()
    tasks, draining, cleanup_ids = await owners(client, ("watch", "maintenance"), release)
    steps = []

    async def websocket(**kwargs):
        steps.append(("websocket", client._auth_state.user_id))

    async def channels():
        steps.append(("channels", client._auth_state.user_id))

    async def avatar():
        steps.append(("avatar", client._auth_state.user_id))

    client.websocket.stop = AsyncMock(side_effect=websocket)
    client._stop_channel_tasks = AsyncMock(side_effect=channels)
    client.gui.login.stop_avatar = AsyncMock(side_effect=avatar)

    async def change():
        async with client.authentication_change():
            activated.set()

    replacement = asyncio.create_task(change())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in draining)), 1)
        for i in range(cancellations):
            replacement.cancel("first cancellation" if i == 0 else "later cancellation")
            await asyncio.sleep(0)
        assert not replacement.done() and not client._resume_mining.is_set()
        assert client._changing_auth and client._auth_state.user_id == 42
        assert all(not task.done() for task in tasks)
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await asyncio.wait_for(replacement, 2)
        assert sorted(cleanup_ids) == [("maintenance", 42), ("watch", 42)]
        assert steps == [("websocket", 42), ("channels", 42), ("avatar", 42)]
        assert not activated.is_set() and not hasattr(client._auth_state, "user_id")
        assert client._resume_mining.is_set() and not client._changing_auth
        assert client._watching_task is None and client._mnt_task is None
    finally:
        release.set()
        await asyncio.gather(*tasks, replacement, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_change_drains_real_callback_before_clearing_identity(runtime):
    client, drop = runtime.twitch, runtime.drop
    release, claim_release, entered, draining = [asyncio.Event() for _ in range(4)]
    tasks, worker_draining, _ = await owners(client, ("watch",), release)
    identities = []

    async def query(operation):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await claim_release.wait()
            identities.append(client._auth_state.user_id)

    client.gql_request = AsyncMock(side_effect=query)

    async def callback(*args):
        await drop.claim()

    ws, _, callback_task = await attach(runtime, callback)
    await asyncio.wait_for(entered.wait(), 1)
    admitted = asyncio.Event()

    async def change():
        async with client.authentication_change():
            admitted.set()

    replacement = asyncio.create_task(change())
    try:
        await asyncio.wait_for(worker_draining[0].wait(), 1)
        replacement.cancel()
        release.set()
        await asyncio.wait_for(draining.wait(), 1)
        assert not replacement.done() and client._auth_state.user_id == 42
        replacement.cancel()
        await asyncio.sleep(0)
        assert not callback_task.done() and not admitted.is_set()
        claim_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(replacement, 2)
        assert identities == [42] and callback_task.cancelled() and not ws.topics
        assert not drop.is_claimed and client.drop_history.total_count == 0
        assert not hasattr(client._auth_state, "user_id") and not admitted.is_set()
        client.gql_request.assert_awaited_once()
    finally:
        release.set()
        claim_release.set()
        callback_task.cancel()
        await asyncio.gather(*tasks, callback_task, replacement, return_exceptions=True)
        await runtime.pool.stop(clear_topics=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["websocket", "channels", "avatar"])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_all_cleanup_phases_finish_before_error_or_cancellation(tmp_path, monkeypatch, phase, failure, cancel):
    client, _, _ = shutdown_runtime(tmp_path, monkeypatch)
    entered, release, admitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    steps = []
    error = RuntimeError("test cleanup failure")

    def cleanup(name):
        async def run(*args, **kwargs):
            steps.append((name, client._auth_state.user_id))
            if name == phase:
                entered.set()
                await release.wait()
                if failure:
                    raise error
        return AsyncMock(side_effect=run)

    client.websocket.stop = cleanup("websocket")
    client._stop_channel_tasks = cleanup("channels")
    client.gui.login.stop_avatar = cleanup("avatar")

    async def change():
        async with client.authentication_change():
            admitted.set()

    replacement = asyncio.create_task(change())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            replacement.cancel("phase caller cancellation")
            await asyncio.sleep(0)
            replacement.cancel("second caller cancellation")
            await asyncio.sleep(0)
        assert not replacement.done() and client._auth_state.user_id == 42
        assert not client._resume_mining.is_set()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="phase caller cancellation"):
                await asyncio.wait_for(replacement, 2)
        elif failure:
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(replacement, 2)
            assert caught.value is error
        else:
            await asyncio.wait_for(replacement, 2)
        assert steps == [("websocket", 42), ("channels", 42), ("avatar", 42)]
        assert admitted.is_set() is (not cancel and not failure)
        assert not hasattr(client._auth_state, "user_id") and client._resume_mining.is_set()
    finally:
        release.set()
        await asyncio.gather(replacement, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["channels", "avatar"])
async def test_already_draining_account_requests_are_not_cancelled_twice(tmp_path, monkeypatch, kind):
    client, _, _ = shutdown_runtime(tmp_path, monkeypatch)
    release = asyncio.Event()
    tasks, draining, observed = await owners(client, ("watch",), release)
    client._watching_task = None
    task, = tasks
    if kind == "channels":
        client._channel_tasks.add(task)
    else:
        client.gui.login = LoginFormManager(MagicMock(), client.gui)
        client.gui.login._avatar_tasks.add(task)
    task.cancel()
    await asyncio.wait_for(draining[0].wait(), 1)

    async def change():
        async with client.authentication_change():
            assert task.done()

    replacement = asyncio.create_task(change())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not replacement.done() and task.cancelling() == 1
        release.set()
        await asyncio.wait_for(replacement, 2)
        assert observed == [("watch", 42)]
    finally:
        release.set()
        await asyncio.gather(task, replacement, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_caller_is_not_joined_from_owned_drain_task(tmp_path, monkeypatch):
    client = miner(tmp_path, monkeypatch)

    async def change():
        client._run_task = asyncio.current_task()
        async with client.authentication_change():
            pass

    await asyncio.wait_for(change(), 1)


@pytest.mark.asyncio
async def test_cancelled_controller_admission_never_installs_replacement(controller, tmp_path, monkeypatch):
    control, session, _, _ = controller
    await control.accept(seed(), authorized=lambda: True)
    before = session.path.read_bytes()
    client = miner(tmp_path, monkeypatch)
    client._auth_state.user_id = 42
    control.activation = client.authentication_change
    session.install = AsyncMock(wraps=session.install)
    release = asyncio.Event()
    tasks, draining, observed = await owners(client, ("watch",), release)
    replacement = asyncio.create_task(control.accept(seed(), authorized=lambda: True))
    try:
        await asyncio.wait_for(draining[0].wait(), 1)
        replacement.cancel("cancel installation")
        await asyncio.sleep(0)
        assert not replacement.done() and client._auth_state.user_id == 42
        release.set()
        with pytest.raises(asyncio.CancelledError, match="cancel installation"):
            await asyncio.wait_for(replacement, 2)
        session.install.assert_not_awaited()
        assert session.path.read_bytes() == before and session.status()["generation"] == 1
        assert observed == [("watch", 42)] and not control._lock.locked()
    finally:
        release.set()
        await asyncio.gather(*tasks, replacement, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_owned_cancellation_still_runs_later_phases(tmp_path, monkeypatch):
    client, _, _ = shutdown_runtime(tmp_path, monkeypatch)
    client.websocket.stop = AsyncMock(side_effect=asyncio.CancelledError("owned cleanup cancellation"))
    client._stop_channel_tasks = AsyncMock()
    admitted = False
    with pytest.raises(asyncio.CancelledError, match="owned cleanup cancellation"):
        async with client.authentication_change():
            admitted = True
    assert not admitted
    client._stop_channel_tasks.assert_awaited_once_with()
    client.gui.login.stop_avatar.assert_awaited_once_with()
    assert client._resume_mining.is_set() and not hasattr(client._auth_state, "user_id")


@pytest.mark.asyncio
async def test_logout_waits_for_cancelled_admission_then_persists_logout(controller, tmp_path, monkeypatch):
    control, session, _, _ = controller
    await control.accept(seed(), authorized=lambda: True)
    client = miner(tmp_path, monkeypatch)
    client._auth_state.user_id = 42
    control.activation = client.authentication_change
    release = asyncio.Event()
    tasks, draining, observed = await owners(client, ("watch",), release)
    replacement = asyncio.create_task(control.accept(seed(), authorized=lambda: True))
    logout = None
    clear_cookies = AsyncMock()
    try:
        await asyncio.wait_for(draining[0].wait(), 1)
        logout = asyncio.create_task(control.logout(clear_cookies))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not logout.done() and not replacement.done()
        assert client._auth_state.user_id == 42 and not session.logged_out
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(replacement, 2)
        await asyncio.wait_for(logout, 2)
        assert observed == [("watch", 42)] and session.logged_out
        assert session.seed() is None and not control._lock.locked()
        clear_cookies.assert_awaited_once_with()
    finally:
        release.set()
        await asyncio.gather(*tasks, replacement, *([logout] if logout else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["websocket", "channels", "avatar"])
async def test_simultaneous_owned_and_caller_cancellation_keeps_caller_message(tmp_path, monkeypatch, phase):
    client, _, _ = shutdown_runtime(tmp_path, monkeypatch)
    steps = []

    def cleanup(name):
        async def run(*args, **kwargs):
            steps.append((name, client._auth_state.user_id))
            if name == phase:
                replacement.cancel("first caller cancellation")
                raise asyncio.CancelledError("owned cleanup cancellation")
        return AsyncMock(side_effect=run)

    client.websocket.stop = cleanup("websocket")
    client._stop_channel_tasks = cleanup("channels")
    client.gui.login.stop_avatar = cleanup("avatar")

    async def change():
        async with client.authentication_change():
            raise AssertionError("Cancelled replacement admitted")

    replacement = asyncio.create_task(change())
    with pytest.raises(asyncio.CancelledError, match="first caller cancellation"):
        await asyncio.wait_for(replacement, 2)
    assert steps == [("websocket", 42), ("channels", 42), ("avatar", 42)]
    assert not hasattr(client._auth_state, "user_id") and client._resume_mining.is_set()
