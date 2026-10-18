"""Context-bound conditional planning, not permission to steer a live miner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Literal

from src.services.deadline_certificate import (
    CertifiedDeadlineForecaster,
    DropTimingBounds,
    RescueCertificate,
    TimingBounds,
)
from src.services.deadline_channels import DeadlineChannelInputs
from src.services.deadline_context import BoundDeadlineInputs, ContextReason, DeadlineContextGuard
from src.services.deadline_forecast import ForecastReason, ForecastSnapshot, ViewingWindow, _Decline
from src.services.deadline_inputs import ProjectionError


if TYPE_CHECKING:
    from src.core.client import Twitch


PlanningReason = ContextReason | ForecastReason | Literal["checkpoint_mismatch"]


@dataclass(frozen=True)
class PlanningAssumptions:
    viewing_windows: tuple[ViewingWindow, ...]
    timing: TimingBounds
    baseline_complete: bool = False


@dataclass(frozen=True)
class ConditionalPlanningResult:
    reason: PlanningReason
    certificate: RescueCertificate | None = None
    valid_until: datetime | None = None


class ConditionalDeadlinePlanner(CertifiedDeadlineForecaster):
    """Join read-only real inputs and explicit hypothetical certificate premises.

    The caller must own the serialized CHANNEL_SWITCH checkpoint. Matching that
    checkpoint does not prove future selector equivalence, uptime, credit or claims.
    No runtime invokes this planner. Its result is not an execution capability and
    must never be reused to act without fresh policy, context and eligibility checks.
    Observation freshness is checked as of capture; valid_until is not a freshness lease.
    An existing watched object does not establish transport readiness: every first
    slot pays its supplied setup bound, even on the currently watched channel.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic,
                 utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        super().__init__()
        self._guard = DeadlineContextGuard(clock=clock)
        self._utc_clock = utc_clock

    def plan(self, twitch: Twitch, *, horizon: datetime,
             assumptions: PlanningAssumptions | None = None, enabled: bool = False,
             local_timezone: str | timezone | None = None) -> ConditionalPlanningResult:
        if enabled is not True:
            return ConditionalPlanningResult("disabled")
        try:
            captured = self._guard.capture(
                twitch, now=self._utc_clock(), horizon=horizon, enabled=True,
                local_timezone=local_timezone, include_channels=True,
            )
            if captured.bound is None:
                return ConditionalPlanningResult(captured.reason)
            bound = captured.bound
            try:
                result = self._plan(bound, assumptions)
            except (ProjectionError, _Decline, _PlanningRefusal) as exc:
                result = ConditionalPlanningResult(exc.reason)
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                result = ConditionalPlanningResult("invalid_snapshot")
            current = self._guard.validate(
                bound, twitch, now=self._utc_clock(), enabled=True, local_timezone=local_timezone,
            )
            if current != "current":
                return ConditionalPlanningResult(current)
            return ConditionalPlanningResult(result.reason, result.certificate,
                                             bound.valid_until if result.certificate is not None else None)
        except (ProjectionError, _Decline) as exc:
            return ConditionalPlanningResult(exc.reason)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return ConditionalPlanningResult("invalid_context")

    def _plan(self, bound: BoundDeadlineInputs,
              assumptions: PlanningAssumptions | None) -> ConditionalPlanningResult:
        if assumptions is None:
            return ConditionalPlanningResult("unknown_coverage")
        if not isinstance(assumptions, PlanningAssumptions):
            return ConditionalPlanningResult("invalid_snapshot")
        if assumptions.baseline_complete is not True:
            return ConditionalPlanningResult("unknown_coverage")
        timing = assumptions.timing
        if not isinstance(timing, TimingBounds) or not isinstance(timing.drops, tuple):
            return ConditionalPlanningResult("invalid_snapshot")
        if len(timing.drops) > 256:
            return ConditionalPlanningResult("budget_exceeded")
        claims = {}
        for item in timing.drops:
            if (not isinstance(item, DropTimingBounds) or type(item.drop_id) is not str
                    or item.drop_id in claims):
                return ConditionalPlanningResult("invalid_snapshot")
            claims[item.drop_id] = self._duration(item.claim_delay, timedelta(days=1)).upper
        snapshot = bound.inputs.to_forecast(claim_delays=claims, viewing_windows=assumptions.viewing_windows)
        state = self._prepare(snapshot)
        if bound.channels is None:
            return ConditionalPlanningResult("unknown_coverage")
        mismatch = self._match_current_routes(state, bound.channels)
        if mismatch is not None:
            raise _PlanningRefusal(mismatch)
        result = self.certify(state, timing, enabled=True, baseline_complete=True)
        if result.certificate is not None:
            first = result.certificate.slots[0]
            routes = (*bound.channels.candidates, bound.channels.watching)
            if not any(route is not None and route.watchable and route.channel_id == first.channel_id
                       and first.drop_id in route.eligible_drop_ids for route in routes):
                raise _PlanningRefusal("checkpoint_mismatch")
        return ConditionalPlanningResult(result.reason, result.certificate)

    @staticmethod
    def _match_current_routes(state: ForecastSnapshot, channels: DeadlineChannelInputs
                              ) -> Literal["checkpoint_mismatch", "unknown_coverage"] | None:
        candidates = {route.channel_id: route for route in channels.candidates}
        watching = channels.watching
        routes = dict(candidates)
        if watching is not None:
            previous = candidates.get(watching.channel_id)
            if previous is not None and (
                previous.game_id, previous.acl_based, previous.mining_priority,
                previous.watchable, previous.eligible_drop_ids,
            ) != (
                watching.game_id, watching.acl_based, watching.mining_priority,
                watching.watchable, watching.eligible_drop_ids,
            ):
                return "checkpoint_mismatch"
            routes[watching.channel_id] = watching
        ready = frozenset(drop.id for drop in state.drops if not drop.prerequisite_ids
                          and drop.starts_at <= state.now < drop.ends_at)
        represented: set[str] = set()
        for window in state.viewing_windows:
            credit = window.drop_ids.intersection(ready)
            if not credit or not window.starts_at <= state.now < window.ends_at:
                continue
            route = routes.get(window.channel_id)
            tracked = candidates.get(window.channel_id)
            order = len(candidates) if tracked is None else tracked.channel_order
            if (route is None or not route.watchable or not credit.issubset(route.eligible_drop_ids)
                    or window.mining_priority != route.mining_priority
                    or window.acl_based is not route.acl_based or window.channel_order != order):
                return "checkpoint_mismatch"
            represented.add(window.channel_id)
        if any(route.watchable and route.channel_id not in represented for route in routes.values()):
            return "unknown_coverage"
        return None


class _PlanningRefusal(ValueError):
    def __init__(self, reason: Literal["checkpoint_mismatch", "unknown_coverage"]):
        self.reason = reason
        super().__init__(reason)
