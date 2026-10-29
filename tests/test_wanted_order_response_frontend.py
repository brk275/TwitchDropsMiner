"""Wanted-order causal metadata and rollback tests using the actual socket handlers."""

import json

import pytest

from tests.javascript_helpers import APP_JS, NODE
from tests.test_telegram_frontend import run_javascript
from tests.test_wanted_queue_frontend import DOM, FUNCTIONS


pytestmark = pytest.mark.skipif(NODE is None, reason='Node.js is required for frontend tests')


def run_order(body):
    source = APP_JS.read_text(encoding='utf-8')
    functions = list(dict.fromkeys([*FUNCTIONS, 'isGameOrderInfo', 'paintGameOrder']))
    functions = [name for name in functions if f'function {name}(' in source]
    events = []
    for name in ('initial_state', 'game_order_update', 'wanted_items_update'):
        start = source.index(f"socket.on('{name}'")
        events.append(source[start:source.index('\n});', start) + 4])
    harness = DOM + r"""
const handlers={};socket.on=(name,callback)=>{handlers[name]=callback;};
function updatePauseUI(){} function clearDropProgress(){}
Object.assign(state,{gameOrderRevision:0,gameOrderServerRevision:null,gameOrderInstance:null,gameOrderEpoch:0});
setWantedOrderError(false);
const A='a'.repeat(32),B='b'.repeat(32);
const info=(revision,games=[],instance_id=A)=>({active:games.length>0,games,revision,instance_id});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const response=data=>({ok:true,json:async()=>({success:true,...data})});
const repaint=()=>{if(typeof paintGameOrder==='function')paintGameOrder();else updateGameOrder(gameOrder);};
const reset=()=>document.getElementById('wanted-order-reset');
const error=()=>document.getElementById('wanted-order-error');
"""
    run_javascript(functions, harness + '\n'.join(events) + '\n(async()=>{\n' + body
                   + '\n})().catch(error=>{console.error(error);process.exitCode=1;});')


@pytest.mark.parametrize('latest', [[], ['Beta', 'Alpha', 'Gamma']], ids=['clear', 'reorder'])
def test_late_post_success_cannot_overwrite_newer_order(latest):
    run_order('const latest=' + json.dumps(latest) + ';' + r"""
handlers.initial_state({game_order:info(0),wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Gamma','Alpha','Beta']);
handlers.game_order_update(info(1,['Gamma','Alpha','Beta']));
handlers.game_order_update(info(2,latest));
request.resolve(response(info(1,['Gamma','Alpha','Beta'])));await pending;
assert.deepEqual(gameOrder.games,latest);assert.equal(gameOrder.active,latest.length>0);
assert.equal(reset().hidden,latest.length===0);assert.equal(wantedOrderBusy,false);
""")


@pytest.mark.parametrize('failure', ['network', 'http', 'application', 'json'])
def test_failed_reset_cannot_restore_tree_replaced_while_pending(failure):
    run_order('const failure=' + json.dumps(failure) + ';' + r"""
handlers.initial_state({game_order:info(1,['Alpha','Beta','Gamma']),wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder([]);
handlers.wanted_items_update([tree[1],tree[2]]);
if(failure==='network')request.reject(new Error('private-host-and-token'));
else request.resolve({ok:failure!=='http',json:async()=>{
 if(failure==='json')throw new Error('private-response');
 return {success:false};
}});
assert.equal(await pending,false);
assert.deepEqual(names(),['Beta','Gamma']);
assert.equal(error().hidden,false);assert.equal(error().textContent,'<img src=x> Failed');
assert.equal(error().children.length,0);assert.deepEqual(socket.emits,['get_wanted_items']);
assert.equal(wantedOrderBusy,false);
""")


@pytest.mark.parametrize('socket_revision', [1, 2])
def test_newer_http_is_not_suppressed_by_older_or_equal_socket(socket_revision):
    run_order('const socketRevision=' + str(socket_revision) + ';' + r"""
handlers.initial_state({game_order:info(2),wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Beta','Alpha','Gamma']);
handlers.game_order_update(info(socketRevision));
request.resolve(response(info(3,['Beta','Alpha','Gamma'])));await pending;
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);assert.equal(reset().hidden,false);
assert.equal(state.gameOrderServerRevision,3);
""")


def test_older_socket_and_same_instance_initial_cannot_undo_http():
    run_order(r"""
handlers.initial_state({game_order:info(0),wanted_items:tree});
fetch=async()=>response(info(2,['Beta','Alpha','Gamma']));
await saveWantedOrder(['Beta','Alpha','Gamma']);
handlers.game_order_update(info(1));
handlers.initial_state({game_order:info(1)});
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);assert.equal(reset().hidden,false);
assert.equal(state.gameOrderServerRevision,2);
""")


@pytest.mark.parametrize('initial_first', [False, True])
def test_same_instance_initial_keeps_pending_newer_http(initial_first):
    run_order('const initialFirst=' + json.dumps(initial_first) + ';' + r"""
if(initialFirst)handlers.initial_state({game_order:info(0)});
renderWantedItems(tree);
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Beta','Alpha','Gamma']);
handlers.initial_state({game_order:info(1)});
request.resolve(response(info(2,['Beta','Alpha','Gamma'])));await pending;
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);assert.equal(reset().hidden,false);
""")


@pytest.mark.parametrize('failure', ['success', 'network', 'http', 'application', 'json'])
@pytest.mark.parametrize('restart', ['new-instance', 'omitted-order', 'legacy-order'])
def test_restart_or_legacy_initial_retires_pending_success_and_errors(failure, restart):
    run_order('const failure=' + json.dumps(failure) + ',restart=' + json.dumps(restart) + ';' + r"""
handlers.initial_state({game_order:info(9,['Alpha']),wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Gamma','Alpha','Beta']);
const initial={wanted_items:[tree[1],tree[0]]};
if(restart==='new-instance')initial.game_order=info(0,[],B);
if(restart==='legacy-order')initial.game_order={active:false,games:[]};
handlers.initial_state(initial);
if(failure==='network')request.reject(new Error('private-token'));
else request.resolve({ok:failure!=='http',json:async()=>{
 if(failure==='json')throw new Error('private-response');
 return {success:failure!=='application',...info(10,['Gamma','Alpha','Beta'])};
}});
await pending;
assert.deepEqual(gameOrder.games,[]);assert.equal(reset().hidden,true);
assert.deepEqual(names(),['Beta','Alpha']);assert.equal(error().hidden,true);
assert.deepEqual(socket.emits,[]);assert.equal(wantedOrderBusy,false);
if(restart==='new-instance'){
 handlers.game_order_update(info(1,['Beta'],B));
 handlers.game_order_update(info(11,[],A));
 assert.deepEqual(gameOrder.games,['Beta']);
}
""")


def test_foreign_instance_http_and_socket_do_not_rebind_context():
    run_order(r"""
handlers.initial_state({game_order:info(1,['Alpha']),wanted_items:tree});
const epoch=state.gameOrderEpoch,observed=state.gameOrderRevision;
fetch=async()=>response(info(99,[],B));await saveWantedOrder([]);
handlers.game_order_update(info(100,[],B));
assert.deepEqual(gameOrder.games,['Alpha']);assert.equal(state.gameOrderInstance,A);
assert.equal(state.gameOrderEpoch,epoch);assert.equal(state.gameOrderRevision,observed);
""")


@pytest.mark.parametrize('versioned', [False, True], ids=['legacy', 'modern'])
def test_pure_repaints_and_equal_versioned_updates_do_not_advance_observation(versioned):
    run_order('const versioned=' + json.dumps(versioned) + ';' + r"""
const initial=versioned?info(1):{active:false,games:[]};
handlers.initial_state({game_order:initial,wanted_items:tree});
const observed=state.gameOrderRevision,epoch=state.gameOrderEpoch;
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Beta','Alpha','Gamma']);
assert.equal(state.gameOrderRevision,observed,'busy paint is not an observation');
if(versioned)handlers.game_order_update({...initial});
repaint();repaint();
assert.equal(state.gameOrderRevision,observed);assert.equal(state.gameOrderEpoch,epoch);
state.translations.gui.wanted.reset='Translated reset';repaint();
assert.equal(reset().textContent,'Translated reset');assert.equal(reset().disabled,true);
request.resolve(response(versioned?info(2,['Beta','Alpha','Gamma']):{active:true,games:['Beta','Alpha','Gamma']}));
await pending;
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);
assert.equal(state.gameOrderRevision,observed+1,'finally paint is not an observation');
assert.equal(reset().disabled,false);
""")


def test_equal_legacy_external_receipt_still_guards_pending_reply():
    run_order(r"""
handlers.initial_state({game_order:{active:false,games:[]},wanted_items:tree});
const observed=state.gameOrderRevision;
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Beta','Alpha','Gamma']);
// Our intermediate active broadcast was lost; this equal clear is a newer receipt.
handlers.game_order_update({active:false,games:[]});
request.resolve(response({active:true,games:['Beta','Alpha','Gamma']}));await pending;
assert.equal(gameOrder.active,false);assert.deepEqual(gameOrder.games,[]);
assert.equal(state.gameOrderRevision,observed+1);assert.equal(reset().hidden,true);
""")


@pytest.mark.parametrize('failure', ['success', 'network', 'http', 'application', 'json'])
@pytest.mark.parametrize('legacy', ['unversioned', 'revision-only'])
def test_initial_modern_instance_retires_known_legacy_requests_and_errors(failure, legacy):
    run_order('const failure=' + json.dumps(failure) + ',legacy=' + json.dumps(legacy) + ';' + r"""
const previous={active:true,games:['Alpha']};
if(legacy==='revision-only')previous.revision=9;
handlers.initial_state({game_order:previous,wanted_items:tree});
const epoch=state.gameOrderEpoch;
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder([]);
setWantedOrderError(true);
handlers.initial_state({game_order:info(0,[],B),wanted_items:[tree[1]]});
if(failure==='network')request.reject(new Error('old legacy failure'));
else request.resolve({ok:failure!=='http',json:async()=>{
 if(failure==='json')throw new Error('old legacy body');
 return {success:failure!=='application',active:true,games:['Alpha']};
}});
await pending;
assert.equal(state.gameOrderEpoch,epoch+1);assert.equal(state.gameOrderInstance,B);
assert.equal(state.gameOrderServerRevision,0);assert.equal(gameOrder.active,false);
assert.deepEqual(names(),['Beta']);assert.equal(error().hidden,true);
assert.deepEqual(socket.emits,[]);assert.equal(wantedOrderBusy,false);
""")


def test_legacy_newer_observation_preserves_best_effort_state():
    run_order(r"""
handlers.initial_state({wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Gamma','Alpha','Beta']);
handlers.game_order_update({active:true,games:['Beta','Alpha','Gamma']});
request.resolve(response({active:true,games:['Gamma','Alpha','Beta']}));await pending;
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);
""")


@pytest.mark.parametrize('newer_tree', [False, True])
def test_failed_optimistic_reorder_rolls_back_only_its_own_tree(newer_tree):
    run_order('const newerTree=' + json.dumps(newer_tree) + ';' + r"""
handlers.initial_state({game_order:info(0),wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Gamma','Alpha','Beta']);
handlers.game_order_update(info(2,['Beta','Alpha','Gamma']));
if(newerTree)handlers.wanted_items_update([tree[1],tree[0]]);
request.reject(new Error('offline'));await pending;
assert.deepEqual(names(),newerTree?['Beta','Alpha']:['Alpha','Beta','Gamma']);
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);
assert.equal(error().hidden,false);assert.deepEqual(socket.emits,['get_wanted_items']);
""")


@pytest.mark.parametrize('http_newer', [False, True])
def test_socket_during_json_read_is_ordered_by_server_revision(http_newer):
    run_order('const httpNewer=' + json.dumps(http_newer) + ';' + r"""
handlers.initial_state({game_order:info(0),wanted_items:tree});
const body=deferred(),started=deferred();
fetch=async()=>({ok:true,json:()=>{started.resolve();return body.promise;}});
const pending=saveWantedOrder(['Gamma','Alpha','Beta']);await started.promise;
handlers.game_order_update(info(2,['Beta','Alpha','Gamma']));
body.resolve({success:true,...info(httpNewer?3:1,['Gamma','Alpha','Beta'])});await pending;
assert.deepEqual(gameOrder.games,httpNewer?['Gamma','Alpha','Beta']:['Beta','Alpha','Gamma']);
""")


@pytest.mark.parametrize('event', ['game_order_update', 'initial_state'])
@pytest.mark.parametrize('invalid', [
    {'active': False, 'games': [], 'revision': value, 'instance_id': 'a' * 32}
    for value in [-1, True, '2', 1.5, None, 2 ** 53]
] + [
    {'active': False, 'games': [], 'revision': 2, 'instance_id': value}
    for value in [None, '', 42, 'private-host', 'a' * 33]
] + [{'active': 'false', 'games': [], 'revision': 2, 'instance_id': 'a' * 32},
     {'active': False, 'games': [1], 'revision': 2, 'instance_id': 'a' * 32}])
def test_malformed_metadata_cannot_mutate_confirmed_context(event, invalid):
    run_order('const event=' + json.dumps(event) + ',invalid=' + json.dumps(invalid) + ';' + r"""
handlers.initial_state({game_order:info(1,['Alpha'])});
const before=[state.gameOrderEpoch,state.gameOrderRevision,state.gameOrderServerRevision,state.gameOrderInstance];
handlers[event](event==='initial_state'?{game_order:invalid}:invalid);
assert.deepEqual(gameOrder.games,['Alpha']);assert.equal(reset().hidden,false);
assert.deepEqual([state.gameOrderEpoch,state.gameOrderRevision,state.gameOrderServerRevision,state.gameOrderInstance],before);
""")


def test_unversioned_frames_cannot_override_modern_instance_until_initial_reset():
    run_order(r"""
handlers.initial_state({game_order:info(4,['Alpha'])});
handlers.game_order_update({active:false,games:[]});
handlers.game_order_update({active:false,games:[],revision:99});
assert.deepEqual(gameOrder.games,['Alpha']);
handlers.initial_state({});
handlers.game_order_update({active:true,games:['Beta']});
assert.deepEqual(gameOrder.games,['Beta']);assert.equal(state.gameOrderInstance,null);
""")


def test_noop_reset_reply_does_not_advance_observation():
    run_order(r"""
handlers.initial_state({game_order:info(0),wanted_items:tree});
const observed=state.gameOrderRevision;
setWantedOrderError(true);fetch=async()=>response(info(0));
await saveWantedOrder([]);
assert.equal(state.gameOrderRevision,observed);assert.equal(state.gameOrderServerRevision,0);
assert.equal(error().hidden,true);assert.equal(reset().disabled,false);
""")


@pytest.mark.parametrize('payload', [None, {},
    {'success': True, 'active': True, 'games': ['Beta'], 'revision': -1, 'instance_id': 'a' * 32},
    {'success': True, 'active': True, 'games': ['Beta'], 'revision': 2, 'instance_id': None},
    {'success': True, 'active': True, 'games': [1], 'revision': 2, 'instance_id': 'a' * 32},
])
def test_malformed_http_is_a_fixed_error_without_mutating_metadata(payload):
    run_order('const payload=' + json.dumps(payload) + ';' + r"""
handlers.initial_state({game_order:info(1,['Alpha']),wanted_items:tree});
const before=[state.gameOrderEpoch,state.gameOrderRevision,state.gameOrderServerRevision,state.gameOrderInstance];
fetch=async()=>({ok:true,json:async()=>payload});
assert.equal(await saveWantedOrder(['Gamma','Alpha','Beta']),false);
assert.deepEqual(gameOrder.games,['Alpha']);assert.deepEqual(names(),['Alpha','Beta','Gamma']);
assert.deepEqual([state.gameOrderEpoch,state.gameOrderRevision,state.gameOrderServerRevision,state.gameOrderInstance],before);
assert.equal(error().textContent,'<img src=x> Failed');assert.equal(error().children.length,0);
assert.deepEqual(socket.emits,['get_wanted_items']);assert.equal(wantedOrderBusy,false);
""")


def test_revision_only_legacy_server_keeps_pause_style_compatibility():
    run_order(r"""
handlers.initial_state({game_order:{active:false,games:[],revision:0},wanted_items:tree});
const request=deferred();fetch=()=>request.promise;
const pending=saveWantedOrder(['Beta','Alpha','Gamma']);
handlers.game_order_update({active:false,games:[],revision:1});
request.resolve(response({active:true,games:['Beta','Alpha','Gamma'],revision:2}));await pending;
handlers.game_order_update({active:false,games:[],revision:1});
assert.deepEqual(gameOrder.games,['Beta','Alpha','Gamma']);
assert.equal(state.gameOrderServerRevision,2);assert.equal(state.gameOrderInstance,null);
""")
