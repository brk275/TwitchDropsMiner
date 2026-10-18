"""Reconnect inventory snapshots must not retain campaigns missing on the server."""

import json
import subprocess

import pytest

from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function


pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for frontend behavior tests")


def run_inventory_script(assertions):
    source = APP_JS.read_text(encoding="utf-8")
    handlers = source[source.index("socket.on('initial_state'"):source.index("// ==================== UI Update Functions")]
    functions = "\n".join(extract_javascript_function(source, name) for name in (
        "addCampaign", "clearInventory", "updateDrop",
    ))
    script = """
const assert = require('node:assert/strict');
const handlers = {}, socket = {on(name,callback) {handlers[name]=callback;}};
const filters = {show_active:true,show_expired:false,game_name_search:['Fixture']};
const selectedInventoryGames = ['Fixture'];
const state = {campaigns:{},channels:{7:{id:7,watching:true}},settings:{inventory_filters:filters}};
const rendered = [];
function renderInventory() {rendered.push(JSON.parse(JSON.stringify(state.campaigns)));}
function clearDropProgress() {}
""" + functions + handlers + """
const old = {id:'old',name:'Removed campaign',active:true,drops:[{id:'old-drop',current_minutes:1}]};
const fresh = {id:'fresh',name:'Current campaign',active:true,drops:[{id:'fresh-drop',current_minutes:17}]};
handlers.initial_state({campaigns:[old]});
""" + assertions
    result = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rows", [
    [{"id": "fresh", "name": "Current campaign", "active": True, "drops": []}],
    [],
    [{"id": "upcoming", "name": "Future campaign", "active": False, "upcoming": True, "drops": []}],
])
def test_full_snapshot_replaces_inventory_instead_of_retaining_removed_campaigns(rows):
    run_inventory_script("""
const rows = ROWS;
const previous = state.campaigns;
handlers.initial_state({campaigns:rows});
assert.deepEqual(Object.keys(state.campaigns),rows.map(row=>row.id));
assert.equal(state.campaigns.old,undefined);
assert.deepEqual(Object.values(state.campaigns),rows);
assert.deepEqual(rendered.at(-1),Object.fromEntries(rows.map(row=>[row.id,row])));
assert.equal(previous.old,old);
assert.equal(state.channels[7].watching,true);
assert.equal(state.settings.inventory_filters,filters);
assert.deepEqual(selectedInventoryGames,['Fixture']);
""".replace("ROWS", json.dumps(rows)))


def test_same_id_snapshot_replaces_obsolete_fields_and_drop_rows():
    run_inventory_script("""
old.obsolete = true;
handlers.initial_state({campaigns:[{id:'old',name:'Updated',active:false,expired:true,
  drops:[{id:'new-drop',current_minutes:0}]}]});
assert.equal(state.campaigns.old.obsolete,undefined);
assert.equal(state.campaigns.old.active,false);
assert.equal(state.campaigns.old.expired,true);
assert.deepEqual(state.campaigns.old.drops,[{id:'new-drop',current_minutes:0}]);
""")


@pytest.mark.parametrize("payload", [{}, {"campaigns": None}])
def test_omitted_or_null_campaigns_keep_existing_partial_event_behavior(payload):
    run_inventory_script("""
const previous = state.campaigns, renderCount = rendered.length;
handlers.initial_state(PAYLOAD);
assert.equal(state.campaigns,previous);
assert.equal(state.campaigns.old,old);
assert.equal(rendered.length,renderCount);
""".replace("PAYLOAD", json.dumps(payload)))


def test_incremental_campaign_drop_and_batch_events_still_work_after_reconnect():
    run_inventory_script("""
handlers.initial_state({campaigns:[fresh]});
handlers.campaign_add({id:'added',name:'New campaign',drops:[]});
handlers.drop_update({campaign_id:'fresh',drop:{id:'fresh-drop',current_minutes:18}});
assert.deepEqual(Object.keys(state.campaigns),['fresh','added']);
assert.equal(state.campaigns.fresh.drops[0].current_minutes,18);
handlers.inventory_batch_update({campaigns:[{...fresh,active:false,expired:true}]});
assert.deepEqual(Object.keys(state.campaigns),['fresh']);
assert.equal(state.campaigns.fresh.expired,true);
handlers.inventory_clear();
assert.deepEqual(state.campaigns,{});
assert.deepEqual(rendered.at(-1),{});
""")
