"""Read-only model inputs, not a live deadline policy or availability prediction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config.settings import MiningHoursSettings
from src.services.deadline_forecast import (
    ForecastDrop,
    ForecastSnapshot,
    MiningWindow,
    ViewingWindow,
)
from src.utils import DropIgnorePolicy
from src.utils.schedule import window_status


if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.drop import TimedDrop


ProjectionReason = Literal["captured", "manual_override", "invalid_snapshot", "unknown_timezone", "budget_exceeded"]


class ProjectionError(ValueError):
    def __init__(self, reason: ProjectionReason):
        self.reason = reason
        super().__init__(reason)


def _utc(value: datetime) -> datetime:
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ProjectionError("invalid_snapshot")
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        raise ProjectionError("invalid_snapshot") from None


class MiningHoursProjector:
    """Project nominal HH:MM policy in an explicit minute-aligned timezone.

    Iterate UTC minutes so gaps/folds do not skip or duplicate elapsed time. Reject
    subminute offsets or observed intra-minute discontinuities rather than round them.
    This consumes trusted installed IANA data, not arbitrary user-supplied TZif files.
    Actual enforcement jitter and future availability are not established here.
    """

    def __init__(self, *, max_minutes: int = 20_162):
        if type(max_minutes) is not int or not 1 <= max_minutes <= 20_162:
            raise ValueError("Invalid mining projection budget")
        self.max_minutes = max_minutes

    def project(self, hours: object, now: datetime, horizon: datetime,
                local_timezone: str | timezone | None) -> tuple[MiningWindow, ...]:
        now, horizon = _utc(now), _utc(horizon)
        if not timedelta(0) < horizon - now <= timedelta(days=14):
            raise ProjectionError("invalid_snapshot")
        try:
            schedule = MiningHoursSettings.normalize(hours)
        except ValueError:
            raise ProjectionError("invalid_snapshot") from None
        if schedule["mode"] == "always" or schedule["start"] == schedule["end"]:
            return (MiningWindow(now, horizon),)
        try:
            if isinstance(local_timezone, str):
                zone = ZoneInfo(local_timezone)
            elif isinstance(local_timezone, timezone):
                zone = local_timezone
            else:
                raise ProjectionError("unknown_timezone")
        except (ZoneInfoNotFoundError, ValueError):
            raise ProjectionError("unknown_timezone") from None

        cursor = now
        windows: list[MiningWindow] = []
        steps = 0
        while cursor < horizon:
            steps += 1
            if steps > self.max_minutes:
                raise ProjectionError("budget_exceeded")
            step = timedelta(minutes=1) - timedelta(seconds=cursor.second, microseconds=cursor.microsecond)
            end = cursor + min(step, horizon - cursor)
            try:
                local = cursor.astimezone(zone)
                last = (end - timedelta(microseconds=1)).astimezone(zone)
                offset, last_offset = local.utcoffset(), last.utcoffset()
            except (OverflowError, ValueError):
                raise ProjectionError("unknown_timezone") from None
            active, _ = window_status(schedule["start"], schedule["end"], local)
            last_active, _ = window_status(schedule["start"], schedule["end"], last)
            if (offset is None or last_offset is None or offset.total_seconds() % 60
                    or last_offset.total_seconds() % 60 or offset != last_offset or active != last_active):
                raise ProjectionError("unknown_timezone")
            if active:
                if windows and windows[-1].ends_at == cursor:
                    windows[-1] = MiningWindow(windows[-1].starts_at, end)
                else:
                    windows.append(MiningWindow(cursor, end))
            cursor = end
        return tuple(windows)


@dataclass(frozen=True)
class ProjectedDrop:
    id: str
    game_id: int
    required_minutes: int
    observed_minutes: int | None
    observed_at: datetime | None
    observed_source: str | None
    starts_at: datetime
    ends_at: datetime
    prerequisite_ids: tuple[str, ...]
    wanted: bool


@dataclass(frozen=True)
class DeadlineModelInputs:
    now: datetime
    horizon: datetime
    ranked_game_ids: tuple[int, ...]
    drops: tuple[ProjectedDrop, ...]
    mining_windows: tuple[MiningWindow, ...]

    @property
    def baseline_complete(self) -> Literal[False]:
        return False

    def to_forecast(self, *, claim_delays: Mapping[str, timedelta],
                    viewing_windows: tuple[ViewingWindow, ...]) -> ForecastSnapshot:
        """Attach explicit hypothetical claim/window inputs, never infer live coverage."""
        keys = {drop.id for drop in self.drops}
        if (not isinstance(claim_delays, Mapping) or set(claim_delays) != keys
                or any(not isinstance(delay, timedelta) or not timedelta(0) <= delay <= timedelta(days=1)
                       for delay in claim_delays.values())):
            raise ValueError("Invalid or unknown claim timing")
        if not isinstance(viewing_windows, tuple):
            raise ValueError("Invalid viewing windows")
        return ForecastSnapshot(self.now, self.horizon, self.ranked_game_ids, tuple(
            ForecastDrop(
                drop.id, drop.game_id, drop.required_minutes, drop.observed_minutes, drop.observed_at,
                drop.starts_at, drop.ends_at, drop.prerequisite_ids,
                wanted=drop.wanted, claim_delay=claim_delays[drop.id],
            ) for drop in self.drops
        ), viewing_windows, self.mining_windows)


@dataclass(frozen=True)
class ProjectionResult:
    reason: ProjectionReason
    inputs: DeadlineModelInputs | None = None


class DeadlineInputAdapter:
    """Capture selected model data without claims, settings edits or channel changes.

    Claimed ancestors are already settled; unclaimed ancestors remain explicit even
    while locked. No future channel, allocation, latency or identity validity is
    invented. This data is not a reusable execution authorization or a live policy.
    """

    def __init__(self, *, max_records: int = 256, max_minutes: int = 20_162):
        if type(max_records) is not int or not 1 <= max_records <= 256:
            raise ValueError("Invalid model projection budget")
        self.max_records = max_records
        self._hours = MiningHoursProjector(max_minutes=max_minutes)

    @staticmethod
    def _key(campaign: str, drop: str) -> str:
        return json.dumps((campaign, drop), ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _observation(drop: TimedDrop, now: datetime) -> tuple[int | None, datetime | None, str | None]:
        reading = drop.progress_observation
        if not isinstance(reading, dict):
            return None, None, None
        minutes, stamp, source = reading.get("minutes"), reading.get("received_at"), reading.get("source")
        if (type(minutes) is not int or not 0 <= minutes <= drop.required_minutes
                or not isinstance(stamp, str) or source not in ("inventory", "websocket", "current_drop")):
            return None, None, None
        try:
            received = _utc(datetime.fromisoformat(stamp))
        except ValueError:
            return None, None, None
        if received > now:
            return None, None, None
        return minutes, received, source

    @staticmethod
    def _ancestors(drops: Mapping[str, TimedDrop], roots: tuple[TimedDrop, ...]) -> frozenset[str]:
        needed: set[str] = set()

        def visit(key: str) -> None:
            drop = drops[key]
            if drop.is_claimed or key in needed:
                return
            needed.add(key)
            for parent in drop.precondition_drops:
                visit(parent)

        for root in roots:
            visit(root.id)
        return frozenset(needed)

    def capture(self, twitch: Twitch, *, now: datetime, horizon: datetime,
                local_timezone: str | timezone | None = None) -> ProjectionResult:
        try:
            manual = twitch.is_manual_mode()
            if type(manual) is not bool:
                raise ProjectionError("invalid_snapshot")
            if (manual or twitch._manual_target_channel is not None
                    or twitch._manual_target_game is not None):
                return ProjectionResult("manual_override")
            now, horizon = _utc(now), _utc(horizon)
            settings = twitch.settings
            names, benefits = settings.games_to_watch, settings.mining_benefits
            if (not isinstance(names, list) or len(names) > 256 or any(not isinstance(name, str) for name in names)
                    or not isinstance(benefits, dict) or any(type(value) is not bool for value in benefits.values())
                    or not isinstance(twitch.inventory, (list, tuple))):
                raise ProjectionError("invalid_snapshot")
            if len(twitch.inventory) > 512:
                raise ProjectionError("budget_exceeded")
            mining = self._hours.project(settings.mining_hours, now, horizon, local_timezone)
            game_names: dict[str, int] = {}
            for campaign in twitch.inventory:
                name, game_id = campaign.game.name.lower(), campaign.game.id
                if type(game_id) is not int or game_id <= 0 or name in game_names and game_names[name] != game_id:
                    raise ProjectionError("invalid_snapshot")
                game_names[name] = game_id
            ranks = tuple(dict.fromkeys(game_names[name.lower()] for name in names if name.lower() in game_names))
            policy = DropIgnorePolicy(settings.drop_name_blacklist)
            projected: list[ProjectedDrop] = []
            campaigns: set[str] = set()
            records = 0
            for campaign in twitch.inventory:
                if campaign.game.id not in ranks:
                    continue
                if campaign.id in campaigns or not isinstance(campaign.id, str) or not campaign.id:
                    raise ProjectionError("invalid_snapshot")
                campaigns.add(campaign.id)
                start, end = _utc(campaign.starts_at), _utc(campaign.ends_at)
                if campaign._valid is not True or campaign.eligible is not True or end <= now or start >= horizon:
                    continue
                drops = tuple(campaign.drops)
                records += len(drops)
                if records > self.max_records:
                    raise ProjectionError("budget_exceeded")
                for drop in drops:
                    if not isinstance(drop.precondition_drops, (list, tuple)):
                        raise ProjectionError("invalid_snapshot")
                    if len(drop.precondition_drops) > 256:
                        raise ProjectionError("budget_exceeded")
                    if (not isinstance(drop.id, str) or not drop.id or type(drop.is_claimed) is not bool
                            or type(drop.required_minutes) is not int or not 0 <= drop.required_minutes <= 525_600
                            or any(not isinstance(parent, str) or not parent for parent in drop.precondition_drops)):
                        raise ProjectionError("invalid_snapshot")
                mineable = policy.evaluate(drops).mineable_ids
                roots = tuple(drop for drop in drops if drop.id in mineable and not drop.is_claimed
                              and min(end, _utc(drop.ends_at)) > now and max(start, _utc(drop.starts_at)) < horizon
                              and any(benefit.is_wanted(benefits) for benefit in drop.benefits))
                wanted = {drop.id for drop in roots}
                needed = self._ancestors(campaign.timed_drops, roots)
                for drop in drops:
                    if drop.id not in needed:
                        continue
                    if (not isinstance(drop.id, str) or not drop.id or type(drop.required_minutes) is not int
                            or not 1 <= drop.required_minutes <= 525_600):
                        raise ProjectionError("invalid_snapshot")
                    drop_start, drop_end = max(start, _utc(drop.starts_at)), min(end, _utc(drop.ends_at))
                    if drop_start >= drop_end:
                        raise ProjectionError("invalid_snapshot")
                    minutes, received, source = self._observation(drop, now)
                    parents = tuple(self._key(campaign.id, parent) for parent in drop.precondition_drops
                                    if not campaign.timed_drops[parent].is_claimed)
                    projected.append(ProjectedDrop(
                        self._key(campaign.id, drop.id), campaign.game.id, drop.required_minutes,
                        minutes, received, source, drop_start, drop_end, parents, drop.id in wanted,
                    ))
            return ProjectionResult("captured", DeadlineModelInputs(now, horizon, ranks, tuple(projected), mining))
        except ProjectionError as exc:
            return ProjectionResult(exc.reason)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return ProjectionResult("invalid_snapshot")
