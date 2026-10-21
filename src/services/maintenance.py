"""
Maintenance service for periodic inventory reloads and local policy triggers.

This service queues game recalculation at known campaign boundaries while preserving
the configured periodic inventory reload cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.config import CALL
from src.utils import task_wrapper


if TYPE_CHECKING:
    from src.core.client import Twitch


logger = logging.getLogger("TwitchDrops")


class MaintenanceService:
    """
    Service responsible for periodic maintenance tasks.

    Handles:
    - Configured periodic inventory reloads
    - Campaign-triggered game recalculation
    - Task scheduling based on time triggers
    """

    def __init__(self, twitch: Twitch) -> None:
        """
        Initialize the maintenance service.

        Args:
            twitch: The Twitch client instance
        """
        self._twitch = twitch

    @task_wrapper(critical=True)
    async def run_maintenance_task(self) -> None:
        """
        Execute the maintenance task loop.

        Keep the inventory deadline anchored to this cycle's configured interval.
        Known policy changes queue the normal games-update path, including its
        claim-ready checks and channel discovery. Inventory fetch restarts the task.

        The maintenance logic:
        1. Wait for the next known boundary or periodic inventory deadline.
        2. Coalesce elapsed boundaries into one queued games update.
        3. At the periodic deadline, request inventory instead.
        """
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(
            minutes=self._twitch.settings.minimum_refresh_interval_minutes
        )

        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break

            next_trigger = next_period
            if self._twitch._mnt_triggers:
                next_trigger = min(next_trigger, self._twitch._mnt_triggers[0])

            trigger_type: str = "Reload" if next_trigger == next_period else "Games update"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                ),
            )

            await asyncio.sleep((next_trigger - now).total_seconds())

            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if now < next_trigger:
                # Timer resolution or a wall-clock correction can wake us early.
                continue

            if next_trigger != next_period:
                while self._twitch._mnt_triggers and self._twitch._mnt_triggers[0] <= now:
                    self._twitch._mnt_triggers.popleft()
                logger.log(CALL, "Maintenance task requests games update")
                self._twitch.request_games_update()

        # this triggers a restart of this task every (up to) <timedelta> minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self._twitch.request_inventory_refresh()
