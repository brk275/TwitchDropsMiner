"""Explicit conditional calendar execution, disconnected from production selection."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Literal

from src.services.deadline_certificate import RescueCertificate
from src.services.deadline_context import BoundDeadlineInputs, DeadlineContextGuard, _freeze
from src.services.deadline_forecast import _Decline
from src.services.deadline_inputs import DeadlineInputAdapter, ProjectionError, _utc
from src.services.deadline_planner import ConditionalDeadlinePlanner, PlanningAssumptions


if TYPE_CHECKING:
    from src.core.client import Twitch


@dataclass(frozen=True)
class CalendarStep:
    reason: str
    action: Literal["reserved", "watch", "stop", "wait", "complete", "refused"]
    wake_at: datetime | None = None


@dataclass
class _Reservation:
    twitch: object = field(repr=False)
    bound: BoundDeadlineInputs = field(repr=False)
    certificate: RescueCertificate = field(repr=False)
    objects: tuple[object, ...] = field(repr=False)
    values: tuple = field(repr=False)
    zone: object = field(repr=False)
    allowance: timedelta
    started_tick: float
    epoch: object = field(repr=False)
    last_wall: datetime
    last_tick: float
    checked_at: datetime
    slot: int = 0
    started: bool = False
    admitted: bool = False
    closed: bool = False


class DeadlineCalendarExecutor:
    """Follow, never shorten or greedily replace, a conditional fixed calendar.

    Only a caller owning serialized CHANNEL_SWITCH checkpoints may call these
    synchronous methods. No runtime constructs this class. There is no setting,
    timer, automatic reevaluation, refresh recovery or future-premise provider.
    wake_at is a request for a future checkpoint, not permission for a timer to
    switch channels. Each checkpoint must explicitly remain enabled.

    Supplied complete availability/allocation and successful timing bounds remain
    hypothetical. Every connection reserves additional bounded dispatch allowance;
    a late dispatch may consume that allowance but never the watch reservation.
    Identity/policy changes, queued work and unknown evidence retire the calendar.
    The caller must then return to ordinary policy; this class does not override
    manual intent, attempt claims, or claim safety after a premise is violated.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic,
                 utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._clock, self._utc_clock = clock, utc_clock
        self._clock_high_water: float | None = None
        self._clock_failed = False
        self._guard = DeadlineContextGuard(clock=self._tick)
        self._planner = ConditionalDeadlinePlanner(clock=self._tick, utc_clock=utc_clock)
        self._reservation: _Reservation | None = None

    def cancel(self) -> None:
        self._reservation = None
        self._guard.retire()
        self._clock_failed = False

    def _refuse(self, reason: str) -> CalendarStep:
        if self._clock_failed and reason != "disabled":
            reason = "clock_changed"
        self.cancel()
        return CalendarStep(reason, "refused")

    def _tick(self) -> float:
        tick = self._clock()
        if type(tick) not in (int, float) or not math.isfinite(tick):
            raise ValueError("Invalid calendar clock")
        if self._clock_high_water is not None and tick < self._clock_high_water:
            self._clock_failed = True
            raise ValueError("Invalid calendar clock")
        self._clock_high_water = float(tick)
        return float(tick)

    def _time(self) -> tuple[datetime, float]:
        return _utc(self._utc_clock()), self._tick()

    @staticmethod
    def _identity(twitch: Twitch) -> tuple[tuple[object, ...], tuple]:
        settings, auth = twitch.settings, twitch._auth_state
        objects: list[object] = [twitch, settings, auth, auth.context_epoch, twitch.inventory,
                                 twitch._campaigns, twitch._drops, twitch.channels,
                                 twitch._channel_service, twitch._watch_service]
        values: list[object] = [auth.user_id, settings.games_to_watch, settings.drop_name_blacklist,
                                settings.mining_benefits, settings.mining_hours, settings.priority_list_only,
                                settings.allow_unlinked_campaigns, settings.connection_quality,
                                tuple(game.id for game in twitch.wanted_games)]
        for campaign in twitch.inventory:
            objects.extend((campaign, campaign.game, campaign.timed_drops))
            values.append((campaign.id, campaign.game.id, campaign.game.name, campaign._valid,
                           campaign.linked, campaign.starts_at, campaign.ends_at,
                           tuple(channel.id for channel in campaign.allowed_channels)))
            for channel in campaign.allowed_channels:
                objects.extend((channel, channel._stream))
            for drop in campaign.drops:
                objects.append(drop)
                values.append((drop.id, drop.name, drop.required_minutes, drop.starts_at, drop.ends_at,
                               drop.precondition_drops,
                               tuple((benefit.id, benefit.name, benefit.type.name) for benefit in drop.benefits)))
        for channel in twitch.channels.values():
            objects.extend((channel, channel._stream))
            values.append((channel.id, channel.acl_based, channel.banned,
                           None if channel._stream is None else channel._stream.broadcast_id,
                           None if channel.game is None else (channel.game.id, channel.game.name),
                           channel.drops_enabled))
        return tuple(objects), _freeze(values)

    @staticmethod
    def _matches(saved: _Reservation, twitch: Twitch) -> bool:
        objects, values = DeadlineCalendarExecutor._identity(twitch)
        return (len(objects) == len(saved.objects)
                and all(a is b for a, b in zip(objects, saved.objects, strict=True))
                and values == saved.values and twitch._deadline_context_epoch is saved.epoch)

    def reserve(self, twitch: Twitch, *, horizon: datetime,
                assumptions: PlanningAssumptions | None = None, enabled: bool = False,
                local_timezone: str | timezone | None = None,
                decision_delay: timedelta = timedelta(seconds=3),
                dispatch_allowance: timedelta = timedelta(seconds=2)) -> CalendarStep:
        self.cancel()
        if enabled is not True:
            return CalendarStep("disabled", "refused")
        try:
            if (not isinstance(decision_delay, timedelta) or not timedelta(0) < decision_delay
                    or not isinstance(dispatch_allowance, timedelta)
                    or not timedelta(0) < dispatch_allowance <= timedelta(seconds=5)
                    or decision_delay + dispatch_allowance >= timedelta(seconds=15)):
                return self._refuse("invalid_context")
            now, tick = self._time()
            result = self._guard.capture(twitch, now=now, horizon=horizon, enabled=True,
                                         local_timezone=local_timezone, include_channels=True)
            if result.bound is None:
                return self._refuse(result.reason)
            bound = result.bound
            latest = now + decision_delay + dispatch_allowance
            if latest >= bound.valid_until:
                return self._refuse("expired")
            if assumptions is None:
                return self._refuse("unknown_coverage")
            if not isinstance(assumptions, PlanningAssumptions):
                return self._refuse("invalid_snapshot")
            timing = assumptions.timing
            setup = self._planner._duration(timing.setup, timedelta(days=1))
            padded = replace(assumptions, timing=replace(timing, setup=replace(
                setup, upper=setup.upper + dispatch_allowance,
            )))
            planned = self._planner._plan(bound, padded, not_before=now + decision_delay)
            if planned.certificate is None:
                return self._refuse(planned.reason)
            objects, values = self._identity(twitch)
            checked, before_validation = self._time()
            if checked < now or before_validation < tick:
                return self._refuse("clock_changed")
            current = self._guard.validate(bound, twitch, now=checked, enabled=True,
                                           local_timezone=local_timezone)
            if current != "current":
                return self._refuse(current)
            after, finished = self._time()
            if (after < checked or finished < max(tick, before_validation)
                    or abs((after - now).total_seconds() - (finished - tick)) > 1):
                return self._refuse("clock_changed")
            effective = max(after, now + timedelta(seconds=finished - tick))
            if effective > latest or effective >= bound.valid_until:
                return self._refuse("expired")
            if not self._fresh(bound, latest):
                return self._refuse("unknown_progress")
            self._reservation = _Reservation(twitch, bound, planned.certificate, objects, values,
                                             local_timezone, dispatch_allowance, tick,
                                             twitch._deadline_context_epoch, after, finished, effective)
            return CalendarStep("rescue", "reserved", planned.certificate.slots[0].connect_at)
        except _Decline as exc:
            return self._refuse(exc.reason)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return self._refuse("invalid_context")

    def _fresh(self, bound: BoundDeadlineInputs, now: datetime) -> bool:
        return all(drop.observed_at is not None
                   and timedelta(0) <= now - drop.observed_at <= self._planner.max_observation_age
                   for drop in bound.inputs.drops)

    def advance(self, twitch: Twitch, *, enabled: bool = False,
                local_timezone: str | timezone | None = None) -> CalendarStep:
        if enabled is not True:
            return self._refuse("disabled")
        saved = self._reservation
        if saved is None or saved.twitch is not twitch:
            return self._refuse("superseded")
        try:
            now, tick = self._time()
            if not self._consistent(saved, now, tick):
                return self._refuse("clock_changed")
            if local_timezone != saved.zone:
                return self._refuse("superseded")
            # The initial binding cannot be replaced by a fresh capture to revive
            # queued work that changed while a proposal waited for admission.
            if not saved.admitted:
                current = self._guard.validate(saved.bound, twitch, now=now, enabled=True,
                                               local_timezone=local_timezone)
                if current != "current":
                    return self._refuse(current)
                bound = saved.bound
            else:
                capture = self._guard.capture(twitch, now=now, horizon=saved.bound.inputs.horizon,
                                              enabled=True, local_timezone=local_timezone, include_channels=True)
                if capture.bound is None:
                    return self._refuse(capture.reason)
                bound = capture.bound
            if not self._matches(saved, twitch):
                return self._refuse("superseded")
            models = {DeadlineInputAdapter._key(item.id, drop.id): drop
                      for item in twitch.inventory for drop in item.drops}
            certificate, slots = saved.certificate, saved.certificate.slots
            remaining_connections = slots[saved.slot + int(saved.started or saved.closed):]
            next_required = next((item for item in remaining_connections
                                  if models[item.drop_id].is_claimed is not True), None)
            latest = None if next_required is None else next_required.connect_at + saved.allowance
            checked = self._check(saved, bound, twitch, local_timezone, latest=latest)
            if checked is not None:
                return self._refuse(checked)
            now = self._opening_time(saved)
            for completion in certificate.completions:
                if completion.claim_ready_at <= saved.checked_at and models[completion.drop_id].is_claimed is not True:
                    return self._refuse("claim_overdue")
            if saved.slot == len(slots):
                if all(models[item.drop_id].is_claimed is True for item in certificate.completions):
                    self.cancel()
                    return CalendarStep("finished", "complete")
                return CalendarStep("awaiting_claim", "wait", self._wake(saved, max(
                    item.claim_ready_at for item in certificate.completions
                )))
            slot = slots[saved.slot]
            if saved.closed:
                if now < slot.until:
                    self._record_time(saved)
                    return CalendarStep("not_due", "wait", self._wake(saved, slot.until))
                saved.slot += 1
                saved.closed = False
                self._record_time(saved)
                return CalendarStep("slot_finished", "wait", self._wake(saved, slots[saved.slot].connect_at)
                                    if saved.slot < len(slots)
                                    else self._wake(saved, max(item.claim_ready_at for item in certificate.completions)))
            if saved.started:
                expected = next((channel for channel in twitch.channels.values()
                                 if str(channel.id) == slot.channel_id), None)
                watched = twitch.watching_channel.get_with_default(None)
                if watched is not expected:
                    return self._refuse("superseded")
                if now >= slot.until or models[slot.drop_id].is_claimed is True:
                    checked = self._check(saved, bound, twitch, local_timezone, latest=latest)
                    if checked is not None:
                        return self._refuse(checked)
                    now = self._opening_time(saved)
                    twitch.stop_watching()
                    saved.epoch = twitch._deadline_context_epoch
                    saved.started = False
                    saved.closed = True
                    if now >= slot.until:
                        saved.slot += 1
                        saved.closed = False
                    return CalendarStep("slot_finished", "stop", self._wake(saved, slot.until) if now < slot.until else (
                        self._wake(saved, slots[saved.slot].connect_at) if saved.slot < len(slots)
                        else self._wake(saved, max(item.claim_ready_at for item in certificate.completions))
                    ))
                self._record_time(saved)
                return CalendarStep("watching", "wait", self._wake(saved, slot.until))
            if models[slot.drop_id].is_claimed is True:
                saved.closed = True
                return CalendarStep("settled_fragment", "wait", self._wake(saved, slot.until))
            if now < slot.connect_at:
                self._record_time(saved)
                return CalendarStep("not_due", "wait", self._wake(saved, slot.connect_at))
            if now > slot.connect_at + saved.allowance:
                return self._refuse("missed_slot")
            route = next((item for item in bound.channels.candidates
                          if item.channel_id == slot.channel_id), None) if bound.channels else None
            if route is None or not route.watchable or slot.drop_id not in route.eligible_drop_ids:
                return self._refuse("checkpoint_mismatch")
            if not self._fresh(bound, now):
                return self._refuse("unknown_progress")
            for drop in bound.inputs.drops:
                if not any(item.drop_id == drop.id for item in slots[saved.slot:]):
                    continue
                capacity = sum((item.until - max(item.watch_from, now) for item in slots[saved.slot:]
                                if item.drop_id == drop.id and item.until > max(item.watch_from, now)), timedelta(0))
                if drop.observed_minutes is None or timedelta(minutes=drop.required_minutes - drop.observed_minutes) > capacity:
                    return self._refuse("work_exceeds_reservation")
            channel = next(channel for channel in twitch.channels.values() if str(channel.id) == slot.channel_id)
            checked = self._check(saved, bound, twitch, local_timezone,
                                  latest=slot.connect_at + saved.allowance)
            if checked is not None:
                return self._refuse(checked)
            twitch.watch(channel)
            saved.started, saved.admitted = True, True
            saved.epoch = twitch._deadline_context_epoch
            return CalendarStep("watching", "watch", slot.until)
        except (AttributeError, KeyError, StopIteration, TypeError, ValueError, OverflowError, ProjectionError):
            return self._refuse("invalid_context")

    def _check(self, saved, bound, twitch, zone, *, latest=None):
        now, tick = self._time()
        if not self._consistent(saved, now, tick):
            return "clock_changed"
        if latest is not None and (now > latest
                                   or tick - saved.started_tick > (latest - saved.bound.inputs.now).total_seconds()):
            return "missed_slot"
        current = self._guard.validate(bound, twitch, now=now, enabled=True, local_timezone=zone)
        if current != "current":
            return current
        after, finished = self._time()
        if after < now or finished < tick or not self._consistent(saved, after, finished):
            return "clock_changed"
        effective = max(after, saved.bound.inputs.now + timedelta(seconds=finished - saved.started_tick))
        if effective >= bound.valid_until:
            return "expired"
        if latest is not None and effective > latest:
            return "missed_slot"
        if not self._fresh(bound, effective):
            return "unknown_progress"
        saved.last_wall, saved.last_tick, saved.checked_at = after, finished, effective
        return None

    def _record_time(self, saved: _Reservation) -> None:
        wall, tick = self._time()
        if not self._consistent(saved, wall, tick):
            raise ValueError("Invalid calendar clock")
        saved.last_wall, saved.last_tick = wall, tick

    @staticmethod
    def _opening_time(saved: _Reservation) -> datetime:
        return min(saved.last_wall, saved.bound.inputs.now + timedelta(seconds=saved.last_tick - saved.started_tick))

    @staticmethod
    def _wake(saved: _Reservation, stamp: datetime) -> datetime:
        elapsed = saved.bound.inputs.now + timedelta(seconds=saved.last_tick - saved.started_tick)
        return max(stamp, saved.last_wall, saved.last_wall + max(timedelta(0), stamp - elapsed))

    @staticmethod
    def _consistent(saved: _Reservation, wall: datetime, tick: float) -> bool:
        return (wall >= saved.last_wall and tick >= saved.last_tick
                and abs((wall - saved.bound.inputs.now).total_seconds() - (tick - saved.started_tick)) <= 1)
