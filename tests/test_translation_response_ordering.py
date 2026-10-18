"""Only the current translation request may replace labels or report failure."""

import pytest

from tests.test_history_frontend import run_javascript


HARNESS = """
let translationsRequestId = 0;
const initial = {language_name:'English'}, oldData = {language_name:'Deutsch'};
const latestData = {language_name:'Francais'};
const state = {translations:initial}, pending = [], rendered = [], errors = [];
const console = {log(){},error(message){errors.push(message);}};
function applyTranslations(data) {rendered.push(data.language_name);}
function fetch(url) {
 if (url !== '/api/translations') throw new Error('Unexpected endpoint');
 return new Promise((resolve,reject)=>pending.push({resolve,reject}));
}
function settle(index,outcome,data) {
 if (outcome === 'network') pending[index].reject(new Error('Network failed'));
 else pending[index].resolve({ok:outcome !== 'http',status:503,
   json:async()=>{if(outcome === 'json') throw new Error('Invalid JSON');return data;}});
}
"""


@pytest.mark.parametrize("old_outcome", ["success", "http", "json", "network"])
@pytest.mark.parametrize("latest_outcome", ["success", "http"])
def test_older_translation_response_cannot_override_newer_request(old_outcome, latest_outcome):
    result = run_javascript(["fetchAndApplyTranslations"], HARNESS + """
(async()=>{
 const old=fetchAndApplyTranslations(), latest=fetchAndApplyTranslations();
 settle(1,LATEST,latestData);await latest;
 const expected={language:state.translations.language_name,rendered:[...rendered],errors:[...errors]};
 settle(0,OLD,oldData);await old;
 process.stdout.write(JSON.stringify({expected,actual:{language:state.translations.language_name,rendered,errors}}));
})();
""".replace("LATEST", repr(latest_outcome)).replace("OLD", repr(old_outcome)))
    assert result["actual"] == result["expected"]
    assert result["actual"]["language"] == ("Francais" if latest_outcome == "success" else "English")
    assert result["actual"]["rendered"] == (["Francais"] if latest_outcome == "success" else [])
    assert len(result["actual"]["errors"]) == (0 if latest_outcome == "success" else 1)


@pytest.mark.parametrize("reject", [False, True])
def test_new_request_during_json_decode_invalidates_old_success_and_failure(reject):
    result = run_javascript(["fetchAndApplyTranslations"], HARNESS + """
(async()=>{
 let finish,fail,entered;
 const decoding=new Promise(resolve=>{entered=resolve;});
 const old=fetchAndApplyTranslations();
 pending[0].resolve({ok:true,json:()=>{entered();return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});}});
 await decoding;
 const latest=fetchAndApplyTranslations();
 settle(1,'success',latestData);await latest;
 if(REJECT)fail(new Error('Late decode failure'));else finish(oldData);
 await old;
 process.stdout.write(JSON.stringify({language:state.translations.language_name,rendered,errors}));
})();
""".replace("REJECT", str(reject).lower()))
    assert result == {"language": "Francais", "rendered": ["Francais"], "errors": []}


@pytest.mark.parametrize("outcome", ["network", "http", "json"])
def test_current_failure_keeps_existing_translations_and_logs_once(outcome):
    result = run_javascript(["fetchAndApplyTranslations"], HARNESS + """
(async()=>{
 const task=fetchAndApplyTranslations();settle(0,OUTCOME,{detail:'Unavailable'});await task;
 process.stdout.write(JSON.stringify({same:state.translations===initial,rendered,errors}));
})();
""".replace("OUTCOME", repr(outcome)))
    assert result == {"same": True, "rendered": [], "errors": ["Failed to fetch translations:"]}


def test_current_success_applies_exact_response_without_changing_settings():
    result = run_javascript(["fetchAndApplyTranslations"], HARNESS + """
(async()=>{
 state.settings={language:'Francais'};const settings=state.settings;
 const task=fetchAndApplyTranslations();settle(0,'success',latestData);await task;
 process.stdout.write(JSON.stringify({same:state.translations===latestData,settings:state.settings===settings,rendered,errors}));
})();
""")
    assert result == {"same": True, "settings": True, "rendered": ["Francais"], "errors": []}
