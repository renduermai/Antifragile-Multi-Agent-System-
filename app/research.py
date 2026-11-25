import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from .db import connect, now


PROXIES = {'GLD': '黄金', 'USO': '原油', 'UNG': '天然气', 'CPER': '铜'}
UNIVERSE = ('SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', *PROXIES)
FUNDS = {'SPY', 'QQQ', 'IWM', *PROXIES}
SYMBOL = re.compile(r'[A-Z][A-Z0-9./-]{0,14}')
EXPLICIT_SYMBOL = re.compile(r'(?:\$|\b(?:NASDAQ|NYSE|NYSEARCA)\s*:\s*)([A-Z][A-Z0-9.-]{0,14})\b')
RULES_VERSION = 'ingest-v2.0'
TITLE_STOP_WORDS = {'and', 'the', 'for', 'from', 'with', 'after', 'before', 'this', 'that'}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).isoformat(timespec='microseconds') if dt.tzinfo else None
    except (ValueError, OverflowError):
        return None


def published_time(value):
    result = timestamp(value)
    if result or not isinstance(value,str):
        return result
    try:
        parsed = parsedate_to_datetime(value)
        return timestamp(parsed.isoformat()) if parsed else None
    except (ValueError, TypeError, OverflowError):
        return None


def article_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None:
            return None
    except ValueError:
        return None
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', urlencode(sorted(query)), ''))


def normalized_text(value):
    plain = unescape(re.sub(r'<[^>]+>', ' ', value))
    return ' '.join(unicodedata.normalize('NFKC', plain).casefold().split())


def field(record, key):
    value = record.get(key)
    return value.strip() if isinstance(value, str) else ''


def normalize_evidence(record, kind):
    if not isinstance(record, dict):
        return None
    is_news = kind == 'news'
    headline = field(record, 'headline' if is_news else 'title')
    url = article_url(record.get('url'))
    raw_id = record.get('id')
    if not headline or (not url and not (is_news and isinstance(raw_id, (str, int)) and raw_id)):
        return None
    full = field(record, 'content') if is_news else ''
    body = full or field(record,'content' if kind == 'tavily' else 'summary' if is_news else 'description')
    headline_key = normalized_text(headline)
    body_key = normalized_text(body)
    first = timestamp(record.get('created_at')) if is_news else None
    if kind == 'tavily':
        first = published_time(record.get('published_date'))
    revision = timestamp(record.get('updated_at')) if is_news else None
    revision = max(filter(None, (first, revision)), default=None)
    symbols = record.get('symbols') if is_news else EXPLICIT_SYMBOL.findall(f'{headline} {body}')
    symbols = sorted({s for s in symbols if isinstance(s, str) and SYMBOL.fullmatch(s)}) if isinstance(symbols, list) else []
    provider = 'alpaca_news' if is_news else 'tavily_search' if kind == 'tavily' else 'brave_search'
    publisher = field(record, 'source') if is_news else ''
    if kind == 'tavily' and url:
        publisher = urlsplit(url).hostname or ''
    upstream_id = str(raw_id) if is_news and raw_id is not None else url
    content_hash = digest([headline, body])
    return {
        'id': digest([provider, upstream_id, content_hash, url, symbols, publisher, first, revision]),
        'provider': provider, 'upstream_id': upstream_id,
        'source_url': field(record, 'url') if url else None, 'canonical_url': url, 'publisher': publisher,
        'source_type': 'news_article' if is_news else 'search_result',
        'headline': headline, 'headline_key': headline_key, 'body': body,
        'body_kind': ('full_text' if full else 'summary' if body and is_news else 'search_snippet' if body else 'headline'),
        'first_public_at': first, 'version_public_at': revision, 'content_hash': content_hash,
        'fingerprint': digest([headline_key, body_key]) if len(body_key) >= 40 else None,
        'symbols': symbols, 'raw_json': json_text(record),
    }


def matched_event(db, item, previous, duplicate):
    if previous or duplicate:
        return (previous or duplicate)['event_id']
    if item['canonical_url']:
        rows = db.execute('SELECT DISTINCT event_id FROM evidence WHERE canonical_url=?',
                          (item['canonical_url'],)).fetchall()
        if len(rows) == 1:
            return rows[0]['event_id']
    if item['first_public_at'] and item['symbols'] and len(item['headline_key']) >= 24:
        dt = datetime.fromisoformat(item['first_public_at'])
        lower, upper = timestamp((dt - timedelta(days=3)).isoformat()), timestamp((dt + timedelta(days=3)).isoformat())
        placeholders = ','.join('?' for _ in item['symbols'])
        candidates = db.execute(
            f'''SELECT DISTINCT e.event_id,e.headline_key FROM evidence e JOIN event_assets a ON e.event_id=a.event_id
                WHERE a.symbol IN ({placeholders}) AND e.first_public_at BETWEEN ? AND ?''',
            (*item['symbols'], lower, upper))
        matching = {row['event_id'] for row in candidates if similar_headline(item['headline_key'], row['headline_key'])}
        if len(matching) == 1:
            return matching.pop()
    return None


def similar_headline(left, right):
    if left == right:
        return True
    if not right:
        return False
    # A new date or number can distinguish two otherwise very similar releases.
    if set(re.findall(r'\d+', left)) != set(re.findall(r'\d+', right)):
        return False
    first = set(re.findall(r'\b[^\W\d_]{3,}\b', left)) - TITLE_STOP_WORDS
    second = set(re.findall(r'\b[^\W\d_]{3,}\b', right)) - TITLE_STOP_WORDS
    shared = first & second
    return len(shared) >= 5 and len(shared) / len(first | second) >= 0.8


def write_version(db, event_id, acquired, result, state=None):
    db.execute('INSERT INTO event_versions(event_id,created_at,result,rules_version) VALUES (?,?,?,?)',
               (event_id, acquired, json_text(result), RULES_VERSION))
    db.execute('UPDATE events SET updated_at=?,state=COALESCE(?,state) WHERE event_id=?',
               (acquired, state, event_id))


def insert_evidence(db, item, acquired):
    symbols = set(item['symbols'])
    if item['source_type'] == 'search_result':
        # Parenthesized exchange tickers and Reuters US RICs in a headline are useful even
        # when a search did not supply ticker metadata. Limit bare ticker matches to known assets.
        for symbol in re.findall(r'\(([A-Z]{1,5})(?:\.[ON])?\)', item['headline']):
            if symbol in UNIVERSE:
                symbols.add(symbol)
    links = [(symbol, item['provider']) for symbol in symbols]
    known = db.execute('SELECT event_id FROM evidence WHERE id=?', (item['id'],)).fetchone()
    if known:
        db.executemany('INSERT OR IGNORE INTO event_assets VALUES (?,?,?)',
                       [(known['event_id'], *link) for link in links])
        return 'unchanged'
    previous = db.execute(
        '''SELECT * FROM evidence WHERE provider=? AND upstream_id=?
           ORDER BY COALESCE(version_public_at, acquired_at) DESC, rowid DESC LIMIT 1''',
        (item['provider'], item['upstream_id'])).fetchone()
    duplicate = db.execute('SELECT * FROM evidence WHERE fingerprint=? ORDER BY rowid LIMIT 1',
                           (item['fingerprint'],)).fetchone() if item['fingerprint'] else None
    event_id = matched_event(db, item, previous, duplicate)
    if event_id is None:
        event_id = uuid4().hex
        db.execute('INSERT INTO events(event_id,state,created_at,title,updated_at) VALUES (?,?,?,?,?)',
                   (event_id, 'watch', acquired, item['headline'], acquired))
    changed = previous and item['content_hash'] != previous['content_hash']
    same_story_copy = previous and not changed and item['canonical_url'] == previous['canonical_url']
    duplicate_id = (previous or duplicate)['id'] if same_story_copy else (
        duplicate['id'] if duplicate and not previous else None)
    data = {key: value for key, value in item.items() if key != 'symbols'}
    # A late-arriving older revision is retained, but never described as replacing a newer one.
    newer = changed and (not item['version_public_at'] or not previous['version_public_at']
                         or item['version_public_at'] >= previous['version_public_at'])
    data.update(event_id=event_id, acquired_at=acquired, duplicate_of=duplicate_id,
                supersedes_id=previous['id'] if newer else None)
    columns = ','.join(data)
    db.execute(f'INSERT INTO evidence({columns}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))
    db.executemany('INSERT OR IGNORE INTO event_assets VALUES (?,?,?)',
                   [(event_id, *link) for link in links])
    if not duplicate_id:
        change = 'source_revision' if newer else 'older_source_version' if changed else 'new_evidence'
        write_version(db, event_id, acquired, {
            'type': change, 'evidence_id': item['id'], 'prior_evidence_id': previous['id'] if changed else None,
            'requires_review': bool(changed),
        })
    else:
        db.execute('UPDATE events SET updated_at=? WHERE event_id=?', (acquired, event_id))
    return 'duplicates' if duplicate_id else 'revisions' if changed else 'added'


def store_evidence(records, kind, cursor=None):
    counts = dict(added=0, revisions=0, duplicates=0, unchanged=0, invalid=0)
    acquired = now()
    with connect('research') as db:
        db.execute('BEGIN IMMEDIATE')
        for record in records:
            item = normalize_evidence(record, kind)
            outcome = insert_evidence(db, item, acquired) if item else 'invalid'
            counts[outcome] += 1
        if cursor:
            db.execute('''INSERT INTO provider_cursors VALUES (?,?,?,?,?) ON CONFLICT(stream) DO UPDATE SET
                          covered_until=excluded.covered_until, window_start=excluded.window_start,
                          window_end=excluded.window_end, page_token=excluded.page_token''', cursor)
    return counts


def store_market(records, feed):
    acquired = now()
    counts = {'added': 0, 'unchanged': 0, 'missing_symbols': []}
    with connect('research') as db:
        for symbol in UNIVERSE:
            snapshot = records.get(symbol)
            trade = snapshot.get('latestTrade') if isinstance(snapshot, dict) else None
            observed = timestamp(trade.get('t')) if isinstance(trade, dict) else None
            try:
                price = Decimal(str(trade.get('p'))) if isinstance(trade, dict) else Decimal(0)
            except InvalidOperation:
                price = Decimal(0)
            if not observed or observed > timestamp(acquired) or not price.is_finite() or price <= 0:
                counts['missing_symbols'].append(symbol)
                continue
            identity = digest([symbol, feed, trade])
            count = db.execute('INSERT OR IGNORE INTO market_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                               (identity, symbol, 'etp' if symbol in PROXIES else 'etf' if symbol in FUNDS else 'equity',
                                PROXIES.get(symbol),
                                'USD', 'share', feed, str(price), observed, acquired, json_text(snapshot))).rowcount
            counts['added' if count else 'unchanged'] += 1
    return counts


def event_list(limit=100):
    with connect('research') as db:
        rows = db.execute('''SELECT e.*,
            (SELECT COUNT(*) FROM evidence WHERE event_id=e.event_id) +
            (SELECT COUNT(*) FROM event_evidence_links WHERE event_id=e.event_id) AS evidence_count
            FROM events e ORDER BY COALESCE(updated_at,created_at) DESC, event_id LIMIT ?''', (limit,))
        return [{**dict(row), 'assets': assets(db, row['event_id'])} for row in rows]


def assets(db, event_id):
    rows = db.execute('SELECT symbol,source FROM event_assets WHERE event_id=? ORDER BY symbol', (event_id,))
    return [{**dict(row), 'in_universe': row['symbol'] in UNIVERSE,
             'proxy_for': PROXIES.get(row['symbol'])} for row in rows]


def event_detail(event_id):
    with connect('research') as db:
        event = db.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
        if not event:
            return None
        evidence = [dict(row) for row in db.execute('''SELECT * FROM evidence
            WHERE event_id=? OR id IN (SELECT evidence_id FROM event_evidence_links WHERE event_id=?)
            ORDER BY COALESCE(version_public_at,acquired_at), rowid''', (event_id, event_id))]
        for item in evidence:
            item['raw'] = json.loads(item.pop('raw_json') or '{}')
        versions = [dict(row) for row in db.execute(
            'SELECT * FROM event_versions WHERE event_id=? ORDER BY id DESC', (event_id,))]
        for version in versions:
            version['result'] = json.loads(version['result'])
        return {**dict(event), 'assets': assets(db, event_id), 'evidence': evidence, 'versions': versions}


def save_review(event_id, state, judgment, unresolved_conflicts, evidence_ids):
    acquired = now()
    with connect('research') as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM events WHERE event_id=?', (event_id,)).fetchone():
            return False
        for evidence_id in set(evidence_ids):
            row = db.execute('SELECT event_id,provider,raw_json FROM evidence WHERE id=?', (evidence_id,)).fetchone()
            if row is None:
                raise ValueError('指定的证据 ID 不存在')
            if row['event_id'] != event_id:
                db.execute('INSERT OR IGNORE INTO event_evidence_links VALUES (?,?,?)',
                           (event_id, evidence_id, acquired))
                kind = 'news' if row['provider'] == 'alpaca_news' else 'tavily' if row['provider']=='tavily_search' else 'search'
                item = normalize_evidence(json.loads(row['raw_json'] or '{}'), kind)
                if item:
                    db.executemany('INSERT OR IGNORE INTO event_assets VALUES (?,?,?)',
                                   [(event_id, symbol, row['provider']) for symbol in item['symbols']])
        write_version(db, event_id, acquired, {
            'type': 'manual_review', 'state': state, 'judgment': judgment,
            'unresolved_conflicts': unresolved_conflicts, 'evidence_ids': evidence_ids,
        }, state)
    return True


def latest_market():
    with connect('research') as db:
        rows = db.execute('''SELECT symbol,instrument_type,proxy_for,currency,unit,feed,price,observed_at,acquired_at
            FROM (SELECT *, ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY observed_at DESC, rowid DESC) AS rank
                  FROM market_observations) WHERE rank=1 ORDER BY symbol''')
        return [dict(row) for row in rows]
