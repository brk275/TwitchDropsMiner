"""Only current history-statistics requests may publish data or errors."""

import pytest

from tests.test_history_frontend import run_javascript


HARNESS = """
let historyStatsRequestId = 0, historyStatsVisible = false;
let historyStatsData = null, historyRequestId = 0, historyCurrentPage = 0;
let historyTotal = 7, historyAllEntries = [{id:'keep'}];
const nodes = {
 'history-stats-panel': {style:{}}, 'history-stats-error': {textContent:''},
 'stats-total': {textContent:'7'},
 'stats-by-game': {firstChild:null}, 'stats-by-month': {firstChild:null}
};
const document = {getElementById: id => nodes[id] || null};
const pending = [], rendered = [], alerts = [];
const window = {confirm: () => true};
const latestData = {total_drops:2,by_game:{Latest:2},by_month:{'2026-10':2}};
const oldData = {total_drops:7,by_game:{Old:7},by_month:{'2026-09':7}};
function fetch(url, options) {
 if (options?.method === 'DELETE') return Promise.resolve({ok:CLEAR_OK,status:503});
 return new Promise((resolve,reject) => pending.push({resolve,reject}));
}
function settle(index, outcome, data) {
 if (outcome === 'network') pending[index].reject(new Error('network'));
 else pending[index].resolve({ok:outcome !== 'http',status:503,
   json:async()=> {if (outcome === 'json') throw new Error('json'); return data;}});
}
function renderHistoryStats(data) {rendered.push(data);}
function historyText(key, values) {return key + (values?.error || '');}
function alert(value) {alerts.push(value);}
function updateHistoryCount() {}
function setHistoryTbodyMessage() {}
function renderHistoryPagination() {}
"""


@pytest.mark.parametrize("old_outcome", ["success", "http", "json", "network"])
@pytest.mark.parametrize("latest_outcome", ["success", "http"])
def test_reopened_stats_ignore_superseded_success_and_errors(old_outcome, latest_outcome):
    result = run_javascript(["toggleHistoryStats"], HARNESS.replace("CLEAR_OK", "true") + """
(async () => {
 const old = toggleHistoryStats();
 await toggleHistoryStats();
 const latest = toggleHistoryStats();
 settle(1, LATEST, latestData);
 await latest;
 const expected = {data:historyStatsData,error:nodes['history-stats-error'].textContent,
                   rendered:[...rendered]};
 settle(0, OLD, oldData);
 await old;
 process.stdout.write(JSON.stringify({expected, actual:{data:historyStatsData,
   error:nodes['history-stats-error'].textContent,rendered},visible:historyStatsVisible}));
})();
""".replace("LATEST", repr(latest_outcome)).replace("OLD", repr(old_outcome)))
    assert result["actual"] == result["expected"]
    assert result["visible"] is True
    if latest_outcome == "success":
        assert result["actual"]["data"]["total_drops"] == 2
        assert result["actual"]["error"] == ""
    else:
        assert result["actual"]["data"] is None
        assert result["actual"]["error"] == "stats_errorHTTP 503"


@pytest.mark.parametrize("old_outcome", ["success", "http", "json", "network"])
@pytest.mark.parametrize("clear_ok", [True, False])
def test_clear_invalidates_stats_only_after_successful_deletion(old_outcome, clear_ok):
    result = run_javascript(
        ["toggleHistoryStats", "clearHistory"],
        HARNESS.replace("CLEAR_OK", str(clear_ok).lower()) + """
(async () => {
 const request = toggleHistoryStats();
 await clearHistory();
 settle(0, OLD, oldData);
 await request;
 process.stdout.write(JSON.stringify({data:historyStatsData,
  error:nodes['history-stats-error'].textContent,rendered,alerts,
  total:historyTotal,entries:historyAllEntries}));
})();
""".replace("OLD", repr(old_outcome)),
    )
    if clear_ok:
        assert result == {
            "data": {"total_drops": 0, "by_game": {}, "by_month": {}},
            "error": "", "rendered": [], "alerts": [], "total": 0, "entries": [],
        }
    else:
        assert result["alerts"] == ["clear_errorHTTP 503"]
        assert result["entries"] == [{"id": "keep"}]
        assert result["total"] == 7
        if old_outcome == "success":
            assert result["data"]["total_drops"] == 7
            assert len(result["rendered"]) == 1
            assert result["error"] == ""
        else:
            assert result["data"] is None
            assert result["rendered"] == []
            assert result["error"].startswith("stats_error")


@pytest.mark.parametrize("outcome", ["success", "network"])
def test_closing_stats_invalidates_pending_result_without_refetch(outcome):
    result = run_javascript(["toggleHistoryStats"], HARNESS.replace("CLEAR_OK", "true") + """
(async () => {
 const request = toggleHistoryStats();
 await toggleHistoryStats();
 settle(0, OUTCOME, oldData);
 await request;
 process.stdout.write(JSON.stringify({data:historyStatsData,rendered,
  error:nodes['history-stats-error'].textContent,requests:pending.length,
  visible:historyStatsVisible,display:nodes['history-stats-panel'].style.display}));
})();
""".replace("OUTCOME", repr(outcome)))
    assert result == {
        "data": None, "rendered": [], "error": "", "requests": 1,
        "visible": False, "display": "none",
    }


@pytest.mark.parametrize("reject", [False, True])
def test_request_superseded_during_json_decode_cannot_publish(reject):
    result = run_javascript(["toggleHistoryStats"], HARNESS.replace("CLEAR_OK", "true") + """
(async () => {
 let finishDecode, rejectDecode, decoding;
 const ready = new Promise(resolve => decoding = resolve);
 const old = toggleHistoryStats();
 pending[0].resolve({ok:true,json:()=> {
   decoding();
   return new Promise((resolve,reject)=> {finishDecode=resolve;rejectDecode=reject;});
 }});
 await ready;
 await toggleHistoryStats();
 const latest = toggleHistoryStats();
 settle(1, 'success', latestData);
 await latest;
 if (REJECT) rejectDecode(new Error('late decode')); else finishDecode(oldData);
 await old;
 process.stdout.write(JSON.stringify({data:historyStatsData,rendered,
  error:nodes['history-stats-error'].textContent}));
})();
""".replace("REJECT", str(reject).lower()))
    assert result["data"]["total_drops"] == 2
    assert result["rendered"] == [result["data"]]
    assert result["error"] == ""
