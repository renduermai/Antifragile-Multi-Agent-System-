import asyncio
from datetime import datetime,timedelta,timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import option_sources
from app.options import evaluate
from tests.test_stage4 import seed
from app.scanning import report_by_id


def reference(at,side='call'):
    expiry=(at+timedelta(days=32)).strftime('%y%m%d')
    return option_sources.normalize({'contract':f'TEST{expiry}{side[0].upper()}00100000',
        'bid':.9,'ask':1,'openInterest':200,'observed_at':(at-timedelta(days=2)).isoformat(),
        'source':'TEST_MARKETDATA','bidSize':10,'askSize':10,'data_type':'delayed_or_unverified'},'TEST')


@pytest.mark.parametrize('side',['call','put'])
def test_delayed_math_uses_same_payoffs_without_execution(tmp_path,monkeypatch,side):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app):
        at,snapshot=seed(side)
        c=reference(at,side)
        result=evaluate(c,snapshot['options_case'],snapshot,at)
        assert result['total_cost']==102 and result['max_loss']==102
        assert result['max_acceptable_ask']>0
        assert result['reference_only'] and not result['checks']['近期实时双边报价']
        assert result['sensitivity']['event_misses_expiry_net']==-102
        assert len(result['payoffs'])==3
        assert result['max_acceptable_ask']*125-22.5+2<=500.00001
        future={**c,'available_at':(at+timedelta(seconds=1)).isoformat()}
        assert 'total_cost' not in evaluate(future,snapshot['options_case'],snapshot,at)


def test_real_entrypoint_locks_probability_and_bounded_sources(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app) as client:
        at,snapshot=seed('call')
        client.put('/api/providers/marketdata',json={'provider':'marketdata','endpoint':'https://fixture.invalid/v1','api_key':'TEST'})
        client.put('/api/providers/options',json={'provider':'massive','endpoint':'https://fixture.invalid','enabled':False})
        calls=[]
        async def mock_query(value,cfg,secret):
            calls.append(value)
            from app.options import snapshot_for
            assert snapshot_for(report_by_id('TEST_REPORT'))==snapshot
            if value.operation=='expirations':
                return {'expirations':[(at+timedelta(days=n)).date().isoformat() for n in (31,32,33,34,35)]}
            c=reference(at)
            return {'rows':[{'contract':c['ticker'][2:],'observed_at':c['observed_at'],'bid':.9,'ask':1,'source':'TEST'}]}
        monkeypatch.setattr(option_sources,'query',mock_query)
        result=client.post('/api/reports/TEST_REPORT/options',json={})
        assert result.is_success,result.text
        body=result.json()
        assert body['quotes'] and body['selected'] is None and body['signal']!='L3'
        assert len(calls)==4 and all(c.strike_limit==3 for c in calls[1:])
        assert body['data_provider'].startswith('MarketData')
        assert body['quotes'][0]['multiplier_assumed']


def test_eod_future_and_search_snippets_do_not_become_quotes(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    at=datetime(2026,11,20,20,55,tzinfo=timezone.utc)
    c=reference(at)
    eod=option_sources.normalize({'contract':c['ticker'][2:],'observed_at':at.replace(hour=0,minute=0).isoformat(),
        'data_type':'historical_eod','source':'TEST','ask':1,'bid':.9},'TEST')
    assert datetime.fromisoformat(eod['available_at'])>at
    with TestClient(app):
        cfg={'provider':'tavily','endpoint':'https://test.invalid','per_run_limit':3,'daily_limit':10}
        def handler(request):
            return httpx.Response(200,json={'results':[{'url':'https://example.invalid/options','title':'TEST','content':'TEST bid 0.5 ask 1 price'}]})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                source=option_sources.MarketOptions(client,{}, {}, cfg,{'api_key':'TEST'})
                rows,gaps=await source.fallback('TEST',None,datetime.now(timezone.utc))
                assert rows==[] and len(source.leads)==1 and gaps
                rows,gaps=await source.fallback('TEST',None,at)
                assert rows==[] and '历史' in gaps[0]
        asyncio.run(run())

