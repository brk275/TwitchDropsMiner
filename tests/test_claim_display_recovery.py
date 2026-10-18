"""Confirmed claims and advisory-read failures retain truthful active display."""

import asyncio
from types import MethodType
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import GQL_OPERATIONS
from src.drop_history import DropHistory
from src.exceptions import GQLException, GQLInvalidResponse
from src.services.message_handlers import MessageHandlerService
from src.web.gui_manager import WebGUIManager
from src.web.managers.campaigns import CampaignProgressManager
from tests.test_claim_followup_reliability import CONFIRMED
from tests.test_claim_followup_reliability import claiming as claiming
from tests.test_stale_progress import mining_state as mining_state


@pytest.mark.asyncio
@pytest.mark.parametrize("switched", [False, True])
@pytest.mark.parametrize("followup", [None, GQLInvalidResponse(), GQLException("test"), TimeoutError()])
async def test_advisory_failure_does_not_reactivate_claimed_reward(claiming, switched, followup):
    state = claiming
    emitter = AsyncMock()
    manager = CampaignProgressManager(MagicMock(emit=emitter))
    state.twitch.gui.progress = manager
    state.twitch.gui.display_drop = MethodType(WebGUIManager.display_drop, state.twitch.gui)
    sibling = state.campaign.timed_drops["sibling"]
    state.drop.display()
    await asyncio.sleep(0)
    emitter.reset_mock()

    async def query(operation):
        if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            if switched:
                sibling.display()
            return CONFIRMED
        if isinstance(followup, Exception):
            raise followup
        return followup

    state.twitch.gql_request = AsyncMock(side_effect=query)
    await MessageHandlerService(state.twitch).process_drops(42, state.event)
    await asyncio.sleep(0)
    assert state.drop.is_claimed and state.drop.real_current_minutes == 60
    assert sibling.real_current_minutes == 10 and not sibling.is_claimed
    assert state.campaign.progress_revision == 1
    assert [row["id"] for row in DropHistory(state.directory).get_entries()] == [state.drop.id]
    state.twitch.restart_watching.assert_called_once_with()
    assert state.twitch.gql_request.await_count == 2
    if switched:
        current = manager.get_current_drop()
        assert current["drop_id"] == sibling.id
        assert manager._current_drop is sibling
        emitter.assert_awaited_once_with("drop_progress", current)
    else:
        assert manager.get_current_drop() is None and manager.minute_almost_done()
        emitter.assert_awaited_once_with("drop_progress_stop", {})
