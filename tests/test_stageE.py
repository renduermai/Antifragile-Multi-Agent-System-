import json
from fastapi.testclient import TestClient
from app.main import app
from app import case_forecast,case_audit
from tests.test_stage5 import MATERIAL,truth
from tests.test_stage3 import simulated_answer

def test_auto_replay_nodes_future_isolation_and_audit_after_lock(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    seen=[]
    async def infer(candidate,cfg,secret,cap,prior,historical=False):
        seen.append(candidate)
        if len(seen)==1:assert 'TEST_SECOND_ONLY' not in str(candidate)
        assert 'TEST_VALIDATION_ONLY' not in str(candidate)
        answer=simulated_answer({'messages':[{}, {'content':json.dumps({'sources':candidate['sources']})}]})
        return answer,None
    monkeypatch.setattr(case_forecast,'infer',infer)
    original=case_audit.evaluate
    def audit(*args,**kw):
        assert len(seen)==2
        return original(*args,**kw)
    monkeypatch.setattr(case_audit,'evaluate',audit)
    with TestClient(app) as c:
        c.put('/api/cases/nvda/input',json={'text':MATERIAL})
        c.put('/api/cases/nvda/validation',json={'text':truth()})
        response=c.post('/api/cases/nvda/replay')
        assert response.status_code==202,response.text
        job=c.get('/api/cases/nvda/replay').json()['job']
        assert job['status']=='completed',job
        assert len(seen)==2 and all(s['status']=='done' for s in job['steps'])
        assert {'screen','research','lock','pricing','audit'}<=set(s['node'] for s in job['steps'])

