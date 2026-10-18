"""Owned state-loop delivery with synthetic clocks/premises and no Twitch transport."""

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth.browser_session import BrowserIdentity
from src.config import ClientType, State
from src.core.client import Twitch
from src.services.deadline_certificate import DropTimingBounds, DurationBounds, TimingBounds
from src.services.deadline_forecast import ViewingWindow
from src.services.deadline_planner import PlanningAssumptions
from src.services.deadline_queue import DeadlineCheckpointQueue
from src.services.inventory_service import InventoryService
from tests.test_deadline_execution import execution as execution_state
from tests.test_deadline_forecast import NOW
from tests.test_deadline_planner import HORIZON


def premises(bound):
    """Explicit hypothetical uptime, allocation and settlement, never live evidence."""
    if any(drop.observed_minutes is None for drop in bound.inputs.drops):
        return None
    views = tuple(ViewingWindow(
        route.channel_id, bound.inputs.now, HORIZON,
        frozenset(drop.id for drop in bound.inputs.drops if drop.game_id == route.game_id),
        True, route.channel_order, route.acl_based, route.mining_priority,
    ) for route in bound.channels.candidates)
    timing = TimingBounds(tuple(DropTimingBounds(
        drop.id, DurationBounds(timedelta(minutes=drop.required_minutes - drop.observed_minutes),
                                timedelta(minutes=drop.required_minutes - drop.observed_minutes)),
        DurationBounds(timedelta(0), timedelta(seconds=20)),
    ) for drop in bound.inputs.drops), DurationBounds(timedelta(0), timedelta(seconds=10)))
    return PlanningAssumptions(views, timing, baseline_complete=True)


class Handle:
    def __init__(self, delay, callback):
        self.delay, self.callback = delay, callback
        self.cancelled = self.fired = False

    def cancel(self):
        self.cancelled = True

    def fire(self, *, force=False):
        assert force or not (self.cancelled or self.fired)
        self.fired = True
        self.callback()


class Scheduler:
    def __init__(self):
        self.handles = []

    def __call__(self, delay, callback):
        handle = Handle(delay, callback)
        self.handles.append(handle)
        return handle

    @property
    def latest(self):
        return next(item for item in reversed(self.handles) if not item.cancelled and not item.fired)


class Runtime:
    def __init__(self, state, monkeypatch, *, real_timer=False):
        self.twitch, self.high, self.urgent, self.executor, _, self.move = state
        twitch = self.twitch
        self.scheduler = Scheduler()
        self.queue = DeadlineCheckpointQueue(
            twitch, executor=self.executor, utc_clock=self.executor._utc_clock,
            schedule=None if real_timer else self.scheduler,
            max_sleep=timedelta(seconds=0.01 if real_timer else 15),
        )
        twitch._set_deadline_queue(self.queue)
        twitch._resume_mining = asyncio.Event()
        twitch._resume_mining.set()
        twitch._watching_restart = asyncio.Event()
        twitch._run_task = twitch._watching_task = twitch._schedule_task = twitch._mnt_task = None
        twitch._channel_tasks = set()
        twitch._mnt_triggers = []
        twitch._browser = MagicMock()
        twitch.diagnostics = MagicMock()
        twitch.session_controller = MagicMock(stop=AsyncMock())
        twitch.helper = MagicMock(stop=AsyncMock())
        twitch.login_browser = MagicMock(stop=AsyncMock())
        twitch.websocket = MagicMock(start=AsyncMock(), stop=AsyncMock())
        twitch.gui.login.stop_avatar = AsyncMock()
        twitch._message_handler_service = MagicMock()
        twitch._inventory_service = InventoryService(twitch)
        monkeypatch.setattr(twitch, "_ensure_api_clients", lambda: None)
        monkeypatch.setattr(twitch, "get_auth", AsyncMock(return_value=twitch._auth_state))
        monkeypatch.setattr(twitch, "_mining_schedule_loop", AsyncMock())
        monkeypatch.setattr(twitch._watch_service, "watch_loop", AsyncMock())
        # Skip only startup fetching. Selection, events, watch/stop and ownership are real.
        monkeypatch.setattr(twitch, "request_inventory_refresh", MagicMock())
        self.observed = asyncio.Queue()
        checkpoint = self.queue.checkpoint

        def observe():
            try:
                result = checkpoint()
            except BaseException as exc:
                self.observed.put_nowait(exc)
                raise
            self.observed.put_nowait(result)
            return result

        monkeypatch.setattr(self.queue, "checkpoint", observe)
        self.root = None

    async def next(self):
        result = await asyncio.wait_for(self.observed.get(), timeout=2)
        if isinstance(result, BaseException):
            raise result
        return result

    async def reserve(self, provider=premises):
        assert self.queue.request(horizon=HORIZON, provider=provider, enabled=True)
        result = await self.next()
        assert result is not None and result.action == "reserved"
        return self.executor._reservation.certificate

    async def tick(self, seconds, *, refresh=False):
        self.move(seconds, refresh=refresh)
        before = (self.twitch.watch.call_count, self.twitch.stop_watching.call_count)
        self.scheduler.latest.fire()
        assert before == (self.twitch.watch.call_count, self.twitch.stop_watching.call_count)
        return await self.next()

    async def stop(self):
        self.twitch.close()
        try:
            await asyncio.wait_for(self.root, timeout=2)
        finally:
            task = self.twitch._watching_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert self.executor._reservation is None and self.executor._guard._capture is None
        assert self.queue._timer is None


@pytest.fixture
def runtime(monkeypatch):
    state = execution_state.__wrapped__(monkeypatch)

    @asynccontextmanager
    async def running(**kwargs):
        run = Runtime(state, monkeypatch, **kwargs)
        run.root = asyncio.create_task(run.twitch.run())
        try:
            assert await run.next() is None
            assert run.twitch._run_task is not asyncio.current_task()
            yield run
        finally:
            await run.stop()

    return running


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "yes"])
def test_disabled_queue_never_reads_client_or_clocks(enabled):
    fail = MagicMock(side_effect=AssertionError("Disabled queue must stay disconnected"))
    queue = DeadlineCheckpointQueue(object(), clock=fail, utc_clock=fail, schedule=fail)
    assert not queue.request(horizon=HORIZON, enabled=enabled)
    assert queue.checkpoint() is None and not queue.pending_checkpoint()
    fail.assert_not_called()


@pytest.mark.parametrize("interval", [0, 1, None, timedelta(0), timedelta(seconds=-1), timedelta(seconds=16)])
def test_invalid_intervals_are_rejected_without_reading_client(interval):
    with pytest.raises(ValueError, match="^Invalid checkpoint interval$"):
        DeadlineCheckpointQueue(object(), max_sleep=interval)


def test_missing_owner_and_cross_client_attachment_are_rejected():
    twitch = object.__new__(Twitch)
    queue = DeadlineCheckpointQueue(twitch)
    assert not queue.request(horizon=HORIZON, provider=premises, enabled=True)
    other = object.__new__(Twitch)
    with pytest.raises(ValueError, match="^Invalid checkpoint queue owner$"):
        other._set_deadline_queue(queue)


@pytest.mark.asyncio
async def test_actual_registered_loop_follows_fixed_calendar_and_original_ranks(runtime):
    async with runtime() as run:
        first, protected = (await run.reserve()).slots
        assert (await run.tick(3)).action == "watch"
        assert run.queue._active and run.twitch.watching_channel.get_with_default(None).id == 20
        run.twitch.gui.display_drop.assert_called_with(run.urgent.timed_drops["urgent"], countdown=False, subone=True)
        assert (await run.tick(90)).action == "wait"
        # Hypothetical settlement only. Public claims retire the queue in a separate test.
        run.urgent.timed_drops["urgent"].is_claimed = True
        assert (await run.tick((first.until - NOW).total_seconds(), refresh=True)).action == "stop"
        assert run.twitch.watching_channel.get_with_default(None) is None
        assert (await run.tick((protected.connect_at - NOW).total_seconds() - 1, refresh=True)).action == "wait"
        assert run.twitch.watch.call_count == 1
        assert (await run.tick((protected.connect_at - NOW).total_seconds(), refresh=True)).action == "watch"
        run.high.timed_drops["reward"].is_claimed = True
        assert (await run.tick((protected.until - NOW).total_seconds(), refresh=True)).action == "stop"
        assert await run.tick((protected.until - NOW).total_seconds(), refresh=True) is None
        assert [call.args[0].id for call in run.twitch.watch.call_args_list] == [20, 10]
        assert run.twitch.settings.games_to_watch == ["High", "Urgent"]
        assert not run.queue._active


@pytest.mark.asyncio
async def test_real_call_later_wakes_owner_without_opening_frozen_future_slot(runtime):
    async with runtime(real_timer=True) as run:
        await run.reserve()
        result = await run.next()
        assert result.action == "wait"
        run.twitch.watch.assert_not_called()
        assert run.queue._active


@pytest.mark.asyncio
async def test_queued_intent_builds_fresh_inputs_after_latency_not_a_rebased_old_plan(runtime):
    async with runtime() as run:
        captured = []

        def provider(bound):
            assert asyncio.current_task() is run.twitch._run_task
            captured.append(bound)
            return premises(bound)

        assert run.queue.request(horizon=HORIZON, provider=provider, enabled=True)
        assert not captured
        run.move(2)
        run.high.timed_drops["reward"].update_minutes(1, source="current_drop")
        assert (await run.next()).action == "reserved"
        assert captured[0].inputs.now == NOW + timedelta(seconds=2)
        assert captured[0].inputs.drops[0].observed_minutes == 1
        assert run.executor._reservation.certificate.slots[0].connect_at == NOW + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_provider_processing_uses_original_capture_budget(runtime):
    async with runtime() as run:
        captured = []

        def slow(bound):
            captured.append(bound.inputs.now)
            result = premises(bound)
            run.move(6)
            return result

        assert run.queue.request(horizon=HORIZON, provider=slow, enabled=True)
        assert await run.next() is None
        assert captured == [NOW]
        assert run.executor._reservation is None and not run.queue._active
        run.twitch.watch.assert_not_called()


@pytest.mark.parametrize("kind", ["unknown", "malformed", "coroutine", "changed"])
@pytest.mark.asyncio
async def test_unusable_provider_never_starts_or_retains_calendar(runtime, kind):
    async with runtime() as run:
        executed = []

        async def unexpected():
            executed.append(True)

        def provider(bound):
            if kind == "unknown":
                return None
            if kind == "malformed":
                return object()
            if kind == "coroutine":
                return unexpected()
            result = premises(bound)
            run.twitch.invalidate_deadline_context()
            return result

        assert run.queue.request(horizon=HORIZON, provider=provider, enabled=True)
        assert await run.next() is None
        assert run.executor._reservation is None and run.executor._guard._capture is None
        assert not executed and not run.queue._active
        run.twitch.watch.assert_not_called()


@pytest.mark.parametrize("operation", ["cancel", "disable", "detach", "restart", "manual", "wrong_task"])
@pytest.mark.asyncio
async def test_cancellation_resumes_normal_policy_and_retires_old_callbacks(runtime, monkeypatch, operation):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        old = run.scheduler.latest
        if operation == "cancel":
            run.queue.cancel()
        elif operation == "disable":
            assert not run.queue.request(horizon=HORIZON, enabled=False)
        elif operation == "detach":
            fallback = asyncio.Event()
            original_wait = run.twitch._wait_for_state_change

            async def wait_after_fallback():
                fallback.set()
                await original_wait()

            monkeypatch.setattr(run.twitch, "_wait_for_state_change", wait_after_fallback)
            run.twitch._set_deadline_queue(None)
        elif operation == "restart":
            run.twitch.restart_watching()
        elif operation == "manual":
            run.twitch.enter_manual_mode(run.twitch.channels[20])
        else:
            assert run.queue.checkpoint() is None
        assert old.cancelled and run.executor._reservation is None
        assert run.twitch._state_change.is_set()
        old.fire(force=True)
        if operation == "detach":
            await asyncio.wait_for(fallback.wait(), timeout=2)
            assert run.twitch.watching_channel.get_with_default(None).id == 10
        elif operation == "wrong_task":
            assert await run.next() is None  # The rejected external call was observed.
            assert await run.next() is None
        else:
            assert await run.next() is None
        if operation != "detach":
            assert run.twitch.watching_channel.get_with_default(None).id == (20 if operation == "manual" else 10)
        run.twitch._state_change.clear()
        old.fire(force=True)
        assert not run.twitch._state_change.is_set() and run.queue._timer is None


@pytest.mark.parametrize("selected", [20, 999])
@pytest.mark.asyncio
async def test_pending_manual_selection_is_checked_before_consuming_gui_getter(runtime, selected):
    async with runtime() as run:
        await run.reserve()
        run.twitch.gui._selected_channel_id = selected

        def consume():
            run.twitch.gui._selected_channel_id = None
            return run.twitch.channels.get(selected)

        run.twitch.gui.channels.get_selection.reset_mock()
        run.twitch.gui.channels.get_selection.side_effect = consume
        assert await run.tick(3) is None
        run.twitch.gui.channels.get_selection.assert_called_once_with()
        assert run.executor._reservation is None
        assert run.twitch.watching_channel.get_with_default(None).id == (20 if selected == 20 else 10)


@pytest.mark.parametrize("operation", ["cancel", "detach", "close"])
@pytest.mark.asyncio
async def test_display_callback_cannot_lose_cancellation_at_event_clear(runtime, operation):
    async with runtime() as run:
        await run.reserve()

        def display(*args, **kwargs):
            run.twitch.gui.display_drop.side_effect = None
            if operation == "cancel":
                run.queue.cancel()
            elif operation == "detach":
                run.twitch._set_deadline_queue(None)
            else:
                run.twitch.close()

        run.twitch.gui.display_drop.side_effect = display
        assert (await run.tick(3)).action == "watch"
        if operation == "cancel":
            assert await run.next() is None
            assert run.twitch.watching_channel.get_with_default(None).id == 10
        elif operation == "close":
            await asyncio.wait_for(run.root, timeout=2)
        else:
            assert run.twitch.watching_channel.get_with_default(None).id == 10
        assert run.executor._reservation is None and run.queue._timer is None


@pytest.mark.asyncio
async def test_request_from_status_callback_survives_normal_event_clear(runtime):
    async with runtime() as run:
        requested = []

        def status(*args, **kwargs):
            if not requested:
                requested.append(run.queue.request(horizon=HORIZON, provider=premises, enabled=True))

        run.twitch.gui.status.update.side_effect = status
        run.twitch._state_change.set()
        assert await run.next() is None
        assert (await run.next()).action == "reserved"
        assert requested == [True]


@pytest.mark.parametrize("change", ["auth_epoch", "user", "runner", "state"])
@pytest.mark.asyncio
async def test_timer_does_not_wake_replacement_context(runtime, change):
    async with runtime() as run:
        await run.reserve()
        old = run.scheduler.latest
        twitch = run.twitch
        owner = twitch._run_task
        if change == "auth_epoch":
            twitch._auth_state.context_epoch = object()
        elif change == "user":
            twitch._auth_state.user_id += 1
        elif change == "runner":
            twitch._run_task = asyncio.current_task()
        else:
            twitch._state = State.IDLE
        old.fire()
        assert not twitch._state_change.is_set()
        assert run.executor._reservation is None and run.queue._timer is None
        twitch._run_task = owner


@pytest.mark.parametrize("operation", ["games", "inventory", "cache", "foreign_claim"])
@pytest.mark.asyncio
async def test_public_policy_refresh_and_claim_requests_immediately_retire_calendar(runtime, operation):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        old = run.scheduler.latest
        if operation == "games":
            run.twitch.request_games_update()
        elif operation == "foreign_claim":
            assert copy.copy(run.urgent.timed_drops["urgent"])._apply_claim_result(True)
        else:
            Twitch.request_inventory_refresh(run.twitch, clear_cache=operation == "cache")
        assert old.cancelled and run.executor._reservation is None
        assert run.twitch._state_change.is_set()
        # Stop before processing refresh I/O. This proves cancellation, NOT safe reentry.
        run.twitch.close()
        old.fire(force=True)
        await asyncio.wait_for(run.root, timeout=2)
        assert run.queue._timer is None


@pytest.mark.asyncio
async def test_root_cancellation_releases_calendar_before_cleanup_await(runtime):
    async with runtime() as run:
        await run.reserve()
        old = run.scheduler.latest
        run.root.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run.root
        assert old.cancelled and run.executor._reservation is None
        # Consume the expected parent cancellation so the harness can verify cleanup.
        run.root = asyncio.create_task(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_timer_cancel_failure_still_retires_bindings_and_client_epoch(runtime):
    async with runtime() as run:
        await run.reserve()
        old_epoch = run.twitch._deadline_context_epoch
        run.queue._timer.cancel = MagicMock(side_effect=RuntimeError("synthetic cancel failure"))
        with pytest.raises(RuntimeError, match="synthetic cancel failure"):
            run.twitch.invalidate_deadline_context()
        assert run.twitch._deadline_context_epoch is not old_epoch
        assert run.executor._reservation is None and run.queue._scope is None
        assert run.queue._timer is None


@pytest.mark.parametrize("error", [
    RuntimeError, asyncio.CancelledError, ValueError, KeyError, TypeError, AttributeError, OverflowError,
])
@pytest.mark.asyncio
async def test_provider_exception_propagates_and_retires_owned_work(runtime, error):
    async with runtime() as run:
        def provider(bound):
            raise error("synthetic provider failure")

        assert run.queue.request(horizon=HORIZON, provider=provider, enabled=True)
        with pytest.raises(error):
            await run.next()
        with pytest.raises(error):
            await run.root
        assert run.executor._guard._capture is None and run.executor._reservation is None
        assert run.queue._scope is None and run.queue._timer is None
        run.root = asyncio.create_task(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_provider_cannot_reenter_queue_during_original_capture(runtime):
    async with runtime() as run:
        def provider(bound):
            assert not run.queue.request(horizon=HORIZON, provider=premises, enabled=True)
            return premises(bound)

        await run.reserve(provider)
        assert run.queue._pending is None


@pytest.mark.asyncio
async def test_nested_general_invalidation_during_watch_cannot_rearm_old_calendar(runtime):
    async with runtime() as run:
        await run.reserve()

        def invalidate(*args, **kwargs):
            run.twitch.gui.channels.set_watching.side_effect = None
            run.twitch.invalidate_deadline_context()

        run.twitch.gui.channels.set_watching.side_effect = invalidate
        assert await run.tick(3) is None
        assert run.executor._reservation is None and run.queue._timer is None
        assert [call.args[0].id for call in run.twitch.watch.call_args_list] == [20, 10]


@pytest.mark.asyncio
async def test_actual_authentication_change_cancels_before_replacing_identity(runtime, monkeypatch):
    async with runtime() as run:
        await run.reserve()
        old = run.scheduler.latest
        monkeypatch.setattr(run.twitch._inventory_service, "clear_cached_state", MagicMock())
        async with run.twitch.authentication_change():
            assert old.cancelled and run.executor._reservation is None
            assert run.queue._timer is None
            run.twitch.close()
            old.fire(force=True)
        await asyncio.wait_for(run.root, timeout=2)
        assert run.twitch._state is State.EXIT


@pytest.mark.parametrize("failure", ["stale", "missed", "rank", "model", "offline"])
@pytest.mark.asyncio
async def test_failed_revalidation_returns_to_ordinary_policy_without_new_calendar(runtime, failure):
    async with runtime() as run:
        await run.reserve()
        if failure == "rank":
            run.twitch.settings.games_to_watch.reverse()
        elif failure == "model":
            run.twitch.inventory = list(run.twitch.inventory)
        elif failure == "offline":
            run.twitch.channels[20]._stream = None
        elif failure == "stale":
            run.high.timed_drops["reward"]._progress_observation["received_at"] = (
                NOW - timedelta(minutes=6)
            ).isoformat()
        assert await run.tick(6 if failure == "missed" else 3) is None
        assert run.executor._reservation is None and run.queue._timer is None
        assert run.twitch.watching_channel.get_with_default(None).id == 10
        run.twitch.watch.assert_not_called()


@pytest.mark.asyncio
async def test_attach_is_idempotent_and_replacement_cannot_reuse_old_callback(runtime):
    async with runtime() as run:
        await run.reserve()
        old = run.scheduler.latest
        epoch = run.twitch._deadline_context_epoch
        run.twitch._set_deadline_queue(run.queue)
        assert epoch is run.twitch._deadline_context_epoch and not old.cancelled
        replacement = DeadlineCheckpointQueue(run.twitch)
        run.twitch._set_deadline_queue(replacement)
        assert old.cancelled and run.executor._reservation is None
        run.twitch._state_change.clear()
        old.fire(force=True)
        assert not run.twitch._state_change.is_set()
        assert replacement._pending is None and not replacement._active


@pytest.mark.asyncio
async def test_same_account_session_renewal_retires_rescue_and_resumes_ranked_policy(runtime):
    async with runtime() as run:
        await run.reserve()
        assert (await run.tick(3)).action == "watch"
        old = run.scheduler.latest
        owner = run.twitch._run_task
        auth = run.twitch._auth_state
        auth.browser_active = True
        auth.access_token, auth.device_id = "old-test-token", "test-device"
        run.twitch._client_type = ClientType.WEB
        run.twitch._http_client = MagicMock()
        run.twitch._gql_client = MagicMock()
        auth.accept_imported_identity(BrowserIdentity(
            auth.user_id, "renewed-test-token", "test-device", ClientType.WEB.USER_AGENT,
        ))
        assert old.cancelled and run.executor._reservation is None
        assert run.twitch._run_task is owner and not owner.done()
        assert await run.next() is None
        assert run.twitch.watching_channel.get_with_default(None).id == 10
        run.twitch._state_change.clear()
        old.fire(force=True)
        assert not run.twitch._state_change.is_set() and run.queue._timer is None


@pytest.mark.parametrize("boundary", ["watch", "stop"])
@pytest.mark.parametrize("state", [State.EXIT, State.IDLE])
@pytest.mark.asyncio
async def test_state_change_inside_executor_action_is_not_lost_on_refusal(runtime, monkeypatch, boundary, state):
    async with runtime() as run:
        await run.reserve()
        if boundary == "stop":
            assert (await run.tick(3)).action == "watch"
            run.urgent.timed_drops["urgent"].is_claimed = True
        reached = asyncio.Event()
        original_wait = run.twitch._wait_for_state_change

        async def wait_barrier():
            if run.twitch._state is state:
                reached.set()
            await original_wait()

        monkeypatch.setattr(run.twitch, "_wait_for_state_change", wait_barrier)
        target = run.twitch.gui.channels.set_watching if boundary == "watch" else run.twitch.gui.clear_drop

        def change_once(*args, **kwargs):
            target.side_effect = None
            run.twitch.close() if state is State.EXIT else run.twitch.change_state(state)

        target.side_effect = change_once
        assert await run.tick(3 if boundary == "watch" else 60) is None
        if state is State.EXIT:
            waiter = asyncio.create_task(reached.wait())
            try:
                done, _ = await asyncio.wait((run.root, waiter), timeout=2,
                                             return_when=asyncio.FIRST_COMPLETED)
                assert run.root in done, "Exit was stranded at a wait instead of completing"
                await run.root
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        else:
            await asyncio.wait_for(reached.wait(), timeout=2)
            assert run.twitch.watching_channel.get_with_default(None) is None
        assert [call.args[0].id for call in run.twitch.watch.call_args_list] == [20]
        assert run.executor._reservation is None and run.queue._timer is None


@pytest.mark.parametrize("boundary,detach", [("watch", False), ("display", False), ("display", True)])
@pytest.mark.parametrize("operation", ["cancel", "replace"])
@pytest.mark.asyncio
async def test_retiring_runner_cannot_fall_back_even_after_queue_detachment(runtime, boundary, detach, operation):
    async with runtime() as run:
        await run.reserve()
        owner = run.twitch._run_task
        replacements = []
        target = run.twitch.gui.channels.set_watching if boundary == "watch" else run.twitch.gui.display_drop

        def retire_once(*args, **kwargs):
            target.side_effect = None
            assert asyncio.current_task() is owner
            if detach:
                run.twitch._set_deadline_queue(None)
            if operation == "cancel":
                owner.cancel()
            else:
                replacement = asyncio.create_task(asyncio.sleep(0))
                replacements.append(replacement)
                run.twitch._run_task = replacement

        target.side_effect = retire_once
        run.move(3)
        run.scheduler.latest.fire()
        outcome = "returned"
        try:
            try:
                await asyncio.wait_for(asyncio.shield(run.root), timeout=2)
            except asyncio.CancelledError:
                outcome = "cancelled"
            except TimeoutError:
                outcome = "stranded"
            calls = [call.args[0].id for call in run.twitch.watch.call_args_list]
        finally:
            run.twitch._run_task = owner
            await asyncio.gather(*replacements)
            if run.root.cancelled():
                run.root = asyncio.create_task(asyncio.sleep(0))
        assert outcome == ("cancelled" if operation == "cancel" else "returned")
        assert calls == [20]
        assert run.executor._reservation is None and run.queue._timer is None
