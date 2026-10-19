"""The dashboard wanted tree serializes real selected reward branches."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.core.client import Twitch
from src.models.game import Game
from src.web.gui_manager import WebGUIManager
from tests.test_priority_fallback import _campaign, _drop, _settings


class TestWantedItems(unittest.TestCase):
    def setUp(self):
        self.twitch = MagicMock(spec=Twitch)
        self.twitch.settings = _settings("Game1", "Game2", priority_list_only=True,
                                        mining_benefits={"BADGE": True, "DIRECT_ENTITLEMENT": False})
        self.twitch.helper = MagicMock()
        self.twitch.get_change_state_callable.return_value = lambda: None
        self.gui = WebGUIManager(self.twitch)
        self.gui._broadcaster = MagicMock()

    def test_get_wanted_tree(self):
        first = _campaign("c1", "Game1", settings=self.twitch.settings,
                          drops=[_drop("Drop1", benefit_type="BADGE")])
        first.name = "Campaign1"
        first.game = Game({"id": "1", "name": "Game1", "boxArtURL": "http://img1"})
        first.timed_drops["Drop1"].benefits[0].name = "Badge1"
        disabled = _campaign("c2", "Game2", game_id=2, settings=self.twitch.settings)
        other = _campaign("c3", "Game3", game_id=3, settings=self.twitch.settings,
                          drops=[_drop("Drop3", benefit_type="BADGE")])
        unavailable = _campaign("c4", "Game1", settings=self.twitch.settings,
                                ends_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                                drops=[_drop("Drop4", benefit_type="BADGE")])
        self.twitch.inventory = [first, disabled, other, unavailable]

        result = self.gui.get_wanted_game_tree()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["game_name"], "Game1")
        self.assertEqual(result[0]["game_icon"], "http://img1")
        campaigns = result[0]["campaigns"]
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(campaigns[0]["name"], "Campaign1")
        self.assertEqual(campaigns[0]["drops"], [{"name": "Drop1", "benefits": ["Badge1"]}])

    def test_get_wanted_tree_claimed_filtering(self):
        first = _campaign("c1", "Game1", settings=self.twitch.settings,
                          drops=[_drop("Drop1", benefit_type="BADGE")])
        first.timed_drops["Drop1"].is_claimed = True
        self.twitch.inventory = [first]
        self.assertEqual(self.gui.get_wanted_game_tree(), [])


if __name__ == "__main__":
    unittest.main()
