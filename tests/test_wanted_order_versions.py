"""Runtime wanted-order versions and the existing authorized response paths."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from src.services.game_order import TemporaryGameOrder
from src.web.gui_manager import WebGUIManager
from tests.javascript_helpers import NODE
from tests.test_wanted_order_response_frontend import run_order
from tests.test_wanted_queue_order import client as client
from tests.test_wanted_queue_order import web


def test_runtime_identity_and_revision_are_stable_and_not_shared():
    order = TemporaryGameOrder()
    initial = order.info()
    assert set(initial) == {'active', 'games', 'revision', 'instance_id'}
    assert initial['revision'] == 0
    assert len(initial['instance_id']) == 32
    assert UUID(initial['instance_id']).version == 4
    assert order.info() == initial
    assert TemporaryGameOrder().info()['instance_id'] != initial['instance_id']


def test_only_normalized_state_changes_advance_revision():
    order = TemporaryGameOrder()
    identity = order.info()['instance_id']
    order.set([' Alpha ', 'Beta', 'alpha'])
    assert order.info() == {'active': True, 'games': ['Alpha', 'Beta'],
                            'revision': 1, 'instance_id': identity}
    order.set(['Alpha', 'Beta', '', ' beta '])
    assert order.info()['revision'] == 1
    copy = order.info()
    copy['games'].reverse()
    order.rank('Beta')
    assert order.info()['games'] == ['Alpha', 'Beta']
    assert order.info()['revision'] == 1
    order.set(['Beta', 'Alpha'])
    assert order.info()['revision'] == 2
    assert order.clear() is True
    assert order.info() == {'active': False, 'games': [], 'revision': 3, 'instance_id': identity}
    assert order.clear() is False
    order.set([])
    assert order.info()['revision'] == 3


@pytest.mark.parametrize('invalid', [None, 'Alpha', [1], ['x' * 201], ['x'] * 201])
def test_rejected_sets_do_not_change_state_or_version(invalid):
    order = TemporaryGameOrder()
    order.set(['Kept'])
    previous = order.info()
    with pytest.raises(ValueError):
        order.set(invalid)
    assert order.info() == previous


def test_api_replies_use_current_instance_and_mutation_revision(client):
    browser, twitch = client
    initial = twitch.game_order.info()
    first = browser.post('/api/wanted-order', json={'games': ['Beta', 'Alpha']}).json()
    assert first == {'success': True, **twitch.game_order.info()}
    assert first['revision'] == initial['revision'] + 1
    assert first['instance_id'] == initial['instance_id']
    duplicate = browser.post('/api/wanted-order', json={'games': [' Beta ', 'Alpha', 'beta']}).json()
    assert duplicate == first
    cleared = browser.post('/api/wanted-order', json={'games': []}).json()
    assert cleared['revision'] == first['revision'] + 1
    assert cleared['instance_id'] == initial['instance_id']
    assert cleared['active'] is False and cleared['games'] == []


@pytest.mark.asyncio
async def test_initial_state_and_order_broadcast_keep_model_info(client, monkeypatch):
    _browser, twitch = client
    twitch.game_order.set(['Beta'])
    twitch.get_manual_mode_info = lambda: {'active': False}
    twitch.pause_info = lambda: {'paused': False}
    gui = MagicMock()
    monkeypatch.setattr(web, 'gui_manager', gui)
    monkeypatch.setattr(web.sio, 'register', MagicMock(return_value=True))
    emit = AsyncMock()
    monkeypatch.setattr(web.sio, 'emit', emit)
    await web.connect('synthetic-dashboard', {'asgi.scope': {}})
    assert emit.call_args.args[0] == 'initial_state'
    assert emit.call_args.args[1]['game_order'] == twitch.game_order.info()
    owner = WebGUIManager.__new__(WebGUIManager)
    owner._broadcaster = SimpleNamespace(emit=AsyncMock())
    owner.broadcast_game_order(twitch.game_order.info())
    await asyncio.sleep(0)
    owner._broadcaster.emit.assert_awaited_once_with('game_order_update', twitch.game_order.info())


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized', [False, True])
async def test_recovery_authorizes_before_reading_and_emits_fresh_order(client, monkeypatch, authorized):
    _browser, twitch = client
    twitch.game_order.set(['Before'])
    read_info = MagicMock(wraps=twitch.game_order.info)
    monkeypatch.setattr(twitch.game_order, 'info', read_info)
    gui = SimpleNamespace(get_wanted_game_tree=MagicMock(return_value=[{'game_name': 'Tree'}]))
    monkeypatch.setattr(web, 'gui_manager', gui)
    events = []

    async def authorize(sid):
        assert sid == 'synthetic-dashboard'
        assert not events
        gui.get_wanted_game_tree.assert_not_called()
        read_info.assert_not_called()
        twitch.game_order.set(['After authorization'])
        return authorized

    async def emit(event, payload, **kwargs):
        events.append((event, payload, kwargs))
        if event == 'wanted_items_update':
            twitch.game_order.set(['Newest'])

    monkeypatch.setattr(web.sio, 'authorize', authorize)
    monkeypatch.setattr(web.sio, 'emit', emit)
    await web.get_wanted_items('synthetic-dashboard')
    if not authorized:
        assert not events
        gui.get_wanted_game_tree.assert_not_called()
        read_info.assert_not_called()
        return
    assert events == [
        ('wanted_items_update', [{'game_name': 'Tree'}], {'to': 'synthetic-dashboard'}),
        ('game_order_update', twitch.game_order.info(), {'to': 'synthetic-dashboard'}),
    ]
    assert events[-1][1]['games'] == ['Newest']
    assert events[-1][1]['revision'] == 3


@pytest.mark.skipif(NODE is None, reason='Node.js is required for frontend tests')
@pytest.mark.parametrize('latest', [[], ['Beta', 'Alpha', 'Gamma']], ids=['clear', 'reorder'])
def test_actual_backend_versions_drive_late_http_socket_ordering(client, latest):
    browser, twitch = client
    initial = twitch.game_order.info()
    older = browser.post('/api/wanted-order', json={'games': ['Gamma', 'Alpha', 'Beta']}).json()
    newer = browser.post('/api/wanted-order', json={'games': latest}).json()
    run_order('const initial=' + json.dumps(initial) + ',older=' + json.dumps(older)
              + ',newer=' + json.dumps(newer) + ';' + r"""
handlers.initial_state({game_order:initial,wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(older.games);
handlers.game_order_update(older);
handlers.game_order_update(newer);
request.resolve({ok:true,json:async()=>older});await pending;
assert.deepEqual(gameOrder.games,newer.games);assert.equal(gameOrder.active,newer.active);
assert.equal(reset().hidden,!newer.active);
""")


@pytest.mark.asyncio
@pytest.mark.skipif(NODE is None, reason='Node.js is required for frontend tests')
async def test_lost_reply_and_missed_broadcast_recover_tree_and_metadata(client, monkeypatch):
    browser, twitch = client
    initial = twitch.game_order.info()
    applied = browser.post('/api/wanted-order', json={'games': ['Beta', 'Alpha']}).json()
    recovered_tree = [{'game_name': name, 'game_icon': None, 'campaigns': []}
                      for name in ['Beta', 'Alpha']]
    monkeypatch.setattr(web, 'gui_manager', SimpleNamespace(
        get_wanted_game_tree=lambda: recovered_tree,
    ))
    monkeypatch.setattr(web.sio, 'authorize', AsyncMock(return_value=True))
    events = []

    async def emit(event, payload, **kwargs):
        assert kwargs == {'to': 'synthetic-dashboard'}
        events.append([event, payload])

    monkeypatch.setattr(web.sio, 'emit', emit)
    await web.get_wanted_items('synthetic-dashboard')
    run_order('const initial=' + json.dumps(initial) + ',applied=' + json.dumps(applied)
              + ',recovery=' + json.dumps(events) + ';' + r"""
handlers.initial_state({game_order:initial,wanted_items:tree});
fetch=async()=>{throw new Error('synthetic lost reply');};
assert.equal(await saveWantedOrder(applied.games),false);
assert.deepEqual(socket.emits,['get_wanted_items']);
for(const [event,payload] of recovery)handlers[event](payload);
assert.deepEqual(names(),['Beta','Alpha']);assert.deepEqual(gameOrder.games,applied.games);
assert.equal(gameOrder.active,true);assert.equal(reset().hidden,false);
assert.equal(state.gameOrderServerRevision,applied.revision);
""")
