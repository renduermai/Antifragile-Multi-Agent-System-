import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect, now
from app.main import app
from app.research import store_evidence
from app.relative_value import features


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))


def seed(number=1):
    stamp = now()
    return store_evidence([{'id':number, 'headline':f'TEST regulatory approval proposal {number}',
        'content':f'TEST issuer confirms regulatory review {number}.', 'symbols':[f'POOL{number}'],
        'url':f'https://news.fixture.invalid/{number}', 'created_at':stamp, 'updated_at':stamp,
        'source':'TEST publisher'}],'news')


def simulated_answer(message):
    packet = json.loads(message['messages'][1]['content'])
    s=packet['sources'][0]
    finding={'kind':'inference','text':'TEST conditional research',
             'refs':[{'id':s['id'],'quote':s['text'][:12]}]}
    answer={k:finding for k in ['catalyst','independent_confirmation','attractor','claims_and_conflicts','network','power','feedback','risk','next_check']}
    answer['action_tree']=[finding,finding]
    answer['scenarios']=[dict(branch=k,condition='TEST conditional', occurrence_window='TEST window',
                              public_window='unknown',price_response='unknown',probability=None,basis=finding,
                              change_if='TEST new public notice') for k in ('base','up','down')]
    answer['joint']={'catalyst':'TEST notice','sufficient_move':'unknown','event':None,'timely':None,
                     'move':None,'exit':None,'basis':finding}
    return answer


def model_fixture(client,monkeypatch,corrupt=False):
    client.put('/api/providers/llm',json={'provider':'openai_compatible','endpoint':'https://model.fixture.invalid',
        'model':'TEST_MODEL','api_key':'TEST_KEY'})
    calls=[]
    original=httpx.AsyncClient
    def handler(request):
        assert request.url.path=='/chat/completions'
        assert request.headers['authorization']=='Bearer TEST_KEY'
        message=json.loads(request.content)
        calls.append(message)
        answer=simulated_answer(message)
        if corrupt:
            answer['risk']={**answer['risk'],'refs':[{'id':'UNSEEN','quote':'made up quote'}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}],
                                       'usage':{'prompt_tokens':911,'completion_tokens':421}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    return calls


def scan(client,mode='all',query=''):
    response=client.post('/api/scans',json={'mode':mode,'query':query})
    assert response.status_code==202,response.text
    record=client.get('/api/runs/'+response.json()['run_id']).json()
    assert record['status'] not in ('failed','running'),record
    return record['result']


def test_graph_cap_cache_snapshot_and_recovery(workspace,monkeypatch):
    with TestClient(app) as client:
        calls=model_fixture(client,monkeypatch)
        for i in range(1,5):
            seed(i)
        first=scan(client)
        assert len(calls)==3 and first['deferred']==1
        report=client.get('/api/reports/'+first['reports'][0]).json()
        assert report['level']=='L1' and report['status']=='analyzed'
        assert report['locked_snapshot']['scenarios']==report['analysis']['scenarios']
        assert report['locked_at'] <= report['market']['assessed_at']
        assert len(client.get('/api/opportunities').json()['items'])==3
        assert 'TEST_KEY' not in json.dumps(report)
        assert client.get('/api/usage').json()[0]['input_tokens']==911
        with connect('research_checkpoints') as db:
            assert db.execute('SELECT count(*) FROM checkpoints WHERE thread_id=?',(report['thread'],)).fetchone()[0]>1
    with TestClient(app) as client:
        second=scan(client)
        assert second['reused']==3 and second['deferred']==0 and len(calls)==4
        only=scan(client,'asset','pool1')
        assert only['reused']==1 and len(only['reports'])==1
        word=scan(client,'keyword','proposal 2')
        assert word['reused']==1 and len(calls)==4
        prior=client.get('/api/reports/'+only['reports'][0]).json()
        client.post('/api/events/'+prior['subject']+'/reviews',json={'state':'active',
            'judgment':'TEST changed','unresolved_conflicts':['TEST unresolved denial']})
        changed=scan(client,'asset','POOL1')
        assert changed['reused']==0 and len(calls)==5
        updated=client.get('/api/reports/'+changed['reports'][0]).json()
        assert len(updated['history'])==2 and updated['snapshot_id']!=prior['snapshot_id']
        assert client.get('/api/reports/'+prior['id']).json()['locked_snapshot']==prior['locked_snapshot']
        assert 'TEST unresolved denial' in calls[-1]['messages'][1]['content']


def test_rejected_citations_and_exhausted_budget(workspace,monkeypatch):
    with TestClient(app) as client:
        seed()
        calls=model_fixture(client,monkeypatch,True)
        client.put('/api/limits',json={'daily_llm_calls':1,'deep_candidates':3})
        first=scan(client)
        assert first['blocked']==1
        report=client.get('/api/reports/'+first['reports'][0]).json()
        assert report['analysis'] is None and report['snapshot_id'] is None
        second=scan(client)
        assert '每日' in client.get('/api/reports/'+second['reports'][0]).json()['problem']
        assert len(calls)==1


def test_price_only_anomaly_and_observation_route(workspace,monkeypatch):
    with TestClient(app) as client:
        calls=model_fixture(client,monkeypatch)
        day=datetime.now(timezone.utc).date()-timedelta(days=72)
        rows=[]
        a,b=100,110
        for i in range(72):
            day+=timedelta(days=1)
            r=0.003*math.sin(i*0.31)
            b*=1+r
            a*=1+r+0.0008*math.cos(i*0.9)+(0.05 if i==71 else 0)
            rows.extend([('iex','NVDA',day.isoformat(),a,400 if i==71 else 100),
                         ('iex','QQQ',day.isoformat(),b,100)])
        with connect('research') as db:
            db.executemany('INSERT INTO daily_bars VALUES (?,?,?,?,?)',rows)
        relative=features('NVDA','iex')
        assert relative['relative']['sessions']==72 and relative['relative']['percentile']==1
        assert relative['volume_ratio']==4 and relative['triggers']
        first=scan(client,'asset','NVDA')
        report=client.get('/api/reports/'+first['reports'][0]).json()
        assert report['path']=='A' and report['level']=='L1' and len(calls)==1
        light=scan(client,'asset','QQQ')
        report=client.get('/api/reports/'+light['reports'][0]).json()
        assert report['level']=='L0' and report['snapshot_id'] is None and len(calls)==1


def test_empty_and_invalid_research_requests(workspace):
    with TestClient(app) as client:
        assert client.post('/api/scans',json={'mode':'asset','query':'bad code!'}).status_code==422
        assert scan(client)['reports']==[]
        assert client.get('/api/scans/latest').json()['status']=='insufficient_data'
        assert client.get('/api/opportunities').json()['items']==[]
        assert client.get('/api/usage').json()==[]


def test_resume_from_graph_checkpoint(workspace, monkeypatch):
    with TestClient(app) as client:
        calls=model_fixture(client,monkeypatch)
        seed()
        first=scan(client)
        report=client.get('/api/reports/'+first['reports'][0]).json()
        with connect('research') as db:
            # Simulate process loss between the final graph checkpoint and report commit.
            db.execute('DELETE FROM reports WHERE id=?',(report['id'],))
            db.execute("UPDATE research_runs SET status='running' WHERE id=?",(report['run_id'],))
    with TestClient(app) as client:
        assert client.get('/api/scans/latest').json()['status']=='interrupted'
        assert client.post('/api/runs/'+report['run_id']+'/resume').status_code==202
        restored=client.get('/api/reports/'+report['id']).json()
        assert len(calls)==1
        assert restored['snapshot_id']==report['snapshot_id']
        assert restored['analysis']==report['analysis']


def test_full_provider_scan_locks_probability_before_iv(workspace, monkeypatch):
    stamp=datetime.now(timezone.utc)
    day=(stamp-timedelta(days=1)).date().isoformat()
    expiry=(stamp+timedelta(days=35)).date().isoformat()
    marks=[]
    interval={'low':0.2,'high':0.5}
    def handler(request):
        path=request.url.path
        if path=='/v1beta1/news':
            assert 'symbols' not in request.url.params
            body={'news':[{'id':84,'headline':'TEST regulator approves merger for new issuer',
                           'content':'TEST signed statement; independent estimates remain uncertain.',
                           'created_at':(stamp-timedelta(hours=1)).isoformat(),
                           'source':'TEST paper','url':'https://test.fixture.invalid/84','symbols':['OUTX']}]}
        elif path=='/res/v1/web/search':
            body={'type':'search','web':{'results':[]}}
        elif path=='/v2/stocks/snapshots':
            body={s:{'latestTrade':{'t':stamp.isoformat(),'p':12.34}} for s in request.url.params['symbols'].split(',')}
        elif path=='/v2/stocks/bars':
            assert request.url.params['adjustment']=='all'
            body={'bars':{'SPY':[{'t':day+'T05:00:00Z','c':129,'v':543}]}}
        elif path=='/chat/completions':
            packet=json.loads(request.content)
            assert 'SENTINEL_OPTION_PRICE' not in str(packet)
            marks.append('forecast')
            answer=simulated_answer(packet)
            for scenario in answer['scenarios']:
                scenario['probability']=interval
            body={'choices':[{'message':{'content':json.dumps(answer)}}]}
        elif path=='/v3/snapshot/options/OUTX':
            with connect('research') as db:
                assert db.execute('SELECT count(*) FROM probability_snapshots').fetchone()[0]==1
            marks.append('market')
            body={'results':[
                {'implied_volatility':iv,'greeks':{'delta':delta},'underlying_asset':{'price':100},
                 'break_even_price':'SENTINEL_OPTION_PRICE','details':{'expiration_date':expiry,'strike_price':strike,
                 'shares_per_contract':100,'ticker':'TEST_'+side,'contract_type':side}}
                for side,delta,iv,strike in [('put',-0.25,0.4,95),('call',0.25,0.35,105),('call',0.5,0.38,100)]]}
        else:
            raise AssertionError(path)
        return httpx.Response(200,json=body)
    with TestClient(app) as client:
        original=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
        for kind,provider in [('news','alpaca'),('market','alpaca'),('search','brave'),
                              ('llm','openai_compatible'),('options','massive')]:
            assert client.put('/api/providers/'+kind,json={'provider':provider,'model':'TEST_MODEL',
                'endpoint':'https://service.fixture.invalid','api_key':'TEST_KEY','secret_key':'TEST_SECRET'}).is_success
        result=scan(client)
        assert marks==['forecast','market']
        data=client.get('/api/reports/'+result['reports'][0]).json()
        assert data['symbols']==['OUTX']
        assert data['market']['terms'][0]['put_minus_call_iv']==pytest.approx(.05)
        assert data['market']['terms'][0]['near_atm_iv']==.38
        assert data['locked_snapshot']['scenarios'][0]['probability']==interval

