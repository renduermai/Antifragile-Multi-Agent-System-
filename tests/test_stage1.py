import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR', str(tmp_path))
    with TestClient(app) as client:
        yield client


def test_startup_and_restart(client):
    assert client.get('/').status_code == 200
    assert len(client.get('/api/cases').json()) == 3
    assert client.get('/api/opportunities').json()['items'] == []
    assert client.get('/api/status').json()['limits']['daily_llm_calls'] == 20
    for _ in range(2):
        with TestClient(app) as second:
            assert len(second.get('/api/cases').json()) == 3
    for database in ['runtime', 'research', 'cases', 'predictions', 'validation']:
        with connect(database) as db:
            versions = [row['version'] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
            assert versions == ([1, 2, 3, 4] if database == 'research' else [1, 2] if database in ('runtime','validation') else [1])


def test_secrets_and_limits(client):
    payload = {'provider': 'openai_compatible', 'endpoint': 'https://example.com/v1', 'api_key': 'TEST_SECRET_SENTINEL', 'model': 'test'}
    result = client.put('/api/providers/llm', json=payload)
    assert result.status_code == 200
    assert 'TEST_SECRET_SENTINEL' not in result.text
    assert 'TEST_SECRET_SENTINEL' not in client.get('/api/providers').text
    payload.pop('api_key')
    client.put('/api/providers/llm', json=payload)
    assert client.get('/api/providers').json()[0]['key_mask']
    payload['clear_key'] = True
    client.put('/api/providers/llm', json=payload)
    assert not client.get('/api/providers').json()[0]['key_mask']
    assert client.put('/api/limits', json={'daily_llm_calls': 0}).status_code == 422
    assert client.put('/api/limits', json={'daily_llm_calls': 7, 'deep_candidates': 2}).status_code == 200
    with TestClient(app) as second:
        assert second.get('/api/status').json()['limits']['deep_candidates'] == 2


def test_content_isolation_and_versions(client):
    client.put('/api/cases/gold/input', json={'text': 'TEST INPUT ONLY'})
    client.put('/api/cases/gold/validation', json={'text': 'TEST FUTURE SECRET'})
    client.put('/api/cases/gold/input', json={'text': 'TEST INPUT V2'})
    assert 'FUTURE' not in client.get('/api/cases/gold/input').text
    with connect('cases') as db:
        assert db.execute('SELECT count(*) FROM input_versions').fetchone()[0] == 2
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='validation_versions'").fetchone()
    assert client.get('/api/cases/bad/input').status_code == 404
    assert client.get('/api/cases/gold/bad').status_code == 404


def test_failed_probe_and_http_redaction(client, monkeypatch):
    assert client.post('/api/providers/llm/test').json()['ok'] is False
    async def denied(self, cfg, secret):
        response = httpx.Response(403, request=httpx.Request('GET', 'https://example.com'), text='TEST_SECRET_IN_ERROR')
        response.raise_for_status()
    monkeypatch.setattr('app.main.HttpProvider.test', denied)
    result = client.post('/api/providers/options/test').json()
    assert result['http_status'] == 403
    assert 'TEST_SECRET_IN_ERROR' not in client.get('/api/usage').text


def test_budget_and_origin(client):
    client.put('/api/limits', json={'daily_llm_calls': 1, 'deep_candidates': 3})
    client.post('/api/providers/llm/test')
    assert client.post('/api/providers/llm/test').status_code == 429
    assert client.put('/api/limits', headers={'origin': 'https://other.example'}, json={}).status_code == 403


def test_real_adapter_contract_with_marked_fixture(client, monkeypatch):
    from app.providers import HttpProvider
    original = httpx.AsyncClient
    def handler(request):
        assert request.headers['authorization'] == 'Bearer TEST_KEY'
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}],
                                        'usage': {'prompt_tokens': 12, 'completion_tokens': 5}})
    monkeypatch.setattr('app.providers.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    client.put('/api/providers/llm', json={'provider': 'openai_compatible', 'endpoint': 'https://fixture.invalid/v1',
                                         'model': 'TEST_FIXTURE', 'api_key': 'TEST_KEY', 'input_price': 1, 'output_price': 2})
    assert client.post('/api/providers/llm/test').json()['ok'] is True
    row = client.get('/api/usage').json()[0]
    assert row['input_tokens'] == 12
    assert row['estimated_cost_usd'] == pytest.approx(.000022)
