import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.db import connect


def test_credit_reservation_cache_time_and_errors(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    requests=[]
    def handler(request):
        requests.append(request)
        assert '/v1/v1/' not in str(request.url)
        assert request.headers['authorization']=='Bearer TEST_TOKEN'
        return httpx.Response(203,json={'s':'ok','optionSymbol':['NVDA261225C00150000'],
            'bid':[1.0],'ask':[1.2],'updated':[1795442400],'iv':[None]},
            headers={'X-Api-Ratelimit-Consumed':'1','X-Api-Ratelimit-Remaining':'99'})
    with TestClient(app) as c:
        c.put('/api/providers/marketdata',json={'provider':'marketdata','endpoint':'https://fixture.invalid/v1',
             'api_key':'TEST_TOKEN','credit_limit':2})
        original=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
        q={'operation':'quotes','symbol':'NVDA','contract':'NVDA261225C00150000','date':'2026-11-20'}
        first=c.post('/api/marketdata/query',json=q)
        assert first.status_code==200,first.text
        row=first.json()['rows'][0]
        assert row['data_type']=='historical_eod' and not row['execution_eligible']
        assert row['multiplier'] is None and row['iv'] is None and row['raw']['ask']==1.2
        assert c.post('/api/marketdata/query',json=q).json()['cached'] and len(requests)==1
        q['date']='2026-11-19'
        assert c.post('/api/marketdata/query',json=q).status_code==200
        q['date']='2026-11-18'
        assert c.post('/api/marketdata/query',json=q).status_code==400
        assert len(requests)==2
        assert c.get('/api/marketdata/budget').json()['used_or_reserved']==2
        assert 'TEST_TOKEN' not in c.get('/api/providers').text
        assert c.post('/api/marketdata/query',json={'operation':'chain','symbol':'NVDA'}).status_code==422


def test_denial_and_disabled(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app) as c:
        cfg={'provider':'marketdata','endpoint':'https://fixture.invalid/v1','api_key':'TEST_TOKEN'}
        c.put('/api/providers/marketdata',json=cfg)
        original=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(403)),**kw))
        assert c.post('/api/providers/marketdata/test').json()['ok'] is False
        assert c.get('/api/marketdata/budget').json()['used_or_reserved']==0
        c.put('/api/providers/marketdata',json={**cfg,'enabled':False})
        assert c.post('/api/marketdata/query',json={'operation':'expirations','symbol':'SPY'}).status_code==400

