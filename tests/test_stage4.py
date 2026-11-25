import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.options import breakeven_probability, contract_from, probability_ev


def test_payoff_probability_bounds():
    weights = {'up': {'low': .3, 'high': .6}, 'base': {'low': .1, 'high': .4},
               'down': {'low': .2, 'high': .5}}
    values = {'up': 600, 'base': 100, 'down': 0}
    assert probability_ev(weights, values) == pytest.approx(200)
    assert probability_ev(weights, values, minimize=False) == pytest.approx(380)
    assert breakeven_probability(200, 700, 50) == pytest.approx(150/650)
    assert breakeven_probability(200, 0, 0) is None


def seed(side):
    at = datetime.now(timezone.utc)
    start = (at + timedelta(days=30)).date().isoformat()
    through = (at + timedelta(days=49)).date().isoformat()
    case = {'side':side,'symbol':'TEST','from_date':start,'through_date':through,'risk_gate':'clear',
            'prices':{'down':{'low':70,'high':80},'base':{'low':100,'high':102},'up':{'low':120,'high':130}}}
    probabilities = {'base':(.3,.4),'up':(.35,.4),'down':(.25,.3)}
    snapshot = {'options_case':case, 'joint':{'exit':None}, 'scenarios':[
        {'branch': branch,'probability':{'low':lo,'high':hi}} for branch,(lo,hi) in probabilities.items()],
        'risk':{'kind':'inference','refs':[{'id':'TEST_SOURCE','quote':'TEST independent risk'}]}}
    report = {'id':'TEST_REPORT','title':'TEST isolated simulation','subject':'TEST_SUBJECT','symbols':['TEST'],
              'snapshot_id':'TEST_LOCK','level':'L2','horizon_end':start,'omitted_priority':0,'review':{}}
    with connect('research') as db:
        db.execute("INSERT INTO research_runs(id,status) VALUES ('TEST_RUN','completed')")
        db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?,?)',(report['id'],'TEST_RUN',report['subject'],
                   'TEST_SIG','analyzed',at.isoformat(),json.dumps(report)))
        db.execute('INSERT INTO probability_snapshots VALUES (?,?,?,?,?)',('TEST_LOCK','TEST_RUN',report['subject'],
                   at.isoformat(),json.dumps(snapshot)))
    return at, snapshot


def quote(at, side, days, strike, timeframe='REAL-TIME'):
    expiry = (at + timedelta(days=days)).date()
    ticker = f'O:TEST{expiry:%y%m%d}{side[0].upper()}{strike*1000:08d}'
    return {'details':{'ticker':ticker,'strike_price':strike,'expiration_date':expiry.isoformat(),
                       'exercise_style':'american','shares_per_contract':100,'contract_type':side},
            'open_interest':160,'underlying_asset':{'price':101,'ticker':'TEST'},
            'last_quote':{'bid':.9,'ask':1,'bid_size':25,'ask_size':30,'timeframe':timeframe,
                          'last_updated':int((at.timestamp()-1)*1e9)},
            'last_trade':{'price':9.0,'sip_timestamp':int((at.timestamp()-2)*1e9)}}


@pytest.mark.parametrize('side',['call','put'])
def test_quote_decision_audit_and_exit(tmp_path,monkeypatch,side):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app) as client:
        at, locked = seed(side)
        records = [quote(at,side,32,100),quote(at,side,45,100),quote(at,side,32,102)]
        request_paths = []
        not_standard = records[2]['details']['ticker']
        adjustable = {**records[0], 'details':{**records[0]['details'],'additional_underlyings':[{}]}}
        assert contract_from(adjustable,'TEST') is None

        def handler(req):
            request_paths.append(req.url.path)
            with connect('research') as db:
                assert json.loads(db.execute("SELECT payload FROM probability_snapshots WHERE id='TEST_LOCK'").fetchone()[0]) == locked
            if req.url.path == '/v3/snapshot/options/TEST':
                assert req.url.params['contract_type'] == side
                return httpx.Response(200,json={'request_id':'TEST_CHAIN','results':records,'next_url':'https://TEST.invalid'})
            ticker = req.url.path.split('/')[-1]
            details = next(c['details'] for c in records if c['details']['ticker']==ticker)
            ref = {**details,'underlying_ticker':'TEST'}
            if ticker == not_standard:
                ref['additional_underlyings'] = [{'type':'currency'}]
            return httpx.Response(200,json={'results':ref})

        original = httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
        path = '/api/reports/TEST_REPORT/options'
        missing = client.post(path,json={}).json()
        assert missing['signal'] == 'L2' and missing['quotes'] == [] and missing['gaps']
        assert request_paths == []
        assert client.put('/api/providers/options',json={'provider':'massive','endpoint':'https://TEST.invalid',
                                                        'api_key':'TEST_KEY'}).is_success

        for r in records:
            r['last_quote']['timeframe'] = 'DELAYED'
        delayed = client.post(path,json={}).json()
        assert delayed['signal'] == 'L2.5' and not delayed['selected']
        assert not delayed['quotes'][0]['checks']['近期实时双边报价']
        for r in records:
            r['last_quote']['timeframe'] = 'REAL-TIME'

        decision = client.post(path,json={}).json()
        assert decision['signal'] == 'L3' and decision['partial_chain']
        selected = decision['selected']
        assert selected['contract_verified'] and selected['ticker'] != not_standard
        assert selected['total_cost'] == 102 and selected['max_loss'] == 102
        assert selected['last_trade']['price'] == 9 and selected['quote']['ask'] == 1
        assert selected['breakeven'] == pytest.approx(selected['strike'] + (1.02 if side=='call' else -1.02))
        assert selected['ev_low'] > 0 and selected['probability_hurdle'] < selected['locked_success_lower']
        assert selected['exit_probability'] is None
        assert len({row['expiry'] for row in decision['quotes']}) == 2
        assert len({row['strike'] for row in decision['quotes']}) == 2

        for r in records:
            r['last_quote']['timeframe'] = 'DELAYED'
        assert client.post(path,json={}).json()['signal'] == 'R1'
        assert client.post(path,json={'review':'realized'}).status_code == 422
        assert client.post(path,json={'review':'realized','note':'TEST catalyst complete'}).json()['signal'] == 'R2'
        assert client.post(path,json={'review':'invalidated','note':'TEST disproof'}).json()['signal'] == 'R3'
        for r in records:
            r['last_quote']['timeframe'] = 'REAL-TIME'
        assert client.post(path,json={}).json()['signal'] == 'R3'
        result = client.get('/api/reports/TEST_REPORT').json()
        assert result['locked_snapshot'] == locked
        assert [d['signal'] for d in result['decisions']] == ['R3','R3','R2','R1','L3','L2.5','L2']
        assert 'TEST_KEY' not in json.dumps(result)

