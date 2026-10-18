"""Optional real collection flights belong to the serialized miner lifecycle."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_deadline_claim_continuation import prepare_claim
from tests.test_deadline_forecast import NOW
from tests.test_deadline_queue import runtime as runtime
from tests.test_inventory_progress import transport, wire


def responses(run, monkeypatch, *, minutes=7):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            now = run.executor._utc_clock()
            return now.replace(tzinfo=None) if tz is None else now.astimezone(tz)

    monkeypatch.setattr("src.services.inventory_service.datetime", Clock)
    transport(run.twitch, [wire(campaign, minutes=minutes) for campaign in run.twitch.inventory])


@pytest.mark.asyncio
async def test_disabled_bridge_and_unadmitted_reservation_do_not_query(runtime, monkeypatch):
    async with runtime() as run:
        fail = AsyncMock(side_effect=AssertionError("Default bridge must not query Twitch"))
        monkeypatch.setattr(run.twitch._inventory_service, "collect_inventory", fail)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        fail.assert_not_awaited()
        assert not run.twitch._deadline_read_tasks


@pytest.mark.asyncio
async def test_unadmitted_reservation_does_not_start_progress_read(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        responses(run, monkeypatch)
        await run.reserve()
        run.twitch.gql_request.assert_not_awaited()
        assert run.queue._progress_task is None


@pytest.mark.asyncio
async def test_actual_loop_refreshes_protected_reports_past_five_minutes_without_rebasing(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        responses(run, monkeypatch, minutes=0)
        certificate = await run.reserve()
        assert (await run.tick(3)).action == "watch"
        assert (await run.next()).action == "wait"
        for seconds in (123, 243, 363, 483):
            responses(run, monkeypatch, minutes=seconds // 60)
            assert (await run.tick(seconds)).action == "wait"
            assert (await run.next()).action == "wait"
            saved = run.executor._reservation
            assert saved.certificate is certificate
            assert run.high.timed_drops["reward"].progress_observation["received_at"] == run.executor._utc_clock().isoformat()
            assert run.twitch.watching_channel.get_with_default(None).id == 20
        assert run.executor._utc_clock() > NOW
        assert run.twitch.watch.call_count == 1
        assert run.twitch.settings.games_to_watch == ["High", "Urgent"]
        assert not run.twitch._deadline_read_tasks and run.queue._progress_task is None


@pytest.mark.asyncio
async def test_two_hour_detour_public_claim_and_protected_handoff_use_real_collection(runtime, monkeypatch, tmp_path):
    async with runtime(refresh_progress=True) as run:
        responses(run, monkeypatch, minutes=0)
        certificate = await run.reserve()
        first, protected = certificate.slots
        assert (await run.tick(3)).action == "watch"
        assert (await run.next()).action == "wait"
        end = (first.until - NOW).total_seconds()
        for seconds in range(123, int(end), 120):
            transport(run.twitch, [
                wire(run.high, minutes=0), wire(run.urgent, minutes=min(120, max(0, (seconds - 15) // 60))),
            ])
            assert (await run.tick(seconds)).action == "wait"
            assert (await run.next()).action == "wait"
            assert run.executor._reservation.certificate is certificate
        assert end > 7200
        drop = run.urgent.timed_drops["urgent"]
        prepare_claim(run, drop, tmp_path, monkeypatch)
        observation = drop.progress_observation
        run.move(end)
        assert await drop.claim() is True
        assert drop.progress_observation == observation
        assert (await run.next()).action == "stop"
        assert run.twitch.watching_channel.get_with_default(None) is None
        assert (await run.tick((protected.connect_at - NOW).total_seconds())).action == "watch"
        assert run.executor._reservation.certificate is certificate
        assert [call.args[0].id for call in run.twitch.watch.call_args_list] == [20, 10]
        assert run.twitch.settings.games_to_watch == ["High", "Urgent"]
        assert run.high.timed_drops["reward"].progress_observation["source"] == "inventory"


@pytest.mark.asyncio
async def test_delayed_read_does_not_block_checkpoint_or_coalesce_into_more_flights(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def blocked(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        collect = AsyncMock(side_effect=blocked)
        monkeypatch.setattr(run.twitch._inventory_service, "collect_inventory", collect)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        await asyncio.wait_for(entered.wait(), 1)
        child = run.queue._progress_task
        assert (await run.tick(123)).action == "wait"
        assert (await run.tick(243)).action == "wait"
        assert collect.await_count == 1 and not child.done()
        run.twitch._set_deadline_queue(None)
        assert child in run.twitch._deadline_read_tasks
        await run.twitch._stop_deadline_read_tasks()
        assert child.done() and cleaned.is_set()
        await asyncio.sleep(0)
        assert not run.twitch._deadline_read_tasks


@pytest.mark.asyncio
async def test_changed_server_metadata_requests_normal_inventory_reload(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        responses(run, monkeypatch)
        payloads = [wire(campaign) for campaign in run.twitch.inventory]
        payloads[1]["name"] = "New campaign metadata"
        transport(run.twitch, payloads)
        run.twitch.request_inventory_refresh.reset_mock()
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        assert await run.next() is None
        run.twitch.request_inventory_refresh.assert_called_once_with()
        assert run.executor._reservation is None
        assert run.high.timed_drops["reward"].real_current_minutes == 0


@pytest.mark.asyncio
async def test_read_error_propagates_in_owner_and_retires_the_calendar(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        collect = AsyncMock(side_effect=RuntimeError("unexpected progress failure"))
        monkeypatch.setattr(run.twitch._inventory_service, "collect_inventory", collect)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        with pytest.raises(RuntimeError, match="unexpected progress failure"):
            await run.next()
        with pytest.raises(RuntimeError, match="unexpected progress failure"):
            await run.root
        assert run.executor._reservation is None
        assert not run.twitch._deadline_read_tasks
        # The context manager still owns its normal shutdown verification.
        run.root = asyncio.create_task(asyncio.sleep(0))


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["auth", "close", "root_cancel"])
async def test_public_lifecycle_drains_detached_inflight_read(runtime, monkeypatch, boundary):
    async with runtime(refresh_progress=True) as run:
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def blocked(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        monkeypatch.setattr(run.twitch._inventory_service, "collect_inventory", blocked)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        await asyncio.wait_for(entered.wait(), 1)
        child = run.queue._progress_task
        run.twitch._set_deadline_queue(None)
        if boundary == "auth":
            monkeypatch.setattr(run.twitch._inventory_service, "clear_cached_state", MagicMock())
            async with run.twitch.authentication_change():
                assert child.done() and cleaned.is_set()
                run.twitch.close()
            await asyncio.wait_for(run.root, 2)
        elif boundary == "close":
            run.twitch.close()
            await asyncio.wait_for(run.root, 2)
        else:
            run.root.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run.root
            run.root = asyncio.create_task(asyncio.sleep(0))
        assert child.done() and cleaned.is_set()
        assert not run.twitch._deadline_read_tasks


@pytest.mark.asyncio
async def test_drain_does_not_cancel_again_during_child_cleanup(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

        async def blocked(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        monkeypatch.setattr(run.twitch._inventory_service, "collect_inventory", blocked)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        await asyncio.wait_for(entered.wait(), 1)
        run.queue.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        drain = asyncio.create_task(run.twitch._stop_deadline_read_tasks())
        try:
            await asyncio.sleep(0)
            assert not drain.done() and not cleaned.is_set()
        finally:
            release.set()
            await asyncio.wait_for(drain, 1)
        assert cleaned.is_set()


@pytest.mark.asyncio
async def test_timeout_detail_cleanup_survives_queue_retirement(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

        async def blocked(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        responses(run, monkeypatch)
        monkeypatch.setattr(run.twitch._inventory_service, "fetch_campaigns", blocked)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        await asyncio.wait_for(entered.wait(), 1)
        child = run.queue._progress_task
        try:
            await asyncio.wait_for(cleaning.wait(), 6)
            assert child.cancelling() == 1
            run.twitch._set_deadline_queue(None)
            drain = asyncio.create_task(run.twitch._stop_deadline_read_tasks())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(drain, 1)
            assert cleaned.is_set(), "Retirement interrupted timeout-owned detail cleanup"
        finally:
            release.set()


@pytest.mark.asyncio
async def test_server_claim_during_newer_minute_update_retires_queue_and_requests_reload(runtime, monkeypatch):
    async with runtime(refresh_progress=True) as run:
        entered, release = asyncio.Event(), asyncio.Event()
        responses(run, monkeypatch)
        payloads = [wire(campaign) for campaign in run.twitch.inventory]
        payloads[0]["timeBasedDrops"][0]["self"]["isClaimed"] = True
        transport(run.twitch, payloads)
        collect = run.twitch._inventory_service.collect_inventory

        async def delayed(**kwargs):
            result = await collect(**kwargs)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(run.twitch._inventory_service, "collect_inventory", delayed)
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        await asyncio.wait_for(entered.wait(), 1)
        drop = run.high.timed_drops["reward"]
        drop.update_minutes(9, source="websocket")
        run.twitch.request_inventory_refresh.reset_mock()
        release.set()
        assert await run.next() is None
        run.twitch.request_inventory_refresh.assert_called_once_with()
        assert run.executor._reservation is None
        assert drop.real_current_minutes == 9 and not drop.is_claimed
        assert run.urgent.timed_drops["urgent"].real_current_minutes == 0
