"""Optional owned-loop checkpoint delivery, never channel actions from timers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol

from src.config import State
from src.services.deadline_execution import CalendarStep, DeadlineCalendarExecutor, PlanningProvider
from src.services.deadline_inputs import _utc
from src.services.inventory_progress import InventoryProgressReader, ProgressReadBatch


if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.drop import BaseDrop


class _ScheduledCall(Protocol):
    def cancel(self) -> None: ...


@dataclass(frozen=True)
class _Scope:
    runner: asyncio.Task = field(repr=False)
    auth: object = field(repr=False)
    auth_epoch: object = field(repr=False)
    user_id: int = field(repr=False)
    epoch: object = field(repr=False)


@dataclass(frozen=True)
class _Request:
    provider: PlanningProvider = field(repr=False)
    horizon: datetime
    zone: str | timezone | None
    decision_delay: timedelta
    dispatch_allowance: timedelta


class DeadlineCheckpointQueue:
    """Accept intent now, build inputs and act only in the registered miner task.

    Nothing constructs this bridge by default. Explicit source-only callers must
    supply a synchronous provider of hypothetical premises; no real availability,
    allocation or timing evidence is inferred. A pending request contains no plan.
    Provider work is charged inside the executor's original captured-time budget.

    Timer callbacks only set the state event. Generation, runner and account checks
    prevent retired callbacks from waking replacement work. Every ordinary context
    invalidation cancels the request/calendar; only the executor's own synchronous
    watch/stop in its owned checkpoint may keep it alive. Recognized confirmed claims
    request owned revalidation on the original calendar. Full inventory replacement
    reentry is not implemented. Never auto-rebase an interrupted certificate.
    Injected schedulers, like call_later, must defer callbacks on this event loop.
    """

    def __init__(self, twitch: Twitch, *, executor: DeadlineCalendarExecutor | None = None,
                 clock: Callable[[], float] = monotonic,
                 utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 schedule: Callable[[float, Callable[[], None]], _ScheduledCall] | None = None,
                 max_sleep: timedelta = timedelta(seconds=15), refresh_progress: bool = False):
        if not isinstance(max_sleep, timedelta) or not timedelta(0) < max_sleep <= timedelta(seconds=15):
            raise ValueError("Invalid checkpoint interval")
        if type(refresh_progress) is not bool:
            raise ValueError("Invalid progress refresh option")
        self._twitch = twitch
        self._executor = executor if executor is not None else DeadlineCalendarExecutor(clock=clock, utc_clock=utc_clock)
        self._utc_clock, self._schedule = utc_clock, schedule
        self._max_sleep = max_sleep.total_seconds()
        self._generation = object()
        self._execution_generation: object | None = None
        self._timer_id: object | None = None
        self._timer: _ScheduledCall | None = None
        self._scope: _Scope | None = None
        self._pending: _Request | None = None
        self._active = False
        self._due = False
        self._zone: str | timezone | None = None
        self._pending_claims: dict[str, BaseDrop] = {}
        self._handled_claims: dict[str, BaseDrop] = {}
        self._progress_reader = (InventoryProgressReader(twitch, clock=self._executor._tick, utc_clock=utc_clock)
                                 if refresh_progress else None)
        self._progress_task: asyncio.Task[ProgressReadBatch] | None = None
        self._progress_started_tick: float | None = None

    def belongs_to(self, twitch: Twitch) -> bool:
        return self._twitch is twitch

    def cancel(self) -> None:
        notify = (self._scope is not None and (self._active or self._pending is not None)
                  and self._current_scope(self._scope))
        self._generation = object()
        self._timer_id = None
        timer, self._timer = self._timer, None
        self._scope = self._pending = None
        self._active = False
        self._due = False
        self._zone = None
        self._pending_claims.clear()
        self._handled_claims.clear()
        task, self._progress_task = self._progress_task, None
        self._progress_started_tick = None
        if task is not None:
            if task.done():
                if not task.cancelled():
                    task.exception()
            elif not task.cancelling():  # type: ignore[attr-defined]  # Python 3.12 runtime.
                task.cancel()
        self._executor.cancel()
        if notify:
            self._twitch._state_change.set()
        if timer is not None:
            timer.cancel()

    def _current_scope(self, scope: _Scope) -> bool:
        twitch = self._twitch
        return (getattr(twitch, "_deadline_queue", None) is self
                and twitch._state is State.CHANNEL_SWITCH
                and getattr(twitch, "_run_task", None) is scope.runner
                and not scope.runner.done() and not scope.runner.cancelling()
                and twitch._auth_state is scope.auth
                and twitch._auth_state.context_epoch is scope.auth_epoch
                and twitch._auth_state.user_id == scope.user_id
                and twitch._deadline_context_epoch is scope.epoch)

    def request(self, *, horizon: datetime, provider: PlanningProvider | None = None,
                enabled: bool = False, local_timezone: str | timezone | None = None,
                decision_delay: timedelta = timedelta(seconds=3),
                dispatch_allowance: timedelta = timedelta(seconds=2)) -> bool:
        if enabled is not True:
            self.cancel()
            return False
        if (self._active or self._pending is not None or self._execution_generation is not None
                or not callable(provider)):
            return False
        twitch = self._twitch
        runner = getattr(twitch, "_run_task", None)
        if not isinstance(runner, asyncio.Task):
            return False
        auth = twitch._auth_state
        scope = _Scope(runner, auth, auth.context_epoch, auth.user_id, twitch._deadline_context_epoch)
        if not self._current_scope(scope):
            return False
        self._scope = scope
        self._pending = _Request(provider, horizon, local_timezone, decision_delay, dispatch_allowance)
        twitch._state_change.set()
        return True

    def request_claim(self, drop: BaseDrop) -> bool:
        if (self._execution_generation is not None or not self._active or self._scope is None
                or not self._current_scope(self._scope)
                or not self._executor.recognizes_claim(self._twitch, drop)):
            return False
        self._pending_claims[drop.id] = drop
        self._due = True
        self._twitch._state_change.set()
        return True

    def owns_claim_followup(self, drop: BaseDrop) -> bool:
        return (self._active and self._scope is not None and self._current_scope(self._scope)
                and (self._pending_claims.get(drop.id) is drop or self._handled_claims.get(drop.id) is drop)
                and self._executor.recognizes_claim(self._twitch, drop))

    def holds_claim_update(self) -> bool:
        return bool(self._pending_claims or self._handled_claims) and self.active_checkpoint()

    def allows_invalidation(self, action: Literal["watch", "stop"] | None) -> bool:
        if action not in ("watch", "stop") or self._execution_generation is not self._generation:
            return False
        scope = self._scope
        return scope is not None and self._current_scope(scope) and asyncio.current_task() is scope.runner

    def pending_checkpoint(self) -> bool:
        if self._pending is None and not self._due:
            return False
        if self._scope is None or not self._current_scope(self._scope):
            self.cancel()
            return False
        return True

    def active_checkpoint(self) -> bool:
        if not self._active or self._scope is None or not self._current_scope(self._scope):
            self.cancel()
            return False
        return True

    def checkpoint(self) -> CalendarStep | None:
        if self._pending is None and not self._active:
            return None
        scope = self._scope
        if scope is None or not self._current_scope(scope) or asyncio.current_task() is not scope.runner:
            self.cancel()
            return None
        generation = self._generation
        self._execution_generation = generation
        self._due = False
        try:
            if not self._consume_progress() or generation is not self._generation:
                return None
            pending, self._pending = self._pending, None
            if pending is not None:
                self._zone = pending.zone
                step = self._executor.reserve(
                    self._twitch, horizon=pending.horizon, provider=pending.provider,
                    enabled=True, local_timezone=pending.zone, decision_delay=pending.decision_delay,
                    dispatch_allowance=pending.dispatch_allowance,
                )
            else:
                step = self._executor.advance(self._twitch, enabled=True, local_timezone=self._zone,
                                              confirmed_claims=tuple(self._pending_claims.values()))
            if generation is not self._generation:
                return None
            if step.action in ("refused", "complete") or step.wake_at is None:
                self.cancel()
                return None
            self._active = True
            self._scope = replace(scope, epoch=self._twitch._deadline_context_epoch)
            if not self._current_scope(self._scope):
                self.cancel()
                return None
            self._handled_claims.update(self._pending_claims)
            self._pending_claims.clear()
            self._arm(step.wake_at)
            self._start_progress()
            if generation is not self._generation:
                return None
            return step
        except BaseException:
            self.cancel()
            raise
        finally:
            self._execution_generation = None

    def _consume_progress(self) -> bool:
        task, reader = self._progress_task, self._progress_reader
        if task is None or not task.done() or reader is None:
            return True
        self._progress_task = None
        generation = self._generation
        scope = self._scope
        reason = reader.apply(task.result(), is_current=lambda: generation is self._generation
                              and scope is not None and self._current_scope(scope))
        if generation is not self._generation:
            return False
        if reason in ("superseded", "inventory_changed"):
            self.cancel()
            if reason == "inventory_changed":
                self._twitch.request_inventory_refresh()
            return False
        return True

    def _start_progress(self) -> None:
        reader = self._progress_reader
        if reader is None or self._progress_task is not None or not self._executor.has_admitted_calendar(self._twitch):
            return
        if any(not task.done() for task in self._twitch._deadline_read_tasks):
            return
        tick = self._executor._tick()
        if self._progress_started_tick is not None and tick - self._progress_started_tick < 120:
            return
        request = reader.prepare()
        scope, generation = self._scope, self._generation
        if request is None or scope is None or not self._current_scope(scope):
            self.cancel()
            return
        self._progress_started_tick = tick
        task = self._progress_task = asyncio.create_task(reader.read(request))
        # Retain cancelled flights after detachment until account/shutdown drains them.
        self._twitch._deadline_read_tasks.add(task)

        def completed(finished: asyncio.Task[ProgressReadBatch]) -> None:
            self._twitch._deadline_read_tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()
            if (generation is self._generation and finished is self._progress_task
                    and self._scope is not None and self._current_scope(self._scope)):
                self._due = True
                self._twitch._state_change.set()

        task.add_done_callback(completed)

    def _arm(self, stamp: datetime) -> None:
        if self._timer is not None:
            self._timer.cancel()
        token = self._timer_id = object()
        delay = max(0.01, min(self._max_sleep, (_utc(stamp) - _utc(self._utc_clock())).total_seconds()))
        def callback():
            self._wake(token)
        self._timer = (self._schedule(delay, callback) if self._schedule is not None
                       else asyncio.get_running_loop().call_later(delay, callback))

    def _wake(self, token: object) -> None:
        if token is not self._timer_id:
            return
        self._timer_id, self._timer = None, None
        scope = self._scope
        if scope is None or not self._current_scope(scope):
            self.cancel()
            return
        self._due = True
        self._twitch._state_change.set()
