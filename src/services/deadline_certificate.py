"""Conditional fixed-calendar certificates for continuously bounded timing inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.services.deadline_forecast import (
    DeadlineForecaster,
    ForecastDrop,
    ForecastReason,
    ForecastSnapshot,
    _Budget,
    _Decline,
)


ZERO = timedelta(0)


@dataclass(frozen=True)
class DurationBounds:
    lower: timedelta
    upper: timedelta


@dataclass(frozen=True)
class DropTimingBounds:
    drop_id: str
    remaining: DurationBounds | None
    claim_delay: DurationBounds | None


@dataclass(frozen=True)
class TimingBounds:
    drops: tuple[DropTimingBounds, ...]
    setup: DurationBounds | None


@dataclass(frozen=True)
class WorkSlot:
    drop_id: str
    channel_id: str
    connect_at: datetime
    watch_from: datetime
    until: datetime


@dataclass(frozen=True)
class Completion:
    drop_id: str
    earned_at: datetime
    claim_ready_at: datetime


@dataclass(frozen=True)
class RescueCertificate:
    target_id: str
    blocker_id: str
    protected_drop_ids: frozenset[str]
    slots: tuple[WorkSlot, ...]
    completions: tuple[Completion, ...]


@dataclass(frozen=True)
class CertificateResult:
    reason: ForecastReason
    certificate: RescueCertificate | None = None


@dataclass(frozen=True)
class _DropBounds:
    remaining: DurationBounds
    claim_delay: DurationBounds


class _NoSchedule(Exception):
    pass


class CertifiedDeadlineForecaster(DeadlineForecaster):
    """Certify one sufficient fixed schedule, not the adaptive greedy policy.

    A continuously ready higher-ranked blocker proves the baseline cannot finish
    the target. Then reserve upper-bound work, claim waits and setup for the target
    chain and ALL higher/equal-ranked wanted rewards, a superset of baseline gains.
    Independent timing values anywhere inside the bounds fit that same calendar.

    This is deliberately incomplete: failure to find a certificate is not proof
    that no safe rescue exists. Windows/allocation and successful bounded claims
    remain explicit assumptions. Windows must exhaust possible baseline credit
    routes, not merely provide guaranteed capacity for the reserved schedule.
    There is no live eligibility adapter or steering.
    """

    def certify(self, snapshot: ForecastSnapshot, timing: TimingBounds, *,
                enabled: bool = False, manual_override: bool = False,
                baseline_complete: bool = False) -> CertificateResult:
        if enabled is not True:
            return CertificateResult("disabled")
        if type(manual_override) is not bool:
            return CertificateResult("invalid_snapshot")
        if manual_override:
            return CertificateResult("manual_override")
        if baseline_complete is not True:
            return CertificateResult("unknown_coverage")
        try:
            state = self._prepare(snapshot)
            budget = _Budget(self.max_steps)
            work, setup = self._bounds(state, timing, frozenset(drop.id for drop in snapshot.drops))
            drops = {drop.id: drop for drop in state.drops}
            ranks = {game: rank for rank, game in enumerate(state.ranked_game_ids)}
            candidates = sorted((drop for drop in state.drops if drop.wanted and not drop.claimed
                                 and drop.ends_at > state.now and drop.ends_at <= state.horizon
                                 and work[drop.id].remaining.lower > ZERO),
                                key=lambda drop: (drop.ends_at, ranks[drop.game_id], drop.id))
            if len(candidates) > 32:
                raise _Decline("budget_exceeded")
            for target in candidates:
                budget.spend(len(drops) + len(state.viewing_windows))
                blocker = self._blocker(state, drops, ranks, work, target, budget)
                if blocker is None:
                    continue
                protected = frozenset(drop.id for drop in state.drops if drop.wanted
                                      and not drop.claimed and drop.id != target.id
                                      and ranks[drop.game_id] <= ranks[target.game_id])
                roots = (target.id, *sorted(protected, key=lambda key: (
                    ranks[drops[key].game_id], drops[key].ends_at, key,
                )))
                try:
                    slots, completed = self._schedule(state, drops, work, setup, roots, budget)
                except _NoSchedule:
                    continue
                if slots and slots[0].connect_at == state.now:
                    return CertificateResult("rescue", RescueCertificate(
                        target.id, blocker.id, protected, tuple(slots), tuple(completed),
                    ))
            return CertificateResult("no_safe_rescue")
        except _Decline as exc:
            return CertificateResult(exc.reason)

    @staticmethod
    def _duration(value: DurationBounds | None, maximum: timedelta) -> DurationBounds:
        if value is None:
            raise _Decline("unknown_timing")
        if (not isinstance(value, DurationBounds) or not isinstance(value.lower, timedelta)
                or not isinstance(value.upper, timedelta) or not ZERO <= value.lower <= value.upper <= maximum):
            raise _Decline("invalid_snapshot")
        return value

    def _bounds(self, state: ForecastSnapshot, timing: TimingBounds, known: frozenset[str]
                ) -> tuple[dict[str, _DropBounds], DurationBounds]:
        if not isinstance(timing, TimingBounds) or not isinstance(timing.drops, tuple):
            raise _Decline("invalid_snapshot")
        if len(timing.drops) > 256:
            raise _Decline("budget_exceeded")
        options: dict[str, DropTimingBounds] = {}
        for item in timing.drops:
            if (not isinstance(item, DropTimingBounds) or not isinstance(item.drop_id, str)
                    or item.drop_id not in known or item.drop_id in options):
                raise _Decline("invalid_snapshot")
            options[item.drop_id] = item
        setup = self._duration(timing.setup, timedelta(days=1))
        needed: dict[str, _DropBounds] = {}
        for drop in state.drops:
            if drop.claimed:
                continue
            item = options.get(drop.id)
            if item is None:
                raise _Decline("unknown_timing")
            needed[drop.id] = _DropBounds(
                self._duration(item.remaining, timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0))),
                self._duration(item.claim_delay, timedelta(days=1)),
            )
        return needed, setup

    @staticmethod
    def _blocker(state: ForecastSnapshot, drops: dict[str, ForecastDrop], ranks: dict[int, int],
                 work: dict[str, _DropBounds], target: ForecastDrop, budget: _Budget) -> ForecastDrop | None:
        for blocker in state.drops:
            budget.spend(1 + len(blocker.prerequisite_ids))
            if (blocker.claimed or ranks[blocker.game_id] >= ranks[target.game_id]
                    or not blocker.starts_at <= state.now < target.ends_at <= blocker.ends_at
                    or work[blocker.id].remaining.lower < target.ends_at - state.now
                    or any(not drops[key].claimed for key in blocker.prerequisite_ids)):
                continue
            budget.spend(sum(1 + len(window.drop_ids) for window in state.viewing_windows))
            if not any(window.starts_at <= state.now and window.ends_at >= target.ends_at
                       and blocker.id in window.drop_ids for window in state.viewing_windows):
                continue
            # A higher-ranked channel could otherwise award the target in parallel.
            if any(target.id in window.drop_ids and window.starts_at < target.ends_at
                   and window.ends_at > state.now and any(
                       key in drops and ranks[drops[key].game_id] <= ranks[blocker.game_id]
                       for key in window.drop_ids
                   ) for window in state.viewing_windows):
                continue
            return blocker
        return None

    def _schedule(self, state: ForecastSnapshot, drops: dict[str, ForecastDrop],
                  work: dict[str, _DropBounds], setup: DurationBounds,
                  roots: tuple[str, ...], budget: _Budget) -> tuple[list[WorkSlot], list[Completion]]:
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(key: str) -> None:
            if key in seen or drops[key].claimed:
                return
            budget.spend(1 + len(drops[key].prerequisite_ids))
            for parent in drops[key].prerequisite_ids:
                visit(parent)
            seen.add(key)
            ordered.append(key)

        for key in roots:
            visit(key)
        slots: list[WorkSlot] = []
        completed: list[Completion] = []
        cursor = state.now
        for key in ordered:
            drop = drops[key]
            cursor = max(cursor, drop.starts_at)
            remaining = work[key].remaining.upper
            if cursor >= min(drop.ends_at, state.horizon):
                raise _NoSchedule
            while remaining > ZERO:
                budget.spend(len(state.viewing_windows))
                viewing = [window for window in state.viewing_windows if key in window.drop_ids]
                budget.spend(max(1, len(viewing) * len(state.mining_windows)))
                options = []
                for window in viewing:
                    for mining in state.mining_windows:
                        start = max(cursor, window.starts_at, mining.starts_at)
                        end = min(drop.ends_at, state.horizon, window.ends_at, mining.ends_at)
                        delay = ZERO if (not slots and start == state.now
                                         and window.channel_id == state.current_channel_id) else setup.upper
                        if end > start and delay < end - start:
                            options.append((start + delay, window.channel_order, window.channel_id, end, start))
                if not options:
                    raise _NoSchedule
                watch, _, channel, end, connect = min(options)
                duration = min(remaining, end - watch)
                cursor = watch + duration
                slots.append(WorkSlot(key, channel, connect, watch, cursor))
                remaining -= duration
            if cursor >= drop.ends_at or work[key].claim_delay.upper > state.horizon - cursor:
                raise _NoSchedule
            ready = cursor + work[key].claim_delay.upper
            completed.append(Completion(key, cursor, ready))
            cursor = ready
        return slots, completed
