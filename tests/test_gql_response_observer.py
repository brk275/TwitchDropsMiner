"""Receipt observers are request-owned, synchronous and inert after scope exit."""

import asyncio
import gc
import weakref
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.utils.gql_response_observer import GQLResponseObserver


def test_observer_is_inert_outside_scope_and_preserves_payload(monkeypatch):
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    clock = MagicMock()
    clock.now.return_value = now
    monkeypatch.setattr("src.utils.gql_response_observer.datetime", clock)
    payload, captured = {"data": {"value": 7}}, []
    observer = GQLResponseObserver(lambda row, stamp: captured.append((row, stamp)))
    GQLResponseObserver.record(payload)
    with observer.observe():
        GQLResponseObserver.record(payload)
    GQLResponseObserver.record(payload)
    assert captured == [(payload, now)] and captured[0][0] is payload
    assert payload == {"data": {"value": 7}}
    clock.now.assert_called_once_with(timezone.utc)


def test_nested_observers_restore_parent_and_cannot_be_reused():
    first, second = [], []
    outer = GQLResponseObserver(lambda row, stamp: first.append(row))
    inner = GQLResponseObserver(lambda row, stamp: second.append(row))
    with outer.observe():
        GQLResponseObserver.record(1)
        with inner.observe():
            with pytest.raises(RuntimeError, match="cannot be reused"), inner.observe():
                pytest.fail("Nested reuse must fail")
            GQLResponseObserver.record(2)
        GQLResponseObserver.record(3)
    with pytest.raises(RuntimeError, match="cannot be reused"), outer.observe():
        pytest.fail("Closed observer must not reactivate")
    GQLResponseObserver.record(4)
    assert first == [1, 3] and second == [2]


@pytest.mark.asyncio
async def test_overlapping_tasks_have_independent_observers():
    ready = [asyncio.Event(), asyncio.Event()]
    captured = [[], []]

    async def request(index):
        with GQLResponseObserver(lambda row, stamp: captured[index].append(row)).observe():
            ready[index].set()
            await ready[1 - index].wait()
            GQLResponseObserver.record(index)

    await asyncio.gather(request(0), request(1))
    GQLResponseObserver.record(3)
    assert captured == [[0], [1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_inherited_child_context_cannot_retain_or_reactivate_callback(failure):
    ready, release = asyncio.Event(), asyncio.Event()
    captured = []

    class Receiver:
        def observe(self, row, stamp):
            captured.append(row)

    receiver = Receiver()
    reference = weakref.ref(receiver)
    observer = GQLResponseObserver(receiver.observe)

    async def child():
        GQLResponseObserver.record("inside")
        ready.set()
        await release.wait()
        GQLResponseObserver.record("after")

    task = None
    try:
        try:
            with observer.observe():
                task = asyncio.create_task(child())
                await ready.wait()
                if failure is not None:
                    raise failure()
        except BaseException as error:
            assert failure is not None and type(error) is failure
        del receiver
        gc.collect()
        assert reference() is None
        release.set()
        await task
        assert captured == ["inside"]
        other = []
        with GQLResponseObserver(lambda row, stamp: other.append(row)).observe():
            GQLResponseObserver.record("new")
        assert other == ["new"] and captured == ["inside"]
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
