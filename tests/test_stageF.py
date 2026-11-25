import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import case_forecast,case_audit
from app.db import connect
from tests.test_stage3 import simulated_answer

@pytest.mark.parametrize('case,rounds',[('gold',5),('oil',5),('nvda',4)])
def test_each_shipped_case_full_replay_with_evidence_boundary(tmp_path,monkeypatch,case,rounds):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    seen=[]
    async def infer(candidate,cfg,secret,cap,prior,historical=False):
        assert historical
        assert all(source['published']<=candidate['as_of'] for source in candidate['sources'])
        assert 'rounds' not in prior
        seen.append(candidate['as_of'])
        return simulated_answer({'messages':[{}, {'content':json.dumps({'sources':candidate['sources']})}]}),None
    monkeypatch.setattr(case_forecast,'infer',infer)
    original=case_audit.evaluate
    def audit(*args,**kwargs):
        assert len(seen)==rounds
        return original(*args,**kwargs)
    monkeypatch.setattr(case_audit,'evaluate',audit)
    with TestClient(app) as client:
        response=client.post('/api/cases/'+case+'/replay')
        assert response.status_code==202,response.text
        job=client.get('/api/cases/'+case+'/replay').json()['job']
        assert job['status']=='completed',job
        assert len(seen)==rounds and seen==sorted(set(seen))
        with connect('validation') as db:
            assert db.execute('select count(*) from case_evaluations where case_id=?',(case,)).fetchone()[0]==rounds
            for row in db.execute('select result from case_evaluations where case_id=?',(case,)):
                result=json.loads(row[0])
                assert result['scores']['temporal']['score']==100
                assert result['scores']['decision']['score']==100


def test_resume_keeps_locked_round_and_requires_fresh_failed_forecast(tmp_path,monkeypatch):
    from tests.test_stage5 import MATERIAL,truth
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    calls=[]
    async def infer(candidate,cfg,secret,cap,prior,historical=False):
        calls.append(candidate['as_of'])
        if len(calls)==2:return None,'TEST recoverable provider failure'
        return simulated_answer({'messages':[{}, {'content':json.dumps({'sources':candidate['sources']})}]}),None
    monkeypatch.setattr(case_forecast,'infer',infer)
    with TestClient(app) as c:
        c.put('/api/cases/nvda/input',json={'text':MATERIAL})
        c.put('/api/cases/nvda/validation',json={'text':truth()})
        c.post('/api/cases/nvda/replay')
        first=c.get('/api/cases/nvda/replay').json()['job']
        assert first['status']=='blocked' and first['round_index']==1
        c.post('/api/cases/nvda/replay?resume=true')
        second=c.get('/api/cases/nvda/replay').json()['job']
        assert second['id']==first['id'] and second['status']=='completed'
        assert len(calls)==3 and calls[0]!=calls[1] and calls[1]==calls[2]

