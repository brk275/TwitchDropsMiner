"""Campaign progress manager for tracking active drop mining progress."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from src.models import TimedDrop
    from src.web.managers.broadcaster import WebSocketBroadcaster


class CampaignProgressManager:
    """Manages active drop mining progress display and countdown timer.

    Tracks the currently mined drop and broadcasts real-time progress updates
    including remaining time and completion percentage to the web interface.
    """

    def __init__(self, broadcaster: WebSocketBroadcaster):
        self._broadcaster = broadcaster
        self._current_drop: TimedDrop | None = None
        self._remaining_seconds: int = 0
        self._display_revision: int = 0
        self._send_lock = asyncio.Lock()

    def _queue(self, event: str, payload: dict[str, object], drop: TimedDrop | None = None) -> None:
        self._display_revision += 1
        asyncio.create_task(self._emit_latest(self._display_revision, event, payload, drop))

    async def _emit_latest(
        self, revision: int, event: str, payload: dict[str, object], drop: TimedDrop | None,
    ) -> None:
        async with self._send_lock:
            if revision != self._display_revision or (
                drop is not None and (drop is not self._current_drop or drop.is_claimed)
            ):
                return
            await self._broadcaster.emit(event, payload)

    def update(self, drop: TimedDrop | None, remaining_seconds: int):
        """Update the current drop progress and remaining time.

        Args:
            drop: The drop currently being mined, or None if no active drop
            remaining_seconds: Estimated seconds remaining for this drop
        """
        if drop is None:
            self.stop_timer()
            return
        if drop.is_claimed:
            # A late claim for an older model must not clear its replacement.
            if self._current_drop is drop:
                self.stop_timer()
            return
        self._current_drop = drop
        self._remaining_seconds = remaining_seconds
        if drop:
            self._queue(
                "drop_progress",
                {
                    "drop_id": drop.id,
                    "drop_name": drop.name,
                    "campaign_name": drop.campaign.name,
                    "campaign_id": drop.campaign.id,
                    "game_name": drop.campaign.game.name,
                    "current_minutes": drop.current_minutes,
                    "required_minutes": drop.required_minutes,
                    "progress": drop.progress,
                    "remaining_seconds": remaining_seconds,
                },
                drop,
            )

    def stop_timer(self):
        """Stop the progress timer and clear the current drop."""
        self._current_drop = None
        self._remaining_seconds = 0
        self._queue("drop_progress_stop", {})

    def minute_almost_done(self) -> bool:
        """Check if the current progress minute is almost complete.

        Returns:
            True if remaining seconds is at or below zero
        """
        return self._remaining_seconds <= 0

    def get_current_drop(self) -> dict | None:
        """Get the current drop progress data for sending to newly connected clients.

        Returns:
            Dictionary with drop progress data, or None if no active drop
        """
        if self._current_drop is None or self._current_drop.is_claimed:
            return None

        drop = self._current_drop
        return {
            "drop_id": drop.id,
            "drop_name": drop.name,
            "campaign_name": drop.campaign.name,
            "campaign_id": drop.campaign.id,
            "game_name": drop.campaign.game.name,
            "current_minutes": drop.current_minutes,
            "required_minutes": drop.required_minutes,
            "progress": drop.progress,
            "remaining_seconds": self._remaining_seconds,
        }
