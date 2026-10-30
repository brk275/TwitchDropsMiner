import json
import subprocess

import pytest

from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function


pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")


def run_javascript(names, body):
    source = APP_JS.read_text(encoding="utf-8")
    functions = []
    for name in names:
        function = extract_javascript_function(source, name)
        if f"async function {name}(" in source:
            function = "async " + function
        functions.append(function)
    result = subprocess.run(
        [NODE, "-"], input="\n".join(functions) + body, text=True, capture_output=True, check=True
    )
    return json.loads(result.stdout)


def test_history_translations_use_text_and_refresh_dynamic_states():
    result = run_javascript(["historyText", "translateHistory", "updateHistoryCount"], """
const title = {dataset: {historyKey: 'title'}};
const input = {dataset: {historyPlaceholder: 'filter_game'}, attrs: {}, setAttribute(k,v) {this.attrs[k]=v;}};
const count = {};
const document = {querySelectorAll: s => s === '[data-history-key]' ? [title] : [input], getElementById: () => count};
const state = {translations: {gui: {history: {title:'历史', filter_game:'游戏', filtered_count:'{shown}/{total}', count:'总数：{total}'}}}};
const historyTotal = 10;
const historyAllEntries = [{}, {}];
const historyCurrentPage = 0;
let historyMessage = null;
const historyStatsData = {};
let pages = 0, stats = 0;
function renderHistoryPage() { pages++; }
function renderHistoryPagination() {}
function renderHistoryStats() { stats++; }
function setHistoryTbodyMessage() {}
translateHistory();
const first = {title:title.textContent, placeholder:input.placeholder, label:input.attrs['aria-label'], count:count.textContent};
state.translations.gui.history.title = '<img src=x onerror=alert(1)>';
state.translations.gui.history.filtered_count = '{shown} shown of {total}';
translateHistory();
process.stdout.write(JSON.stringify({first, literal:title.textContent, count:count.textContent, pages, stats}));
""")
    assert result == {
        "first": {"title": "历史", "placeholder": "游戏", "label": "游戏", "count": "2/10"},
        "literal": "<img src=x onerror=alert(1)>", "count": "2 shown of 10", "pages": 2, "stats": 2,
    }


def test_history_load_clamps_page_and_ignores_old_filter_response():
    result = run_javascript(["loadHistory"], """
const filter = {value: '原神'};
const since = {value: '2026-09-01'};
const document = {getElementById: id => id === 'history-filter-game' ? filter : since};
let historyRequestId = 0, historyCurrentPage = 7, historyTotal = 0, historyAllEntries = [];
const HISTORY_PAGE_SIZE = 50;
const pending = [], urls = [], pages = [];
function fetch(url) { urls.push(url); return new Promise(resolve => pending.push(resolve)); }
function updateHistoryCount() {}
function renderHistoryPage(page) { pages.push(page); }
function renderHistoryPagination() {}
function setHistoryTbodyMessage() {}
(async () => {
 const old = loadHistory();
 filter.value = 'Rust';
 const latest = loadHistory();
 pending[1]({ok:true, json:async()=>({total:1,entries:[{id:'latest'}]})});
 await latest;
 pending[0]({ok:true, json:async()=>({total:100,entries:[{id:'old'}]})});
 await old;
 const params = new URL(urls[0], 'http://test').searchParams;
 process.stdout.write(JSON.stringify({entries:historyAllEntries,page:historyCurrentPage,total:historyTotal,pages,
 game:params.get('game'),since:params.get('since')}));
})();
""")
    assert result == {"entries": [{"id": "latest"}], "page": 0, "total": 1, "pages": [0],
                      "game": "原神", "since": "2026-09-01"}


def test_clear_history_requires_confirmation_and_successful_response():
    result = run_javascript(["clearHistory"], """
let historyRequestId = 0, historyCurrentPage = 2, historyTotal = 1, historyAllEntries = [{id:'keep'}];
let historyStatsRequestId = 0;
let historyStatsData = null, historyStatsVisible = false, confirm = false, ok = false, calls = 0;
const document = {getElementById: () => null};
const window = {confirm: () => confirm};
function historyText(key) { return key; }
const alerts = [];
function alert(message) { alerts.push(message); }
async function fetch() { calls++; return {ok,status:500}; }
function updateHistoryCount() {}
function setHistoryTbodyMessage() {}
function renderHistoryPagination() {}
(async () => {
 await clearHistory();
 const cancelledCalls = calls;
 confirm = true;
 await clearHistory();
 const afterFailure = [...historyAllEntries];
 ok = true;
 await clearHistory();
 process.stdout.write(JSON.stringify({cancelledCalls,afterFailure,alerts,calls,
 entries:historyAllEntries,total:historyTotal,page:historyCurrentPage,historyRequestId,historyStatsData}));
})();
""")
    assert result == {"cancelledCalls": 0, "afterFailure": [{"id": "keep"}], "alerts": ["clear_error"],
                      "calls": 2, "entries": [], "total": 0, "page": 0, "historyRequestId": 1,
                      "historyStatsData": {"total_drops": 0, "by_game": {}, "by_month": {}}}


STATS_HARNESS = """
let historyRequestId = 0, historyStatsRequestId = 0, historyCurrentPage = 2;
let historyTotal = 9, historyAllEntries = [{id:'keep'}];
let historyStatsVisible = false, historyStatsData = {total_drops:9};
const nodes = {
 'history-stats-panel': {style:{}}, 'history-stats-error': {textContent:''},
 'stats-total': {textContent:'9'}, 'stats-by-game': {}, 'stats-by-month': {},
};
const document = {getElementById: id => nodes[id] || null};
const window = {confirm: () => true};
const requests = [], alerts = [], renders = [];
let deleteOk = true;
function fetch(url) {
 if (url === '/api/history') return Promise.resolve({ok:deleteOk,status:500});
 return new Promise((resolve,reject) => requests.push({resolve,reject}));
}
function historyText(key) { return key; }
function alert(message) { alerts.push(message); }
function renderHistoryStats(data) { renders.push(data.total_drops); nodes['stats-total'].textContent = String(data.total_drops); }
function updateHistoryCount() {}
function setHistoryTbodyMessage() {}
function renderHistoryPagination() {}
"""


@pytest.mark.parametrize("visible", [True, False])
@pytest.mark.parametrize("phase,outcome", [
    ("fetch", "success"), ("json", "success"), ("fetch", "network"),
    ("fetch", "http"), ("json", "error"),
])
def test_stats_reply_cannot_restore_cleared_counts_or_errors(visible, phase, outcome):
    result = run_javascript(["toggleHistoryStats", "clearHistory"], STATS_HARNESS + f"""
(async () => {{
 const old = toggleHistoryStats();
 let bodyResolve, bodyReject;
 if ({json.dumps(phase)} === 'json') {{
  requests[0].resolve({{ok:true,json:()=>new Promise((resolve,reject)=>{{bodyResolve=resolve;bodyReject=reject;}})}});
  await Promise.resolve(); await Promise.resolve();
 }}
 if (!{json.dumps(visible)}) await toggleHistoryStats();
 await clearHistory();
 if ({json.dumps(phase)} === 'json') {{
  if ({json.dumps(outcome)} === 'error') bodyReject(new Error('synthetic body failure'));
  else bodyResolve({{total_drops:9}});
 }} else if ({json.dumps(outcome)} === 'network') requests[0].reject(new Error('synthetic network failure'));
 else requests[0].resolve({{ok:{json.dumps(outcome != "http")},status:500,json:async()=>({{total_drops:9}})}});
 await old;
 process.stdout.write(JSON.stringify({{data:historyStatsData,total:nodes['stats-total'].textContent,
  error:nodes['history-stats-error'].textContent,renders,entries:historyAllEntries}}));
}})();
""")
    assert result == {"data": {"total_drops": 0, "by_game": {}, "by_month": {}},
                      "total": "0", "error": "", "renders": [], "entries": []}


@pytest.mark.parametrize("failure", [False, True])
def test_reopened_stats_rejects_older_reply_or_error(failure):
    result = run_javascript(["toggleHistoryStats"], STATS_HARNESS + f"""
(async () => {{
 const old = toggleHistoryStats();
 await toggleHistoryStats();
 const latest = toggleHistoryStats();
 requests[1].resolve({{ok:true,json:async()=>({{total_drops:12}})}});
 await latest;
 if ({json.dumps(failure)}) requests[0].reject(new Error('synthetic old error'));
 else requests[0].resolve({{ok:true,json:async()=>({{total_drops:9}})}});
 await old;
 process.stdout.write(JSON.stringify({{total:historyStatsData.total_drops,renders,error:nodes['history-stats-error'].textContent}}));
}})();
""")
    assert result == {"total": 12, "renders": [12], "error": ""}


def test_failed_clear_keeps_inflight_stats_valid():
    result = run_javascript(["toggleHistoryStats", "clearHistory"], STATS_HARNESS + """
(async () => {
 const pending = toggleHistoryStats();
 deleteOk = false;
 await clearHistory();
 requests[0].resolve({ok:true,json:async()=>({total_drops:12})});
 await pending;
 process.stdout.write(JSON.stringify({total:historyStatsData.total_drops,renders,alerts,entries:historyAllEntries}));
})();
""")
    assert result == {"total": 12, "renders": [12], "alerts": ["clear_error"], "entries": [{"id": "keep"}]}


def test_fresh_stats_after_clear_can_show_new_claims():
    result = run_javascript(["toggleHistoryStats", "clearHistory"], STATS_HARNESS + """
(async () => {
 nodes['history-stats-error'].textContent = 'old error';
 await clearHistory();
 const clearedError = nodes['history-stats-error'].textContent;
 const pending = toggleHistoryStats();
 requests[0].resolve({ok:true,json:async()=>({total_drops:1})});
 await pending;
 process.stdout.write(JSON.stringify({total:historyStatsData.total_drops,renders,clearedError}));
})();
""")
    assert result == {"total": 1, "renders": [1], "clearedError": ""}
