"""Only accepted claim confirmations can supersede pending progress reads."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.drop_history import DropHistory
from tests.test_stale_progress import mining_state as mining_state
from tests.test_stale_progress import poll


ACCEPTED = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
CONFIRMATIONS = [
    pytest.param(None, False, id="null"),
    pytest.param({"data": {"claimDropRewards": True}}, False, id="boolean-result"),
    pytest.param({**ACCEPTED, "errors": [{"message": "unconfirmed"}]}, False, id="partial-error"),
    pytest.param({**ACCEPTED, "error": None}, False, id="legacy-error"),
    pytest.param(ACCEPTED, True, id="accepted"),
    pytest.param({"data": {"claimDropRewards": {"status": "DROP_INSTANCE_ALREADY_CLAIMED"}}}, True, id="already-claimed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["drop", "sibling"])
@pytest.mark.parametrize("reply", ["session", "null", "error", "timeout"])
@pytest.mark.parametrize("payload,confirmed", CONFIRMATIONS)
async def test_only_confirmed_claim_retires_pending_progress(
    mining_state, tmp_path, source, reply, payload, confirmed,
):
    twitch, _, campaign = mining_state
    drop = campaign.timed_drops[source]
    for item in campaign.drops:
        item.extra_current_minutes = 2
    revision = campaign.progress_revision
    twitch.drop_history = DropHistory(tmp_path)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    confirmation = AsyncMock(return_value=payload)

    async def during_request(request):
        outer = twitch.gql_request
        twitch.gql_request = confirmation
        try:
            assert await drop.claim() is confirmed
        finally:
            twitch.gql_request = outer
        assert campaign.progress_revision == revision + int(confirmed)
        assert DropHistory(tmp_path).total_count == int(confirmed)

    assert await poll(twitch, during_request, returned_minutes=9, response=reply) == [20]
    confirmation.assert_awaited_once()
    assert drop.is_claimed is confirmed
    if confirmed:
        assert (drop.real_current_minutes, drop.extra_current_minutes) == (60, 0)
        other = next(item for item in campaign.drops if item is not drop)
        assert (other.real_current_minutes, other.extra_current_minutes) == (10, 2)
        assert campaign.progress_revision == revision + 1
        twitch.drop_history.record.assert_called_once_with(drop, campaign)
        twitch.request_games_update.assert_called_once_with()
        twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
        drop._send_telegram_notification.assert_awaited_once_with()
        drop._send_discord_notification.assert_awaited_once_with()
    else:
        assert [(item.real_current_minutes, item.extra_current_minutes) for item in campaign.drops] == (
            [(9, 0)] * 2 if reply == "session" else [(10, 3)] * 2
        )
        assert campaign.progress_revision == revision + int(reply == "session")
        twitch.drop_history.record.assert_not_called()
        twitch.request_games_update.assert_not_called()
        twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
        drop._send_telegram_notification.assert_not_awaited()
        drop._send_discord_notification.assert_not_awaited()
