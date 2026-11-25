import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import case_audit, case_forecast
from app.case_material import CaseInputError, parse, timestamp
from app.db import connect
from app.main import app
from test_stage3 import simulated_answer
from test_stage4 import quote


FIRST = '2025-02-07T16:00:00-05:00'
SECOND = '2025-02-10T09:30:00-05:00'
MATERIAL = f"""@symbol TEST
@round {FIRST}
@evidence TEST_ONE 2025-02-07T15:45:00-05:00 https://fixture.invalid/public-1
TEST_FIRST_ONLY documented change in plans.
@round {SECOND}
@evidence TEST_TWO 2025-02-09T13:00:00-05:00 https://another.invalid/public-2
TEST_SECOND_ONLY new statement with final confirmation.
@quote TEST_QUOTE 2025-02-10T09:30:00-05:00 https://fixture.invalid/quote
{{"TEST_QUOTE_SENTINEL":true}}
"""


def root(case='nvda'):
    return '/api/cases/'+case+'/workflow'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app) as browser:
        yield browser


def save(client, kind, text, case='nvda'):
    response = client.put('/api/cases/'+case+'/'+kind,json={'text':text})
    assert response.is_success
    return response.json()['version']


def truth(first=FIRST):
    return json.dumps({'rounds':[
        {'cutoff':first,
         'outcome':{'summary':'TEST_VALIDATION_ONLY outcome','event':True,'branch':'up',
                    'occurred_at':'2025-02-09T10:00:00-05:00','published_at':'2025-02-09T13:00:00-05:00'},
         'reference':{'evidence_ids':['TEST_ONE','TEST_TWO'],'conflict_ids':['TEST_ONE'],'notes':'TEST_REFERENCE_ONLY'}},
        {'cutoff':SECOND,'outcome':{'summary':'TEST SECOND ROUND ACTUAL'}},
    ]})


def test_safe_material_rejects_unassigned_and_future():
    with pytest.raises(CaseInputError, match='截断'):
        parse('TEST without time')
    with pytest.raises(CaseInputError,match='晚于'):
        parse(MATERIAL.replace('2025-02-07T15:45:00-05:00','2025-02-07T16:01:00-05:00'))
    with pytest.raises(CaseInputError,match='严格递增'):
        parse(MATERIAL.replace(SECOND,FIRST))
    with pytest.raises(CaseInputError,match='带时区'):
        parse(MATERIAL.replace(FIRST,'2025-02-07T16:00:00'))
    assert parse(MATERIAL)['rounds'][0]['cutoff']=='2025-02-07T21:00:00+00:00'


def test_case_lifecycle_no_validation_or_future_leak(client,monkeypatch):
    assert client.get(root()+'/preview').status_code == 422
    assert client.get('/api/cases/gold/input').json()['text'] == ''
    one = save(client,'input',MATERIAL)
    validation_version = save(client,'validation',truth())
    assert client.get(root()+'/preview').json()['rounds'][1]['quote_ids'] == ['TEST_QUOTE']
    cfg={'provider':'openai_compatible','endpoint':'https://model.cases.invalid','api_key':'TEST_KEY','model':'TEST_MODEL'}
    assert client.put('/api/providers/llm',json=cfg).is_success
    packets = []
    original = httpx.AsyncClient

    def model(request):
        assert request.url.host == 'model.cases.invalid'
        assert request.url.path == '/chat/completions'
        packet = json.loads(request.content)
        for forbidden in ('TEST_VALIDATION_ONLY','TEST_REFERENCE_ONLY','TEST_QUOTE_SENTINEL','TEST_NEW_VERSION'):
            assert forbidden not in str(packet)
        body = json.loads(packet['messages'][1]['content'])
        if not packets:
            assert 'TEST_SECOND_ONLY' not in str(packet)
            assert not body['prior_research']
        else:
            assert body['prior_research']['previous_forecast']
            assert 'TEST_FIRST_ONLY' in str(packet) and 'TEST_SECOND_ONLY' in str(packet)
        packets.append(body)
        answer = simulated_answer(packet)
        answer['joint']['event']={'low':.4,'high':.6}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}],
                                       'usage':{'prompt_tokens':411,'completion_tokens':117}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(model),**kw))
    data = {'input_version':one,'round_index':0,'confirmed':False}
    assert client.post(root()+'/runs',json=data).status_code == 422
    started = client.post(root()+'/runs',json={**data,'confirmed':True})
    assert started.status_code == 202
    job=started.json()
    path=root()+'/predictions/'+job['forecast_id']
    locked=client.get(path).json()
    assert locked['status']=='locked' and len(locked['snapshot']['sources'])==1
    assert locked['snapshot']['simulation']['execution']=='none'
    assert client.get('/api/opportunities').json()['items']==[]
    assert client.get(root('gold')+'/predictions/'+job['forecast_id']).status_code == 404

    evaluated=client.post(path+'/evaluations',json={'validation_version':validation_version}).json()
    result=evaluated['result']
    assert result['scores']['probability']['brier']==.25
    assert result['scores']['evidence']['outside_visible_material']==['TEST_TWO']
    assert result['scores']['options']['score'] is None
    assert result['scores']['decision']['score']==100
    assert result['scores']['timing']['within_locked_window']
    assert client.post(path+'/evaluations',json={}).json()['id']==evaluated['id']
    assert client.get(path).json()==locked
    assert 'TEST_VALIDATION_ONLY' not in json.dumps(locked)
    with pytest.raises(Exception,match='locked prediction'):
        with connect('predictions') as db:
            db.execute('UPDATE case_predictions SET snapshot=NULL WHERE id=?',(job['forecast_id'],))

    save(client,'input',MATERIAL.replace('TEST_SECOND_ONLY','TEST_NEW_VERSION'))
    next_job=client.post(root()+'/runs/'+job['series_id']+'/next',json={'confirmed':True})
    assert next_job.status_code == 202
    next_path=root()+'/predictions/'+next_job.json()['forecast_id']
    second=client.get(next_path).json()
    assert second['status']=='locked' and second['input_version']==one
    assert second['snapshot']['prior_forecast']==job['forecast_id']
    assert len(second['snapshot']['sources'])==2
    assert client.post(root()+'/runs/'+job['series_id']+'/next',json={'confirmed':True}).status_code == 409
    assert client.post(next_path+'/evaluations',json={'validation_version':validation_version}).json()['result']['actual']['summary']=='TEST SECOND ROUND ACTUAL'
    assert len(packets)==2
    assert all(row['detail']=='案例盲测' for row in client.get('/api/usage').json())
    with TestClient(app) as reopened:
        assert reopened.get(path).json()==locked
        assert reopened.get(path+'/evaluations').json()[0]['id']==evaluated['id']


def test_validation_requires_lock_before_opening_database(client,monkeypatch):
    version=save(client,'input',MATERIAL)
    cfg={'provider':'openai_compatible','endpoint':'https://model.cases.invalid','model':'TEST_MODEL'}
    _,forecast_id=case_forecast.begin('nvda',cfg,version=version,index=0)
    original=case_audit.connect

    def guarded(name):
        assert name != 'validation', 'TEST leak into pending forecast'
        return original(name)

    monkeypatch.setattr(case_audit,'connect',guarded)
    path=root()+'/predictions/'+forecast_id
    assert client.post(path+'/evaluations',json={}).status_code==409
    assert client.get(path+'/evaluations').json()==[]
    with pytest.raises(case_forecast.CaseConflict):
        case_forecast.begin('nvda',cfg,version=version,index=1)


def test_missing_llm_stores_blocked_without_prediction(client):
    version=save(client,'input',MATERIAL)
    job=client.post(root()+'/runs',json={'input_version':version,'round_index':1,'confirmed':True}).json()
    path=root()+'/predictions/'+job['forecast_id']
    record=client.get(path).json()
    assert record['status']=='blocked' and record['snapshot'] is None
    assert 'API Key' in record['problem']
    assert client.post(path+'/evaluations',json={}).status_code==409
    assert client.get('/api/usage').json()==[]


def test_historical_option_review_and_independent_audit(client,monkeypatch):
    at=timestamp(FIRST)
    archived=quote(at,'call',35,100)
    order={'underlying':'TEST','observed_at':'2025-02-07T15:59:59-05:00','quote_kind':'NBBO',
           'standard_deliverable':True,'snapshot':archived}
    material=MATERIAL.split('@round '+SECOND)[0]+(
        '@evidence TEST_CONFIRM 2025-02-07T15:58:00-05:00 https://independent.invalid/statement\n'
        'TEST_CONFIRM independent observation of catalyst.\n'
        '@quote TEST_NBBO 2025-02-07T16:00:00-05:00 https://fixture.invalid/archive-quote\n'+json.dumps(order))
    version=save(client,'input',material)
    assert client.put('/api/providers/llm',json={'provider':'openai_compatible','endpoint':'https://model.cases.invalid',
        'api_key':'TEST_KEY','model':'TEST_MODEL'}).is_success
    original_review=case_forecast.option_review

    def after_lock(*args):
        with connect('predictions') as db:
            assert db.execute("SELECT independent FROM case_predictions WHERE status='running'").fetchone()[0]
        return original_review(*args)
    monkeypatch.setattr(case_forecast,'option_review',after_lock)
    original=httpx.AsyncClient

    def model(req):
        packet=json.loads(req.content)
        assert 'TEST_NBBO' not in str(packet)
        assert 'strike_price' not in json.loads(packet['messages'][1]['content'])['sources'][0]['text']
        answer=simulated_answer(packet)
        pair=[{'id':'TEST_ONE','quote':'TEST_FIRST_ONLY'}, {'id':'TEST_CONFIRM','quote':'TEST_CONFIRM'}]
        answer['catalyst']={'kind':'fact','refs':pair[:1],'text':'TEST source confirmed catalyst'}
        answer['independent_confirmation']={'kind':'fact','refs':pair,'text':'TEST two separate observations'}
        answer['options_case']={'symbol':'TEST','side':'call','from_date':'2025-03-09','through_date':'2025-03-16',
            'risk_gate':'clear','prices':{'base':{'low':99,'high':101},'up':{'low':120,'high':130},
                                         'down':{'low':70,'high':80}}}
        for scenario in answer['scenarios']:
            scenario['probability']={'low':.25,'high':.45}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(model),**kw))
    response=client.post(root()+'/runs',json={'input_version':version,'round_index':0,'confirmed':True})
    assert response.status_code==202
    path=root()+'/predictions/'+response.json()['forecast_id']
    snapshot=client.get(path).json()['snapshot']
    assert snapshot['simulation']['signal']=='L3'
    assert snapshot['simulation']['execution']=='hypothetical'
    ticker=snapshot['simulation']['selected']['ticker']
    actual={'rounds':[{'cutoff':FIRST,'outcome':{'branch':'up','symbol':'TEST','terminal_price':121.0,
        'terminal_at':'2025-03-14T16:00:00-04:00','price_url':'https://fixture.invalid/close'},
        'exit_quote':{'ticker':ticker,'bid':2.1,'bid_size':10,'observed_at':'2025-03-13T16:00:00-04:00',
                      'url':'https://fixture.invalid/exit'}}]}
    first=save(client,'validation',json.dumps(actual))
    scored=client.post(path+'/evaluations',json={}).json()['result']['scores']
    assert scored['options']['score']==100 and scored['options']['calculation'][0]['ev_low']['match']
    assert scored['market']['matching_bands']==['up']
    assert scored['returns']['quote_based_pnl_usd']==108
    assert scored['returns']['reported_actual_trade_pnl_usd'] is None
    actual['rounds'][0]['actual_trade_pnl_usd']=50
    second=save(client,'validation',json.dumps(actual))
    assert client.post(path+'/evaluations',json={}).is_success
    assert [e['validation_version'] for e in client.get(path+'/evaluations').json()]==[second,first]

