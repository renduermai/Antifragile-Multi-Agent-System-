import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from .db import DailyLimitReached, connect, now, reserve_usage
from .providers import HttpProvider
from .research import UNIVERSE, digest, json_text, store_evidence, store_market


DEFAULT_SEARCH = 'major global market events central bank trade policy commodity supply'


class SourceError(Exception):
    def __init__(self, status, message, status_code=None):
        super().__init__(message)
        self.status = status
        self.status_code = status_code


class CollectionRunning(Exception):
    pass


class Requests:
    def __init__(self, client, kind, config, secrets):
        self.client = client
        self.kind = kind
        self.config = config
        self.secrets = secrets
        self.provider = HttpProvider(kind)
        self.attempts = 0

    async def get(self, path, params):
        try:
            self.provider.connection(self.config, self.secrets)
        except ValueError as error:
            raise SourceError('unconfigured', str(error)) from None
        if self.attempts >= self.config['per_run_limit']:
            raise SourceError('budget', '已达本轮接口调用上限')
        try:
            usage_id = reserve_usage(self.kind, self.config['provider'], self.config['daily_limit'])
        except DailyLimitReached:
            raise SourceError('budget', '已达接口每日调用上限（UTC 日界）') from None
        self.attempts += 1
        result = {'status': 'failed', 'message': '请求未完成'}
        try:
            payload = await self.provider.get(self.client, self.config, self.secrets, path, params)
            result = {'status': 'success', 'message': 'HTTP JSON 响应已接收'}
            return payload
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            message = {401: '认证失败', 403: '当前授权未开放此端点或数据源',
                       429: 'Provider 限流或额度不足'}.get(code, f'Provider 请求失败（HTTP {code}）')
            result['message'] = message
            raise SourceError('http_error', message, code) from None
        except httpx.HTTPError:
            result['message'] = '网络或连接超时'
            raise SourceError('connection_error', result['message']) from None
        except ValueError:
            result['message'] = 'Provider JSON 格式不符合要求'
            raise SourceError('invalid_response', result['message']) from None
        finally:
            with connect() as db:
                db.execute('UPDATE usage SET status=?,detail=? WHERE id=?',
                           (result['status'], json_text(result), usage_id))


async def collect_news(requests, report, symbol=None):
    # A stable window keeps a continuation token valid across attempts and restarts.
    stream = 'alpaca_news:' + digest(requests.config['endpoint'].rstrip('/'))[:16]
    if symbol:
        stream += ':' + symbol
    with connect('research') as db:
        row = db.execute('SELECT * FROM provider_cursors WHERE stream=?', (stream,)).fetchone()
    covered = row['covered_until'] if row else None
    token = row['page_token'] if row else None
    if row and row['window_end']:
        start, end = row['window_start'], row['window_end']
    else:
        # Stay behind the newest interval so delayed-access accounts can request the same window.
        safe_end = datetime.now(timezone.utc) - timedelta(minutes=16)
        lower = datetime.fromisoformat(covered) if covered else safe_end - timedelta(hours=12)
        if lower >= safe_end:
            raise SourceError('unavailable', '当前时间尚未超过已采集截止时间')
        start = (lower - timedelta(hours=2) if covered else lower).isoformat()
        end = min(safe_end, lower + timedelta(hours=12)).isoformat()
    report.update(start=start, end=end, covered_until=covered, continuation=bool(token),
                  added=0, revisions=0, duplicates=0, unchanged=0, invalid=0)
    while True:
        params = {'start': start, 'end': end, 'sort': 'asc', 'limit': 50, 'include_content': 'true'}
        if symbol:
            params['symbols'] = symbol
        if token:
            params['page_token'] = token
        try:
            body = await requests.get('/v1beta1/news', params)
        except SourceError as error:
            if token and error.status_code == 400:
                with connect('research') as db:
                    db.execute('UPDATE provider_cursors SET page_token=NULL WHERE stream=?', (stream,))
                raise SourceError('http_error', '分页请求被拒绝，下轮将从当前窗口首页重试') from None
            raise
        records = body.get('news')
        next_token = body.get('next_page_token')
        if not isinstance(records, list) or (next_token is not None and not isinstance(next_token, str)):
            raise SourceError('invalid_response', '新闻列表或分页字段无效，游标保持原位')
        if next_token and next_token == token:
            raise SourceError('invalid_response', '新闻分页游标未前进')
        cursor = (stream, covered, start, end, next_token) if next_token else (stream, end, None, None, None)
        counts = store_evidence(records, 'news', cursor)
        for key, count in counts.items():
            report[key] += count
        token = next_token
        report.update(continuation=bool(token), covered_until=cursor[1])
        if not token:
            return


async def collect_search(requests, report, query):
    report['query'] = query or DEFAULT_SEARCH
    if requests.config['provider'] == 'tavily':
        payload = await requests.get('/search', {'q':report['query'],'count':10})
        records = payload.get('results')
        if not isinstance(records,list):
            raise SourceError('invalid_response','Tavily 新闻搜索结果格式无效')
        report.update(store_evidence(records,'tavily'), provider='tavily')
        return
    payload = await requests.get('/res/v1/web/search',
                                 {'q': report['query'], 'freshness': 'pw', 'count': 15, 'result_filter': 'web'})
    web = payload.get('web')
    if web is None and payload.get('type') == 'search':
        web = {'results': []}
    if not isinstance(web, dict) or not isinstance(web.get('results'), list):
        raise SourceError('invalid_response', '独立搜索结果格式无效')
    report.update(store_evidence(web['results'], 'search'))


async def collect_market(requests, report):
    feed = requests.config['feed']
    report['feed'] = feed
    payload = await requests.get('/v2/stocks/snapshots',
                                 {'symbols': ','.join(UNIVERSE), 'feed': feed, 'currency': 'USD'})
    if any(not isinstance(snapshot, dict) for snapshot in payload.values()):
        raise SourceError('invalid_response', '行情快照格式无效')
    report.update(store_market(payload, feed))
    if len(report['missing_symbols']) == len(UNIVERSE):
        raise SourceError('unavailable', '接口未返回重点行情池的有效成交快照')
    if report['missing_symbols']:
        raise SourceError('partial', '本轮行情覆盖不完整')


async def collect_source(client, kind, config, secrets, query, symbol=None):
    requests = Requests(client, kind, config, secrets)
    report = {'status': 'ok'}
    try:
        if kind == 'news':
            await collect_news(requests, report, symbol)
        elif kind == 'search':
            await collect_search(requests, report, query)
        else:
            await collect_market(requests, report)
    except SourceError as error:
        report.update(status=error.status, message=str(error), http_status=error.status_code)
    report['requests'] = requests.attempts
    return kind, report


def run_summary():
    with connect('research') as db:
        latest = db.execute('SELECT * FROM collection_runs ORDER BY rowid DESC LIMIT 1').fetchone()
        success = db.execute("""SELECT finished_at FROM collection_runs
            WHERE state='succeeded' ORDER BY rowid DESC LIMIT 1""").fetchone()
    latest_run = dict(latest) if latest else None
    if latest_run:
        latest_run['result'] = json.loads(latest_run['result'] or '{}')
    return latest_run, success['finished_at'] if success else None


async def run_collection(query, configs, secrets, symbol=None):
    run_id = uuid4().hex
    started = now()
    try:
        with connect('research') as db:
            db.execute("INSERT INTO collection_runs(id,started_at,state,query) VALUES (?,?,'running',?)",
                       (run_id, started, query))
    except sqlite3.IntegrityError:
        raise CollectionRunning from None
    result = {}
    state = 'interrupted'
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            sources = await asyncio.gather(*(collect_source(client, kind, config, secrets.get(kind, {}), query, symbol)
                                             for kind, config in configs.items()), return_exceptions=True)
        for kind, response in zip(configs, sources):
            if isinstance(response, BaseException):
                result[kind] = {'status': 'failed', 'message': '采集异常，已提交的证据与分页仍可恢复'}
            else:
                result[response[0]] = response[1]
        statuses = {report['status'] for report in result.values()}
        progressed = any(report.get('added') or report.get('revisions') or report.get('continuation')
                         for report in result.values())
        state = ('succeeded' if statuses == {'ok'} else
                 'unconfigured' if statuses == {'unconfigured'} else
                 'partial' if statuses & {'ok', 'partial'} or progressed else 'failed')
    finally:
        finished = now()
        with connect('research') as db:
            db.execute('UPDATE collection_runs SET finished_at=?,state=?,result=? WHERE id=?',
                       (finished, state, json_text(result), run_id))
    return {'id': run_id, 'started_at': started, 'finished_at': finished, 'state': state, 'result': result}
