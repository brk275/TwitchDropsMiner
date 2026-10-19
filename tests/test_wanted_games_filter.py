"""Wanted game filtering uses real campaign eligibility and benefit selection."""

import unittest
from datetime import datetime, timedelta, timezone

from src.services.stream_selector import StreamSelector
from tests.test_priority_fallback import _campaign, _drop, _settings


class TestWantedGamesFilter(unittest.TestCase):
    def test_filter_wanted_campaigns(self):
        settings = _settings("Game1", "Game2", priority_list_only=True,
                             mining_benefits={"BADGE": True, "DIRECT_ENTITLEMENT": False})
        selected = _campaign("selected", "Game1", settings=settings,
                             drops=[_drop("selected-drop", benefit_type="BADGE")])
        disabled = _campaign("disabled", "Game2", game_id=2, settings=settings)
        unselected = _campaign("unselected", "Game3", game_id=3, settings=settings,
                               drops=[_drop("unselected-drop", benefit_type="BADGE")])
        claimed = _campaign("claimed", "Game1", settings=settings,
                            drops=[_drop("claimed-drop", benefit_type="BADGE")])
        claimed.timed_drops["claimed-drop"].is_claimed = True
        expired = _campaign("expired", "Game1", settings=settings,
                            ends_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                            drops=[_drop("expired-drop", benefit_type="BADGE")])

        games = StreamSelector().get_wanted_games(
            settings, [selected, disabled, unselected, claimed, expired],
        )
        self.assertEqual([game.name for game in games], ["Game1"])


if __name__ == "__main__":
    unittest.main()
