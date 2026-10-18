"""
Inventory service for managing campaigns, drops, and inventory fetching.

This service handles fetching campaign data from Twitch's GraphQL API,
managing the inventory state, and determining active campaigns.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from dateutil.parser import isoparse

from src.api import GQLClient
from src.config import GQL_OPERATIONS
from src.exceptions import ExitRequest, GQLException
from src.i18n import _
from src.models import DropsCampaign
from src.utils import chunk
from src.utils.gql_response_observer import GQLResponseObserver


if TYPE_CHECKING:
    from collections.abc import Callable

    from src.config import JsonType
    from src.core.client import Twitch
    from src.models.channel import Channel


logger = logging.getLogger("TwitchDrops")


@dataclass(frozen=True)
class _ProgressReceipt:
    row: JsonType = field(repr=False)
    campaign_id: str
    drop_id: str
    minutes: int
    received_at: datetime


class _ProgressReceipts:
    """Keep response provenance through merges without modifying wire payloads."""

    _MAX_ROWS = 8192

    def __init__(self) -> None:
        self._rows: dict[int, _ProgressReceipt] = {}
        self._exhausted = False

    def record(self, campaign: JsonType, received_at: datetime) -> None:
        if self._exhausted:
            return
        campaign_id = campaign.get("id")
        rows = campaign.get("timeBasedDrops")
        if not isinstance(campaign_id, str) or not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            progress = row.get("self")
            minutes = progress.get("currentMinutesWatched") if isinstance(progress, dict) else None
            if type(minutes) is int:
                if id(row) in self._rows:
                    continue
                if len(self._rows) >= self._MAX_ROWS:
                    # Discarded retries must not retain rows indefinitely or renew old evidence.
                    self._rows.clear()
                    self._exhausted = True
                    return
                # Hold the row alive and retain the first receipt if a caller reuses it.
                self._rows[id(row)] = _ProgressReceipt(
                    row, campaign_id, row["id"], minutes, received_at,
                )

    def record_response(self, response: object, received_at: datetime) -> None:
        if self._exhausted:
            return
        rows = response if isinstance(response, list) else [response]
        for row in rows:
            data = row.get("data") if isinstance(row, dict) else None
            if not isinstance(data, dict):
                continue
            user = data.get("currentUser")
            if isinstance(user, dict):
                inventory = user.get("inventory")
                if isinstance(inventory, dict):
                    self._record_campaigns(inventory.get("dropCampaignsInProgress"), received_at)
                self._record_campaigns(user.get("dropCampaigns"), received_at)
            user = data.get("user")
            campaign = user.get("dropCampaign") if isinstance(user, dict) else None
            if isinstance(campaign, dict):
                self.record(campaign, received_at)

    def _record_campaigns(self, campaigns: object, received_at: datetime) -> None:
        if isinstance(campaigns, list):
            for campaign in campaigns:
                if isinstance(campaign, dict):
                    self.record(campaign, received_at)

    def for_campaign(self, campaign: JsonType) -> dict[str, datetime]:
        receipts: dict[str, datetime] = {}
        if self._exhausted:
            return receipts
        rows = campaign.get("timeBasedDrops")
        if not isinstance(rows, list):
            return receipts
        for row in rows:
            receipt = self._rows.get(id(row))
            if receipt is None or receipt.row is not row:
                continue
            progress = row.get("self")
            minutes = progress.get("currentMinutesWatched") if isinstance(progress, dict) else None
            if (campaign["id"] == receipt.campaign_id and row.get("id") == receipt.drop_id
                    and type(minutes) is int and minutes == receipt.minutes):
                receipts[receipt.drop_id] = receipt.received_at
        return receipts


class InventoryService:
    """
    Service responsible for inventory and campaign management.

    Handles:
    - Fetching campaign details from GraphQL
    - Fetching inventory (in-progress campaigns)
    - Determining active campaign for a channel
    - Managing campaign data and claimed benefits
    """

    def __init__(self, twitch: Twitch) -> None:
        """
        Initialize the inventory service.

        Args:
            twitch: The Twitch client instance
        """
        self._twitch = twitch

    def _clear_inventory_state(self) -> None:
        """Clear derived campaign and drop state before replacing inventory."""
        self._twitch._drops.clear()
        self._twitch._campaigns.clear()
        self._twitch.gui.inv.clear()
        self._twitch.inventory.clear()
        self._twitch._mnt_triggers.clear()

    def clear_cached_state(self) -> None:
        """Clear local derived miner state while preserving credentials and settings."""
        logger.info("Clearing local derived campaign and channel state")

        self._twitch.stop_watching()
        self._twitch.restart_watching()

        tracked_channels = list(self._twitch.channels.values())
        self._twitch._remove_channel_topics(tracked_channels)
        self._twitch.channels.clear()
        self._twitch.gui.channels.clear()
        self._twitch.gui.clear_channel_selection()

        self._twitch.clear_manual_mode("Local cache cleared")
        self._twitch.wanted_games.clear()
        self._clear_inventory_state()
        self._twitch.gui.set_games(set())
        self._twitch.gui.broadcast_wanted_items()

        if self._twitch._mnt_task is not None and not self._twitch._mnt_task.done():
            self._twitch._mnt_task.cancel()
        self._twitch._mnt_task = None

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]], *,
        _receipts: _ProgressReceipts | None = None,
    ) -> dict[str, JsonType]:
        """
        Fetch detailed campaign data for a chunk of campaign IDs.

        Args:
            campaigns_chunk: List of (campaign_id, campaign_data) tuples

        Returns:
            Dictionary mapping campaign IDs to their detailed data
        """
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        if not campaign_ids:
            return {}
        auth_state = await self._twitch.get_auth()

        response_list_raw = await self._twitch.gql_request(
            [
                GQL_OPERATIONS["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        received_at = datetime.now(timezone.utc)

        # Ensure we have a list
        response_list: list[JsonType] = (
            response_list_raw if isinstance(response_list_raw, list) else [response_list_raw]
        )

        if len(response_list) != len(campaign_ids):
            raise GQLException("Invalid campaign details response")
        fetched_data: dict[str, JsonType] = {}
        try:
            for response_json in response_list:
                campaign_data = response_json["data"]["user"]["dropCampaign"]
                if campaign_data is None:
                    continue
                campaign_id = campaign_data["id"]
                if (not isinstance(campaign_id, str) or campaign_id not in campaign_ids
                        or campaign_id in fetched_data):
                    raise GQLException("Invalid campaign details response")
                fetched_data[campaign_id] = campaign_data
                if _receipts is not None:
                    _receipts.record(campaign_data, received_at)
        except (KeyError, TypeError):
            raise GQLException("Invalid campaign details response") from None

        # Catalog-only records cannot replace unavailable details for new campaigns.
        return GQLClient.merge_data(
            {key: campaign_ids[key] for key in fetched_data}, fetched_data,
        )

    async def collect_inventory(
        self, *, status_update: Callable[[str], None] | None = None,
        max_campaigns: int | None = None, max_drops: int | None = None,
    ) -> list[DropsCampaign]:
        """Collect detached campaigns without replacing live state or publishing images."""
        for limit in (max_campaigns, max_drops):
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("Invalid inventory collection limit")
        receipts = _ProgressReceipts()
        with GQLResponseObserver(receipts.record_response).observe():
            return await self._collect_inventory(receipts, status_update, max_campaigns, max_drops)

    async def _collect_inventory(
        self, receipts: _ProgressReceipts, status_update: Callable[[str], None] | None,
        max_campaigns: int | None, max_drops: int | None,
    ) -> list[DropsCampaign]:
        """Fetch within the owned response-observation scope."""
        status_update = status_update if status_update is not None else lambda message: None
        status_update(_.t["gui"]["status"]["fetching_inventory"])

        # fetch in-progress campaigns (inventory)
        response = await self._twitch.gql_request(GQL_OPERATIONS["Inventory"])
        received_at = datetime.now(timezone.utc)
        inventory: JsonType = response["data"]["currentUser"]["inventory"]
        ongoing_campaigns: list[JsonType] = inventory["dropCampaignsInProgress"] or []
        self._check_collection_budget(ongoing_campaigns, max_campaigns, max_drops)
        for campaign_data in ongoing_campaigns:
            receipts.record(campaign_data, received_at)

        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, datetime] = {
            b["id"]: isoparse(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]
        }

        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}

        # fetch general available campaigns data (campaigns)
        response = await self._twitch.gql_request(GQL_OPERATIONS["Campaigns"])
        received_at = datetime.now(timezone.utc)
        available_list: list[JsonType] = response["data"]["currentUser"]["dropCampaigns"] or []
        self._check_collection_budget(available_list, max_campaigns, max_drops)
        for campaign_data in available_list:
            receipts.record(campaign_data, received_at)
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {
            c["id"]: c
            for c in available_list
            if c["status"] in applicable_statuses  # that are currently not expired
        }

        # Keep inventory progress/claim data usable if its details disappear mid-refresh.
        known_metadata = {
            key: data for key, data in available_campaigns.items() if key in inventory_data
        }
        if known_metadata:
            inventory_data = GQLClient.merge_data(inventory_data, known_metadata)

        # fetch detailed data for each campaign, in chunks
        status_update(_.t["gui"]["status"]["fetching_campaigns"])
        fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.fetch_campaigns(campaigns_chunk, _receipts=receipts))
            for campaigns_chunk in chunk(available_campaigns.items(), 20)
        ]

        try:
            for coro in asyncio.as_completed(fetch_campaigns_tasks):
                chunk_campaigns_data = await coro
                self._check_collection_budget(list(chunk_campaigns_data.values()), max_campaigns, max_drops)
                # merge the inventory and campaigns datas together
                inventory_data = GQLClient.merge_data(inventory_data, chunk_campaigns_data)
                self._check_collection_budget(list(inventory_data.values()), max_campaigns, max_drops)
        finally:
            # as_completed does not own children, including on parent cancellation.
            for task in fetch_campaigns_tasks:
                task.cancel()
            await asyncio.gather(*fetch_campaigns_tasks, return_exceptions=True)

        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            if inventory_data[campaign_id]["game"] is None:
                del inventory_data[campaign_id]

        # use the merged data to create campaign objects
        campaigns: list[DropsCampaign] = [
            DropsCampaign(self._twitch, campaign_data, claimed_benefits,
                          progress_receipts=receipts.for_campaign(campaign_data))
            for campaign_data in inventory_data.values()
        ]
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)
        return campaigns

    @staticmethod
    def _check_collection_budget(
        campaigns: list[JsonType], max_campaigns: int | None, max_drops: int | None,
    ) -> None:
        if max_campaigns is not None and len(campaigns) > max_campaigns:
            raise GQLException("Inventory collection exceeds record budget")
        if max_drops is not None and sum(len(c.get("timeBasedDrops") or []) for c in campaigns) > max_drops:
            raise GQLException("Inventory collection exceeds record budget")

    async def fetch_inventory(self) -> None:
        """Collect campaigns, replace the live inventory and publish its GUI state."""
        status_update = self._twitch.gui.status.update
        campaigns = await self.collect_inventory(status_update=status_update)
        self._clear_inventory_state()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)

        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._twitch._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            self._twitch.inventory.append(campaign)
            self._twitch._campaigns[campaign.id] = campaign

        # concurrently add the campaigns into the GUI
        # NOTE: this fetches pictures from the CDN, so might be slow without a cache
        status_update(
            _.t["gui"]["status"]["adding_campaigns"].format(counter=f"(0/{len(campaigns)})")
        )
        add_campaign_tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self._twitch.gui.inv.add_campaign(campaign))
            for campaign in campaigns
        ]

        try:
            for i, coro in enumerate(asyncio.as_completed(add_campaign_tasks), start=1):
                await coro
                status_update(
                    _.t["gui"]["status"]["adding_campaigns"].format(
                        counter=f"({i}/{len(campaigns)})"
                    )
                )
                # this is needed here explicitly, because cache reads from disk don't raise this
                from src.config import State

                if self._twitch._state == State.EXIT:
                    raise ExitRequest()
        finally:
            for task in add_campaign_tasks:
                task.cancel()
            await asyncio.gather(*add_campaign_tasks, return_exceptions=True)

        self._twitch._mnt_triggers.extend(sorted(switch_triggers))

        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._twitch._mnt_triggers and self._twitch._mnt_triggers[0] <= now:
            self._twitch._mnt_triggers.popleft()

        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._twitch._mnt_task is not None and not self._twitch._mnt_task.done():
            self._twitch._mnt_task.cancel()
        self._twitch._mnt_task = asyncio.create_task(
            self._twitch._maintenance_service.run_maintenance_task()
        )

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        """
        Determine the active campaign for a given channel (or watching channel).

        Returns the wanted campaign with the least remaining minutes that can be
        earned on the specified channel. An explicit channel takes precedence over
        the watched channel. This is a local guess, not confirmed Twitch allocation.

        Args:
            channel: The channel to check (defaults to watching channel)

        Returns:
            The active DropsCampaign, or None if no campaign can be earned
        """
        if not self._twitch.wanted_games:
            return None

        watching_channel = channel if channel is not None else self._twitch.watching_channel.get_with_default(None)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None

        campaigns: list[DropsCampaign] = []
        for campaign in self._twitch.inventory:
            if campaign.game in self._twitch.wanted_games and campaign.can_earn(watching_channel):
                campaigns.append(campaign)

        if campaigns:
            campaigns.sort(key=lambda c: c.remaining_minutes)
            return campaigns[0]

        return None
