"""Pure conditional forecasts, not a live mining policy or availability prediction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import product
from sys import maxsize
from typing import Literal

from src.services.channel_switch_policy import ChannelSwitchPolicy


ForecastReason = Literal[
    "disabled", "manual_override", "invalid_snapshot", "unknown_progress",
    "unknown_allocation", "budget_exceeded", "incomplete_horizon", "no_ranked_work",
    "no_safe_rescue", "unknown_timing", "unknown_coverage", "rescue",
]


@dataclass(frozen=True)
class ForecastDrop:
    id: str
    game_id: int
    required_minutes: int
    observed_minutes: int | None
    observed_at: datetime | None
    starts_at: datetime
    ends_at: datetime
    prerequisite_ids: tuple[str, ...] = ()
    claimed: bool = False
    wanted: bool = True
    claim_delay: timedelta = timedelta(seconds=20)


@dataclass(frozen=True)
class MiningWindow:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class ViewingWindow:
    channel_id: str
    starts_at: datetime
    ends_at: datetime
    drop_ids: frozenset[str]
    allocation_known: bool = False
    channel_order: int = 0
    acl_based: bool = False
    mining_priority: tuple[int, int] | None = None


@dataclass(frozen=True)
class ForecastSnapshot:
    now: datetime
    horizon: datetime
    ranked_game_ids: tuple[int, ...]
    drops: tuple[ForecastDrop, ...]
    viewing_windows: tuple[ViewingWindow, ...]
    mining_windows: tuple[MiningWindow, ...]
    current_channel_id: str | None = None


@dataclass(frozen=True)
class DropTimingOptions:
    drop_id: str
    progress_offsets: tuple[timedelta, ...] | None = None
    claim_delays: tuple[timedelta, ...] | None = None


@dataclass(frozen=True)
class TimingUncertainty:
    """Finite alternatives, not endpoints standing in for a continuous interval."""

    drops: tuple[DropTimingOptions, ...]
    switch_delays: tuple[timedelta, ...] | None = None


@dataclass(frozen=True)
class RescuePlan:
    drop_id: str
    game_id: int
    channel_id: str
    earned_at: datetime
    protected_drop_ids: frozenset[str]


@dataclass(frozen=True)
class ForecastResult:
    reason: ForecastReason
    plan: RescuePlan | None = None
    baseline_earned_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Choice:
    channel_id: str
    game_id: int
    credits: frozenset[str]
    starts_at: datetime
    channel_order: int
    acl_based: bool
    mining_priority: tuple[int, int]


@dataclass
class _Run:
    earned: dict[str, datetime]
    first: _Choice | None = None


@dataclass(frozen=True)
class _Timing:
    progress_offsets: dict[str, timedelta]
    claim_delays: dict[str, timedelta]
    switch_delay: timedelta = timedelta(0)


class _Decline(Exception):
    def __init__(self, reason: ForecastReason):
        self.reason = reason


@dataclass
class _Budget:
    steps: int
    work: int = 1_000_000

    def spend(self, work: int) -> None:
        self.steps -= 1
        self.work -= work
        if self.steps < 0 or self.work < 0:
            raise _Decline("budget_exceeded")


class DeadlineForecaster:
    """Compare one-reward detours without changing settings, models or channels.

    Viewing windows declare conditional, simultaneous credit, not an inferred Twitch
    allocation rule. The future adapter must reject uncertain allocation/availability,
    enforce real eligibility, and invalidate decisions when its snapshot changes.
    analyze() uses exact inputs. analyze_robust() exhausts explicitly supplied finite
    timing alternatives, not continuous uncertainty or unknown future claim failures.
    """

    def __init__(self, *, max_steps: int = 10_000,
                 max_observation_age: timedelta = timedelta(minutes=5)):
        if type(max_steps) is not int or not 1 <= max_steps <= 100_000:
            raise ValueError("Invalid forecast step budget")
        if not isinstance(max_observation_age, timedelta) or not timedelta(0) <= max_observation_age <= timedelta(days=1):
            raise ValueError("Invalid observation age bound")
        self.max_steps = max_steps
        self.max_observation_age = max_observation_age

    def analyze(self, snapshot: ForecastSnapshot, *, enabled: bool = False,
                manual_override: bool = False) -> ForecastResult:
        if enabled is not True:
            return ForecastResult("disabled")
        if type(manual_override) is not bool:
            return ForecastResult("invalid_snapshot")
        if manual_override:
            return ForecastResult("manual_override")
        try:
            state = self._prepare(snapshot)
            return self._compare(state, (_Timing({}, {}),), _Budget(self.max_steps))
        except _Decline as exc:
            return ForecastResult(exc.reason)

    def analyze_robust(self, snapshot: ForecastSnapshot, uncertainty: TimingUncertainty, *,
                       enabled: bool = False, manual_override: bool = False) -> ForecastResult:
        """Require one immediate action to pass every finite timing combination."""
        if enabled is not True:
            return ForecastResult("disabled")
        if type(manual_override) is not bool:
            return ForecastResult("invalid_snapshot")
        if manual_override:
            return ForecastResult("manual_override")
        try:
            state = self._prepare(snapshot)
            budget = _Budget(self.max_steps)
            known_ids = frozenset(drop.id for drop in snapshot.drops)
            return self._compare(state, self._timings(state, uncertainty, budget, known_ids), budget)
        except _Decline as exc:
            return ForecastResult(exc.reason)

    def _compare(self, state: ForecastSnapshot, timings: tuple[_Timing, ...], budget: _Budget) -> ForecastResult:
        ranks = {game: rank for rank, game in enumerate(state.ranked_game_ids)}
        drops = {drop.id: drop for drop in state.drops}
        baselines = []
        baseline_ranks = []
        earned_union: set[str] = set()
        refusal: ForecastReason | None = None
        for timing in timings:
            baseline = self._simulate(state, drops, ranks, budget, timing=timing)
            earned = frozenset(baseline.earned)
            earned_union.update(earned)
            if any(
                not drop.claimed and drop.id not in earned and drop.ends_at > state.horizon
                for drop in state.drops
            ):
                refusal = "incomplete_horizon"
            if baseline.first is None or baseline.first.starts_at != state.now:
                if refusal is None:
                    refusal = "no_ranked_work"
            else:
                baseline_ranks.append(ranks[baseline.first.game_id])
            baselines.append(baseline)
        if refusal is not None:
            return ForecastResult(refusal, baseline_earned_ids=frozenset(earned_union))
        candidates = sorted((
            drop for drop in state.drops
            if drop.wanted and not drop.claimed and drop.id not in earned_union
            and all(ranks[drop.game_id] > rank for rank in baseline_ranks)
            and drop.starts_at < state.horizon and drop.ends_at > state.now
        ), key=lambda drop: (drop.ends_at, ranks[drop.game_id], drop.id))
        if len(candidates) > 32:
            raise _Decline("budget_exceeded")
        for target in candidates:
            rescue_ids = self._ancestors(target.id, drops)
            protected_union: set[str] = set()
            action: tuple[str, int] | None = None
            earned_at = state.now
            for timing, baseline in zip(timings, baselines, strict=True):
                detour = self._simulate(state, drops, ranks, budget, rescue_ids, timing)
                protected = frozenset(
                    key for key in baseline.earned if ranks[drops[key].game_id] <= ranks[target.game_id]
                )
                first = detour.first
                if (
                    target.id not in detour.earned or not protected.issubset(detour.earned)
                    or first is None or first.starts_at != state.now
                    or not first.credits.intersection(rescue_ids)
                ):
                    break
                game = min(
                    (drops[key].game_id for key in first.credits.intersection(rescue_ids)),
                    key=ranks.__getitem__,
                )
                if action is not None and action != (first.channel_id, game):
                    break
                action = (first.channel_id, game)
                earned_at = max(earned_at, detour.earned[target.id])
                protected_union.update(protected)
            else:
                if action is not None:
                    return ForecastResult("rescue", RescuePlan(
                        target.id, action[1], action[0], earned_at, frozenset(protected_union),
                    ), frozenset(earned_union))
        return ForecastResult("no_safe_rescue", baseline_earned_ids=frozenset(earned_union))

    @staticmethod
    def _alternatives(value: tuple[timedelta, ...] | None, maximum: timedelta) -> tuple[timedelta, ...]:
        if value is None:
            raise _Decline("unknown_timing")
        if not isinstance(value, tuple) or not value:
            raise _Decline("invalid_snapshot")
        if len(value) > 64:
            raise _Decline("budget_exceeded")
        if any(not isinstance(item, timedelta) or not timedelta(0) <= item <= maximum for item in value):
            raise _Decline("invalid_snapshot")
        return tuple(sorted(set(value)))

    def _timings(self, state: ForecastSnapshot, uncertainty: TimingUncertainty, budget: _Budget,
                 known_ids: frozenset[str]) -> tuple[_Timing, ...]:
        if not isinstance(uncertainty, TimingUncertainty) or not isinstance(uncertainty.drops, tuple):
            raise _Decline("invalid_snapshot")
        if len(uncertainty.drops) > 256:
            raise _Decline("budget_exceeded")
        options: dict[str, DropTimingOptions] = {}
        for option in uncertainty.drops:
            if (
                not isinstance(option, DropTimingOptions) or not isinstance(option.drop_id, str)
                or option.drop_id not in known_ids or option.drop_id in options
            ):
                raise _Decline("invalid_snapshot")
            options[option.drop_id] = option
        labels = [("switch", "")]
        choices = [self._alternatives(uncertainty.switch_delays, timedelta(days=1))]
        count = len(choices[0])
        for drop in state.drops:
            if drop.claimed:
                continue
            option = options.get(drop.id)
            if option is None:
                raise _Decline("unknown_timing")
            remaining = timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0))
            for kind, alternatives, maximum in (
                ("progress", option.progress_offsets, remaining),
                ("claim", option.claim_delays, timedelta(days=1)),
            ):
                values = self._alternatives(alternatives, maximum)
                count *= len(values)
                if count > 64:
                    raise _Decline("budget_exceeded")
                labels.append((kind, drop.id))
                choices.append(values)
        variants = []
        for combination in product(*choices):
            budget.spend(len(labels))
            progress, claims = {}, {}
            for (kind, key), value in zip(labels, combination, strict=True):
                if kind == "progress":
                    progress[key] = value
                elif kind == "claim":
                    claims[key] = value
            variants.append(_Timing(progress, claims, combination[0]))
        return tuple(variants)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        try:
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise _Decline("invalid_snapshot")
            return value.astimezone(timezone.utc)
        except (OverflowError, ValueError, TypeError):
            raise _Decline("invalid_snapshot") from None

    def _prepare(self, state: ForecastSnapshot) -> ForecastSnapshot:
        if not isinstance(state, ForecastSnapshot) or any(not isinstance(value, tuple) for value in (
            state.ranked_game_ids, state.drops, state.viewing_windows, state.mining_windows,
        )):
            raise _Decline("invalid_snapshot")
        if len(state.drops) > 256 or len(state.viewing_windows) > 512 or len(state.mining_windows) > 512:
            raise _Decline("budget_exceeded")
        if (
            any(not isinstance(drop, ForecastDrop) for drop in state.drops)
            or any(not isinstance(window, ViewingWindow) for window in state.viewing_windows)
            or any(not isinstance(window, MiningWindow) for window in state.mining_windows)
        ):
            raise _Decline("invalid_snapshot")
        now, horizon = self._utc(state.now), self._utc(state.horizon)
        if state.current_channel_id is not None and (not isinstance(state.current_channel_id, str) or not state.current_channel_id):
            raise _Decline("invalid_snapshot")
        if not timedelta(0) < horizon - now <= timedelta(days=14):
            raise _Decline("invalid_snapshot")
        ranked = state.ranked_game_ids
        if (
            len(ranked) > 256 or any(type(game) is not int or game <= 0 for game in ranked)
            or len(set(ranked)) != len(ranked)
        ):
            raise _Decline("invalid_snapshot")
        drops: dict[str, ForecastDrop] = {}
        for drop in state.drops:
            if (
                not isinstance(drop.id, str) or not drop.id or drop.id in drops
                or type(drop.game_id) is not int or drop.game_id <= 0
                or type(drop.required_minutes) is not int or not 1 <= drop.required_minutes <= 525_600
                or type(drop.claimed) is not bool or type(drop.wanted) is not bool
                or not isinstance(drop.claim_delay, timedelta)
                or not timedelta(0) <= drop.claim_delay <= timedelta(days=1)
                or not isinstance(drop.prerequisite_ids, tuple)
                or len(drop.prerequisite_ids) > 256
                or any(not isinstance(key, str) or not key for key in drop.prerequisite_ids)
            ):
                raise _Decline("invalid_snapshot")
            start, end = self._utc(drop.starts_at), self._utc(drop.ends_at)
            if start >= end:
                raise _Decline("invalid_snapshot")
            drops[drop.id] = replace(drop, starts_at=start, ends_at=end)
        needed: set[str] = set()
        for drop in drops.values():
            if drop.wanted and drop.game_id in ranked and not drop.claimed:
                needed.update(self._ancestors(drop.id, drops))
        for key in needed:
            drop = drops[key]
            if drop.claimed:
                continue
            minutes = drop.observed_minutes
            if minutes is None or drop.observed_at is None:
                raise _Decline("unknown_progress")
            if type(minutes) is not int or not 0 <= minutes <= drop.required_minutes:
                raise _Decline("invalid_snapshot")
            observed = self._utc(drop.observed_at)
            if not timedelta(0) <= now - observed <= self.max_observation_age:
                raise _Decline("unknown_progress")
        windows: list[ViewingWindow] = []
        by_channel: dict[str, list[ViewingWindow]] = {}
        for window in state.viewing_windows:
            start, end = self._utc(window.starts_at), self._utc(window.ends_at)
            if (
                not isinstance(window.channel_id, str) or not window.channel_id or start >= end
                or not isinstance(window.drop_ids, frozenset) or not window.drop_ids
                or not window.drop_ids.issubset(drops)
                or type(window.channel_order) is not int or window.channel_order < 0
                or type(window.acl_based) is not bool
            ):
                raise _Decline("invalid_snapshot")
            priority = window.mining_priority
            if priority is not None and (
                not isinstance(priority, tuple) or len(priority) != 2
                or any(type(value) is not int for value in priority)
                or priority[0] not in (0, 1) or not 0 <= priority[1] <= maxsize
            ):
                raise _Decline("invalid_snapshot")
            normalized = replace(window, starts_at=start, ends_at=end)
            by_channel.setdefault(window.channel_id, []).append(normalized)
            if window.drop_ids.intersection(needed) and start < horizon and end > now:
                if window.allocation_known is not True:
                    raise _Decline("unknown_allocation")
                windows.append(normalized)
        for channel_windows in by_channel.values():
            ordered = sorted(channel_windows, key=lambda item: item.starts_at)
            if any(a.ends_at > b.starts_at for a, b in zip(ordered, ordered[1:], strict=False)):
                raise _Decline("invalid_snapshot")
        mining: list[MiningWindow] = []
        normalized_mining = sorted((
            MiningWindow(self._utc(window.starts_at), self._utc(window.ends_at))
            for window in state.mining_windows
        ), key=lambda item: item.starts_at)
        for mining_window in normalized_mining:
            if mining_window.starts_at >= mining_window.ends_at:
                raise _Decline("invalid_snapshot")
            if mining and mining[-1].ends_at >= mining_window.starts_at:
                previous = mining[-1]
                mining[-1] = MiningWindow(previous.starts_at, max(previous.ends_at, mining_window.ends_at))
            else:
                mining.append(mining_window)
        return ForecastSnapshot(now, horizon, tuple(ranked), tuple(
            drops[key] for key in sorted(needed)
        ), tuple(windows), tuple(mining), state.current_channel_id)

    @staticmethod
    def _ancestors(key: str, drops: dict[str, ForecastDrop]) -> frozenset[str]:
        visiting: set[str] = set()
        complete: set[str] = set()

        def visit(current: str) -> None:
            if current in visiting or current not in drops:
                raise _Decline("invalid_snapshot")
            if current in complete:
                return
            visiting.add(current)
            drop = drops[current]
            if not drop.claimed:
                for parent in drop.prerequisite_ids:
                    if parent not in drops or drops[parent].game_id != drop.game_id:
                        raise _Decline("invalid_snapshot")
                    visit(parent)
            visiting.remove(current)
            complete.add(current)

        visit(key)
        return frozenset(complete)

    @staticmethod
    def _simulate(state: ForecastSnapshot, drops: dict[str, ForecastDrop], ranks: dict[int, int],
                  budget: _Budget, rescue_ids: frozenset[str] = frozenset(), timing: _Timing | None = None) -> _Run:
        timing = timing or _Timing({}, {})
        remaining = {
            key: timedelta(minutes=drop.required_minutes - (drop.observed_minutes or 0))
            - timing.progress_offsets.get(key, timedelta(0))
            for key, drop in drops.items() if not drop.claimed
        }
        claim_ready = {key: state.now for key, drop in drops.items() if drop.claimed}
        run = _Run({key: state.now for key, duration in remaining.items() if not duration})
        for key in run.earned:
            claim_ready[key] = state.now + min(timing.claim_delays.get(key, drops[key].claim_delay), state.horizon - state.now)
        boundaries = {state.horizon}
        intervals: tuple[ForecastDrop | ViewingWindow | MiningWindow, ...] = (
            *state.drops, *state.viewing_windows, *state.mining_windows,
        )
        for interval in intervals:
            boundaries.update((interval.starts_at, interval.ends_at))
        time = state.now
        selected_channel = state.current_channel_id
        setup_until = time
        while time < state.horizon:
            window_count = len(state.viewing_windows)
            budget.spend(1 + len(drops) * max(1, window_count) + len(boundaries)
                         + window_count * max(1, window_count.bit_length()))
            watchable = frozenset(
                key for key, duration in remaining.items()
                if (duration > timedelta(0) or claim_ready.get(key, time) > time)
                and drops[key].starts_at <= time < drops[key].ends_at
                and all(parent in claim_ready and claim_ready[parent] <= time for parent in drops[key].prerequisite_ids)
            )
            ready = frozenset(key for key in watchable if remaining[key] > timedelta(0))
            choices = []
            if any(window.starts_at <= time < window.ends_at for window in state.mining_windows):
                for window in state.viewing_windows:
                    eligible = window.drop_ids.intersection(watchable)
                    if window.starts_at <= time < window.ends_at and eligible:
                        # The real selector can retain completed, unclaimed work.
                        # Occupancy is distinct from additional watch-time credit.
                        credits = eligible.intersection(ready)
                        game = min((drops[key].game_id for key in eligible), key=ranks.__getitem__)
                        choices.append(_Choice(
                            window.channel_id, game, credits, time, window.channel_order,
                            window.acl_based, window.mining_priority or (0, ranks[game]),
                        ))
            # Only preempt while the target/ancestor work still needs watching.
            rescue_choices = [choice for choice in choices if choice.credits.intersection(rescue_ids)]
            candidates = sorted(rescue_choices or choices, key=lambda choice: (
                choice.mining_priority, choice.channel_order, choice.channel_id,
            ))
            current = next((choice for choice in candidates if choice.channel_id == selected_channel), None)
            active = next((choice for choice in candidates if ChannelSwitchPolicy.should_switch(
                choice.mining_priority, choice.acl_based,
                current_priority=current.mining_priority if current is not None else None,
                current_acl_based=current.acl_based if current is not None else False,
            )), current)
            if active is None:
                selected_channel = None
            else:
                if run.first is None:
                    run.first = active
                if active.channel_id != selected_channel:
                    selected_channel = active.channel_id
                    setup_until = time + min(timing.switch_delay, state.horizon - time)
            future = [stamp for stamp in boundaries.union(claim_ready.values()) if stamp > time]
            end = min(future)
            if active is not None and time < setup_until:
                time = min(end, setup_until)
                continue
            duration = end - time
            if active is not None and active.credits:
                duration = min(duration, *(remaining[key] for key in active.credits))
                for key in active.credits:
                    remaining[key] -= duration
                    if not remaining[key] and time + duration < drops[key].ends_at:
                        completed_at = time + duration
                        run.earned[key] = completed_at
                        delay = timing.claim_delays.get(key, drops[key].claim_delay)
                        claim_ready[key] = completed_at + min(delay, state.horizon - completed_at)
            time += duration
        return run
