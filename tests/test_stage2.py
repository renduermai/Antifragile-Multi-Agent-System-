from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv('AF_DATA_DIR', str(tmp_path))


def transport(monkeypatch, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setattr('app.collection.httpx.AsyncClient', lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))


def configure(client, kind, limit=6):
    provider = 'brave' if kind == 'search' else 'alpaca'
    create = client.put(f'/api/providers/{kind}', json={
        'provider': provider, 'endpoint': f'https://{kind}.fixture.invalid',
        'api_key': 'TEST_KEY', 'secret_key': 'TEST_SECRET', 'per_run_limit': limit,
    })
    assert create.status_code == 200


def test_missing_keys_produces_no_market_or_evidence(isolated_storage):
    with TestClient(app) as client:
        run = client.post('/api/collections', json={}).json()
        assert run['state'] == 'unconfigured'
        assert run['result']['news']['message'] == 'API Key 未配置'
        for endpoint in ('events', 'market/observations/latest', 'usage'):
            assert client.get(f'/api/{endpoint}').json() == []
        assert client.get('/api/status').json()['last_successful_scan'] is None


def test_cross_day_news_continuation_and_immutable_memory(isolated_storage, monkeypatch):
    from app import collection

    class Clock(datetime):
        current = datetime.now(timezone.utc) - timedelta(days=2)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(collection, 'datetime', Clock)
    base = Clock.current - timedelta(hours=2)
    original = {
        'id': 11, 'headline': 'TEST Merger approval under review by national regulator',
        'content': '<p>TEST Original article body with enough text to identify a syndicated copy.</p>',
        'url': 'https://primary.fixture.invalid/story?utm_source=feed', 'source': 'TEST_PRIMARY',
        'created_at': base.isoformat(), 'updated_at': base.isoformat(), 'symbols': ['TESTX'],
    }
    copy = {**original, 'id': 12, 'url': 'https://copy.fixture.invalid/story', 'source': 'TEST_COPY'}
    revised = {**original, 'content': '<p>TEST Revised text adds a new regulatory response.</p>',
               'updated_at': (base + timedelta(days=1)).isoformat()}
    follow_up = {**original, 'id': 13, 'headline': 'TEST Merger approval under review by national regulator again',
                 'url': 'https://other.fixture.invalid/followup', 'content': '<p>TEST New reporting with a different claim.</p>',
                 'created_at': (base + timedelta(days=1)).isoformat(), 'updated_at': (base + timedelta(days=1)).isoformat()}
    seen = []

    def handler(request):
        assert request.url.path == '/v1beta1/news'
        assert 'symbols' not in request.url.params
        assert request.headers['APCA-API-KEY-ID'] == 'TEST_KEY'
        seen.append(dict(request.url.params))
        pages = [
            {'news': [original], 'next_page_token': 'TEST_PAGE_2'},
            {'news': [original, copy]},
            {'news': []},
            {'news': [revised, follow_up]},
            {'news': [revised, follow_up]},
        ]
        page = pages[len(seen) - 1]
        upper = datetime.fromisoformat(seen[-1]['end'])
        assert all(datetime.fromisoformat(record['updated_at']) <= upper for record in page['news'])
        return httpx.Response(200, json=page)

    transport(monkeypatch, handler)
    with TestClient(app) as client:
        configure(client, 'news', limit=1)
        first = client.post('/api/collections', json={}).json()
        assert first['result']['news']['continuation']
        events = client.get('/api/events').json()
        event_id = events[0]['event_id']
        assert events[0]['assets'][0]['in_universe'] is False
        evidence_id = client.get(f'/api/events/{event_id}').json()['evidence'][0]['id']
        review = client.post(f'/api/events/{event_id}/reviews', json={
            'state': 'active', 'judgment': 'TEST Historical judgment',
            'unresolved_conflicts': ['TEST Dispute remains open'], 'evidence_ids': [evidence_id],
        })
        assert review.status_code == 201

    Clock.current += timedelta(days=1)
    with TestClient(app) as client:
        resumed = client.post('/api/collections', json={}).json()['result']['news']
        assert resumed['duplicates'] == 1 and resumed['unchanged'] == 1
        assert resumed['continuation'] is False
        assert seen[0]['start'] == seen[1]['start'] and seen[0]['end'] == seen[1]['end']
        assert seen[1]['page_token'] == 'TEST_PAGE_2'
        assert client.post('/api/collections', json={}).json()['result']['news']['added'] == 0
        second_day = client.post('/api/collections', json={}).json()['result']['news']
        assert (second_day['added'], second_day['revisions']) == (1, 1)
        assert seen[2]['start'] < seen[1]['end'] < seen[2]['end']
        Clock.current += timedelta(minutes=5)
        repeated = client.post('/api/collections', json={}).json()['result']['news']
        assert repeated['unchanged'] == 2

    with TestClient(app) as client:
        assert len(client.get('/api/events').json()) == 1
        event = client.get(f'/api/events/{event_id}').json()
        assert event['state'] == 'active'
        assert len(event['evidence']) == 4
        old = next(item for item in event['evidence'] if item['id'] == evidence_id)
        assert old['raw'] == original and old['body'] == original['content']
        newer = next(item for item in event['evidence'] if item['supersedes_id'] == evidence_id)
        assert newer['first_public_at'] == old['first_public_at']
        assert newer['version_public_at'] > old['version_public_at']
        assert newer['occurred_at'] is None
        assert len(event['versions']) == 4
        judgment = next(item['result'] for item in event['versions'] if item['result']['type'] == 'manual_review')
        assert judgment['unresolved_conflicts'] == ['TEST Dispute remains open']
        assert all(row['kind'] == 'news' for row in client.get('/api/usage').json())


def test_search_and_market_cover_distinct_instruments(isolated_storage, monkeypatch):
    observed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    def handler(request):
        if request.url.host == 'search.fixture.invalid':
            assert request.url.params['q'] == 'TEST keyword'
            return httpx.Response(200, json={'web': {'results': [{
                'title': 'TEST new energy announcement (NASDAQ:TESTX)',
                'url': 'https://news.fixture.invalid/article', 'description': 'TEST snippet',
                'page_age': observed,
            }]}})
        assert 'GLD' in request.url.params['symbols']
        return httpx.Response(200, json={
            'GLD': {'latestTrade': {'p': 42.01, 't': observed}},
            'AAPL': {'latestTrade': {'p': 50, 't': observed}},
            'SPY': {'latestTrade': {'p': 51, 't': observed}},
        })

    transport(monkeypatch, handler)
    with TestClient(app) as client:
        configure(client, 'search')
        configure(client, 'market')
        for _ in range(2):
            run = client.post('/api/collections', json={'query': 'TEST keyword'}).json()
        assert run['state'] == 'partial'
        assert run['result']['market']['unchanged'] == 3
        prices = {row['symbol']: row for row in client.get('/api/market/observations/latest').json()}
        assert [prices[symbol]['instrument_type'] for symbol in ('GLD', 'AAPL', 'SPY')] == ['etp', 'equity', 'etf']
        assert prices['GLD']['unit'] == 'share' and prices['GLD']['proxy_for'] == '黄金'
        assert prices['GLD']['price'] == '42.01'
        events = client.get('/api/events').json()
        assert {asset['symbol'] for asset in events[0]['assets']} == {'TESTX'}
        assert not events[0]['assets'][0]['in_universe']
        evidence = client.get(f"/api/events/{events[0]['event_id']}").json()['evidence'][0]
        assert evidence['body_kind'] == 'search_snippet' and evidence['first_public_at'] is None
        assert client.get('/api/opportunities').json()['items'] == []


def test_denied_source_preserves_existing_cursor(isolated_storage, monkeypatch):
    def denied(request):
        return httpx.Response(403, text='TEST_PRIVATE_UPSTREAM_ERROR')

    transport(monkeypatch, denied)
    with TestClient(app) as client:
        configure(client, 'news')
        run = client.post('/api/collections', json={}).json()
        assert '未开放' in run['result']['news']['message']
        assert run['result']['news']['requests'] == 1
        with connect('research') as db:
            assert db.execute('SELECT COUNT(*) FROM provider_cursors').fetchone()[0] == 0
        assert 'TEST_PRIVATE_UPSTREAM_ERROR' not in client.get('/api/usage').text
