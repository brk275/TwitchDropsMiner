"""Temporary deadline overrides without rewriting the user's ranked game list."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from time import monotonic
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable

    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign
    from src.models.channel import Channel
    from src.models.drop import TimedDrop


logger = logging.getLogger("TwitchDrops")


@dataclass(frozen=True)
class DeadlinePlan:
    campaign: DropsCampaign
    drop: TimedDrop
    rank: int
    channels: frozenset[int]
    finish: datetime
    deadline: datetime

    @property
    def key(self) -> tuple[str, str]:
        return self.campaign.id, self.drop.id


class DeadlineService:
    """Use conservative branch budgets and real minutes for imminent deadlines.

    Serializing independent prerequisites overestimates work deliberately. This is
    a best-effort local estimate, not a promise that Twitch will allocate progress.
    """

    BUFFER = timedelta(minutes=15)
    SOON = timedelta(hours=24)
    MIN_SLACK = timedelta(minutes=30)

    def __init__(self, twitch: Twitch, *, enabled: bool = False) -> None:
        self._twitch = twitch
        self.enabled = enabled
        self.target: DeadlinePlan | None = None
        self._protected: tuple[frozenset[int], ...] = ()
        self._last_discovery = monotonic()

    def clear(self) -> None:
        self.target = None
        self._protected = ()
        self._last_discovery = monotonic()

    def discovery_due(self) -> bool:
        if not self.enabled or monotonic() - self._last_discovery < 300:
            return False
        self._last_discovery = monotonic()
        return True

    @classmethod
    def finish_at(cls, drop: TimedDrop, start: datetime) -> datetime | None:
        """Budget each unclaimed prerequisite once, with its own time window."""
        campaign = drop.campaign
        completed: set[str] = set()
        visiting: set[str] = set()
        cursor = max(start, campaign.starts_at)

        def visit(node: TimedDrop) -> bool:
            nonlocal cursor
            if node.is_claimed or node.id in completed:
                return True
            if node.id in visiting or not node.is_watch_drop or node.is_ignored:
                return False
            visiting.add(node.id)
            for parent_id in node.precondition_drops:
                parent = campaign.timed_drops.get(parent_id)
                if parent is None or not visit(parent):
                    return False
            # Reserve a minute even when the watch time is complete but no claim
            # is confirmed. Never use extra_current_minutes (local estimates).
            cursor = max(cursor, node.starts_at) + timedelta(
                minutes=max(1, node.required_minutes - node.real_current_minutes),
            )
            if cursor + cls.BUFFER >= min(node.ends_at, campaign.ends_at):
                return False
            visiting.remove(node.id)
            completed.add(node.id)
            return True

        return cursor if visit(drop) else None

    def prepare(self, channels: Iterable[Channel], *, now: datetime | None = None) -> None:
        """Freeze one decision before sorting; no mutation during key evaluation."""
        previous = self.target.key if self.target else None
        self.target = None
        self._protected = ()
        owner = self._twitch
        hours = getattr(owner.settings, "mining_hours", None)
        if (not self.enabled or owner.is_manual_mode()
                or isinstance(hours, dict) and hours.get("mode") == "range"):
            return
        now = now or datetime.now(timezone.utc)
        candidates = tuple(channels)
        ranks = {game.id: index for index, game in enumerate(owner.wanted_games)}
        selected = {name.lower() for name in owner.settings.games_to_watch}
        plans: list[DeadlinePlan] = []
        selected_available = False
        for campaign in owner.inventory:
            if campaign.game.id not in ranks:
                continue
            available = frozenset(
                channel.id for channel in candidates
                if channel.online and not channel.banned
                and (campaign.game.is_special() or channel.drops_enabled)
                and campaign.can_mine(channel)
            )
            if not available:
                continue
            if campaign.game.name.lower() in selected:
                selected_available = True
            for drop in campaign.mining_selection().targets:
                finish = self.finish_at(drop, now)
                if finish is not None:
                    plans.append(DeadlinePlan(
                        campaign, drop, ranks[campaign.game.id], available,
                        finish, min(drop.ends_at, campaign.ends_at),
                    ))
        # Selected games always outrank runtime fallback. Fallback can only
        # preempt another fallback, never a selected game.
        selected_plans = [p for p in plans if p.campaign.game.name.lower() in selected]
        if selected_available and not selected_plans:
            return
        pool = selected_plans or plans
        if not pool:
            return
        baseline_rank = min(p.rank for p in pool)
        baseline = min((p for p in pool if p.rank == baseline_rank), key=lambda p: p.finish)
        urgent: list[DeadlinePlan] = []
        for plan in pool:
            if plan.rank <= baseline_rank or plan.deadline - now > self.SOON:
                continue
            slack = plan.deadline - plan.finish - self.BUFFER
            if (plan.key != previous
                    and slack > max(self.MIN_SLACK, baseline.finish - now)):
                continue
            # Refuse a detour that makes any currently feasible higher-ranked
            # reward branch miss its own (or a prerequisite's) deadline.
            if any(
                self.finish_at(higher.drop, plan.finish + self.BUFFER) is None
                for higher in pool if higher.rank < plan.rank
            ):
                continue
            urgent.append(plan)
        self.target = min(urgent, key=lambda p: (p.deadline, p.rank, p.key), default=None)
        if self.target is not None:
            self._protected = tuple(dict.fromkeys(
                p.channels for p in pool if p.rank < self.target.rank
            ))
        current = self.target.key if self.target else None
        if current != previous:
            logger.info("Deadline override changed: %s", current or "ranked order")

    def prioritizes(self, channel: Channel) -> bool:
        target = self.target
        hours = getattr(self._twitch.settings, "mining_hours", None)
        return (
            self.enabled and not self._twitch.is_manual_mode()
            and not (isinstance(hours, dict) and hours.get("mode") == "range")
            and target is not None and channel.id in target.channels
            and any(campaign is target.campaign for campaign in self._twitch.inventory)
            and target.campaign.timed_drops.get(target.drop.id) is target.drop
            and target.drop in target.campaign.mining_selection().targets
            and target.campaign.can_mine(channel)
        )

    def cap_channels(self, ordered: list[Channel], limit: int) -> list[Channel]:
        """Retain representatives needed to reassess and return from the detour."""
        if self.target is None or len(ordered) <= limit:
            return ordered[:limit]
        required: set[int] = set()
        for group in (self.target.channels, *self._protected):
            if not required.intersection(group):
                representative = next((ch for ch in ordered if ch.id in group), None)
                if representative is not None:
                    required.add(representative.id)
        if len(required) > limit:
            self.clear()
            return sorted(ordered, key=self._twitch._channel_service.get_mining_priority)[:limit]
        optional = iter(ch for ch in ordered if ch.id not in required)
        keep = required | {ch.id for ch in islice(optional, limit - len(required))}
        return [ch for ch in ordered if ch.id in keep]
