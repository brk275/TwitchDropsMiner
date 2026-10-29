"""Managed channel lookups cannot outlive their publishing authority."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.constants import CALL
from src.core.client import Twitch
from src.exceptions import ExitRequest, LoginException, MinerException
from src.models.channel import Channel
from src.services.channel_service import ChannelService
from tests.channel_refresh_helpers import (
    channel,
    client,
    force_available_drops_branch,
    live,
    setup_client,
)
from tests.test_session_lifecycle import miner


@pytest.mark.parametrize("observation", ["viewers", "stream", "offline", "retire"])
def test_bulk_and_refresh_counters_preserve_their_distinct_observation_contracts(monkeypatch, observation):
    twitch, target = setup(monkeypatch, True)
    twitch.websocket = MagicMock()
    generation, revision = target._refresh_generation, target.stream_revision
    if observation == "viewers":
        target.viewers = target.viewers
    elif observation == "stream":
        target.external_update(live(target.id)["data"]["user"], [])
    elif observation == "offline":
        target.external_update({"stream": None}, [])
    else:
        Twitch._remove_channel_topics(twitch, [target])
    assert target.stream_revision == revision + 1
    if observation == "viewers":
        assert target._refresh_generation == generation
    else:
        assert target._refresh_generation == (generation[0] + 1, generation[1])


@pytest.mark.asyncio
async def test_registry_generation_retires_bulk_publication_without_claiming_to_join_it(monkeypatch):
    twitch, target = setup(monkeypatch, True)
    twitch.websocket = MagicMock()
    original = target._stream
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operations):
        entered.set()
        await release.wait()
        return [live(target.id)]

    twitch.gql_request = AsyncMock(side_effect=request)
    service = ChannelService(twitch)
    worker = asyncio.create_task(service.bulk_check_online([target]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await Twitch._stop_channel_tasks(twitch)
        assert not worker.done()  # This bulk caller is owned outside the managed registry.
        release.set()
        await asyncio.wait_for(worker, 1)
        assert target._stream is original
        twitch.gql_request = AsyncMock(return_value=[live(target.id)])
        await service.bulk_check_online([target])
        assert target._stream.broadcast_id == 999999
    finally:
        release.set()
        await drain(twitch, worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["offline", "topics", "registry"])
@pytest.mark.parametrize("adapter", ["propagates", "suppresses", "uncancels"])
async def test_retired_first_read_does_not_start_new_optional_lookup(monkeypatch, retirement, adapter):
    twitch, target = setup(monkeypatch, False)
    twitch.websocket = MagicMock()
    target._display_name = None
    force_available_drops_branch(monkeypatch)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        if "channelID" in operation["variables"]:
            second_entered.set()
            await second_release.wait()
            return {"data": {"channel": {"viewerDropCampaigns": []}}}
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if adapter == "propagates":
                raise
            if adapter == "uncancels":
                asyncio.current_task().uncancel()
            await release.wait()
        response = live(target.id)
        response["data"]["user"]["displayName"] = "Retired name"
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    worker = target._stream_refresh_task
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if retirement == "offline":
            target.set_offline()
        elif retirement == "topics":
            Twitch._remove_channel_topics(twitch, [target])
        else:
            stop = asyncio.create_task(Twitch._stop_channel_tasks(twitch))
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        for _ in range(40):
            if second_entered.is_set() or worker.done():
                break
            await asyncio.sleep(0)
        dispatched = second_entered.is_set()
        second_release.set()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)
        if stop is not None:
            await asyncio.wait_for(stop, 1)
        assert target._stream is None and target._display_name is None
        twitch.on_channel_update.assert_not_called()
        assert not dispatched, "Retired first read started a new AvailableDrops lookup"
    finally:
        release.set()
        second_release.set()
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
        await drain(twitch, worker)


@pytest.mark.asyncio
async def test_current_refresh_still_performs_the_optional_lookup(monkeypatch):
    twitch, target = setup(monkeypatch, False)
    target._display_name = None
    force_available_drops_branch(monkeypatch)
    response = live(target.id)
    response["data"]["user"]["displayName"] = "Fresh name"
    twitch.gql_request = AsyncMock(side_effect=[
        response, {"data": {"channel": {"viewerDropCampaigns": []}}},
    ])

    assert await target.update_stream()

    assert twitch.gql_request.await_count == 2
    assert "channelID" in twitch.gql_request.await_args_list[1].args[0]["variables"]
    assert target._display_name == "Fresh name"
    twitch.on_channel_update.assert_called_once()


def setup(monkeypatch, initially_online):
    monkeypatch.setattr("src.models.channel.ONLINE_DELAY", timedelta(0))
    twitch = client()
    twitch._channel_tasks = set()
    twitch._channel_tasks_generation = 0
    twitch.on_channel_update = MagicMock()
    target = channel(twitch, online=initially_online)
    return twitch, target


async def drain(twitch, *owned):
    tasks = set(twitch._channel_tasks)
    tasks.update(task for task in owned if task is not None)
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=1)
        await asyncio.gather(*done, return_exceptions=True)
        assert not pending, "Test cleanup did not drain its captured work"
    await asyncio.sleep(0)


async def join(twitch):
    while twitch._channel_tasks:
        await asyncio.gather(*list(twitch._channel_tasks), return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("event", ["none", "offline", "remove"])
async def test_managed_refresh_cannot_publish_after_retirement(monkeypatch, initially_online, event):
    twitch, target = setup(monkeypatch, initially_online)
    original = target._stream
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        entered.set()
        await release.wait()
        return live(target.id)

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if event == "offline":
            target.set_offline()
        elif event == "remove":
            target.remove()
        calls_after_event = twitch.on_channel_update.call_count
        release.set()
        outcome, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert not isinstance(outcome, BaseException) or isinstance(outcome, asyncio.CancelledError)
        if event == "none":
            assert target.online and target._stream.broadcast_id == 999999
            assert twitch.on_channel_update.call_count == 1
        else:
            assert target._stream is (None if event == "offline" else original)
            assert twitch.on_channel_update.call_count == calls_after_event
        assert target._pending_stream_up is None
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
async def test_events_coalesce_while_the_managed_query_is_in_flight(monkeypatch, initially_online):
    twitch, target = setup(monkeypatch, initially_online)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = active = peak = 0

    async def request(operation):
        nonlocal calls, active, peak
        calls += 1
        ordinal = calls
        active += 1
        peak = max(peak, active)
        try:
            if ordinal == 1:
                entered.set()
                await release.wait()
            result = live(target.id)
            result["data"]["user"]["stream"]["viewersCount"] = 100 if ordinal == 1 else 150
            return result
        finally:
            active -= 1

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        target.check_online()
        target.check_online()
        for _ in range(3):
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(join(twitch), 1)
        assert twitch.gql_request.await_count == 2
        assert peak == 1
        assert target.viewers == 150
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
async def test_events_before_query_dispatch_still_coalesce_to_one_read(monkeypatch, initially_online):
    twitch, target = setup(monkeypatch, initially_online)
    twitch.gql_request = AsyncMock(return_value=live(target.id))
    target.check_online()
    target.check_online()
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(join(twitch), 1)
        twitch.gql_request.assert_awaited_once()
        assert target.online
    finally:
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
async def test_failed_lookup_releases_the_owner_for_retry(monkeypatch, initially_online):
    twitch, target = setup(monkeypatch, initially_online)
    original = target._stream
    twitch.gql_request = AsyncMock(side_effect=MinerException("Synthetic lookup failure"))
    target.check_online()
    first = target._pending_stream_up
    second = None
    try:
        await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 1)
        assert target._stream is original and target._pending_stream_up is None
        twitch.gql_request.side_effect = None
        twitch.gql_request.return_value = live(target.id)
        target.check_online()
        second = target._pending_stream_up
        assert second is not first
        await asyncio.wait_for(second, 1)
        assert target.online and target._stream.broadcast_id == 999999
        assert twitch.gql_request.await_count == 2
    finally:
        await drain(twitch, first, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
async def test_managed_reply_does_not_replace_a_newer_bulk_snapshot(monkeypatch, initially_online):
    twitch, target = setup(monkeypatch, initially_online)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        stale_response = live(target.id)
        entered.set()
        await release.wait()
        return stale_response

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        newer = live(target.id)["data"]["user"]
        newer["stream"]["id"] = "777777"
        newer["stream"]["viewersCount"] = 150
        target.external_update(newer, [])
        fresh = target._stream
        release.set()
        outcome, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert not isinstance(outcome, BaseException) or isinstance(outcome, asyncio.CancelledError)
        assert target._stream is fresh and target.viewers == 150
        assert twitch.on_channel_update.call_count == 0
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("stream_down", [False, True])
async def test_delayed_reply_cannot_redirect_watching_after_stream_down(
    tmp_path, monkeypatch, manual, stream_down,
):
    twitch, high, selected, _ = setup_client(tmp_path, monkeypatch)
    high._stream = None
    if not manual:
        twitch.clear_manual_mode("Automatic fixture")
    monkeypatch.setattr("src.models.channel.ONLINE_DELAY", timedelta(0))
    before = [(drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed, drop.claim_id)
              for campaign in twitch.inventory for drop in campaign.drops]
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        response = live(high.id)
        response["data"]["user"]["displayName"] = "Fixture"
        response["data"]["user"]["broadcastSettings"]["game"] = {"id": "1", "name": "High"}
        entered.set()
        await release.wait()
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    high.check_online()
    task = high._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert twitch.watching_channel.get_with_default(None) is selected
        if stream_down:
            await twitch._message_handler_service.process_stream_state(high.id, {"type": "stream-down"})
        release.set()
        outcome, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert not isinstance(outcome, BaseException) or isinstance(outcome, asyncio.CancelledError)
        expected = selected if stream_down or manual else high
        after = [(drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed, drop.claim_id)
                 for campaign in twitch.inventory for drop in campaign.drops]
        assert after == before
        assert twitch.drop_history.total_count == 0
        assert twitch.settings.games_to_watch == ["High", "Low"]
        twitch.gql_request.assert_awaited_once()
        assert twitch.watching_channel.get_with_default(None) is expected
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("event", ["offline", "remove"])
async def test_retired_refresh_cannot_publish_when_transport_suppresses_cancellation(
    monkeypatch, initially_online, event,
):
    twitch, target = setup(monkeypatch, initially_online)
    original = target._stream
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request(operation):
        response = live(target.id)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        target.set_offline() if event == "offline" else target.remove()
        if not task.cancelling():
            task.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
        calls_after_event = twitch.on_channel_update.call_count
        release.set()
        outcome, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert not isinstance(outcome, BaseException) or isinstance(outcome, asyncio.CancelledError)
        assert target._stream is (None if event == "offline" else original)
        assert twitch.on_channel_update.call_count == calls_after_event
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_online", [False, True])
async def test_channel_registry_joins_transport_while_cancellation_is_draining(monkeypatch, initially_online):
    twitch, target = setup(monkeypatch, initially_online)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request(operation):
        response = live(target.id)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        stop = asyncio.create_task(Twitch._stop_channel_tasks(twitch))
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not stop.done()
        release.set()
        await asyncio.wait_for(stop, 1)
        assert task.done() and not twitch._channel_tasks
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        try:
            if stop is not None:
                await asyncio.wait_for(asyncio.shield(stop), 1)
        finally:
            await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("failure", ["request", "login", "exit", "unexpected", "callback"])
async def test_failure_retries_only_recoverable_queries_with_queued_events(monkeypatch, queued, failure):
    twitch, target = setup(monkeypatch, True)
    original = target._stream
    entered, release = asyncio.Event(), asyncio.Event()
    errors = {"request": MinerException, "login": LoginException,
              "exit": ExitRequest, "unexpected": ValueError, "callback": MinerException}
    error = errors[failure]()
    calls = 0

    async def request(operation):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            if failure != "callback":
                raise error
        return live(target.id)

    twitch.gql_request = AsyncMock(side_effect=request)
    if failure == "callback":
        twitch.on_channel_update.side_effect = error
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if queued:
            target.check_online()
        release.set()
        outcome, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        retry = queued and failure == "request"
        assert calls == (2 if retry else 1)
        assert (outcome is None) if retry else isinstance(outcome, errors[failure])
        if failure != "callback" and not retry:
            assert target._stream is original
        assert target._pending_stream_up is None
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("null_stream", [False, True])
@pytest.mark.parametrize("direct", [False, True])
async def test_newer_observation_supersedes_direct_and_managed_queries(monkeypatch, null_stream, direct):
    twitch, target = setup(monkeypatch, True)
    target._display_name = None
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        response = live(target.id)
        response["data"]["user"]["displayName"] = "Outdated name"
        entered.set()
        await release.wait()
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    if direct:
        task = asyncio.create_task(target.update_stream())
    else:
        target.check_online()
        task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        newer = live(target.id)["data"]["user"]
        # The same broadcast is still a newer observation.
        newer["stream"]["viewersCount"] = 150
        if null_stream:
            newer["stream"] = None
        target.external_update(newer, [])
        fresh = target._stream
        release.set()
        await asyncio.wait_for(task, 1)
        assert target._stream is fresh
        assert target._display_name is None
        assert target.online is (not null_stream)
        twitch.on_channel_update.assert_not_called()
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
async def test_invalid_external_update_does_not_revoke_valid_managed_query(monkeypatch):
    twitch, target = setup(monkeypatch, True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        entered.set()
        await release.wait()
        return live(target.id)

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(KeyError):
            target.external_update({"stream": {"id": "bad"}}, [])
        release.set()
        await asyncio.wait_for(task, 1)
        assert target._stream.broadcast_id == 999999
        twitch.on_channel_update.assert_called_once()
    finally:
        release.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_failure", [False, True])
async def test_retired_owner_cannot_consume_replacement_followup(monkeypatch, old_failure):
    twitch, target = setup(monkeypatch, True)
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    cancelled = asyncio.Event()
    calls = 0

    async def request(operation):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        try:
            await release[index].wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release[index].wait()
        if index == 0 and old_failure:
            raise MinerException("Retired query failure")
        response = live(target.id)
        response["data"]["user"]["stream"]["viewersCount"] = 100 + index
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    old = target._pending_stream_up
    replacement = None
    try:
        await asyncio.wait_for(entered[0].wait(), 1)
        target.set_offline()
        await asyncio.wait_for(cancelled.wait(), 1)
        target.check_online()
        replacement = target._pending_stream_up
        await asyncio.wait_for(entered[1].wait(), 1)
        target.check_online()
        release[0].set()
        await asyncio.wait_for(asyncio.gather(old, return_exceptions=True), 1)
        assert replacement in twitch._channel_tasks and not replacement.done()
        assert not target.online
        release[1].set()
        await asyncio.wait_for(entered[2].wait(), 1)
        assert target.viewers == 101
        release[2].set()
        await asyncio.wait_for(replacement, 1)
        assert calls == 3 and target.viewers == 102
        assert twitch.on_channel_update.call_count == 3  # offline, replacement and follow-up
    finally:
        for event in release:
            event.set()
        await drain(twitch, old, replacement)


@pytest.mark.asyncio
async def test_events_during_followup_delay_coalesce_but_queries_queue_another_read(monkeypatch):
    twitch, target = setup(monkeypatch, False)
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    calls = 0

    def display(*args, **kwargs):
        if target._pending_stream_up is not None:
            target.check_online()
            target.check_online()

    async def request(operation):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()
        return live(target.id)

    twitch.gui.channels.display.side_effect = display
    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        assert target.pending_online and not target.offline
        for index in range(3):
            await asyncio.wait_for(entered[index].wait(), 1)
            assert target._pending_stream_up is None
            if index == 0:
                assert target.offline and not target.pending_online
            if index < 2:
                target.check_online()
                target.check_online()
            release[index].set()
        await asyncio.wait_for(task, 1)
        assert calls == 3 and target.online and not target.pending_online
    finally:
        for event in release:
            event.set()
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_done_callback_cannot_clear_replacement_before_dispatch(monkeypatch, completed):
    twitch, target = setup(monkeypatch, False)
    original_done = Channel._stream_refresh_done
    deferred = []
    monkeypatch.setattr(Channel, "_stream_refresh_done", lambda self, task: deferred.append(task))
    twitch.gql_request = AsyncMock(return_value=live(target.id))
    target.check_online()
    old = target._pending_stream_up
    replacement = None
    try:
        if not completed:
            old.cancel()  # Before the coroutine's first instruction.
        await asyncio.wait_for(asyncio.gather(old, return_exceptions=True), 1)
        target.check_online()
        replacement = target._pending_stream_up
        original_done(target, old)
        assert replacement is not old and target._pending_stream_up is replacement
        await asyncio.wait_for(replacement, 1)
        original_done(target, replacement)
        assert not twitch._channel_tasks and target._pending_stream_up is None
    finally:
        await drain(twitch, old, replacement)
        for task in deferred:
            original_done(target, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("uncancel", [False, True])
async def test_registry_cancellation_revokes_publication_and_queued_work(monkeypatch, queued, uncancel):
    twitch, target = setup(monkeypatch, False)
    target._display_name = None
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request(operation):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if uncancel:
                asyncio.current_task().uncancel()
            cancelled.set()
            await release.wait()
        result = live(target.id)
        result["data"]["user"]["displayName"] = "Retired name"
        return result

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if queued:
            target.check_online()
        stop = asyncio.create_task(Twitch._stop_channel_tasks(twitch))
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not stop.done()
        release.set()
        await asyncio.wait_for(stop, 1)
        assert task.done() and not twitch._channel_tasks
        if not uncancel:
            assert task.cancelled()
        assert target._display_name is None and target.offline
        twitch.on_channel_update.assert_not_called()
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        try:
            if stop is not None:
                await asyncio.wait_for(asyncio.shield(stop), 1)
        finally:
            await drain(twitch, task)


@pytest.mark.asyncio
async def test_failure_observation_never_logs_exception_or_cause(monkeypatch, caplog):
    twitch, target = setup(monkeypatch, True)
    caplog.set_level(CALL, logger="TwitchDrops")

    async def request(operation):
        try:
            raise ValueError("SECRET-CAUSE")
        except ValueError as exc:
            raise MinerException("SECRET-TRANSPORT") from exc

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    task = target._pending_stream_up
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert "Channel refresh task failed" in caplog.text
        assert "SECRET" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncancel", [False, True])
@pytest.mark.parametrize("tracked", [False, True])
async def test_authentication_change_waits_for_real_retired_channel_lookup(
    tmp_path, monkeypatch, uncancel, tracked,
):
    twitch = miner(tmp_path, monkeypatch)
    twitch.websocket.stop = AsyncMock()
    twitch._auth_state.user_id = 17
    target = Channel(twitch, id=100, login="fixture")
    twitch.channels[target.id] = target
    monkeypatch.setattr("src.models.channel.ONLINE_DELAY", timedelta(0))
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def request(operation):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if uncancel:
                asyncio.current_task().uncancel()
            cancelled.set()
            await release.wait()
        observed.append(twitch._auth_state.user_id)
        result = live(target.id)
        result["data"]["user"]["displayName"] = "Old account name"
        return result

    async def replace():
        async with twitch.authentication_change():
            assert task.done() and not twitch._channel_tasks
            assert target.offline and target._display_name is None
            twitch._auth_state.user_id = 42

    twitch.gql_request = AsyncMock(side_effect=request)
    twitch.on_channel_update = MagicMock()
    target.check_online()
    task = target._pending_stream_up
    replacement = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        target.check_online()
        if not tracked:
            twitch.channels.clear()  # Registry ownership survives rediscovery/removal from the map.
        replacement = asyncio.create_task(replace())
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not replacement.done() and twitch._auth_state.user_id == 17
        release.set()
        await asyncio.wait_for(replacement, 1)
        assert observed == [17]
        assert not hasattr(twitch._auth_state, "user_id")  # Context exit clears transient identity.
        twitch.on_channel_update.assert_not_called()
        twitch.gql_request.assert_awaited_once()
    finally:
        release.set()
        try:
            if replacement is not None:
                await asyncio.wait_for(asyncio.shield(replacement), 1)
        finally:
            await drain(twitch, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_first", [False, True])
async def test_replaced_owner_cannot_publish_after_transport_uncancels(monkeypatch, old_first):
    twitch, target = setup(monkeypatch, False)
    target._display_name = None
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    cancelled = asyncio.Event()
    calls = 0

    async def request(operation):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        try:
            await release[index].wait()
        except asyncio.CancelledError:
            # Synthetic adapter explicitly acknowledges and clears cancellation.
            asyncio.current_task().uncancel()
            cancelled.set()
            await release[index].wait()
        response = live(target.id)
        response["data"]["user"]["stream"]["viewersCount"] = 100 + index
        response["data"]["user"]["displayName"] = "old-name" if index == 0 else "new-name"
        return response

    twitch.gql_request = AsyncMock(side_effect=request)
    target.check_online()
    old = target._pending_stream_up
    replacement = None
    try:
        await asyncio.wait_for(entered[0].wait(), 1)
        old.cancel()
        target.check_online()  # Before transport clears the cancellation request.
        replacement = target._pending_stream_up
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(entered[1].wait(), 1)
        if old_first:
            release[0].set()
            await asyncio.wait_for(old, 1)
            assert target.offline and target._display_name is None
            twitch.on_channel_update.assert_not_called()
        release[1].set()
        await asyncio.wait_for(replacement, 1)
        release[0].set()
        await asyncio.wait_for(old, 1)
        assert calls == 2 and target.viewers == 101 and target.name == "new-name"
        twitch.on_channel_update.assert_called_once()
        assert not twitch._channel_tasks
    finally:
        for event in release:
            event.set()
        await drain(twitch, old, replacement)
