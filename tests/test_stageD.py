import json
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app import scanning
from tests.test_stage4 import seed
from app.db import connect
from app import presentation


def test_refresh_scopes_existing_event_and_preserves_snapshots(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app) as c:
        seed('call')
        calls=[]
        async def execute(*args,**kw):
            calls.append(kw)
            with connect('research') as db:
                db.execute("UPDATE research_runs SET status='completed' WHERE id=?",(args[0],))
        monkeypatch.setattr(scanning,'execute',execute)
        r=c.post('/api/reports/TEST_REPORT/refresh?cost=true')
        assert r.status_code==202 and calls[0]['target']=='TEST_SUBJECT' and calls[0]['cost']
        report=c.get('/api/reports/TEST_REPORT').json()
        assert report['locked_snapshot'] and report['timeline'][0]['id']=='TEST_REPORT'
        assert c.post('/api/reports/missing/refresh').status_code==404


def test_translation_is_cached_and_original_evidence_is_immutable(tmp_path,monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    with TestClient(app) as c:
        seed('call')
        with connect('research') as db:
            r=json.loads(db.execute("SELECT payload FROM reports WHERE id='TEST_REPORT'").fetchone()[0])
            r['sources']=[{'id':'TEST_SOURCE','source_type':'news_article','text':'TEST original public evidence'}]
            db.execute("UPDATE reports SET payload=? WHERE id='TEST_REPORT'",(json.dumps(r),))
        c.put('/api/providers/llm',json={'provider':'openai_compatible','endpoint':'https://fixture.invalid','model':'TEST','api_key':'TEST'})
        calls=[]
        async def chat(base,headers,body,timeout):
            calls.append(body)
            return httpx.Response(200,request=httpx.Request('POST',base),json={'choices':[{'message':{'content':json.dumps({'items':[{'id':'TEST_SOURCE','title':'测试原始证据','summary':'测试材料的中文摘要'}]})}}]})
        monkeypatch.setattr(presentation,'post_chat',chat)
        first=c.post('/api/reports/TEST_REPORT/presentation')
        assert first.status_code==200,first.text
        assert c.post('/api/reports/TEST_REPORT/presentation').json()==first.json() and len(calls)==1
        assert scanning.report_by_id('TEST_REPORT')['sources'][0]['text']=='TEST original public evidence'

