"""Guarded progress-only consumption, never live model replacement or channel actions."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Literal

from src.config.constants import MAX_EXTRA_MINUTES
from src.exceptions import ExitRequest, MinerException
from src.services.deadline_context import DeadlineContextGuard, _freeze
from src.services.deadline_execution import DeadlineCalendarExecutor
from src.services.deadline_inputs import _utc


if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign


@dataclass(frozen=True)
class ProgressReadRequest:
    identity: tuple[tuple[object, ...], tuple] = field(repr=False)
    signature: tuple = field(repr=False)
    campaigns: tuple[tuple[DropsCampaign, int], ...] = field(repr=False)
    auth: object = field(repr=False)
    auth_epoch: object = field(repr=False)
    user_id: int = field(repr=False)
    started_at: datetime
    started_tick: float


@dataclass(frozen=True)
class _Reading:
    campaign_id: str
    drop_id: str
    claimed: bool
    minutes: int | None
    received_at: datetime | None


@dataclass(frozen=True)
class ProgressReadBatch:
    request: ProgressReadRequest = field(repr=False)
    signature: tuple | None = field(repr=False)
    readings: tuple[_Reading, ...] = field(repr=False)


RefreshReason = Literal["applied", "unchanged", "unavailable", "superseded", "inventory_changed"]


class InventoryProgressReader:
    """Collect in a child; validate and publish only inside the registered miner task."""

    def __init__(
        self, twitch: Twitch, *, clock: Callable[[], float] = monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._twitch, self._clock, self._utc_clock = twitch, clock, utc_clock

    def _current(self) -> tuple[datetime, float]:
        twitch = self._twitch
        task = asyncio.current_task()
        if task is not twitch._run_task or task is None or task.cancelling():  # type: ignore[attr-defined]  # Python 3.12 runtime.
            raise ValueError("Inactive inventory progress owner")
        now, tick = _utc(self._utc_clock()), self._clock()
        if type(tick) not in (int, float) or not math.isfinite(tick):
            raise ValueError("Invalid inventory progress clock")
        DeadlineContextGuard._context(twitch, now)
        return now, float(tick)

    @staticmethod
    def _signature(campaigns: list[DropsCampaign]) -> tuple:
        if len(campaigns) > 512 or sum(len(c.timed_drops) for c in campaigns) > 256:
            raise ValueError("Inventory progress record budget exceeded")
        values = []
        ids = set()
        for campaign in sorted(campaigns, key=lambda c: c.id):
            if campaign.id in ids or len(campaign.allowed_channels) > 512:
                raise ValueError("Invalid inventory progress campaign")
            ids.add(campaign.id)
            drops = []
            for drop in sorted(campaign.drops, key=lambda d: d.id):
                if len(drop.benefits) > 256 or len(drop.precondition_drops) > 256:
                    raise ValueError("Inventory progress edge budget exceeded")
                drops.append((drop.id, drop.name, drop.required_minutes, drop.starts_at, drop.ends_at,
                              drop.precondition_drops,
                              tuple((b.id, b.name, b.type.name) for b in drop.benefits)))
            values.append((campaign.id, campaign.name, campaign.game.id, campaign.game.name,
                           campaign._valid, campaign.linked, campaign.link_url,
                           campaign.starts_at, campaign.ends_at,
                           tuple(sorted(c.id for c in campaign.allowed_channels)), tuple(drops)))
        return _freeze(tuple(values))

    def prepare(self) -> ProgressReadRequest | None:
        try:
            now, tick = self._current()
            twitch, auth = self._twitch, self._twitch._auth_state
            return ProgressReadRequest(
                DeadlineCalendarExecutor._identity(twitch), self._signature(twitch.inventory),
                tuple((campaign, campaign.progress_revision) for campaign in twitch.inventory),
                auth, auth.context_epoch, auth.user_id, now, tick,
            )
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return None

    async def read(self, request: ProgressReadRequest) -> ProgressReadBatch:
        auth = self._twitch._auth_state
        if auth is not request.auth or auth.context_epoch is not request.auth_epoch or auth.user_id != request.user_id:
            return ProgressReadBatch(request, None, ())
        try:
            async with asyncio.timeout(5):  # type: ignore[attr-defined]  # Python 3.12 runtime.
                campaigns = await self._twitch._inventory_service.collect_inventory(max_campaigns=512, max_drops=256)
            signature = self._signature(campaigns)
            readings = []
            for campaign in campaigns:
                for drop in campaign.drops:
                    if type(drop.is_claimed) is not bool:
                        raise ValueError("Invalid inventory claim observation")
                    observation = drop.progress_observation
                    minutes, stamp = None, None
                    if observation is not None:
                        minutes = observation["minutes"]
                        stamp = _utc(datetime.fromisoformat(observation["received_at"]))
                    readings.append(_Reading(campaign.id, drop.id, drop.is_claimed, minutes, stamp))
            return ProgressReadBatch(request, signature, tuple(readings))
        except ExitRequest:
            raise
        except (TimeoutError, KeyError, TypeError, ValueError, MinerException):
            return ProgressReadBatch(request, None, ())

    def _can_publish(self, request: ProgressReadRequest, is_current: Callable[[], bool]) -> bool:
        try:
            if not is_current():
                return False
            self._current()
            auth = self._twitch._auth_state
            return auth is request.auth and auth.context_epoch is request.auth_epoch and auth.user_id == request.user_id
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return False

    def apply(self, batch: ProgressReadBatch, *, is_current: Callable[[], bool] = lambda: True) -> RefreshReason:
        try:
            if not is_current():
                return "superseded"
            now, tick = self._current()
            request, twitch = batch.request, self._twitch
            if (not timedelta(0) <= now - request.started_at <= timedelta(seconds=15)
                    or not 0 <= tick - request.started_tick <= 15):
                return "superseded"
            objects, values = DeadlineCalendarExecutor._identity(twitch)
            if (len(objects) != len(request.identity[0])
                    or any(a is not b for a, b in zip(objects, request.identity[0], strict=True))
                    or values != request.identity[1]):
                return "superseded"
            if batch.signature is None:
                return "unavailable"
            if self._signature(twitch.inventory) != request.signature:
                return "superseded"
            if batch.signature != request.signature:
                return "inventory_changed"
            rows = {(row.campaign_id, row.drop_id): row for row in batch.readings}
            updates = []
            changed_campaigns = []
            exhausted = []
            for campaign, revision in request.campaigns:
                superseded_minutes = campaign.progress_revision != revision
                changes = []
                for drop in campaign.drops:
                    if drop.is_claimed:
                        continue
                    row = rows.get((campaign.id, drop.id))
                    if row is None or row.claimed:
                        return "inventory_changed"
                    if superseded_minutes:
                        continue
                    if (type(row.minutes) is not int or not 0 <= row.minutes <= drop.required_minutes
                            or row.received_at is None or row.received_at > now):
                        continue
                    previous = drop.progress_observation
                    if previous is not None and row.received_at < _utc(datetime.fromisoformat(previous["received_at"])):
                        continue
                    if drop.extra_current_minutes >= MAX_EXTRA_MINUTES:
                        exhausted.append(drop)
                    changes.append((drop, row.minutes, row.received_at))
                if changes:
                    changed_campaigns.append(campaign)
                    updates.extend(changes)
            finished_at, finished_tick = self._current()
            if (not timedelta(0) <= finished_at - request.started_at <= timedelta(seconds=15)
                    or not 0 <= finished_tick - request.started_tick <= 15):
                return "superseded"
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return "superseded"

        # Commit all guarded direct reports before callbacks can retire the owner.
        for campaign in changed_campaigns:
            campaign.progress_revision += 1
        for drop, minutes, stamp in updates:
            drop._accept_inventory_observation(minutes, stamp)
        for drop, _, _ in updates:
            if not self._can_publish(request, is_current):
                return "superseded"
            drop._on_state_changed()
        if not self._can_publish(request, is_current):
            return "superseded"
        if any(drop.can_earn() for drop in exhausted):
            twitch.request_games_update()
        return "applied" if updates else "unchanged"
