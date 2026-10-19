from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cached_property
from itertools import chain
from typing import TYPE_CHECKING

from dateutil.parser import isoparse

from src.config.constants import State
from src.exceptions import MinerException
from src.models.channel import Channel
from src.models.drop import TimedDrop
from src.models.game import Game
from src.utils import DropIgnoreEvaluation, DropIgnorePolicy, DropIgnoreReason


if TYPE_CHECKING:
    from collections import abc

    from src.config.constants import JsonType
    from src.core.client import Twitch


logger = logging.getLogger("TwitchDrops")


@dataclass(frozen=True)
class CampaignMiningSelection:
    """Wanted rewards and the unclaimed prerequisite work needed for them."""

    targets: tuple[TimedDrop, ...]
    branches: tuple[TimedDrop, ...]


class DropsCampaign:
    def __init__(self, twitch: Twitch, data: JsonType, claimed_benefits: dict[str, datetime]):
        raw_drops = data.get("timeBasedDrops")
        if not isinstance(raw_drops, list):
            raise MinerException("Invalid campaign drop list")
        drop_data_by_id: dict[str, JsonType] = {}
        for drop_data in raw_drops:
            drop_id = drop_data.get("id") if isinstance(drop_data, dict) else None
            if (not isinstance(drop_id, str) or not drop_id.strip()
                    or drop_id in drop_data_by_id):
                raise MinerException("Invalid campaign drop list")
            drop_data_by_id[drop_id] = drop_data
        self._twitch: Twitch = twitch
        # Shared progress updates can affect every eligible sibling drop.
        self.progress_revision: int = 0
        self.id: str = data["id"]
        self.campaign_url: str = f"https://www.twitch.tv/drops/campaigns?dropID={self.id}"
        self.name: str = data["name"]
        self.game: Game = Game(data["game"])
        self.linked: bool = data["self"]["isAccountConnected"]
        self.link_url: str = data["accountLinkURL"]
        # campaign's image actually comes from the game object
        # we use regex to get rid of the dimensions part (ex. ".../game_id-285x380.jpg")
        self.starts_at: datetime = isoparse(data["startAt"])
        self.ends_at: datetime = isoparse(data["endAt"])
        self._valid: bool = data["status"] != "EXPIRED"
        allowed: JsonType = data["allow"]
        self.allowed_channels: list[Channel] = (
            [Channel.from_acl(twitch, channel_data) for channel_data in allowed["channels"]]
            if allowed["channels"] and allowed.get("isEnabled", True)
            else []
        )
        self.timed_drops: dict[str, TimedDrop] = {
            drop_id: TimedDrop(self, drop_data, claimed_benefits)
            for drop_id, drop_data in drop_data_by_id.items()
        }
        self._drop_ignore_cache_key: (
            tuple[tuple[str, ...], tuple[tuple[str, bool], ...]] | None
        ) = None
        self._drop_ignore_cache: DropIgnoreEvaluation | None = None

    def __repr__(self) -> str:
        return f"Campaign({self.game!s}, {self.name}, {self.claimed_drops}/{self.total_drops})"

    @property
    def drops(self) -> abc.Iterable[TimedDrop]:
        return self.timed_drops.values()

    @cached_property
    def watch_drops(self) -> tuple[TimedDrop, ...]:
        """Return drops that can be earned by watching a stream."""
        return tuple(drop for drop in self.drops if drop.is_watch_drop)

    def _drop_ignore_evaluation(self) -> DropIgnoreEvaluation:
        """Return a policy result cached against keywords and claim state."""
        keywords = tuple(
            DropIgnorePolicy.normalize_keywords(
                getattr(self._twitch.settings, "drop_name_blacklist", [])
            )
        )
        claimed_state = tuple((drop.id, drop.is_claimed) for drop in self.drops)
        cache_key = (keywords, claimed_state)
        if cache_key != self._drop_ignore_cache_key or self._drop_ignore_cache is None:
            policy = DropIgnorePolicy(keywords)
            self._drop_ignore_cache = policy.evaluate(tuple(self.drops))
            self._drop_ignore_cache_key = cache_key
        return self._drop_ignore_cache

    @property
    def mineable_drop_ids(self) -> frozenset[str]:
        """Return unclaimed drops that still contribute to a mineable reward branch."""
        return self._drop_ignore_evaluation().mineable_ids

    @property
    def mineable_watch_drops(self) -> tuple[TimedDrop, ...]:
        """Return watch drops that the miner still needs for useful branches."""
        mineable_ids = self.mineable_drop_ids
        return tuple(drop for drop in self.watch_drops if drop.id in mineable_ids)

    def get_drop_ignore_reason(self, drop_id: str) -> DropIgnoreReason | None:
        """Return the ignore reason for a drop, if one applies."""
        return self._drop_ignore_evaluation().reasons.get(drop_id)

    def is_drop_mineable(self, drop_id: str) -> bool:
        """Return whether an unclaimed drop is part of a useful reward branch."""
        return drop_id in self.mineable_drop_ids

    def mining_selection(
        self, allowed_benefits: dict[str, bool] | None = None, *, stamp: datetime | None = None,
    ) -> CampaignMiningSelection:
        """Select wanted branches within the normal one-hour discovery horizon.

        This is a local targeting policy, separate from raw eligibility, Twitch
        observations and claims. Recompute against current preferences and time.
        """
        now = datetime.now(timezone.utc)
        if stamp is None:
            stamp = now + timedelta(hours=1)
        if (not self.eligible or not self._valid or self.ends_at <= now
                or self.starts_at >= stamp):
            return CampaignMiningSelection((), ())
        if allowed_benefits is None:
            allowed_benefits = self._twitch.settings.mining_benefits
        available_after = max(now, self.starts_at)
        available_before = min(stamp, self.ends_at)
        mineable = self.mineable_drop_ids
        branches: dict[str, frozenset[str] | None] = {}

        def collect(drop_id: str, visiting: frozenset[str]) -> frozenset[str] | None:
            drop = self.timed_drops.get(drop_id)
            if drop is None:
                return None
            if drop.is_claimed:
                return frozenset()
            if drop_id in visiting or drop_id not in mineable:
                return None
            if drop_id in branches:
                return branches[drop_id]
            if drop.ends_at <= available_after or drop.starts_at >= available_before:
                branches[drop_id] = None
                return None
            branch = {drop_id}
            for prerequisite_id in drop.precondition_drops:
                prerequisite = collect(prerequisite_id, visiting | {drop_id})
                if prerequisite is None:
                    branches[drop_id] = None
                    return None
                branch.update(prerequisite)
            branches[drop_id] = frozenset(branch)
            return branches[drop_id]

        targets: list[TimedDrop] = []
        selected_ids: set[str] = set()
        for drop in self.watch_drops:
            if not drop.has_wanted_unclaimed_benefits(allowed_benefits):
                continue
            if (branch := collect(drop.id, frozenset())) is not None:
                targets.append(drop)
                selected_ids.update(branch)
        return CampaignMiningSelection(
            tuple(targets), tuple(drop for drop in self.watch_drops if drop.id in selected_ids),
        )

    def can_mine(self, channel: Channel | None = None) -> bool:
        """Whether a wanted branch can currently progress on this channel."""
        return self._base_can_earn(channel) and any(
            drop._base_can_earn() for drop in self.mining_selection().branches
        )

    def can_mine_within(
        self, stamp: datetime, allowed_benefits: dict[str, bool] | None = None,
    ) -> bool:
        return any(
            drop._can_earn_within(stamp)
            for drop in self.mining_selection(allowed_benefits, stamp=stamp).branches
        )

    def first_mining_drop(self, channel: Channel | None = None) -> TimedDrop | None:
        return min(
            (drop for drop in self.mining_selection().branches if drop.can_earn(channel)),
            key=lambda drop: drop.remaining_minutes, default=None,
        )

    @property
    def mining_remaining_minutes(self) -> int:
        return max(
            (drop.total_remaining_minutes for drop in self.mining_selection().targets), default=0,
        )

    @property
    def time_triggers(self) -> set[datetime]:
        return set(
            chain(
                (self.starts_at, self.ends_at),
                *((d.starts_at, d.ends_at) for d in self.timed_drops.values()),
            )
        )

    @property
    def active(self) -> bool:
        return self._valid and self.starts_at <= datetime.now(timezone.utc) < self.ends_at

    @property
    def upcoming(self) -> bool:
        return self._valid and datetime.now(timezone.utc) < self.starts_at

    @property
    def expired(self) -> bool:
        return not self._valid or self.ends_at <= datetime.now(timezone.utc)

    @property
    def total_drops(self) -> int:
        return len(self.watch_drops)

    @property
    def eligible(self) -> bool:
        return (
            self.linked
            or self.has_badge_or_emote
            or getattr(self._twitch.settings, "allow_unlinked_campaigns", False) is True
        )

    @cached_property
    def has_badge_or_emote(self) -> bool:
        return any(
            benefit.type.is_badge_or_emote() for drop in self.drops for benefit in drop.benefits
        )

    @property
    def finished(self) -> bool:
        return all(drop.is_claimed for drop in self.watch_drops)

    @property
    def mining_finished(self) -> bool:
        """Return whether the miner has no nonignored reward branch left to pursue."""
        return not self.mineable_drop_ids

    @property
    def claimed_drops(self) -> int:
        return sum(drop.is_claimed for drop in self.watch_drops)

    @property
    def remaining_drops(self) -> int:
        return sum(not drop.is_claimed for drop in self.watch_drops)

    @property
    def ignored_drops(self) -> int:
        """Return unclaimed watch drops ignored directly or by dependency."""
        return sum(drop.is_ignored for drop in self.watch_drops)

    @property
    def skipped_drops(self) -> int:
        """Return unclaimed prerequisite drops no longer needed by a reward branch."""
        return sum(
            not drop.is_claimed and not drop.is_ignored and not drop.is_mineable
            for drop in self.watch_drops
        )

    @property
    def required_minutes(self) -> int:
        return max((drop.total_required_minutes for drop in self.mineable_watch_drops), default=0)

    @property
    def remaining_minutes(self) -> int:
        return max(
            (drop.total_remaining_minutes for drop in self.mineable_watch_drops),
            default=0,
        )

    @property
    def progress(self) -> float:
        watch_drops = self.watch_drops
        return sum(drop.progress for drop in watch_drops) / len(watch_drops) if watch_drops else 0.0

    @property
    def availability(self) -> float:
        return min(
            (drop.availability for drop in self.mineable_watch_drops),
            default=float("inf"),
        )

    @property
    def first_drop(self) -> TimedDrop | None:
        drops: list[TimedDrop] = sorted(
            (drop for drop in self.watch_drops if drop.can_earn()),
            key=lambda d: d.remaining_minutes,
        )
        return drops[0] if drops else None

    def _update_real_minutes(self, delta: int) -> None:
        for drop in self.drops:
            drop._update_real_minutes(delta)
        if (first_drop := self.first_drop) is not None:
            first_drop.display()

    def _base_can_earn(
        self, channel: Channel | None = None, ignore_channel_status: bool = False
    ) -> bool:
        return (
            self.eligible  # account is eligible
            and self.active  # campaign is active (and valid)
            and (
                channel is None
                or (  # channel isn't specified,
                    # or there's no ACL, or the channel is in the ACL
                    (not self.allowed_channels or channel in self.allowed_channels)
                    # and the channel plays the campaign's game, or is a live
                    # participant in a special-category campaign with an ACL
                    and (
                        ignore_channel_status
                        or channel.game is not None
                        and channel.game == self.game
                        or (
                            channel.online
                            and self.game.is_special()
                            and bool(self.allowed_channels)
                        )
                    )
                )
            )
        )

    def get_drop(self, drop_id: str) -> TimedDrop | None:
        """Get a specific drop by ID from this campaign."""
        return self.timed_drops.get(drop_id)

    def preconditions_chain(self) -> set[str]:
        """Return prerequisite IDs that still participate in mineable branches."""
        mineable_ids = self.mineable_drop_ids
        return set(
            chain.from_iterable(
                (
                    prerequisite_id
                    for prerequisite_id in drop.precondition_drops
                    if prerequisite_id in mineable_ids
                )
                for drop in self.drops
                if drop.id in mineable_ids
            )
        )

    def can_earn(self, channel: Channel | None = None, ignore_channel_status: bool = False) -> bool:
        # True if any of the containing drops can be earned
        return self._base_can_earn(channel, ignore_channel_status) and any(
            drop._base_can_earn() for drop in self.drops
        )

    def can_earn_within(self, stamp: datetime) -> bool:
        # Same as can_earn, but doesn't check the channel
        # and uses a future timestamp to see if we can earn this campaign later
        return (
            self.eligible
            and self._valid
            and self.ends_at > datetime.now(timezone.utc)
            and self.starts_at < stamp
            and any(drop._can_earn_within(stamp) for drop in self.drops)
        )

    def bump_minutes(self, channel: Channel) -> None:
        """
        Bump local estimates only for earnable work in wanted branches.
        Used when websocket updates aren't available.
        """
        # NOTE: Use a temporary list to ensure all drops are bumped before checking
        limits_reached = [drop._bump_minutes(channel) for drop in self.mining_selection().branches]
        if any(limits_reached):
            # Executes if any drop's extra_current_minutes reach MAX_EXTRA_MINUTES
            # TODO: Figure out a better way to handle this case
            logger.warning(
                f'At least one of the drops in campaign "{self.name}({self.game.name})" '
                "has reached the maximum extra minutes limit!"
            )
            self._twitch.change_state(State.CHANNEL_SWITCH)
        if (first_drop := self.first_mining_drop(channel)) is not None:
            first_drop.display()

    def has_wanted_unclaimed_benefits(self, allowed_benefits: dict[str, bool]) -> bool:
        return any(drop.has_wanted_unclaimed_benefits(allowed_benefits) for drop in self.drops)
