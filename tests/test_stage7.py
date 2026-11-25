import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import backup
from app.db import connect, now
from app.main import app


def test_tavily_without_alpaca_news_or_options(tmp_path, monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR', str(tmp_path))
    record={'title':'TEST supply disruption at NASDAQ:OUTX','url':'https://example.invalid/notice',
            'published_date':'Mon, 23 Nov 2026 03:00:00 GMT','content':'TEST primary source excerpt'}
    def handler(request):
        assert request.url.path=='/search' and request.method=='POST'
        assert request.headers['Authorization']=='Bearer TEST_TAVILY'
        assert json.loads(request.content)['topic']=='news'
        return httpx.Response(200,json={'results':[record]})
    with TestClient(app) as client:
        original=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
        assert client.put('/api/providers/search',json={'provider':'tavily','endpoint':'https://tavily.fixture.invalid',
                                                        'api_key':'TEST_TAVILY'}).is_success
        assert client.post('/api/providers/search/test').json()['ok']
        result=client.post('/api/collections',json={}).json()
        assert result['result']['market']['status']=='unconfigured'
        assert result['result']['search']['added']==1
        event=client.get('/api/events').json()[0]
        evidence=client.get('/api/events/'+event['event_id']).json()['evidence'][0]
        assert evidence['provider']=='tavily_search' and evidence['raw']==record
        assert evidence['body_kind']=='search_snippet' and evidence['first_public_at'].startswith('2026-11-23T03')
        assert event['assets'][0]['symbol']=='OUTX'
        assert client.post('/api/collections',json={}).json()['result']['search']['unchanged']==1
        client.put('/api/providers/search',json={'provider':'brave','endpoint':'https://brave.fixture.invalid'})
        assert not next(p for p in client.get('/api/providers').json() if p['kind']=='search')['key_mask']


def test_backup_restore_provenance_and_corruption(tmp_path, monkeypatch):
    source=tmp_path/'source'
    monkeypatch.setenv('AF_DATA_DIR',str(source))
    with TestClient(app) as client:
        client.put('/api/cases/gold/input',json={'text':'TEST archived case'})
        client.put('/api/cases/gold/validation',json={'text':'TEST separate outcome'})
        client.put('/api/providers/llm',json={'provider':'openai_compatible','endpoint':'https://test.invalid',
                                              'api_key':'TEST_PRIVATE'})
        archive=tmp_path/'backup'
        backup.run(backup.create, source, archive)
        assert not (archive/'secrets.json').exists()
    dest=tmp_path/'restored'
    backup.run(backup.restore, archive, dest)
    monkeypatch.setenv('AF_DATA_DIR',str(dest))
    with TestClient(app) as restored:
        assert restored.get('/api/cases/gold/input').json()['text']=='TEST archived case'
        assert restored.get('/api/cases/gold/validation').json()['text']=='TEST separate outcome'
        assert not restored.get('/api/providers').json()[0]['key_mask']
    with pytest.raises(ValueError):
        backup.run(backup.restore, archive, dest)
    with (archive/'runtime.sqlite3').open('ab') as f:
        f.write(b'TEST change')
    with pytest.raises(ValueError,match='校验值'):
        backup.run(backup.restore, archive,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()



def test_model_citation_choices_match_only_visible_evidence():
    from app.hypotheses import citation_choices, validate_answer
    from tests.test_stage3 import simulated_answer

    original = [{'id':'@src1','text':'TEST original source. Exact archived figures.'}]
    packet, choices = citation_choices(original)
    source = packet[0]
    answer = simulated_answer({'messages':[{}, {'content':json.dumps({'sources':packet})}]})
    answer['risk'] = {'kind':'inference','text':'TEST assessment',
                      'evidence_ids':['E1-1']}
    sources = {s['id']:s['text'] for s in original}
    checked = validate_answer(json.dumps(answer),sources,choices)
    assert checked['risk']['refs'] == [{'id':source['id'],'quote':source['excerpts']['E1-1']}]
    answer['risk']['evidence_ids'] = ['E1-99']
    with pytest.raises(ValueError):
        validate_answer(json.dumps(answer),sources,choices)


def test_archive_search_tickers_and_exclude_error_pages(tmp_path,monkeypatch):
    from app.research import store_evidence
    from app.scanning import candidates

    monkeypatch.setenv('AF_DATA_DIR',str(tmp_path))
    records=[{'title':'TEST NVIDIA (NVDA.O) supply disruption at NASDAQ:OUTX',
              'url':'https://example.invalid/test1','content':'TEST dated report','published_date':now()},
             {'title':'TEST Federal Reserve Board - Page not Found',
              'url':'https://example.invalid/test2','content':'TEST central bank page missing','published_date':now()}]
    with TestClient(app):
        assert store_evidence(records,'tavily')['added']==2
        with connect('research') as db:
            db.execute('DELETE FROM event_assets WHERE symbol=?',('NVDA',))
        assert store_evidence(records,'tavily')['unchanged']==2
        assert set(candidates('asset','NVDA','iex')[0]['symbols'])=={'NVDA','OUTX'}
        assert [c['title'] for c in candidates('all','','iex')]==[records[0]['title']]
