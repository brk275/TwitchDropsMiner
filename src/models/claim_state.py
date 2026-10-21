"""Process-local positive claim evidence for one authenticated account lifetime."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from src.core.client import Twitch


class ClaimState:
    """Retain accepted acknowledgements, independently of replaceable inventory."""

    def __init__(self) -> None:
        self.active = True
        self._confirmed: set[tuple[str, str]] = set()

    @classmethod
    def for_client(cls, twitch: Twitch) -> ClaimState:
        state = getattr(twitch, "_claim_state", None)
        if not isinstance(state, cls):
            state = cls()
            twitch._claim_state = state
        return state

    def contains(self, campaign_id: str, drop_id: str) -> bool:
        return self.active and (campaign_id, drop_id) in self._confirmed

    def confirm(self, campaign_id: str, drop_id: str) -> bool:
        """Admit once before optional delivery, without using persisted history."""
        key = (campaign_id, drop_id)
        if not self.active or key in self._confirmed:
            return False
        self._confirmed.add(key)
        return True

    def retire(self) -> None:
        self.active = False
        self._confirmed.clear()
