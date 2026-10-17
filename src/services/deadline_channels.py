"""Current channel eligibility, not future availability or a credit-allocation rule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from src.config import MAX_INT
from src.models.channel import Channel
from src.services.channel_service import ChannelService
from src.services.channel_switch_policy import ChannelSwitchPolicy
from src.services.deadline_inputs import DeadlineInputAdapter, DeadlineModelInputs, ProjectionError
from src.services.watch_service import WatchService


if TYPE_CHECKING:
    from src.core.client import Twitch
    from src.models.drop import TimedDrop


@dataclass(frozen=True)
class ProjectedChannel:
    channel_id: str
    channel_order: int | None
    game_id: int | None
    acl_based: bool
    mining_priority: tuple[int, int]
    watchable: bool
    eligible_drop_ids: frozenset[str]


@dataclass(frozen=True)
class DeadlineChannelInputs:
    captured_at: datetime
    candidates: tuple[ProjectedChannel, ...]
    watching: ProjectedChannel | None
    automatic_action: Literal["switch", "keep", "idle"]
    automatic_channel_id: str | None

    @property
    def baseline_complete(self) -> Literal[False]:
        return False

    @property
    def allocation_known(self) -> Literal[False]:
        return False


class DeadlineChannelAdapter:
    """Read actual selector inputs at a caller-owned serialized checkpoint.

    Use through DeadlineContextGuard to bind lifecycle/identity and charge elapsed
    time. Runtime eligibility getters may warm derived policy caches; this performs
    no claims, selection, settings edits, network calls or readiness checks. A set
    of eligible rewards does not establish simultaneous Twitch credit. Tracked and
    watched objects stay separate, even when a replacement has the same channel ID.
    """

    def __init__(self, *, max_work: int = 2_000_000):
        if type(max_work) is not int or not 1 <= max_work <= 2_000_000:
            raise ValueError("Invalid channel projection budget")
        self.max_work = max_work

    def capture(self, twitch: Twitch, inputs: DeadlineModelInputs) -> DeadlineChannelInputs:
        channels, inventory, wanted = twitch.channels, twitch.inventory, twitch.wanted_games
        if (not isinstance(channels, dict) or not isinstance(inventory, (list, tuple))
                or not isinstance(wanted, (list, tuple))
                or not isinstance(inputs, DeadlineModelInputs)):
            raise ProjectionError("invalid_snapshot")
        if len(channels) > 199 or len(inventory) > 512 or len(inputs.drops) > 256 or len(wanted) > 256:
            raise ProjectionError("budget_exceeded")
        selector, watcher = twitch._channel_service, twitch._watch_service
        if (not isinstance(selector, ChannelService) or not isinstance(watcher, WatchService)
                or selector._twitch is not twitch or watcher._twitch is not twitch):
            raise ProjectionError("invalid_snapshot")
        drops: dict[str, TimedDrop] = {}
        campaign_work = 0
        acl_work = 0
        for campaign in inventory:
            if campaign._twitch is not twitch:
                raise ProjectionError("invalid_snapshot")
            if len(campaign.timed_drops) + len(drops) > 256:
                raise ProjectionError("budget_exceeded")
            if not isinstance(campaign.allowed_channels, (list, tuple)):
                raise ProjectionError("invalid_snapshot")
            if len(campaign.allowed_channels) > 512:
                raise ProjectionError("budget_exceeded")
            campaign_work += 1 + len(campaign.timed_drops) ** 2
            acl_work += 2 * len(campaign.allowed_channels)
            for drop in campaign.drops:
                if drop._twitch is not twitch or drop.campaign is not campaign:
                    raise ProjectionError("invalid_snapshot")
                key = DeadlineInputAdapter._key(campaign.id, drop.id)
                if key in drops:
                    raise ProjectionError("invalid_snapshot")
                drops[key] = drop
        selected = frozenset(drop.id for drop in inputs.drops)
        if len(selected) != len(inputs.drops) or not selected.issubset(drops):
            raise ProjectionError("invalid_snapshot")
        acl_work += sum(len(drops[key].campaign.allowed_channels) for key in selected)
        # Charge membership and repeated policy-key scans, not just route/drop pairs.
        # This is logical work admission, not a wall-clock execution-time guarantee.
        work = (len(channels) + 1) * (
            1 + 4 * campaign_work + 2 * len(wanted) * (len(inventory) + 1) + acl_work
            + len(selected) * (len(wanted) + 2 * len(drops) + 1)
        )
        if work > self.max_work:
            raise ProjectionError("budget_exceeded")

        def project(channel: Channel, order: int | None) -> ProjectedChannel:
            if (not isinstance(channel, Channel) or channel._twitch is not twitch
                    or type(channel.id) is not int or channel.id <= 0
                    or type(channel.acl_based) is not bool or type(channel.drops_enabled) is not bool):
                raise ProjectionError("invalid_snapshot")
            game = channel.game
            if game is not None and (type(game.id) is not int or game.id <= 0):
                raise ProjectionError("invalid_snapshot")
            priority = selector.get_mining_priority(channel)
            if (type(priority) is not tuple or len(priority) != 2
                    or any(type(value) is not int for value in priority)
                    or priority[0] not in (0, 1) or not 0 <= priority[1] <= MAX_INT):
                raise ProjectionError("invalid_snapshot")
            watchable = watcher.can_watch(channel)
            if type(watchable) is not bool:
                raise ProjectionError("invalid_snapshot")
            eligible = set()
            if watchable:
                for key in selected:
                    drop = drops[key]
                    if (drop.campaign.game in twitch.wanted_games
                            and (drop.campaign.game.is_special() or channel.drops_enabled)):
                        can_earn = drop.can_earn(channel)
                        if type(can_earn) is not bool:
                            raise ProjectionError("invalid_snapshot")
                        if can_earn:
                            eligible.add(key)
            return ProjectedChannel(str(channel.id), order, None if game is None else game.id,
                                    channel.acl_based, priority, watchable, frozenset(eligible))

        candidates = []
        for order, (channel_id, channel) in enumerate(channels.items()):
            if type(channel_id) is not int or channel_id != channel.id:
                raise ProjectionError("invalid_snapshot")
            candidates.append(project(channel, order))
        current = twitch.watching_channel.get_with_default(None)
        watching = None if current is None else project(current, None)
        eligible_current = watching if watching is not None and watching.watchable else None
        for candidate in sorted(candidates, key=lambda channel: channel.mining_priority):
            if candidate.watchable and ChannelSwitchPolicy.should_switch(
                candidate.mining_priority, candidate.acl_based,
                current_priority=None if eligible_current is None else eligible_current.mining_priority,
                current_acl_based=False if eligible_current is None else eligible_current.acl_based,
            ):
                return DeadlineChannelInputs(inputs.now, tuple(candidates), watching, "switch", candidate.channel_id)
        return DeadlineChannelInputs(inputs.now, tuple(candidates), watching,
                                     "idle" if eligible_current is None else "keep",
                                     None if eligible_current is None else eligible_current.channel_id)
