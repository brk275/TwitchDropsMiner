"""Live claim history must meet the same row contract as restored history."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.drop_history import HISTORY_FILENAME, DropHistory
from tests.test_stale_progress import make_campaign


def inputs():
    drop = SimpleNamespace(id='reward', name='Reward', benefits=[SimpleNamespace(name='Benefit')],
                           required_minutes=30)
    campaign = SimpleNamespace(id='campaign', name='Campaign', game=SimpleNamespace(name='Game'))
    return drop, campaign


@pytest.mark.parametrize('field', ['id', 'name', 'campaign_id', 'campaign_name', 'game', 'benefit'])
@pytest.mark.parametrize('value', [None, 7, '\ud800'])
def test_invalid_live_history_record_does_not_poison_later_valid_claims(tmp_path, field, value, caplog):
    history = DropHistory(tmp_path)
    previous, campaign = inputs()
    previous.id = 'previous'
    history.record(previous, campaign)
    target = tmp_path / HISTORY_FILENAME
    original = target.read_bytes()
    drop, campaign = inputs()
    if field == 'campaign_id':
        campaign.id = value
    elif field == 'campaign_name':
        campaign.name = value
    elif field == 'game':
        campaign.game.name = value
    elif field == 'benefit':
        drop.benefits[0].name = value
    else:
        setattr(drop, field, value)

    history.record(drop, campaign)

    assert [row['id'] for row in history.get_entries()] == ['previous']
    assert target.read_bytes() == original
    assert 'Ignored invalid drop history record.' in caplog.text
    assert 'surrogates' not in caplog.text
    good, campaign = inputs()
    good.id = 'later'
    history.record(good, campaign)
    assert [row['id'] for row in DropHistory(tmp_path).get_entries()] == ['later', 'previous']


@pytest.mark.parametrize('minutes', [False, -1, 1.5, None, '30'])
def test_invalid_live_minute_requirement_is_not_recorded(tmp_path, minutes):
    history = DropHistory(tmp_path)
    drop, campaign = inputs()
    drop.required_minutes = minutes
    history.record(drop, campaign)
    assert history.total_count == 0
    assert not (tmp_path / HISTORY_FILENAME).exists()


def test_valid_unicode_and_zero_minute_records_round_trip(tmp_path):
    history = DropHistory(tmp_path)
    drop, campaign = inputs()
    drop.name = 'Reward \U0001f680'
    drop.benefits[0].name = '\u5956\u52b1'
    drop.required_minutes = 0
    history.record(drop, campaign)
    rows = history.get_entries()
    assert len(rows) == 1
    assert rows[0]['drop_name'] == drop.name
    assert rows[0]['benefits'] == ['\u5956\u52b1']
    assert DropHistory(tmp_path).get_entries() == rows
    assert json.loads((tmp_path / HISTORY_FILENAME).read_text())['entries'] == rows


def test_invalid_record_does_not_reserve_id_for_corrected_later_record(tmp_path):
    history = DropHistory(tmp_path)
    drop, campaign = inputs()
    drop.name = None
    history.record(drop, campaign)
    drop.name = 'Corrected reward'
    history.record(drop, campaign)
    assert history.total_count == 1
    assert DropHistory(tmp_path).get_entries()[0]['drop_name'] == 'Corrected reward'


def test_duplicate_record_preserves_existing_short_circuit(tmp_path, monkeypatch):
    history = DropHistory(tmp_path)
    drop, campaign = inputs()
    history.record(drop, campaign)
    monkeypatch.setattr(history, '_save', MagicMock(side_effect=AssertionError('No duplicate write')))
    drop.name = None
    history.record(drop, campaign)
    assert history.total_count == 1


@pytest.mark.asyncio
async def test_bad_live_history_metadata_does_not_interrupt_confirmed_claim(tmp_path):
    owner = SimpleNamespace(
        settings=SimpleNamespace(drop_name_blacklist=[]), gui=MagicMock(),
        request_games_update=MagicMock(), print=MagicMock(), drop_history=DropHistory(tmp_path),
    )
    owner.gui.broadcast_wanted_items_now = AsyncMock()
    campaign = make_campaign(owner)
    campaign.name = 'Invalid \ud800 campaign'
    drop = campaign.timed_drops['drop']
    owner.gql_request = AsyncMock(return_value={'data': {'claimDropRewards': {'status': 'ELIGIBLE_FOR_ALL'}}})
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()

    assert await drop.claim() is True

    assert drop.is_claimed and drop.real_current_minutes == drop.required_minutes
    assert campaign.progress_revision == 1
    owner.gql_request.assert_awaited_once()
    owner.gui.broadcast_wanted_items_now.assert_awaited_once()
    drop._send_telegram_notification.assert_awaited_once()
    drop._send_discord_notification.assert_awaited_once()
    assert owner.drop_history.total_count == 0
    assert not (tmp_path / HISTORY_FILENAME).exists()
