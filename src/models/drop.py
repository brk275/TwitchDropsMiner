from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, TypedDict, TypeGuard

from dateutil.parser import isoparse

from src.config.constants import MAX_EXTRA_MINUTES
from src.config.operations import GQL_OPERATIONS
from src.exceptions import GQLException, MinerException
from src.i18n import _
from src.models.benefit import Benefit
from src.services.discord_service import DiscordNotifier
from src.services.telegram_service import TelegramNotifier
from src.utils import DropIgnoreReason


if TYPE_CHECKING:
    from src.config.constants import JsonType
    from src.core.client import Twitch
    from src.models.campaign import DropsCampaign
    from src.models.channel import Channel


logger = logging.getLogger("TwitchDrops")
DIMS_PATTERN = re.compile(r"-\d+x\d+(?=\.(?:jpg|png|gif)$)", re.I)

ProgressSource = Literal["inventory", "websocket", "current_drop"]


class ProgressObservation(TypedDict):
    minutes: int
    source: ProgressSource
    received_at: str


def remove_dimensions(url: str) -> str:
    """Remove dimension suffix from Twitch image URLs (e.g., -285x380.jpg)."""
    return DIMS_PATTERN.sub("", url)


class BaseDrop:
    def __init__(
        self, campaign: DropsCampaign, data: JsonType, claimed_benefits: dict[str, datetime]
    ):
        claim_data = data.get("self")
        if "self" in data and (
            not isinstance(claim_data, dict)
            or type(claim_data.get("isClaimed")) is not bool
            or "dropInstanceID" not in claim_data
            or (claim_data["dropInstanceID"] is not None
                and not self._is_claim_id(claim_data["dropInstanceID"]))
        ):
            raise MinerException("Invalid drop claim state")
        self._twitch: Twitch = campaign._twitch
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.campaign: DropsCampaign = campaign
        self.benefits: list[Benefit] = [Benefit(b) for b in (data["benefitEdges"] or [])]
        self.starts_at: datetime = isoparse(data["startAt"])
        self.ends_at: datetime = isoparse(data["endAt"])
        self.claim_id: str | None = None
        self.is_claimed: bool = False
        if isinstance(claim_data, dict):
            self.claim_id = claim_data["dropInstanceID"]
            self.is_claimed = claim_data["isClaimed"]
        elif (
            # If there's no self edge available, we can use claimed_benefits to determine
            # (with pretty good certainty) if this drop has been claimed or not.
            # To do this, we check if the benefitEdges appear in claimed_benefits, and then
            # deref their "lastAwardedAt" timestamps into a list to check against.
            # If the benefits were claimed while the drop was active,
            # the drop has been claimed too.
            (
                dts := [
                    claimed_benefits[bid]
                    for benefit in self.benefits
                    if (bid := benefit.id) in claimed_benefits
                ]
            )
            and all(self.starts_at <= dt < self.ends_at for dt in dts)
        ):
            self.is_claimed = True
        self.precondition_drops: list[str] = [d["id"] for d in (data["preconditionDrops"] or [])]

    def __repr__(self) -> str:
        if self.is_claimed:
            additional = ", claimed=True"
        elif self.can_earn():
            additional = ", can_earn=True"
        else:
            additional = ""
        return f"Drop({self.rewards_text()}{additional})"

    @property
    def preconditions_met(self) -> bool:
        campaign = self.campaign
        return all(
            (prerequisite := campaign.timed_drops.get(pid)) is not None
            and prerequisite.is_claimed
            for pid in self.precondition_drops
        )

    @property
    def ignore_reason(self) -> DropIgnoreReason | None:
        """Return why this unclaimed drop is ignored, if applicable."""
        if self.is_claimed:
            return None
        return self.campaign.get_drop_ignore_reason(self.id)

    @property
    def is_directly_ignored(self) -> bool:
        """Return whether this drop's name directly matches a configured keyword."""
        reason = self.ignore_reason
        return reason is not None and reason.kind == "keyword"

    @property
    def is_ignored(self) -> bool:
        """Return whether this drop is ignored directly or by a blocked dependency."""
        return self.ignore_reason is not None

    @property
    def is_mineable(self) -> bool:
        """Return whether this drop still contributes to a useful reward branch."""
        return not self.is_claimed and self.campaign.is_drop_mineable(self.id)

    def _on_state_changed(self) -> None:
        raise NotImplementedError

    def _base_earn_conditions(self) -> bool:
        # define when a drop can be earned or not
        return (
            self.is_mineable  # unclaimed and contributes to a nonignored reward branch
            and self.preconditions_met  # preconditions are met
        )

    def _base_can_earn(self) -> bool:
        # cross-participates in can_earn and can_earn_within handling, where a timeframe is added
        return (
            self._base_earn_conditions()
            # is within the timeframe
            and self.starts_at <= datetime.now(timezone.utc) < self.ends_at
        )

    def _can_earn_within(self, stamp: datetime) -> bool:
        # NOTE: This does not check the campaign's eligibility or active status
        return (
            self._base_earn_conditions()
            and self.ends_at > datetime.now(timezone.utc)
            and self.starts_at < stamp
        )

    def can_earn(self, channel: Channel | None = None, ignore_channel_status: bool = False) -> bool:
        return self._base_can_earn() and self.campaign._base_can_earn(
            channel, ignore_channel_status
        )

    @property
    def claim_pending(self) -> bool:
        return self.id in self.campaign._pending_claims

    @property
    def can_claim(self) -> bool:
        # The API guide permits claims up to 14 days after the campaign ends:
        # https://dev.twitch.tv/docs/drops/technical-guide/#can-claims-occur-after-the-campaign-ends
        # This bounds attempts with existing claim IDs; Twitch can reject them earlier.
        return (
            self._is_claim_id(self.claim_id)
            and not self.is_claimed
            and datetime.now(timezone.utc) < self.campaign.ends_at + timedelta(days=14)
        )

    @staticmethod
    def _is_claim_id(claim_id: object) -> TypeGuard[str]:
        return isinstance(claim_id, str) and bool(claim_id.strip())

    def update_claim(self, claim_id: object) -> bool:
        """Admit an opaque nonblank ID; leave the previous ID intact on rejection."""
        if not self._is_claim_id(claim_id):
            return False
        self.claim_id = claim_id
        return True

    async def generate_claim(self) -> None:
        # claim IDs now appear to be constructed from other IDs we have access to
        # Format: UserID#CampaignID#DropID
        # NOTE: This marks a drop as a ready-to-claim, so we may want to later ensure
        # its mining progress is finished first
        auth_state = await self.campaign._twitch.get_auth()
        self.claim_id = f"{auth_state.user_id}#{self.campaign.id}#{self.id}"

    def rewards_text(self, delim: str = ", ") -> str:
        return delim.join(benefit.name for benefit in self.benefits)

    def has_wanted_unclaimed_benefits(self, allowed_benefits: dict[str, bool]) -> bool:
        return len(self.get_wanted_unclaimed_benefits(allowed_benefits)) > 0

    def get_wanted_unclaimed_benefits(self, allowed_benefits: dict[str, bool]) -> list[str]:
        if self.is_claimed or not self.is_mineable:
            return []
        return [benefit.name for benefit in self.benefits if benefit.is_wanted(allowed_benefits)]

    def _apply_claim_result(self, result: bool) -> bool:
        """Commit a claim result synchronously; return whether it is newly claimed."""
        newly_claimed = result and not self.is_claimed
        if newly_claimed:
            self.is_claimed = True
            self.campaign.progress_revision += 1
            # A mixed campaign can still earn unwanted rewards after its last
            # wanted claim. Rebuild policy so fallback can take over promptly.
            self._twitch.request_games_update()
        return newly_claimed

    async def claim(self) -> bool:
        with self._twitch.claim_request():
            pending = self.campaign._pending_claims
            pending[self.id] = pending.get(self.id, 0) + 1
            try:
                result = await self._claim()
            finally:
                pending[self.id] -= 1
                if not pending[self.id]:
                    del pending[self.id]
        # Commit confirmed state before an optional notification can be cancelled.
        if self._apply_claim_result(result):
            claim_text = (
                f"{self.campaign.game.name}\n"
                f"{self.rewards_text()} "
                f"({self.campaign.claimed_drops}/{self.campaign.total_drops})"
            )
            # two different claim texts, becase a new line after the game name
            # looks ugly in the output window - replace it with a space
            self._twitch.print(
                _.t["status"]["claimed_drop"].format(drop=claim_text.replace("\n", " "))
            )
            await self._twitch.gui.broadcast_wanted_items_now()
            await self._send_telegram_notification()
            await self._send_discord_notification()
        elif not result:
            logger.error(f"Drop claim has potentially failed! Drop ID: {self.id}")
        return result

    async def _send_telegram_notification(self) -> None:
        """Notify once per successful claim without affecting the claim result."""
        try:
            bot_token = self._twitch.settings.telegram_bot_token
            chat_id = self._twitch.settings.telegram_chat_id
            if bot_token and chat_id:
                await TelegramNotifier(bot_token, chat_id, diagnostics=getattr(self._twitch, "diagnostics", None)).notify_drop_claimed(self)
        except Exception:
            logger.warning("Failed to send Telegram notification")

    async def _send_discord_notification(self) -> None:
        """Notify on the shared claim transition; transport failures cannot undo it."""
        try:
            webhook_url = self._twitch.settings.discord_webhook_url
            if webhook_url:
                await DiscordNotifier(
                    webhook_url, diagnostics=getattr(self._twitch, "diagnostics", None)
                ).notify_drop_claimed(self)
        except Exception:
            # Exception messages may contain the URL's secret token.
            logger.warning("Failed to send Discord notification")

    async def _claim(self) -> bool:
        """
        Returns True if the claim succeeded, False otherwise.
        """
        if self.is_claimed:
            return True
        if not self.can_claim:
            return False
        try:
            response = await self._twitch.gql_request(
                GQL_OPERATIONS["ClaimDrop"].with_variables(
                    {"input": {"dropInstanceID": self.claim_id}}
                )
            )
        except GQLException:
            # regardless of the error, we have to assume
            # the claiming operation has potentially failed
            return False
        if (not isinstance(response, dict) or "error" in response
                or response.get("errors") not in (None, [])):
            return False
        data = response.get("data")
        if not isinstance(data, dict) or data.get("errors") not in (None, []):
            return False
        result = data.get("claimDropRewards")
        return isinstance(result, dict) and isinstance(result.get("status"), str) and result["status"] in (
            "ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED",
        )


class TimedDrop(BaseDrop):
    def __init__(
        self, campaign: DropsCampaign, data: JsonType, claimed_benefits: dict[str, datetime]
    ):
        required_minutes = data.get("requiredMinutesWatched")
        if type(required_minutes) is not int or required_minutes < 0:
            raise MinerException("Invalid drop watch requirement")
        progress = data.get("self")
        minutes = progress.get("currentMinutesWatched") if isinstance(progress, dict) else None
        if minutes is not None and (type(minutes) is not int or minutes < 0):
            raise MinerException("Invalid drop watched minutes")
        super().__init__(campaign, data, claimed_benefits)
        self.real_current_minutes: int = minutes if minutes is not None else 0
        self.required_minutes: int = required_minutes
        self.extra_current_minutes: int = 0
        self._progress_observation: ProgressObservation | None = None
        if isinstance(progress, dict):
            self._record_progress_observation(minutes, "inventory")
        if self.is_claimed:
            # claimed drops may report inconsistent current minutes, so we need to overwrite them
            self.real_current_minutes = self.required_minutes

    def __repr__(self) -> str:
        if self.is_claimed:
            additional = ", claimed=True"
        elif self.can_earn():
            additional = ", can_earn=True"
        else:
            additional = ""
        if 0 < self.current_minutes < self.required_minutes:
            minutes = f", {self.current_minutes}/{self.required_minutes}"
        else:
            minutes = ""
        return f"Drop({self.rewards_text()}{minutes}{additional})"

    @property
    def current_minutes(self) -> int:
        return self.real_current_minutes + self.extra_current_minutes

    @property
    def observed_minutes(self) -> int | None:
        """Return the last direct reading, excluding estimates and sibling inference."""
        return self._progress_observation["minutes"] if self._progress_observation is not None else None

    @property
    def progress_observation(self) -> ProgressObservation | None:
        """Expose a copy so serialized payloads cannot mutate the recorded reading."""
        return self._progress_observation.copy() if self._progress_observation is not None else None

    def _record_progress_observation(self, minutes: object, source: ProgressSource | None) -> bool:
        if (
            source is None or source not in ("inventory", "websocket", "current_drop")
            or type(minutes) is not int or type(self.required_minutes) is not int
            or not 0 <= minutes <= self.required_minutes
        ):
            return False
        self._progress_observation = {
            "minutes": minutes, "source": source,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        return True

    @property
    def is_watch_drop(self) -> bool:
        """Return whether this drop can be earned by watching a stream."""
        return self.required_minutes > 0

    @property
    def remaining_minutes(self) -> int:
        return self.required_minutes - self.current_minutes

    @property
    def total_required_minutes(self) -> int:
        return self._total_chain_minutes(remaining=False, visiting=frozenset())

    @property
    def total_remaining_minutes(self) -> int:
        return self._total_chain_minutes(remaining=True, visiting=frozenset())

    def _total_chain_minutes(
        self, *, remaining: bool, visiting: frozenset[str]
    ) -> int:
        """Return the longest prerequisite chain without recursing through cycles."""
        if self.id in visiting or remaining and self.is_claimed:
            return 0

        next_visiting = visiting | {self.id}
        prerequisite_minutes = (
            prerequisite._total_chain_minutes(
                remaining=remaining, visiting=next_visiting
            )
            for prerequisite_id in self.precondition_drops
            if (prerequisite := self.campaign.timed_drops.get(prerequisite_id))
            is not None
        )
        own_minutes = self.remaining_minutes if remaining else self.required_minutes
        return own_minutes + max(prerequisite_minutes, default=0)

    @property
    def progress(self) -> float:
        if self.current_minutes <= 0 or not self.is_watch_drop:
            return 0.0
        elif self.current_minutes >= self.required_minutes:
            return 1.0
        return self.current_minutes / self.required_minutes

    @property
    def availability(self) -> float:
        import math

        now = datetime.now(timezone.utc)
        if self.is_watch_drop and self.total_remaining_minutes > 0 and now < self.ends_at:
            return ((self.ends_at - now).total_seconds() / 60) / self.total_remaining_minutes
        return math.inf

    def _base_earn_conditions(self) -> bool:
        return super()._base_earn_conditions() and self.is_watch_drop

    def _base_can_earn(self) -> bool:
        return self.extra_current_minutes < MAX_EXTRA_MINUTES and super()._base_can_earn()

    def _can_earn_within(self, stamp: datetime) -> bool:
        return self.extra_current_minutes < MAX_EXTRA_MINUTES and super()._can_earn_within(stamp)

    def can_reconcile_progress(self, channel: Channel | None = None) -> bool:
        """Accept confirmed progress without treating the local estimate cap as evidence."""
        return super()._base_can_earn() and self.campaign._base_can_earn(channel)

    def _on_state_changed(self) -> None:
        self._twitch.gui.inv.update_drop(self)

    def _update_real_minutes(self, delta: int) -> None:
        if not self.can_reconcile_progress():
            return
        new_minutes = self.real_current_minutes + delta
        # Keep the existing lower-bound guard for inferred sibling progress.
        if new_minutes < 0:
            new_minutes = self.real_current_minutes
        new_minutes = min(new_minutes, self.required_minutes)
        if new_minutes == self.real_current_minutes and self.extra_current_minutes == 0:
            return
        self.real_current_minutes = new_minutes
        self.extra_current_minutes = 0
        self._on_state_changed()

    def _bump_minutes(self, channel: Channel | None) -> bool:
        if self.can_earn(channel):
            self.extra_current_minutes += 1
            self._on_state_changed()
            if self.extra_current_minutes >= MAX_EXTRA_MINUTES:
                return True
        return False

    def _apply_claim_result(self, result: bool) -> bool:
        newly_claimed = super()._apply_claim_result(result)
        if result:
            self.real_current_minutes = self.required_minutes
            self.extra_current_minutes = 0
            if newly_claimed:
                self._twitch.drop_history.record(self, self.campaign)
        self._on_state_changed()
        return newly_claimed

    def display(self, *, countdown: bool = True, subone: bool = False) -> None:
        """Display this drop in the GUI with progress information."""
        self._twitch.gui.display_drop(self, countdown=countdown, subone=subone)

    def update_minutes(self, new_minutes: int, *, source: ProgressSource | None = None) -> None:
        """Update the current watched minutes for this drop."""
        if type(new_minutes) is not int:
            return
        # Even an unchanged observation supersedes an older in-flight poll.
        self.campaign.progress_revision += 1
        if not self.can_reconcile_progress():
            return
        observed = self._record_progress_observation(new_minutes, source)
        previous = (self.real_current_minutes, self.extra_current_minutes)
        delta: int = new_minutes - self.real_current_minutes
        if self.real_current_minutes + delta < 0:
            delta = -self.real_current_minutes
        elif self.real_current_minutes + delta > self.required_minutes:
            delta = self.required_minutes - self.real_current_minutes
        exhausted = [
            drop for drop in self.campaign.drops
            if drop.extra_current_minutes >= MAX_EXTRA_MINUTES
        ]
        self.campaign._update_real_minutes(delta)
        if observed and (self.real_current_minutes, self.extra_current_minutes) == previous:
            self._on_state_changed()
        if any(drop.can_earn() for drop in exhausted):
            self._twitch.request_games_update()
