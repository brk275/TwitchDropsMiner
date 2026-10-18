"""Short-lived context checks for experimental inputs, never permission to mine."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Literal

from src.config import State
from src.services.deadline_channels import DeadlineChannelAdapter, DeadlineChannelInputs
from src.services.deadline_inputs import (
    DeadlineInputAdapter,
    DeadlineModelInputs,
    ProjectionError,
    ProjectionReason,
    _utc,
)


if TYPE_CHECKING:
    from src.core.client import Twitch


ContextReason = ProjectionReason | Literal[
    "disabled", "current", "superseded", "expired", "inactive", "invalid_context",
]


class _Refusal(ValueError):
    def __init__(self, reason: ContextReason):
        self.reason = reason
        super().__init__(reason)


def _freeze(value: object) -> tuple:
    """Copy only explicitly selected, bounded non-secret state, with strict types."""
    remaining = 32_768

    def visit(item: object, depth: int) -> tuple:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            raise _Refusal("budget_exceeded")
        if item is None or type(item) in (bool, int):
            return type(item).__name__, item
        if type(item) is str:
            if len(item) > 4096:
                raise _Refusal("budget_exceeded")
            return "str", item
        if type(item) is float and math.isfinite(item):
            return "float", item
        if isinstance(item, datetime):
            return "datetime", _utc(item)
        if isinstance(item, (list, tuple)) and type(item) in (list, tuple):
            if len(item) > remaining:
                raise _Refusal("budget_exceeded")
            return type(item).__name__, tuple(visit(child, depth + 1) for child in item)
        if type(item) is dict:
            if len(item) > remaining or any(type(key) is not str or len(key) > 4096 for key in item):
                raise _Refusal("invalid_context")
            return "mapping", tuple((key, visit(item[key], depth + 1)) for key in sorted(item))
        raise _Refusal("invalid_context")

    return visit(value, 0)


@dataclass(frozen=True)
class BoundDeadlineInputs:
    inputs: DeadlineModelInputs = field(repr=False)
    valid_until: datetime
    channels: DeadlineChannelInputs | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ContextResult:
    reason: ContextReason
    bound: BoundDeadlineInputs | None = None


@dataclass(frozen=True)
class _Context:
    objects: tuple[object, ...] = field(repr=False)
    values: tuple = field(repr=False)
    boundaries: tuple[datetime, ...]

    def matches(self, other: _Context) -> bool:
        return (len(self.objects) == len(other.objects)
                and all(a is b for a, b in zip(self.objects, other.objects, strict=True))
                and self.values == other.values and self.boundaries == other.boundaries)


@dataclass(frozen=True)
class _Capture:
    bound: BoundDeadlineInputs
    context: _Context = field(repr=False)
    started: float
    zone: tuple
    last_wall: datetime
    last_tick: float


class DeadlineContextGuard:
    """Bind one capture to a quiescent client's current non-secret state.

    This is not an execution capability or proof of availability/baseline coverage.
    A future controller must revalidate synchronously immediately before action,
    without an intervening await, and still enforce eligibility and a valid policy.
    The caller must own the serialized CHANNEL_SWITCH checkpoint; enum state alone
    cannot establish quiescence for direct concurrent service calls outside it.
    No runtime invokes this guard and no settings enable it. Each refusal latches;
    restored values cannot revive a rejected or superseded capture.
    Optional expires_at only shortens the same exclusive UTC/monotonic lifetime.
    """

    def __init__(self, *, max_age: timedelta = timedelta(seconds=15),
                 clock: Callable[[], float] = monotonic):
        if not isinstance(max_age, timedelta) or not timedelta(0) < max_age <= timedelta(seconds=30):
            raise ValueError("Invalid context lifetime")
        self._max_age = max_age
        self._clock = clock
        self._adapter = DeadlineInputAdapter()
        self._channels = DeadlineChannelAdapter()
        self._capture: _Capture | None = None

    def _tick(self) -> float:
        tick = self._clock()
        if type(tick) not in (int, float) or not math.isfinite(tick):
            raise _Refusal("invalid_context")
        return float(tick)

    def retire(self) -> None:
        """Release the current private binding without reading or changing a client."""
        self._capture = None

    @staticmethod
    def _zone(value: str | timezone | None) -> tuple:
        if value is None or type(value) is str:
            return _freeze(value)
        if isinstance(value, timezone):
            return _freeze(value.utcoffset(None).total_seconds())
        raise _Refusal("unknown_timezone")

    @staticmethod
    def _context(twitch: Twitch, now: datetime) -> _Context:
        auth = twitch._auth_state
        flags = (twitch._changing_auth, twitch._games_update_pending,
                 twitch._inventory_refresh_pending, twitch._clear_cache_pending)
        if (any(type(flag) is not bool for flag in flags)
                or type(twitch._inventory_loaded) is not bool
                or type(auth.user_id) is not int or auth.user_id <= 0
                or type(auth.context_epoch) is not object
                or type(twitch._deadline_context_epoch) is not object):
            raise _Refusal("invalid_context")
        if (any(flags) or not twitch._inventory_loaded or auth._logged_in.is_set() is not True
                or twitch._state is not State.CHANNEL_SWITCH or twitch.is_mining_time() is not True):
            raise _Refusal("inactive")
        manual = twitch.is_manual_mode()
        if type(manual) is not bool:
            raise _Refusal("invalid_context")
        if (manual or twitch._manual_target_channel is not None or twitch._manual_target_game is not None
                or twitch.gui._selected_channel_id is not None):
            raise _Refusal("manual_override")
        settings, inventory, channels = twitch.settings, twitch.inventory, twitch.channels
        if (type(settings.priority_list_only) is not bool or type(settings.allow_unlinked_campaigns) is not bool
                or type(settings.connection_quality) is not int):
            raise _Refusal("invalid_context")
        if (not isinstance(inventory, (list, tuple)) or not isinstance(channels, dict)
                or not isinstance(twitch._campaigns, dict) or not isinstance(twitch._drops, dict)):
            raise _Refusal("invalid_context")
        if (len(inventory) > 512 or len(channels) > 199 or len(twitch._drops) > 256
                or len(twitch._campaigns) > 512 or not isinstance(twitch.wanted_games, (list, tuple))
                or len(twitch.wanted_games) > 256):
            raise _Refusal("budget_exceeded")
        watched = twitch.watching_channel.get_with_default(None)
        objects: list[object] = [twitch, auth, settings, inventory, channels,
                                 twitch._campaigns, twitch._drops, watched,
                                 auth.context_epoch, twitch._deadline_context_epoch]
        for service in (getattr(twitch, "_channel_service", None), getattr(twitch, "_watch_service", None)):
            objects.extend((service, getattr(service, "_twitch", None)))
        values: list[object] = [auth.user_id, (
            settings.games_to_watch, settings.drop_name_blacklist, settings.mining_benefits,
            settings.mining_hours, settings.priority_list_only, settings.allow_unlinked_campaigns,
            settings.connection_quality,
        ), tuple(game.id for game in twitch.wanted_games)]
        boundaries: list[datetime] = []
        campaign_ids: set[str] = set()
        drop_ids: set[str] = set()
        seen_channels: set[int] = set()
        channel_edges = 0

        def channel_state(channel) -> None:
            nonlocal channel_edges
            channel_edges += 1
            if channel_edges > 4096:
                raise _Refusal("budget_exceeded")
            # Relationships matter even when the same node's state was visited earlier.
            objects.extend((channel, channel._twitch))
            if id(channel) in seen_channels:
                return
            seen_channels.add(id(channel))
            if len(seen_channels) > 512:
                raise _Refusal("budget_exceeded")
            stream = channel._stream
            objects.extend((stream, channel._pending_stream_up))
            values.append((channel.id, channel.acl_based, channel.banned, channel._banned,
                           channel._ban_expires_at, channel._ban_permanent, channel._watch_broadcast_id,
                           None if stream is None else (
                               stream.broadcast_id, stream.viewers, stream.drops_enabled,
                               None if stream.game is None else (stream.game.id, stream.game.name),
                           )))

        for campaign in inventory:
            if (type(campaign.id) is not str or not campaign.id or campaign.id in campaign_ids
                    or twitch._campaigns.get(campaign.id) is not campaign
                    or type(campaign.progress_revision) is not int or campaign.progress_revision < 0
                    or not isinstance(campaign.timed_drops, dict)):
                raise _Refusal("invalid_context")
            campaign_ids.add(campaign.id)
            if len(campaign.timed_drops) + len(drop_ids) > 256 or len(campaign.allowed_channels) > 512:
                raise _Refusal("budget_exceeded")
            objects.extend((campaign, campaign._twitch, campaign.game, campaign.timed_drops))
            boundaries.extend((_utc(campaign.starts_at), _utc(campaign.ends_at)))
            values.append((campaign.id, campaign.game.id, campaign.game.name, campaign._valid, campaign.linked,
                           campaign.starts_at, campaign.ends_at, campaign.progress_revision,
                           tuple(channel.id for channel in campaign.allowed_channels)))
            for channel in campaign.allowed_channels:
                channel_state(channel)
            for key, drop in campaign.timed_drops.items():
                if (type(key) is not str or key != drop.id or key in drop_ids
                        or twitch._drops.get(key) is not drop or drop.campaign is not campaign):
                    raise _Refusal("invalid_context")
                drop_ids.add(key)
                if (not isinstance(drop.benefits, (list, tuple)) or len(drop.benefits) > 256
                        or not isinstance(drop.precondition_drops, (list, tuple))
                        or len(drop.precondition_drops) > 256):
                    raise _Refusal("budget_exceeded")
                objects.extend((drop, drop._twitch))
                boundaries.extend((_utc(drop.starts_at), _utc(drop.ends_at)))
                values.append((drop.id, drop.name, drop.required_minutes, drop.real_current_minutes,
                               drop.extra_current_minutes, drop.is_claimed, drop.starts_at, drop.ends_at,
                               drop.precondition_drops, DeadlineInputAdapter._observation(drop, now),
                               tuple((benefit.id, benefit.name, benefit.type.name) for benefit in drop.benefits)))
        if set(twitch._campaigns) != campaign_ids or set(twitch._drops) != drop_ids:
            raise _Refusal("invalid_context")
        for channel_id, channel in channels.items():
            if type(channel_id) is not int or channel_id != channel.id:
                raise _Refusal("invalid_context")
            channel_state(channel)
        if watched is not None:
            channel_state(watched)
        return _Context(tuple(objects), _freeze(values), tuple(boundaries))

    def capture(self, twitch: Twitch, *, now: datetime, horizon: datetime, enabled: bool = False,
                local_timezone: str | timezone | None = None, include_channels: bool = False,
                expires_at: datetime | None = None) -> ContextResult:
        self._capture = None
        if enabled is not True:
            return ContextResult("disabled")
        if type(include_channels) is not bool:
            return ContextResult("invalid_context")
        try:
            now = _utc(now)
            deadline = None if expires_at is None else _utc(expires_at)
            if deadline is not None and deadline <= now:
                return ContextResult("expired")
            started, zone = self._tick(), self._zone(local_timezone)
            context = self._context(twitch, now)
            result = self._adapter.capture(twitch, now=now, horizon=horizon, local_timezone=local_timezone)
            if result.inputs is None:
                return ContextResult(result.reason)
            inputs = result.inputs
            channels = self._channels.capture(twitch, inputs) if include_channels else None
            if include_channels and not context.matches(self._context(twitch, now)):
                return ContextResult("superseded")
            edges = (*context.boundaries,
                     *(edge for window in inputs.mining_windows for edge in (window.starts_at, window.ends_at)))
            until = min(now + self._max_age, inputs.horizon, *(edge for edge in edges if edge > now))
            if deadline is not None:
                until = min(until, deadline)
            finished = self._tick()
            elapsed = finished - started
            if not 0 <= elapsed < (until - now).total_seconds():
                return ContextResult("expired")
            wall_finish = now + timedelta(seconds=elapsed)
            if wall_finish >= until:
                return ContextResult("expired")
            bound = BoundDeadlineInputs(inputs, until, channels)
            self._capture = _Capture(bound, context, started, zone, wall_finish, finished)
            return ContextResult("captured", bound)
        except (ProjectionError, _Refusal) as exc:
            return ContextResult(exc.reason)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return ContextResult("invalid_context")

    def validate(self, bound: BoundDeadlineInputs, twitch: Twitch, *, now: datetime,
                 enabled: bool = False, local_timezone: str | timezone | None = None) -> ContextReason:
        saved, self._capture = self._capture, None
        if enabled is not True:
            return "disabled"
        if saved is None or bound is not saved.bound:
            return "superseded"
        try:
            now = _utc(now)
            checked = self._tick()
            lifetime = (bound.valid_until - bound.inputs.now).total_seconds()
            if (not 0 <= checked - saved.started < lifetime
                    or checked < saved.last_tick or not saved.last_wall <= now < bound.valid_until):
                return "expired"
            if (self._zone(local_timezone) != saved.zone
                    or not saved.context.matches(self._context(twitch, bound.inputs.now))):
                return "superseded"
            finished = self._tick()
            wall_finish = now + timedelta(seconds=finished - checked)
            if (not checked <= finished or not 0 <= finished - saved.started < lifetime
                    or wall_finish >= bound.valid_until):
                return "expired"
            self._capture = replace(saved, last_wall=wall_finish, last_tick=finished)
            return "current"
        except (ProjectionError, _Refusal) as exc:
            return exc.reason
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return "invalid_context"
